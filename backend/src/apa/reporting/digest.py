"""A-12 Reporting Agent (blueprint v2 4.7): weekly and monthly digests pushed on a schedule, never on request.

Every number comes from SQL; there is no model in this path, so a digest cannot invent a figure.
(The blueprint allows an optional LLM narrative; leaving it out keeps these messages exact and free.)

What is not here yet: goal progress, because the goals table is not built."""

from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from apa.core.format import rupiah
from apa.core.logging import get_logger
from apa.core.settings import get_settings
from apa.db.models.budget import BudgetRule
from apa.db.models.ledger import Transaction
from apa.db.models.ops import TelegramMessage

log = get_logger(__name__)

TOP_N = 3
DIGEST_KEY = "apa:digest:"


async def _totals(sm: async_sessionmaker, start: datetime, end: datetime):
    """(spent, earned, count, {category: spent}, top merchants) of committed transactions in [start, end)."""
    committed = Transaction.status == "committed"
    window = (Transaction.timestamp >= start, Transaction.timestamp < end)
    async with sm() as s:
        by_cat = (
            await s.execute(
                select(
                    Transaction.direction,
                    Transaction.primary_category,
                    func.count(),
                    func.sum(Transaction.total_paid_amount),
                )
                .where(committed, *window)
                .group_by(Transaction.direction, Transaction.primary_category)
            )
        ).all()
        merchants = (
            await s.execute(
                select(Transaction.merchant, func.sum(Transaction.total_paid_amount).label("t"))
                .where(committed, Transaction.direction == "out", Transaction.merchant.is_not(None), *window)
                .group_by(Transaction.merchant)
                .order_by(func.sum(Transaction.total_paid_amount).desc())
                .limit(TOP_N)
            )
        ).all()
    spent = sum((r[3] for r in by_cat if r[0] == "out"), Decimal(0))
    earned = sum((r[3] for r in by_cat if r[0] == "in"), Decimal(0))
    n = sum(r[2] for r in by_cat)
    cats = {(r[1] or "Tanpa kategori"): r[3] for r in by_cat if r[0] == "out"}
    return spent, earned, n, cats, [(m, t) for m, t in merchants]


async def _budget_lines(sm: async_sessionmaker, month_start) -> list[str]:
    async with sm() as s:
        rules = (
            (await s.execute(select(BudgetRule).where(BudgetRule.period_month == month_start)))
            .scalars()
            .all()
        )
    rated = [
        (r, (r.spent_amount / r.current_limit) if r.current_limit else Decimal(0))
        for r in rules
        if r.current_limit
    ]
    rated.sort(key=lambda x: x[1], reverse=True)
    lines = []
    for r, ratio in rated[:TOP_N]:
        flag = "🔴" if ratio >= 1 else "🟡" if ratio >= Decimal("0.8") else "🟢"
        lines.append(
            f"{flag} {r.category}: {rupiah(r.spent_amount)} / {rupiah(r.current_limit)} ({int(ratio * 100)}%)"
        )
    return lines


async def _attention(sm: async_sessionmaker) -> list[str]:
    async with sm() as s:
        held = (
            await s.execute(
                select(func.count()).select_from(Transaction).where(Transaction.status == "quarantined")
            )
        ).scalar_one()
    return [f"⏳ {held} transaksi ditahan menunggu keputusan (lihat /menu → Ditahan)"] if held else []


def _change(now_v: Decimal, before: Decimal) -> str:
    if before <= 0:
        return ""
    pct = int((now_v - before) / before * 100)
    return f" ({'+' if pct >= 0 else ''}{pct}% vs periode sebelumnya)"


async def weekly(sm: async_sessionmaker, tz: ZoneInfo, now: datetime | None = None) -> str | None:
    """The last 7 days. None when there was nothing to report (no digest is sent for an empty week)."""
    end = (now or datetime.now(tz)).astimezone(tz)
    start = end - timedelta(days=7)
    spent, earned, n, cats, merchants = await _totals(sm, start, end)
    if n == 0:
        return None
    before, *_ = await _totals(sm, start - timedelta(days=7), start)
    month = end.date().replace(day=1)
    lines = [
        f"📅 Ringkasan mingguan ({start:%d/%m} - {end:%d/%m}) · {n} transaksi",
        f"Keluar {rupiah(spent)}{_change(spent, before)}" + (f" · Masuk {rupiah(earned)}" if earned else ""),
    ]
    top = sorted(cats.items(), key=lambda kv: kv[1], reverse=True)[:TOP_N]
    lines += [f"  • {c}: {rupiah(v)}" for c, v in top]
    if merchants:
        lines.append("Terbesar: " + ", ".join(f"{m} {rupiah(t)}" for m, t in merchants))
    budget = await _budget_lines(sm, month)
    if budget:
        lines += ["Anggaran bulan ini (paling mepet):", *budget]
    return "\n".join(lines + await _attention(sm))


async def monthly(sm: async_sessionmaker, tz: ZoneInfo, now: datetime | None = None) -> str | None:
    """The month so far (sent on its last day, so it is the whole month)."""
    end = (now or datetime.now(tz)).astimezone(tz)
    start = end.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    prev_end = start
    prev_start = (start - timedelta(days=1)).replace(day=1)
    spent, earned, n, cats, merchants = await _totals(sm, start, end + timedelta(seconds=1))
    if n == 0:
        return None
    before, *_ = await _totals(sm, prev_start, prev_end)
    lines = [
        f"🗓️ Ringkasan bulanan {start:%m/%Y} · {n} transaksi",
        f"Keluar {rupiah(spent)}{_change(spent, before)} · Masuk {rupiah(earned)}",
    ]
    if earned > 0:
        lines.append(
            f"Selisih {rupiah(earned - spent)} ({int((earned - spent) / earned * 100)}% dari pemasukan)"
        )
    top = sorted(cats.items(), key=lambda kv: kv[1], reverse=True)[:5]
    lines += [f"  • {c}: {rupiah(v)}" for c, v in top]
    if merchants:
        lines.append("Terbesar: " + ", ".join(f"{m} {rupiah(t)}" for m, t in merchants))
    budget = await _budget_lines(sm, start.date())
    if budget:
        lines += ["Anggaran (paling mepet):", *budget]
    return "\n".join(lines + await _attention(sm))


async def deliver(sm: async_sessionmaker, notifier, redis, key: str, text: str) -> bool:
    """Send once per `key` (e.g. 'weekly:2026-W43'): a restart at the scheduled minute must not repeat it."""
    if redis is not None and not await redis.set(DIGEST_KEY + key, "1", nx=True, ex=40 * 86400):
        log.info("digest_already_sent", key=key)
        return False
    async with sm() as s:
        tm = TelegramMessage(
            direction="out", chat_id=get_settings().telegram_owner_id, kind="digest", body=text
        )
        s.add(tm)
        await s.flush()
        tm_id = tm.id
        await s.commit()
    try:
        message_id = await notifier.send(text)
    except Exception as e:  # Telegram down: the digest is not worth failing the worker over
        log.warning("digest_send_failed", error=str(e))
        if redis is not None:
            await redis.delete(DIGEST_KEY + key)  # allow the next tick to try again
        return False
    if message_id is not None:
        async with sm() as s:
            tm = await s.get(TelegramMessage, tm_id)
            tm.message_id = message_id
            await s.commit()
    return True
