"""Real Postgres + Redis end-to-end test. Skipped unless both are provided:

    TEST_DATABASE_URL=postgresql+asyncpg://postgres:pw@localhost:5432/apa_test
    TEST_REDIS_URL=redis://localhost:6379/15
    pytest tests/integration -v

The database must be EMPTY and disposable: the test runs the migration, then drops everything.
"""

import uuid

import pytest

from tests.integration.conftest import HAVE_INFRA

pytestmark = pytest.mark.skipif(not HAVE_INFRA, reason="TEST_DATABASE_URL/TEST_REDIS_URL not set")


async def test_migration_seeds_policy_rules(stack):
    from sqlalchemy import text

    async with stack() as s:
        rows = (await s.execute(text("SELECT rule_key, value_numeric FROM policy_rules"))).all()
    assert dict(rows)["single_tx_ceiling"] == 2500000
    assert dict(rows)["velocity_max_tx"] == 5


async def test_explainability_log_is_append_only(stack):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    async with stack() as s:
        await s.execute(
            text(
                "INSERT INTO explainability_log (decision_type, verdict, justification, row_hash) "
                "VALUES ('config_change', 'seed', 'test', repeat('a', 64))"
            )
        )
        await s.commit()
    for stmt in ("UPDATE explainability_log SET verdict='x'", "DELETE FROM explainability_log",
                 # CASCADE gets past the FK refusal so the BEFORE TRUNCATE trigger is what blocks it
                 "TRUNCATE explainability_log CASCADE"):
        async with stack() as s:
            with pytest.raises(DBAPIError, match="append-only"):
                await s.execute(text(stmt))
                await s.commit()


async def test_ingest_then_worker_end_to_end(stack, monkeypatch):
    import httpx
    from arq import create_pool
    from sqlalchemy import select

    from apa.core.redis import arq_redis_settings
    from apa.db.models.ops import InboundEvent
    from apa.gateway.main import app
    from apa.workers import settings as worker

    # point the app and worker at the test database
    monkeypatch.setattr(worker, "SessionLocal", stack)
    import apa.core.db as core_db

    monkeypatch.setattr(core_db, "SessionLocal", stack)

    async def _session():
        async with stack() as s:
            yield s

    app.dependency_overrides[core_db.get_session] = _session
    key = f"e2e-{uuid.uuid4().hex}"
    body = {
        "idempotency_key": key,
        "package_name": "com.bca",
        "text": "Pembayaran QRIS Rp18.000 ke KOPI KENANGAN rek 1234567890 berhasil.",
        "posted_at": "2026-10-23T12:00:00+07:00",
    }
    hdr = {"x-api-key": "test-key"}
    # Same event loop as the DB engine (the sync TestClient would run the app in another loop).
    async with app.router.lifespan_context(app):  # real arq pool on test Redis
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post("/ingest/notification", json=body, headers=hdr)
            assert r.status_code == 202 and r.json()["duplicate"] is False
            event_id = r.json()["event_id"]

            dup = await client.post("/ingest/notification", json=body, headers=hdr)
            assert dup.json()["duplicate"] is True

    pool = await create_pool(arq_redis_settings())
    try:
        queued = await pool.queued_jobs()
        assert [j.function for j in queued].count("process_inbound_event") == 1  # no double enqueue
    finally:
        await pool.aclose()

    assert await worker.process_inbound_event({}, event_id) == "parsed"
    async with stack() as s:
        ev = (await s.execute(select(InboundEvent).where(InboundEvent.id == event_id))).scalar_one()
    assert ev.status == "parsed" and ev.processed_at is not None
    assert ev.payload["parsed"]["amount"] == "18000"
    assert "1234567890" not in ev.payload["text"]  # PII stripped before storage
    app.dependency_overrides.clear()


# ---- which apps are listened to, promotions, and the cap on model calls --------------------------------------------------------


class _ArqLike:
    """A Redis that also records enqueue_job, like the worker's pool."""

    def __new__(cls, url):
        from redis.asyncio import Redis

        class R(Redis):
            jobs: list = []

            async def enqueue_job(self, *a, **k):
                self.jobs.append(a)

        r = R.from_url(url)
        r.jobs = []
        return r


@pytest.fixture
async def worker_env(stack, monkeypatch):
    import os

    from apa.workers import settings as worker

    monkeypatch.setattr(worker, "SessionLocal", stack)
    r = _ArqLike(os.environ["TEST_REDIS_URL"])
    yield worker, {"redis": r}, r
    await r.aclose()


async def _notify(stack, worker, ctx, text, package="com.bca"):
    from tests.integration.helpers import raw_event

    eid = await raw_event(stack, text, source="android_listener", package=package)
    status = await worker.process_inbound_event(ctx, eid)
    from sqlalchemy import select

    from apa.db.models.ops import InboundEvent

    async with stack() as s:
        ev = (await s.execute(select(InboundEvent).where(InboundEvent.id == eid))).scalar_one()
    return status, ev.payload


PAY = "Pembayaran QRIS Rp18.000 ke KOPI KENANGAN berhasil."


