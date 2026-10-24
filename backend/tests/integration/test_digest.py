"""A-12 digests: exact numbers from SQL, pushed on a schedule, sent once."""

import os
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from redis.asyncio import Redis

from apa.core.settings import get_settings
from apa.db.models.budget import BudgetRule
from apa.db.models.ledger import Transaction
from apa.db.models.ops import TelegramMessage
from apa.reporting import digest
from apa.workers import settings as worker
from tests.integration.conftest import HAVE_INFRA
from tests.integration.helpers import FakeNotifier, rows

pytestmark = pytest.mark.skipif(not HAVE_INFRA, reason="TEST_DATABASE_URL/TEST_REDIS_URL not set")

TZ = ZoneInfo("Asia/Jakarta")
NOW = datetime(2026, 10, 25, 20, 0, tzinfo=TZ)  # a Sunday evening


async def add(stack, days_ago, amount, category="Food & Dining", merchant="Warteg", *, direction="out",
              status="committed"):
    async with stack() as s:
        s.add(Transaction(
            timestamp=NOW - timedelta(days=days_ago, hours=1), source_channel="manual_text",
            payment_wallet="cash_wallet", merchant=merchant, direction=direction,
            total_paid_amount=Decimal(amount), net_user_expense=Decimal(amount if direction == "out" else 0),
            primary_category=category, execution_mode="consensus_auto", status=status,
        ))
        await s.commit()


async def budget(stack, category, limit, spent):
    async with stack() as s:
        s.add(BudgetRule(category=category, period_month=NOW.date().replace(day=1), base_limit=Decimal(limit),
                         current_limit=Decimal(limit), spent_amount=Decimal(spent)))
        await s.commit()


@pytest.fixture
async def redis(stack):
    r = Redis.from_url(os.environ["TEST_REDIS_URL"])
    yield r
    await r.aclose()


async def test_the_weekly_digest_has_exact_totals_categories_merchants_and_the_change(stack):
    await add(stack, 1, 100_000, "Food & Dining", "Warteg")
    await add(stack, 2, 50_000, "Food & Dining", "Kopi")
    await add(stack, 3, 200_000, "Transport", "Grab")
    await add(stack, 4, 1_000_000, "Income", "Gaji", direction="in")
    await add(stack, 9, 175_000, "Food & Dining", "Warteg")  # the previous week
    text = await digest.weekly(stack, TZ, NOW)
    assert "4 transaksi" in text and "Keluar Rp 350.000 (+100% vs periode sebelumnya)" in text
    assert "Masuk Rp 1.000.000" in text
    assert "• Food & Dining: Rp 150.000" in text and "• Transport: Rp 200.000" in text
    assert text.index("Transport") < text.index("Food & Dining: Rp 150") or "Transport: Rp 200.000" in text
    assert "Terbesar: Grab Rp 200.000, Warteg Rp 100.000, Kopi Rp 50.000" in text


async def test_only_committed_money_counts(stack):
    await add(stack, 1, 100_000)
    await add(stack, 1, 5_000_000, status="quarantined")
    await add(stack, 1, 700_000, status="rolled_back")
    text = await digest.weekly(stack, TZ, NOW)
    assert "1 transaksi" in text and "Keluar Rp 100.000" in text and "5.000.000" not in text
    assert "⏳ 1 transaksi ditahan menunggu keputusan" in text  # a held one is flagged for the owner


async def test_the_budget_section_shows_the_three_tightest_with_flags(stack):
    await add(stack, 1, 10_000)
    for cat, limit, spent in [("A", 100, 120), ("B", 100, 85), ("C", 100, 10), ("D", 100, 50)]:
        await budget(stack, cat, limit * 1000, spent * 1000)
    text = await digest.weekly(stack, TZ, NOW)
    assert "🔴 A: Rp 120.000 / Rp 100.000 (120%)" in text and "🟡 B:" in text and "🟢 D:" in text
    assert "C:" not in text.split("Anggaran")[1]  # only the top 3


async def test_an_empty_week_sends_nothing(stack):
    await add(stack, 20, 100_000)
    assert await digest.weekly(stack, TZ, NOW) is None
    assert await digest.monthly(stack, TZ, datetime(2026, 9, 1, 12, tzinfo=TZ)) is None


