"""merchant_cache learning and Telegram intake against a real Postgres + Redis."""

import asyncio
import statistics
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tests.integration.conftest import HAVE_INFRA

pytestmark = pytest.mark.skipif(not HAVE_INFRA, reason="TEST_DATABASE_URL/TEST_REDIS_URL not set")

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


async def _visit(stack, name, amount, when, **kw):
    from apa.ingestion.merchants import record_visit

    async with stack() as s:
        row = await record_visit(s, name, Decimal(amount), when, **kw)
        await s.commit()
        return row


async def test_learning_matches_independent_statistics(stack):
    from apa.ingestion.merchants import get_stats

    amounts = [18000, 22000, 20000, 25000, 15000, 19000]
    for i, a in enumerate(amounts):
        await _visit(stack, "KOPI KENANGAN 0042", a, T0 + timedelta(days=i * 2))

    async with stack() as s:
        st = await get_stats(s, "kopi kenangan")  # different spelling, same normalized key
    assert st.visit_count == 6
    assert abs(st.avg_amount - Decimal(sum(amounts)) / 6) <= Decimal("0.005")  # rounded to 2 dp
    assert abs(float(st.stddev_amount) - statistics.stdev(amounts)) < 0.01
    assert st.usable_for_deviation
    assert st.visits_per_week == Decimal("4.20")  # 6 visits over 10 days = 1.43 weeks


async def test_remove_visit_is_the_exact_inverse(stack):
    from apa.ingestion.merchants import get_stats, remove_visit

    for a in (10000, 20000, 30000):
        await _visit(stack, "Warkop Ujang", a, T0)
    before = None
    async with stack() as s:
        before = await get_stats(s, "warkop ujang")
    await _visit(stack, "Warkop Ujang", 99999, T0)  # a visit that is then undone
    async with stack() as s:
        await remove_visit(s, "Warkop Ujang", Decimal(99999))
        await s.commit()
        after = await get_stats(s, "warkop ujang")
    assert after == before


async def test_removing_the_last_visit_resets_to_clean_state(stack):
    from apa.ingestion.merchants import get_stats, remove_visit

    await _visit(stack, "Toko Baru", 5000, T0)
    async with stack() as s:
        await remove_visit(s, "Toko Baru", Decimal(5000))
        await s.commit()
        st = await get_stats(s, "toko baru")
    assert st.visit_count == 0 and st.avg_amount is None


async def test_concurrent_first_visits_do_not_lose_updates_or_crash(stack):
    from apa.ingestion.merchants import get_stats

    await asyncio.gather(*(_visit(stack, "Indomaret", 10000, T0) for _ in range(8)))
    async with stack() as s:
        st = await get_stats(s, "indomaret")
    assert st.visit_count == 8 and st.avg_amount == Decimal("10000.00")


async def test_learning_never_overrides_a_manual_category(stack):
    from sqlalchemy import select

    from apa.db.models.budget import MerchantCache

    async with stack() as s:
        s.add(MerchantCache(merchant_key="gym", display_name="Gym", default_category="Health",
                            source="manual"))
        await s.commit()
    await _visit(stack, "Gym", 150000, T0, category="Entertainment", agent="A-05")
    async with stack() as s:
        row = (await s.execute(select(MerchantCache).where(MerchantCache.merchant_key == "gym"))).scalar_one()
    assert (row.default_category, row.source, row.default_agent) == ("Health", "manual", "A-05")


async def test_lookup_by_alias_pattern_and_baseline_text(stack):
    from sqlalchemy import update

    from apa.db.models.budget import MerchantCache
    from apa.ingestion.merchants import baseline_context

    for a, d in ((40000, 0), (50000, 3), (45000, 7), (45000, 14)):
        await _visit(stack, "Warung Soto Pak Kumis", a, T0 + timedelta(days=d), category="Food & Dining")
    async with stack() as s:
        await s.execute(update(MerchantCache).values(match_patterns=["soto kumis"]))
        await s.commit()
        text = await baseline_context(s, "Soto Kumis")  # found via match_patterns, not the key
        none = await baseline_context(s, "Tempat Tidak Dikenal")
    assert none is None
    assert "Rp 45.000" in text and "Food & Dining" in text and "4 kunjungan" in text


async def test_baseline_context_needs_history(stack):
    from apa.ingestion.merchants import baseline_context

    await _visit(stack, "Sekali Doang", 10000, T0)
    async with stack() as s:
        assert await baseline_context(s, "Sekali Doang") is None  # one visit is not a baseline


async def test_telegram_text_becomes_a_deduplicated_event_and_parses(stack, monkeypatch):
    from arq import create_pool
    from sqlalchemy import select

    from apa.bot import main as bot
    from apa.core.redis import arq_redis_settings
    from apa.db.models.ops import InboundEvent
    from apa.workers import settings as worker

    monkeypatch.setattr(bot, "SessionLocal", stack)
    monkeypatch.setattr(worker, "SessionLocal", stack)
    msg = SimpleNamespace(
        text="kopi susu 18k tunai rek 1234567890", message_id=77, chat=SimpleNamespace(id=555),
        date=datetime(2026, 10, 23, 12, 0, tzinfo=UTC),
    )
    arq = await create_pool(arq_redis_settings())
    try:
        first = await bot.ingest_text(arq, msg)
        replay = await bot.ingest_text(arq, msg)  # Telegram redelivers the same update
        assert first is not None and replay is None
        jobs = [j.function for j in await arq.queued_jobs()]
        assert jobs.count("process_inbound_event") == 1
    finally:
        await arq.aclose()

    assert await worker.process_inbound_event({}, first) == "parsed"
    async with stack() as s:
        ev = (await s.execute(select(InboundEvent).where(InboundEvent.id == first))).scalar_one()
    assert ev.source == "telegram_shorthand" and ev.payload["parsed"]["amount"] == "18000"
    assert "1234567890" not in ev.payload["text"]