async def test_a_promotion_from_a_listened_app_is_skipped_and_a_real_payment_is_not(stack, worker_env):
    worker, ctx, redis = worker_env
    status, payload = await _notify(stack, worker, ctx, "Cashback hingga Rp 50.000 untuk transaksi pertama! Buruan klaim")
    assert (status, payload["skip_reason"]) == ("skipped", "promo") and redis.jobs == []  # nothing queued, no model call
    status, payload = await _notify(stack, worker, ctx, "Tagihan kartu kredit Rp 1.200.000 jatuh tempo 25 Okt")
    assert (status, payload["skip_reason"]) == ("skipped", "reminder")
    assert (await _notify(stack, worker, ctx, PAY))[0] == "parsed"


async def test_an_app_the_owner_picks_is_listened_to_and_others_still_are_not(stack, worker_env):
    from apa.ingestion.packages import AppIn, approved_wallets, register

    worker, ctx, _ = worker_env
    jatim = "id.co.bankjatim.jconnect"
    assert (await _notify(stack, worker, ctx, PAY, package=jatim))[1]["skip_reason"] == "unknown_package"
    async with stack() as s:
        picks = await register(s, [AppIn(package=jatim, label="JConnect Mobile Bank Jatim"),
                                   AppIn(package="com.bca", label="BCA"),  # built in: not stored twice
                                   AppIn(package="not a package!", label="x"),  # invalid: dropped
                                   AppIn(package="com.tiktok.lite", label="TikTok Lite")])
    assert picks == {jatim: "bank_jatim", "com.tiktok.lite": "bank_tiktok_lite"}  # the owner chose it, so it is listened to
    status, payload = await _notify(stack, worker, ctx, PAY, package=jatim)
    assert status == "parsed" and payload["parsed"]["wallet"] == "bank_jatim"
    assert (await _notify(stack, worker, ctx, PAY, package="com.instagram.android"))[1]["skip_reason"] == "unknown_package"
    async with stack() as s:
        assert (await approved_wallets(s))["com.bca"] == "bank_bca" and jatim in await approved_wallets(s)


async def test_picking_apps_again_replaces_the_previous_picks(stack):
    from apa.ingestion.packages import AppIn, approved_extra, register

    async with stack() as s:
        await register(s, [AppIn(package="id.co.a.app", label="A Bank")])
        await register(s, [AppIn(package="id.co.b.app", label="B Bank")])
        assert list(await approved_extra(s)) == ["id.co.b.app"]
        await register(s, [])
        assert await approved_extra(s) == {}


async def test_unrecognised_notifications_reach_the_model_only_up_to_the_hourly_cap(stack, worker_env, monkeypatch):
    from apa.core.settings import get_settings

    worker, ctx, redis = worker_env
    monkeypatch.setenv("NOTIFICATION_LLM_PER_HOUR", "2")
    get_settings.cache_clear()
    weird = "Transfer ke ANDI Rp 10.000 dan menerima kredit"  # both directions: regex declines, the model would decide
    results = [await _notify(stack, worker, ctx, weird + f" {i}") for i in range(4)]
    assert [r[0] for r in results] == ["needs_llm", "needs_llm", "skipped", "skipped"]
    assert results[2][1]["skip_reason"] == "flood_guard"
    assert [j[0] for j in redis.jobs].count("extract_event") == 2  # only two ever reached the extractor
    assert (await _notify(stack, worker, ctx, PAY))[0] == "parsed"  # a recognised payment never counts against the cap
    get_settings.cache_clear()


async def test_the_picks_endpoint_needs_the_key_and_reports_what_is_listened_to(stack, monkeypatch):
    import httpx

    import apa.core.db as core_db
    from apa.gateway.main import app

    async def _session():
        async with stack() as s:
            yield s

    app.dependency_overrides[core_db.get_session] = _session
    body = {"apps": [{"package": "id.co.bankjatim.jconnect", "label": "JConnect Mobile Bank Jatim"},
                     {"package": "id.bni.wondr", "label": "Wondr"}]}
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            assert (await c.post("/ingest/packages", json=body)).status_code == 401
            r = await c.post("/ingest/packages", json=body, headers={"x-api-key": "test-key"})
            assert r.status_code == 200 and r.json()["listening_extra"] == {"id.co.bankjatim.jconnect": "bank_jatim"}  # Wondr is built in
            assert (await c.post("/ingest/packages", json={"apps": [{"package": "a.b"}] * 101},
                                 headers={"x-api-key": "test-key"})).status_code == 422
    finally:
        app.dependency_overrides.clear()


# ---- learned templates, end to end -------------------------------------------------------------------------------------------

UNUSUAL = "Transaksi kartu kredit Rp {amount} di {merchant} ref {ref} berhasil diproses"


def unusual(amount="75.000", merchant="NETFLIX.COM", ref="8841"):
    return UNUSUAL.format(amount=amount, merchant=merchant, ref=ref)


async def _model_read(stack, text, package="com.bca", **over):
    """What the extractor does after the model read a notification correctly: it learns the wording."""
    from apa.ingestion import templates

    parsed = {"amount": "75000", "direction": "out", "merchant": "NETFLIX.COM", "channel": "debit", **over}
    async with stack() as s:
        learned = await templates.learn(s, package, text, parsed)
        await s.commit()
    return learned


