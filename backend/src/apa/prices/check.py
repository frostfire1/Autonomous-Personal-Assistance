"""The daily price check: gather prices, find genuine rises, size the monthly cost, and shift budget to cover it.
Autonomous within bounds, as the blueprint asks (v4.1 4.4b): no confirmation button, no model deciding a number.

The shift is limited three ways: at most 15% of the SOURCE budget's base limit (its elastic band), never below what the
source has already spent, and never beyond what the target's own 15% band allows. Every shift is logged in
budget_rebalance_log with an audit row, and the owner is told once, with the evidence."""

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from apa.core.format import rupiah
from apa.core.logging import get_logger
from apa.db.models.budget import BudgetRebalanceLog, BudgetRule, MacroAssumption, PriceBasket
from apa.db.models.intel import NewsArticle
from apa.news.fetch import FetchError, fetch_bytes
from apa.news.i18n import money, t
from apa.prices import store
from apa.prices.engine import Anomaly, detect, monthly_impact, personal_index
from apa.reporting.digest import DIGEST_KEY, deliver
from apa.safety.audit_log import append_explainability

log = get_logger(__name__)

SOURCES = ("Social", "Entertainment", "Shopping")  # discretionary, in the order they give first (blueprint: A-05 Social)
ASSUMPTION_KEY = "food_price_shift"


