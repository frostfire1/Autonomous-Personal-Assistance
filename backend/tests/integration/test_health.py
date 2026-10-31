"""Health: intake estimates, ingest, warnings, stress shield, fatigue defence, the Huawei client and the API."""

import os
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy import select

from apa.chat.snapshot import health_snapshot
from apa.core import secrets
from apa.core.settings import get_settings
from apa.db.models.audit import ExplainabilityLog
from apa.db.models.budget import BudgetRule
from apa.db.models.health import (
    BiomarkerMetric,
    HealthWarning,
    HuaweiHealthMetric,
    SleepSession,
    StressReading,
)
from apa.db.models.ledger import Transaction, TransactionItem
from apa.db.models.ops import TelegramMessage
from apa.gateway.routers import health as health_router
from apa.health.ingest import HealthIn, MetricIn, SleepIn, StressIn, ingest, stress_grade
from apa.health.jobs import (
    check_intake,
    check_sleep,
    check_stress_shield,
    refresh_biomarkers,
    today_intake_tuple,
)
from apa.health.nutrition import estimate
from apa.safety.audit_log import verify_log
from tests.integration.conftest import HAVE_INFRA
from tests.integration.helpers import FakeLLM, FakeNotifier, commit, new_event, parsed, rows

TZ = ZoneInfo("Asia/Jakarta")
NOW = datetime.now(TZ).replace(hour=15, minute=0, second=0, microsecond=0)

# ---- pure -------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "caffeine", "sugar", "sodium"),
    [("Kopi Susu Aren", 95, 18, 0), ("es teh manis", 25, 20, 0), ("ES TEH", 25, 20, 0), ("indomie goreng", 0, 2, 1500),
     ("Kopi", 80, 4, 0), ("Red Bull 250ml", 80, 27, 0), ("nasi putih", None, None, None), ("", None, None, None)],
)
def test_the_longest_matching_keyword_gives_the_estimate(name, caffeine, sugar, sodium):
    e = estimate(name)
    if caffeine is None:
        assert e is None
    else:
        assert (e.caffeine_mg, e.sugar_g, e.sodium_mg) == (Decimal(caffeine), Decimal(sugar), Decimal(sodium))


@pytest.mark.parametrize(("score", "grade"), [(10, "low"), (39, "low"), (40, "normal"), (60, "medium"), (75, "high"), (100, "high")])
def test_stress_grades(score, grade):
    assert stress_grade(score) == grade


@pytest.mark.parametrize(
    "bad",
    [{"resting_heart_rate": 5}, {"spo2_pct": 120}, {"step_count": -1}, {"step_count": 10**7}, {"active_calories": 10**6}],
)
def test_implausible_readings_are_rejected(bad):
    with pytest.raises(ValueError):
        MetricIn(recorded_at=datetime.now(UTC), **bad)


def test_unknown_fields_and_oversized_payloads_are_rejected():
    with pytest.raises(ValueError):
        HealthIn(device_id="d", metrics=[], bogus=1)
    with pytest.raises(ValueError):
        HealthIn(device_id="d", metrics=[MetricIn(recorded_at=datetime.now(UTC))] * 501)
    with pytest.raises(ValueError):
        HealthIn(device_id="", metrics=[])
    with pytest.raises(ValueError):
        SleepIn(sleep_date=date.today(), total_sleep_mins=2000)


# ---- ingest -------------------------------------------------------------------------------------------------------

infra = pytest.mark.skipif(not HAVE_INFRA, reason="TEST_DATABASE_URL/TEST_REDIS_URL not set")


@pytest.fixture
async def redis(stack):
    r = Redis.from_url(os.environ["TEST_REDIS_URL"])
    yield r
    await r.aclose()


def payload(**over):
    base = dict(
        device_id="phone-1",
        metrics=[MetricIn(recorded_at=NOW - timedelta(hours=2), resting_heart_rate=58, step_count=4200)],
        sleep=[SleepIn(sleep_date=NOW.date(), total_sleep_mins=330, sleep_score=64, deep_sleep_pct=Decimal("12"))],
        stress=[StressIn(timestamp=NOW - timedelta(minutes=m), stress_score=80) for m in (100, 70, 40, 10)],
    )
    return HealthIn(**{**base, **over})


