"""Price intelligence end to end: receipts and feeds to prices, a real rise to a bounded, audited budget shift."""

import os
from datetime import timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis

from apa.core.settings import get_settings
from apa.db.models.audit import ExplainabilityLog
from apa.db.models.budget import BudgetRebalanceLog, BudgetRule, CommodityPrice, MacroAssumption
from apa.db.models.intel import NewsArticle
from apa.db.models.ledger import Transaction, TransactionItem
from apa.gateway.routers import prices as prices_router
from apa.prices import check, store
from apa.safety.audit_log import verify_log
from tests.integration.conftest import HAVE_INFRA
from tests.integration.helpers import FakeNotifier, rows

infra = pytest.mark.skipif(not HAVE_INFRA, reason="TEST_DATABASE_URL/TEST_REDIS_URL not set")
from datetime import datetime  # noqa: E402

TZ = ZoneInfo("Asia/Jakarta")
NOW = datetime.now(TZ).replace(hour=9, minute=0, second=0, microsecond=0)
TODAY = NOW.date()
D = Decimal


@pytest.fixture
async def redis(stack):
    r = Redis.from_url(os.environ["TEST_REDIS_URL"])
    yield r
    await r.aclose()


async def prices(stack, code="beras", base=10000, now=12500, region="Nasional"):
    """A month at `base`, then two days at `now`."""
    await store.ensure_basket(stack)
    for d in (30, 25, 20, 15, 12, 10):
        await store.record(stack, code, region, TODAY - timedelta(days=d), D(base))
    for d in (3, 1):
        await store.record(stack, code, region, TODAY - timedelta(days=d), D(now))


async def budgets(stack, *, groceries=1_000_000, social=1_000_000, social_spent=0, ent=1_000_000, ent_spent=0):
    month = TODAY.replace(day=1)
    async with stack() as s:
        for cat, lim, spent in (("Groceries", groceries, 0), ("Social", social, social_spent), ("Entertainment", ent, ent_spent)):
            s.add(BudgetRule(category=cat, period_month=month, base_limit=D(lim), current_limit=D(lim), spent_amount=D(spent)))
        await s.commit()


async def limits(stack):
    return {r.category: r.current_limit for r in await rows(stack, BudgetRule)}


@infra
async def test_the_default_basket_is_seeded_once(stack):
    assert await store.ensure_basket(stack) == 8
    assert await store.ensure_basket(stack) == 0
    assert len(await store.basket(stack)) == 8


@infra
async def test_implausible_or_unknown_prices_are_refused(stack):
    await store.ensure_basket(stack)
    assert await store.record(stack, "beras", "Nasional", TODAY, D(14000)) is True
    assert await store.record(stack, "beras", "Nasional", TODAY, D(50)) is False  # below any real price
    assert await store.record(stack, "beras", "Nasional", TODAY, D(99_000_000)) is False  # a unit error
    assert await store.record(stack, "emas", "Nasional", TODAY, D(14000)) is False  # not in the basket
    assert len(await rows(stack, CommodityPrice)) == 1


@infra
async def test_the_same_day_and_region_is_overwritten_not_duplicated(stack):
    await store.ensure_basket(stack)
    await store.record(stack, "beras", "Nasional", TODAY, D(14000))
    await store.record(stack, "beras", "Nasional", TODAY, D(15000))
    (r,) = await rows(stack, CommodityPrice)
    assert r.price == D(15000)


@infra
async def test_receipts_become_unit_prices_for_committed_purchases_only(stack):
    await store.ensure_basket(stack)
    async with stack() as s:
        for status, price in (("committed", 68000), ("quarantined", 99000), ("rolled_back", 99000)):
            tx = Transaction(timestamp=NOW - timedelta(days=1), source_channel="telegram_photo", payment_wallet="cash_wallet",
                             merchant="Pasar", direction="out", total_paid_amount=D(price), net_user_expense=D(price),
                             primary_category="Groceries", execution_mode="consensus_auto", status=status)
            s.add(tx)
            await s.flush()
            s.add(TransactionItem(transaction_id=tx.id, item_name="Beras Premium 5kg", quantity=D(1), unit_price=D(price),
                                  total_price=D(price)))
            s.add(TransactionItem(transaction_id=tx.id, item_name="Kopi Sachet", quantity=D(1), unit_price=D(2000), total_price=D(2000)))
        await s.commit()
    assert await store.harvest_receipts(stack, TZ, NOW) == 1
    (r,) = await rows(stack, CommodityPrice)
    assert (r.commodity_code, r.pihps_region, r.price) == ("beras", "personal", D(13600))