class Observation(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    region: str = Field(default=store.DEFAULT_REGION, max_length=48)
    date: date
    price: Decimal = Field(gt=0, le=store.MAX_PRICE)


async def ingest_observations(sm: async_sessionmaker, obs: list[Observation]) -> int:
    """From the ingest API or a feed. Unknown commodities and implausible prices are skipped, not fatal."""
    await store.ensure_basket(sm)
    return sum([await store.record(sm, o.code, o.region, o.date, o.price) for o in obs])


async def pull_feed(sm: async_sessionmaker, client: httpx.AsyncClient, url: str) -> int:
    """An optional JSON feed ([{code, region, date, price}, ...]) configured as PRICE_FEED_URL. Fetched through the
    same safe fetcher as the news (no internal addresses, size cap, timeout)."""
    import json

    try:
        raw = json.loads(await fetch_bytes(client, url, max_bytes=1_000_000))
        obs = [Observation.model_validate(x) for x in raw][:2000]
    except (FetchError, ValueError, ValidationError, TypeError) as e:
        log.warning("price_feed_failed", error=type(e).__name__)
        return 0
    return await ingest_observations(sm, obs)


async def _news_hint(sm: async_sessionmaker, names: list[str], now: datetime) -> str | None:
    """A possible cause, quoted from a stored headline with its source, offered as a guess and never as a fact."""
    async with sm() as s:
        arts = (
            await s.execute(select(NewsArticle).order_by(NewsArticle.published_at.desc().nullslast()).limit(400))
        ).scalars().all()
    words = [w.lower() for n in names for w in n.split()] + ["harga pangan", "bahan pokok"]
    for a in arts:
        if a.published_at and (now - a.published_at).days <= 7 and any(w in a.title.lower() for w in words):
            return f"Dugaan penyebab (bukan fakta): «{a.title[:110]}» ({a.source})"
    return None


async def _shift(sm: async_sessionmaker, amount: Decimal, anomalies: list[Anomaly], impact: Decimal, tz: ZoneInfo,
                 now: datetime) -> tuple[Decimal, str, str] | None:
    """Move budget to the groceries category. -> (amount moved, source, target) or None if nothing could move."""
    month = now.astimezone(tz).date().replace(day=1)
    async with sm() as s:
        target_name = (await s.execute(select(PriceBasket.budget_category).where(
            PriceBasket.commodity_code == anomalies[0].code))).scalar_one_or_none() or "Groceries"
        dst = (await s.execute(select(BudgetRule).where(BudgetRule.category == target_name, BudgetRule.period_month == month)
                               .with_for_update())).scalar_one_or_none()
        if dst is None or dst.is_sinking_fund:
            return None
        dst_room = dst.base_limit * (1 + dst.elastic_band_pct / 100) - dst.current_limit
        remaining, moved_from = amount, []
        for name in SOURCES:
            if remaining <= 0 or dst_room <= 0:
                break
            src = (await s.execute(select(BudgetRule).where(
                BudgetRule.category == name, BudgetRule.period_month == month,
                BudgetRule.is_sinking_fund.is_(False), BudgetRule.is_essential.is_(False)).with_for_update())).scalar_one_or_none()
            if src is None:
                continue
            src_room = min(src.current_limit - src.base_limit * (1 - src.elastic_band_pct / 100),
                           src.current_limit - src.spent_amount)
            move = min(remaining, src_room, dst_room).quantize(Decimal("1"))
            if move <= 0:
                continue
            before_s, before_d = src.current_limit, dst.current_limit
            src.current_limit -= move
            dst.current_limit += move
            remaining -= move
            dst_room -= move
            row = await append_explainability(
                s, decision_type="budget_rebalance", verdict="rebalance:price_spike",
                justification=f"staple prices rose: {', '.join(f'{a.code} +{int(a.rise * 100)}%' for a in anomalies)}; "
                              f"estimated extra {rupiah(impact)} a month; moved {rupiah(move)} {name} -> {target_name}",
            )
            s.add(BudgetRebalanceLog(
                trigger_type="pihps_spike", from_category=name, to_category=target_name, period_month=month, amount=move,
                from_limit_before=before_s, from_limit_after=src.current_limit, to_limit_before=before_d,
                to_limit_after=dst.current_limit, trigger_ref={"commodities": [a.code for a in anomalies],
                                                               "extra_monthly": str(impact)},
                explainability_id=row.id))
            moved_from.append(name)
        if not moved_from:
            return None
        await s.commit()
        return amount - remaining, " + ".join(moved_from), target_name


async def run_price_check(
    sm: async_sessionmaker, notifier: Any, redis: Any | None, tz: ZoneInfo, now: datetime
) -> dict:
    """-> {'anomalies': [...], 'impact': Decimal, 'moved': Decimal|None}."""
    await store.ensure_basket(sm)
    await store.harvest_receipts(sm, tz, now)
    today = now.astimezone(tz).date()
    items = await store.basket(sm)
    anomalies = []
    for b in items:
        a = detect(b.commodity_code, await store.series(sm, b.commodity_code, today), today)
        if a is not None:
            anomalies.append(a)
    if not anomalies:
        return {"anomalies": [], "impact": Decimal(0), "moved": None}
    qty = {b.commodity_code: b.monthly_qty for b in items}
    impact = monthly_impact(anomalies, qty)
    names = {b.commodity_code: b.commodity_name for b in items}
    async with sm() as s:
        stmt = insert(MacroAssumption).values(
            assumption_key=ASSUMPTION_KEY, value=max(a.rise for a in anomalies) * 100, unit="pct", effective_from=today,
            source="receipts+feed", projected_monthly_impact=impact)
        await s.execute(stmt.on_conflict_do_update(index_elements=["assumption_key", "effective_from"], set_={
            "value": stmt.excluded.value, "projected_monthly_impact": impact}))
        await s.commit()
    key = f"price:{today.isoformat()}"
    if redis is not None and await redis.exists(DIGEST_KEY + key):  # already shifted and told today
        return {"anomalies": anomalies, "impact": impact, "moved": None}
    moved = await _shift(sm, impact, anomalies, impact, tz, now) if impact > 0 else None
    lines = ["📈 Harga bahan pokok naik: " + ", ".join(f"{names[a.code]} +{int(a.rise * 100)}%" for a in anomalies) + "."]
    lines.append(f"Perkiraan tambahan biaya {rupiah(impact)} per bulan untuk keranjangmu.")
    lines.append(f"Anggaran disesuaikan otomatis: {rupiah(moved[0])} dipindah dari {moved[1]} ke {moved[2]} (maks 15%)."
                 if moved else "Anggaran tidak diubah (tidak ada pos yang cukup longgar dalam batas 15%).")
    if hint := await _news_hint(sm, [names[a.code] for a in anomalies], now):
        lines.append(hint)
    await deliver(sm, notifier, redis, key, "\n".join(lines))
    return {"anomalies": anomalies, "impact": impact, "moved": moved}


async def price_line(sm: async_sessionmaker, now: datetime, lang: str = "id") -> str | None:
    """For the spoken briefing: the latest price shift, if it is recent."""
    async with sm() as s:
        row = (await s.execute(select(MacroAssumption).where(MacroAssumption.assumption_key == ASSUMPTION_KEY)
                               .order_by(MacroAssumption.effective_from.desc()).limit(1))).scalar_one_or_none()
    if row is None or (now.date() - row.effective_from).days > 3 or not row.projected_monthly_impact:
        return None
    return t(lang, "price", pct=int(row.value), amount=money(row.projected_monthly_impact, lang))


async def basket_index(sm: async_sessionmaker, now: datetime, tz: ZoneInfo) -> Decimal | None:
    """The owner's personal inflation index: their basket's cost now against its baseline (1.00 = unchanged)."""
    today = now.astimezone(tz).date()
    items = await store.basket(sm)
    latest: dict[str, Decimal] = {}
    for b in items:
        s = await store.series(sm, b.commodity_code, today, days=14)
        if s:
            latest[b.commodity_code] = sorted(s)[-1][1]
    return personal_index(latest, {b.commodity_code: b.baseline_price for b in items}, {b.commodity_code: b.monthly_qty for b in items})