@infra
async def test_ingest_stores_readings_and_resending_adds_nothing(stack):
    first = await ingest(stack, payload(), source="health_connect_bridge")
    assert first == {"metrics": 1, "sleep": 1, "stress": 4}
    again = await ingest(stack, payload(), source="health_connect_bridge")
    assert again == {"metrics": 0, "sleep": 0, "stress": 0}
    assert len(await rows(stack, HuaweiHealthMetric)) == 1 and len(await rows(stack, StressReading)) == 4
    (m,) = await rows(stack, HuaweiHealthMetric)
    assert (m.device_id, m.sync_source, m.step_count) == ("phone-1", "health_connect_bridge", 4200)


@infra
async def test_a_poor_night_switches_fatigue_defence_on_and_a_good_one_off(stack):
    await ingest(stack, payload(), source="x")
    (s,) = await rows(stack, SleepSession)
    assert s.fatigue_defense_active is True  # score 64 < 70, deep 12 < 15
    good = SleepIn(sleep_date=NOW.date(), total_sleep_mins=450, sleep_score=88, deep_sleep_pct=Decimal("22"))
    await ingest(stack, payload(sleep=[good], metrics=[], stress=[]), source="x")
    (s,) = await rows(stack, SleepSession)
    assert s.fatigue_defense_active is False and s.total_sleep_mins == 450  # updated in place, not duplicated


@infra
async def test_stress_readings_get_their_grade(stack):
    await ingest(stack, payload(), source="x")
    assert {r.stress_grade for r in await rows(stack, StressReading)} == {"high"}


# ---- intake and warnings ---------------------------------------------------------------------------------------------


async def purchase(stack, merchant, amount=20000, hours_ago=1, items=None):
    async with stack() as s:
        tx = Transaction(timestamp=NOW - timedelta(hours=hours_ago), source_channel="manual_text", payment_wallet="cash_wallet",
                         merchant=merchant, direction="out", total_paid_amount=Decimal(amount), net_user_expense=Decimal(amount),
                         primary_category="Food & Dining", execution_mode="consensus_auto", status="committed")
        s.add(tx)
        await s.flush()
        for name, qty in items or []:
            s.add(TransactionItem(transaction_id=tx.id, item_name=name, quantity=Decimal(qty), unit_price=Decimal(1000),
                                  total_price=Decimal(1000) * qty))
        await s.commit()
        return tx.id


@infra
async def test_todays_intake_counts_receipt_lines_with_quantity_and_loose_purchases_once(stack):
    await purchase(stack, "WARUNG X", items=[("Es Teh Manis", 2), ("Nasi Putih", 1)])  # 2 x (25 mg, 20 g)
    await purchase(stack, "kopi susu")  # no lines: estimated from the name
    await purchase(stack, "kopi susu", hours_ago=30)  # yesterday: not today
    row = await refresh_biomarkers(stack, TZ, NOW)
    assert (row.cumulative_caffeine_mg, row.cumulative_sugar_g) == (Decimal(50 + 95), Decimal(40 + 18))
    assert row.caffeine_limit_breached is False and row.sugar_limit_breached is True  # 58 g > the 30 g limit
    again = await refresh_biomarkers(stack, TZ, NOW)
    assert again.id == row.id and len(await rows(stack, BiomarkerMetric)) == 1  # one row per day, updated in place


@infra
async def test_a_receipt_with_lines_is_not_counted_twice_through_its_merchant_name(stack):
    await purchase(stack, "kopi kenangan", items=[("Aren Latte", 1)])  # lines say latte (95 mg); merchant says kopi
    row = await refresh_biomarkers(stack, TZ, NOW)
    assert row.cumulative_caffeine_mg == Decimal(95)


@infra
async def test_an_early_warning_comes_at_80_percent_once_a_day_and_is_recorded(stack, redis):
    n = FakeNotifier()
    for _ in range(3):
        await purchase(stack, "kopi susu")  # 3 x 95 = 285 mg: below 80% of 400 (320)
    first = await check_intake(stack, n, redis, TZ, NOW)
    assert first == ["sugar_overload"]  # 3 x 18 g = 54 g is past the 30 g limit; caffeine is still under 80%
    await purchase(stack, "americano")  # +95 = 380 mg
    sent = await check_intake(stack, n, redis, TZ, NOW)
    assert sent == ["caffeine_spike"]  # sugar was already said today
    assert "Kafein hari ini sekitar 380 mg (perkiraan), 95%" in n.sent[1] and "lewat jam 14:00" in n.sent[1]
    assert await check_intake(stack, n, redis, TZ, NOW) == []  # nothing is repeated today
    assert {w.warning_type for w in await rows(stack, HealthWarning)} == {"caffeine_spike", "sugar_overload"}
    assert all(m.kind == "digest" for m in await rows(stack, TelegramMessage))