@infra
async def test_a_real_rise_shifts_budget_with_a_log_an_audit_row_and_one_message(stack, redis):
    await prices(stack)
    await budgets(stack)
    n = FakeNotifier()
    out = await check.run_price_check(stack, n, redis, TZ, NOW)
    # 8 kg a month x (12500 - 10000) = 20.000 extra
    assert out["impact"] == D(20000) and out["moved"] == (D(20000), "Social", "Groceries")
    lim = await limits(stack)
    assert lim["Groceries"] == D(1_020_000) and lim["Social"] == D(980_000) and lim["Entertainment"] == D(1_000_000)
    (log,) = await rows(stack, BudgetRebalanceLog)
    assert (log.trigger_type, log.amount, log.from_category, log.to_category) == ("pihps_spike", D(20000), "Social", "Groceries")
    assert (log.from_limit_before, log.from_limit_after, log.to_limit_after) == (D(1_000_000), D(980_000), D(1_020_000))
    assert [e.decision_type for e in await rows(stack, ExplainabilityLog)] == ["budget_rebalance"]
    async with stack() as s:
        assert await verify_log(s) is None
    (m,) = await rows(stack, MacroAssumption)
    assert m.projected_monthly_impact == D(20000)
    assert len(n.sent) == 1 and "Beras +25%" in n.sent[0] and "Rp 20.000" in n.sent[0] and "dipindah dari Social ke Groceries" in n.sent[0]


@infra
async def test_running_again_the_same_day_neither_shifts_nor_tells_twice(stack, redis):
    await prices(stack)
    await budgets(stack)
    n = FakeNotifier()
    await check.run_price_check(stack, n, redis, TZ, NOW)
    again = await check.run_price_check(stack, n, redis, TZ, NOW)
    assert again["moved"] is None and len(n.sent) == 1 and len(await rows(stack, BudgetRebalanceLog)) == 1
    assert (await limits(stack))["Groceries"] == D(1_020_000)


@infra
async def test_a_steady_price_changes_nothing_and_says_nothing(stack, redis):
    await prices(stack, now=10500)
    await budgets(stack)
    n = FakeNotifier()
    out = await check.run_price_check(stack, n, redis, TZ, NOW)
    assert out["anomalies"] == [] and n.sent == [] and await rows(stack, BudgetRebalanceLog) == []
    assert await limits(stack) == {"Groceries": D(1_000_000), "Social": D(1_000_000), "Entertainment": D(1_000_000)}


@infra
async def test_a_shift_never_exceeds_fifteen_percent_of_the_source_budget(stack, redis):
    await prices(stack, base=10000, now=40000)  # 8 x 30.000 = 240.000 extra, far beyond what may move
    await budgets(stack, social=1_000_000, ent=1_000_000)
    n = FakeNotifier()
    out = await check.run_price_check(stack, n, redis, TZ, NOW)
    lim = await limits(stack)
    assert out["moved"][0] == D(150_000)  # Social's band (15% of 1.000.000), also the target's band
    assert lim["Social"] == D(850_000) and lim["Groceries"] == D(1_150_000)


@infra
async def test_the_source_band_alone_can_limit_the_shift(stack, redis):
    await prices(stack, base=10000, now=40000)
    await budgets(stack, groceries=2_000_000)  # the target could take 300.000; each source may give only 150.000
    await check.run_price_check(stack, FakeNotifier(), redis, TZ, NOW)
    assert [(r.from_category, r.amount) for r in await rows(stack, BudgetRebalanceLog)] == [
        ("Social", D(150_000)), ("Entertainment", D(90_000))]  # Social stops at its band; the next source covers the rest


@infra
async def test_the_target_band_alone_can_limit_the_shift(stack, redis):
    await prices(stack, base=10000, now=40000)
    await budgets(stack, groceries=400_000)  # the target may take only 60.000 more
    out = await check.run_price_check(stack, FakeNotifier(), redis, TZ, NOW)
    assert out["moved"][0] == D(60_000) and (await limits(stack))["Groceries"] == D(460_000)


@infra
async def test_an_old_headline_is_not_offered_as_a_cause(stack, redis):
    await prices(stack)
    await budgets(stack)
    async with stack() as s:
        s.add(NewsArticle(url="http://x/2", title="Harga beras naik lama", dedup_hash="h2", source="Old",
                          published_at=NOW - timedelta(days=30)))
        await s.commit()
    n = FakeNotifier()
    await check.run_price_check(stack, n, redis, TZ, NOW)
    assert "Dugaan" not in n.sent[0]


@infra
async def test_a_shift_never_takes_money_the_source_has_already_spent(stack, redis):
    await prices(stack)
    await budgets(stack, social=1_000_000, social_spent=995_000, ent=1_000_000, ent_spent=0)
    out = await check.run_price_check(stack, FakeNotifier(), redis, TZ, NOW)
    lim = await limits(stack)
    # Social has only 5.000 unspent, so it gives that and Entertainment covers the rest
    assert out["moved"] == (D(20000), "Social + Entertainment", "Groceries")
    assert lim["Social"] == D(995_000) and lim["Entertainment"] == D(985_000) and lim["Groceries"] == D(1_020_000)
    assert [(r.from_category, r.amount) for r in await rows(stack, BudgetRebalanceLog)] == [("Social", D(5000)), ("Entertainment", D(15000))]