async def test_the_monthly_digest_covers_the_month_and_compares_with_the_last(stack):
    now = datetime(2026, 10, 31, 20, 0, tzinfo=TZ)
    async with stack() as s:
        for ts, amt, cat, d in [
            (datetime(2026, 10, 2, 9, tzinfo=TZ), 300_000, "Food & Dining", "out"),
            (datetime(2026, 10, 15, 9, tzinfo=TZ), 200_000, "Transport", "out"),
            (datetime(2026, 10, 25, 9, tzinfo=TZ), 5_000_000, "Income", "in"),
            (datetime(2026, 9, 20, 9, tzinfo=TZ), 250_000, "Food & Dining", "out"),
        ]:
            s.add(Transaction(timestamp=ts, source_channel="manual_text", payment_wallet="cash_wallet", merchant="M",
                              direction=d, total_paid_amount=Decimal(amt),
                              net_user_expense=Decimal(amt if d == "out" else 0), primary_category=cat,
                              execution_mode="consensus_auto", status="committed"))
        await s.commit()
    text = await digest.monthly(stack, TZ, now)
    assert "10/2026 · 3 transaksi" in text and "Keluar Rp 500.000 (+100% vs periode sebelumnya)" in text
    assert "Masuk Rp 5.000.000" in text and "Selisih Rp 4.500.000 (90% dari pemasukan)" in text


async def test_a_digest_is_delivered_once_recorded_and_not_repeated(stack, redis):
    n = FakeNotifier()
    assert await digest.deliver(stack, n, redis, "weekly:2026-W43", "isi") is True
    assert await digest.deliver(stack, n, redis, "weekly:2026-W43", "isi") is False  # a restart at 20:00
    assert n.sent == ["isi"]
    (tm,) = await rows(stack, TelegramMessage)
    assert (tm.kind, tm.body, tm.message_id) == ("digest", "isi", 101)
    assert await digest.deliver(stack, n, redis, "weekly:2026-W44", "lagi") is True  # the next week is new


async def test_a_failed_send_can_be_retried_by_the_next_tick(stack, redis):
    class Down(FakeNotifier):
        async def send(self, text):
            raise RuntimeError("telegram down")

    assert await digest.deliver(stack, Down(), redis, "weekly:2026-W43", "isi") is False
    assert await digest.deliver(stack, FakeNotifier(), redis, "weekly:2026-W43", "isi") is True


class FrozenDatetime(datetime):
    frozen = NOW

    @classmethod
    def now(cls, tz=None):
        return cls.frozen.astimezone(tz) if tz else cls.frozen


async def test_the_weekly_cron_sends_the_digest_without_any_command(stack, redis, monkeypatch):
    monkeypatch.setattr(worker, "datetime", FrozenDatetime)
    await add(stack, 1, 100_000)
    n = FakeNotifier()
    ctx = {"sessionmaker": stack, "notifier": n, "redis": redis}
    await worker.weekly_digest(ctx)
    await worker.weekly_digest(ctx)  # fired twice: still one message
    assert len(n.sent) == 1 and "Ringkasan mingguan" in n.sent[0]


async def test_the_monthly_cron_only_fires_on_the_last_day(stack, redis, monkeypatch):
    monkeypatch.setattr(worker, "datetime", FrozenDatetime)
    n = FakeNotifier()
    ctx = {"sessionmaker": stack, "notifier": n, "redis": redis}
    async with stack() as s:
        for day in (10, 31):
            s.add(Transaction(timestamp=datetime(2026, 10, day, 9, tzinfo=TZ), source_channel="manual_text",
                              payment_wallet="cash_wallet", merchant="M", direction="out",
                              total_paid_amount=Decimal(1000), net_user_expense=Decimal(1000),
                              primary_category="Other", execution_mode="consensus_auto", status="committed"))
        await s.commit()
    FrozenDatetime.frozen = datetime(2026, 10, 30, 20, 0, tzinfo=TZ)
    await worker.monthly_digest(ctx)
    assert n.sent == []
    FrozenDatetime.frozen = datetime(2026, 10, 31, 20, 0, tzinfo=TZ)
    await worker.monthly_digest(ctx)
    assert len(n.sent) == 1 and "Ringkasan bulanan 10/2026" in n.sent[0]
    FrozenDatetime.frozen = NOW


async def test_digests_are_registered_on_the_schedule():
    names = {job.coroutine.__name__ for job in worker.WorkerSettings.cron_jobs}
    assert {"weekly_digest", "monthly_digest"} <= names
    assert get_settings().telegram_owner_id is not None