@infra
async def test_the_warning_text_makes_clear_it_is_an_estimate_and_gives_no_medical_advice(stack, redis):
    n = FakeNotifier()
    for _ in range(4):
        await purchase(stack, "kopi susu")
    await check_intake(stack, n, redis, TZ, NOW)
    text = " ".join(n.sent)
    assert "perkiraan" in text and "diagnos" not in text.lower() and "obat" not in text.lower()


@infra
async def test_intake_reaches_the_debate_and_a_bad_night_switches_fatigue_defence_on_there(stack):
    await purchase(stack, "kopi susu")
    await refresh_biomarkers(stack, TZ, NOW)
    await ingest(stack, payload(metrics=[], stress=[]), source="x")  # a poor night
    llm = FakeLLM()
    eid = await new_event(stack, parsed(), at=NOW)
    assert await commit(stack, eid, llm) == "committed"
    prompts = [u for _, u in llm.calls]
    assert prompts and all("intake so far today: caffeine 95.00 mg" in u for u in prompts)
    assert all("fatigue defense is ACTIVE" in u for u in prompts)
    good = FakeLLM()
    await ingest(stack, payload(sleep=[SleepIn(sleep_date=NOW.date(), total_sleep_mins=470, sleep_score=90,
                                                 deep_sleep_pct=Decimal("22"))], metrics=[], stress=[]), source="x")
    assert await commit(stack, await new_event(stack, parsed(amount="19000"), at=NOW), good) == "committed"
    assert all("fatigue defense" not in u for _, u in good.calls)


@infra
async def test_a_short_night_is_said_once_in_the_morning_and_not_before_six(stack, redis):
    n = FakeNotifier()
    await ingest(stack, payload(metrics=[], stress=[]), source="x")  # 330 minutes
    assert await check_sleep(stack, n, redis, TZ, NOW.replace(hour=5)) is False
    assert await check_sleep(stack, n, redis, TZ, NOW.replace(hour=8)) is True
    assert await check_sleep(stack, n, redis, TZ, NOW.replace(hour=9)) is False
    assert "5 jam 30 menit" in n.sent[0] and len(n.sent) == 1
    ok = SleepIn(sleep_date=NOW.date(), total_sleep_mins=420, sleep_score=80, deep_sleep_pct=Decimal("20"))
    await ingest(stack, payload(sleep=[ok], metrics=[], stress=[]), source="x")
    assert await check_sleep(stack, FakeNotifier(), None, TZ, NOW.replace(hour=10)) is False


# ---- stress shield -----------------------------------------------------------------------------------------------------


async def entertainment(stack, spent=0):
    async with stack() as s:
        s.add(BudgetRule(category="Entertainment", period_month=NOW.date().replace(day=1), base_limit=Decimal(1_000_000),
                         current_limit=Decimal(1_000_000), spent_amount=Decimal(spent)))
        await s.commit()


@infra
async def test_sustained_high_stress_moves_a_bounded_amount_with_a_neutral_note_once_a_day(stack, redis):
    await entertainment(stack)
    await ingest(stack, payload(metrics=[], sleep=[]), source="x")
    n = FakeNotifier()
    moved = await check_stress_shield(stack, n, redis, TZ, NOW)
    assert moved == Decimal(100_000)
    assert "Mental Care" in n.sent[0] and "Entertainment" in n.sent[0]
    limits = {b.category: b.current_limit for b in await rows(stack, BudgetRule)}
    assert limits == {"Entertainment": Decimal(900_000), "Mental Care": Decimal(100_000)}
    flagged = await rows(stack, StressReading, StressReading.stress_shield_triggered.is_(True))
    assert len(flagged) == 4 and sum(1 for r in flagged if r.mental_buffer_allocated) == 1
    assert await check_stress_shield(stack, n, redis, TZ, NOW) is None  # once today
    async with stack() as s:
        assert await verify_log(s) is None
        assert any("stress recovery" in r.justification for r in
                   (await s.execute(select(ExplainabilityLog))).scalars())