async def test_wording_the_model_read_twice_is_then_read_by_the_rules_with_no_model_call(stack, worker_env):
    worker, ctx, redis = worker_env
    assert (await _notify(stack, worker, ctx, unusual()))[0] == "needs_llm"  # nothing known yet: the model's turn
    await _model_read(stack, unusual())
    assert (await _notify(stack, worker, ctx, unusual("99.000", "Alfamart", "1")))[0] == "needs_llm"  # one example is not enough
    await _model_read(stack, unusual("99.000", "Alfamart", "1"), amount="99000", merchant="Alfamart")
    before = len([j for j in redis.jobs if j[0] == "extract_event"])
    status, payload = await _notify(stack, worker, ctx, unusual("120.500", "Tokopedia", "9932"))
    assert status == "parsed" and payload["parsed_by"] == "template"
    assert (payload["parsed"]["amount"], payload["parsed"]["merchant"], payload["parsed"]["direction"]) == ("120500", "Tokopedia", "out")
    assert payload["parsed"]["wallet"] == "bank_bca"
    assert len([j for j in redis.jobs if j[0] == "extract_event"]) == before  # the model was not asked this time


async def test_a_learned_template_never_reads_a_promotion_or_a_code(stack, worker_env):
    worker, ctx, _ = worker_env
    await _model_read(stack, unusual())
    await _model_read(stack, unusual("99.000", "Alfamart", "1"), amount="99000", merchant="Alfamart")
    # same skeleton words plus a promotional or credential word: the filters run before any template
    assert (await _notify(stack, worker, ctx, unusual() + " cashback hingga Rp 50.000"))[1]["skip_reason"] == "promo"
    assert (await _notify(stack, worker, ctx, "Kode OTP 123456 " + unusual()))[1]["skip_reason"] == "otp_or_credential"


async def test_templates_belong_to_the_app_that_taught_them(stack, worker_env):
    worker, ctx, _ = worker_env
    await _model_read(stack, unusual(), package="com.bca")
    await _model_read(stack, unusual("99.000", "Alfamart", "1"), package="com.bca", amount="99000", merchant="Alfamart")
    assert (await _notify(stack, worker, ctx, unusual("1.000", "X", "2"), package="id.dana"))[0] == "needs_llm"


async def test_a_model_that_contradicts_itself_retires_the_template(stack, worker_env):
    worker, ctx, _ = worker_env
    await _model_read(stack, unusual())
    await _model_read(stack, unusual("99.000", "Alfamart", "1"), amount="99000", merchant="Alfamart", direction="in")
    assert (await _notify(stack, worker, ctx, unusual("5.000", "Z", "3")))[0] == "needs_llm"


async def test_the_extractor_learns_from_a_notification_the_model_read_but_not_from_typed_text(stack, monkeypatch):
    from apa.db.models.budget import PolicyRule
    from apa.ingestion.llm_extract import Extraction
    from apa.pipeline import extract as extract_mod
    from tests.integration.helpers import FakeNotifier, raw_event, rows

    async def fake_extract(llm, *, source, text, known_wallet=None):
        return Extraction("parsed", parsed={"amount": "75000", "direction": "out", "merchant": "NETFLIX.COM", "channel": "debit",
                                            "wallet": known_wallet}, model="m", tokens=1)

    monkeypatch.setattr(extract_mod, "extract", fake_extract)
    eid = await raw_event(stack, unusual(), source="android_listener", package="com.bca", status="needs_llm")
    assert await extract_mod.run_extract(stack, None, FakeNotifier(), None, eid) == "parsed"
    (rule,) = await rows(stack, PolicyRule, PolicyRule.rule_key == "notification_templates")
    assert rule.value_json["com.bca"][0]["hits"] == 1
    typed = await raw_event(stack, unusual(), source="telegram_shorthand", status="needs_llm")
    await extract_mod.run_extract(stack, None, FakeNotifier(), None, typed)
    (rule,) = await rows(stack, PolicyRule, PolicyRule.rule_key == "notification_templates")
    assert rule.value_json["com.bca"][0]["hits"] == 1  # what the owner typed teaches nothing about bank wording


async def test_a_learning_failure_never_blocks_the_payment(stack, monkeypatch):
    from apa.ingestion.llm_extract import Extraction
    from apa.pipeline import extract as extract_mod
    from tests.integration.helpers import FakeNotifier, raw_event

    async def fake_extract(llm, *, source, text, known_wallet=None):
        return Extraction("parsed", parsed={"amount": "75000", "direction": "out", "merchant": "NETFLIX.COM", "channel": "debit",
                                            "wallet": known_wallet}, model="m", tokens=1)

    async def boom(*a, **k):
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(extract_mod, "extract", fake_extract)
    monkeypatch.setattr(extract_mod.templates, "learn", boom)
    eid = await raw_event(stack, unusual(), source="android_listener", package="com.bca", status="needs_llm")
    assert await extract_mod.run_extract(stack, None, FakeNotifier(), None, eid) == "parsed"