@infra
async def test_essential_and_sinking_fund_budgets_are_never_the_source(stack, redis):
    await prices(stack)
    month = TODAY.replace(day=1)
    async with stack() as s:
        s.add(BudgetRule(category="Groceries", period_month=month, base_limit=D(1_000_000), current_limit=D(1_000_000)))
        s.add(BudgetRule(category="Social", period_month=month, base_limit=D(1_000_000), current_limit=D(1_000_000), is_essential=True))
        s.add(BudgetRule(category="Entertainment", period_month=month, base_limit=D(1_000_000), current_limit=D(1_000_000), is_sinking_fund=True))
        await s.commit()
    n = FakeNotifier()
    out = await check.run_price_check(stack, n, redis, TZ, NOW)
    assert out["moved"] is None and await rows(stack, BudgetRebalanceLog) == []
    assert "Anggaran tidak diubah" in n.sent[0]  # still told, with the reason nothing moved


@infra
async def test_no_groceries_budget_means_no_shift_but_the_owner_is_still_told(stack, redis):
    await prices(stack)
    n = FakeNotifier()
    out = await check.run_price_check(stack, n, redis, TZ, NOW)
    assert out["moved"] is None and len(n.sent) == 1 and "Beras +25%" in n.sent[0]


@infra
async def test_a_recent_headline_is_offered_as_a_guess_never_a_fact(stack, redis):
    await prices(stack)
    await budgets(stack)
    async with stack() as s:
        s.add(NewsArticle(url="http://x/1", title="Harga beras naik di sejumlah pasar", dedup_hash="h1", source="Antara",
                          published_at=NOW - timedelta(days=1)))
        s.add(NewsArticle(url="http://x/2", title="Harga beras naik lama", dedup_hash="h2", source="Old",
                          published_at=NOW - timedelta(days=30)))
        await s.commit()
    n = FakeNotifier()
    await check.run_price_check(stack, n, redis, TZ, NOW)
    assert "Dugaan penyebab (bukan fakta)" in n.sent[0] and "Antara" in n.sent[0] and "Old" not in n.sent[0]


@infra
async def test_the_briefing_line_exists_only_while_the_shift_is_recent(stack, redis):
    assert await check.price_line(stack, NOW) is None
    await prices(stack)
    await budgets(stack)
    await check.run_price_check(stack, FakeNotifier(), redis, TZ, NOW)
    line = await check.price_line(stack, NOW)
    assert "naik sekitar 25 persen" in line and "Rp 20.000" in line
    assert await check.price_line(stack, NOW + timedelta(days=4)) is None


@infra
async def test_the_personal_index_reflects_the_basket_against_its_baseline(stack):
    await store.ensure_basket(stack)
    assert await check.basket_index(stack, NOW, TZ) is None
    await store.record(stack, "beras", "Nasional", TODAY, D(14000))  # equal to the seeded baseline
    assert await check.basket_index(stack, NOW, TZ) == D("1.000")
    await store.record(stack, "beras", "Nasional", TODAY, D(28000))
    assert await check.basket_index(stack, NOW, TZ) == D("2.000")


# ---- the API ----------------------------------------------------------------------------------------------------------------


@pytest.fixture
async def api(stack, monkeypatch):
    from fastapi import FastAPI

    monkeypatch.setenv("INGEST_API_KEY", "k")
    get_settings.cache_clear()
    monkeypatch.setattr(prices_router, "SessionLocal", stack)
    app = FastAPI()
    app.include_router(prices_router.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


def obs(code="beras", price=14000, **kw):
    return {"code": code, "date": TODAY.isoformat(), "price": price, **kw}


@infra
async def test_prices_can_be_posted_with_the_key_and_bad_ones_are_skipped(stack, api):  # on a fresh database too
    body = {"observations": [obs(), obs("emas"), obs(price=5), obs("telur", 31000, region="Jakarta")]}
    assert (await api.post("/prices/ingest", json=body)).status_code == 401
    ok = await api.post("/prices/ingest", json=body, headers={"x-api-key": "k"})
    assert ok.status_code == 200 and ok.json()["stored"] == 2
    assert {(r.commodity_code, r.pihps_region) for r in await rows(stack, CommodityPrice)} == {("beras", "Nasional"), ("telur", "Jakarta")}


@infra
@pytest.mark.parametrize("body", [{"observations": []}, {}, {"observations": [obs(price=-1)]}, {"observations": [obs(price="x")]},
                                  {"observations": [obs()] * 501}])
async def test_malformed_price_payloads_are_rejected(stack, api, body):
    assert (await api.post("/prices/ingest", json=body, headers={"x-api-key": "k"})).status_code == 422


@infra
async def test_the_morning_briefing_mentions_a_recent_staple_rise(stack, redis):
    from apa.news import briefing as brief

    class Quiet:
        async def complete(self, *a, **k):
            raise RuntimeError("no model needed for this section")

    plain = await brief.build(stack, Quiet(), redis, TZ, NOW, kind="short")
    assert "harga" not in [s.topic for s in plain.sections]
    await prices(stack)
    await budgets(stack)
    await check.run_price_check(stack, FakeNotifier(), redis, TZ, NOW)
    b = await brief.build(stack, Quiet(), redis, TZ, NOW, kind="short")
    assert "harga" in [s.topic for s in b.sections] and "Harga bahan pokok naik sekitar 25 persen" in b.text