@infra
async def test_a_single_spike_or_a_dip_below_the_threshold_does_not_trigger_the_shield(stack, redis):
    await entertainment(stack)
    spike = [StressIn(timestamp=NOW - timedelta(minutes=5), stress_score=95)]
    dip = [StressIn(timestamp=NOW - timedelta(minutes=m), stress_score=s)
           for m, s in ((100, 80), (70, 40), (40, 80), (10, 80))]
    for readings in (spike, dip):
        await ingest(stack, payload(metrics=[], sleep=[], stress=readings), source="x")
        assert await check_stress_shield(stack, FakeNotifier(), redis, TZ, NOW) is None
    burst = [StressIn(timestamp=NOW - timedelta(minutes=m), stress_score=90) for m in (3, 2, 1)]
    await ingest(stack, payload(metrics=[], sleep=[], stress=burst), source="x")
    assert await check_stress_shield(stack, FakeNotifier(), redis, TZ, NOW) is None  # a burst, not a stretch


@infra
async def test_the_shield_moves_nothing_when_no_discretionary_budget_has_room(stack, redis):
    await entertainment(stack, spent=1_000_000)
    await ingest(stack, payload(metrics=[], sleep=[]), source="x")
    assert await check_stress_shield(stack, FakeNotifier(), redis, TZ, NOW) is None


@infra
async def test_the_health_snapshot_reports_sleep_intake_and_steps_or_says_there_is_nothing(stack):
    assert "Belum ada data kesehatan" in await health_snapshot(stack, TZ, NOW)
    await purchase(stack, "kopi susu")
    await refresh_biomarkers(stack, TZ, NOW)
    await ingest(stack, payload(stress=[]), source="x")
    text = await health_snapshot(stack, TZ, NOW)
    assert "kafein 95 mg" in text and "5 jam 30 menit" in text and "mode kelelahan aktif" in text and "LANGKAH terakhir: 4200" in text
    assert (await today_intake_tuple(stack, TZ, NOW))[0] == Decimal(95)


# ---- Secrets Encryption --------------------------------------------------------------------------------------------------


@pytest.fixture
def secrets_env(monkeypatch):
    monkeypatch.setenv("SECRETS_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_secrets_refuse_to_store_anything_without_a_valid_key(monkeypatch):
    monkeypatch.delenv("SECRETS_KEY", raising=False)
    get_settings.cache_clear()
    with pytest.raises(secrets.SecretsUnavailable):
        secrets.encrypt("token")
    monkeypatch.setenv("SECRETS_KEY", "not-a-key")
    get_settings.cache_clear()
    with pytest.raises(secrets.SecretsUnavailable):
        secrets.encrypt("token")
    get_settings.cache_clear()


def test_secrets_round_trip_and_a_wrong_key_cannot_read(secrets_env, monkeypatch):
    token = secrets.encrypt("sekret")
    assert token != "sekret" and secrets.decrypt(token) == "sekret"
    monkeypatch.setenv("SECRETS_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()
    with pytest.raises(secrets.SecretsUnavailable):
        secrets.decrypt(token)


# ---- the API -------------------------------------------------------------------------------------------------------------


@pytest.fixture
async def api(stack, monkeypatch):
    from fastapi import FastAPI

    monkeypatch.setenv("INGEST_API_KEY", "k")
    get_settings.cache_clear()
    monkeypatch.setattr(health_router, "SessionLocal", stack)
    app = FastAPI()
    app.include_router(health_router.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


BODY = {"device_id": "phone-1", "metrics": [{"recorded_at": NOW.isoformat(), "step_count": 1000}],
        "sleep": [{"sleep_date": NOW.date().isoformat(), "total_sleep_mins": 400, "sleep_score": 80}]}


@infra
async def test_the_app_posts_readings_with_the_key_and_may_retry(stack, api):
    assert (await api.post("/health/ingest", json=BODY)).status_code == 401
    ok = await api.post("/health/ingest", json=BODY, headers={"x-api-key": "k"})
    assert ok.status_code == 200 and ok.json()["added"] == {"metrics": 1, "sleep": 1, "stress": 0}
    again = await api.post("/health/ingest", json=BODY, headers={"x-api-key": "k"})
    assert again.json()["added"] == {"metrics": 0, "sleep": 0, "stress": 0}
    (m,) = await rows(stack, HuaweiHealthMetric)
    assert m.sync_source == "health_connect_bridge"


@infra
async def test_bad_readings_are_refused_whole_not_partly_stored(stack, api):
    bad = {**BODY, "metrics": [{"recorded_at": NOW.isoformat(), "resting_heart_rate": 3}]}
    r = await api.post("/health/ingest", json=bad, headers={"x-api-key": "k"})
    assert r.status_code == 422 and await rows(stack, HuaweiHealthMetric) == [] and await rows(stack, SleepSession) == []
    extra = await api.post("/health/ingest", json={**BODY, "bogus": 1}, headers={"x-api-key": "k"})
    assert extra.status_code == 422
