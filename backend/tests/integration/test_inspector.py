"""The inspector: what the owner sent, what the system answered, and why. Read-only."""

import pytest
from httpx import ASGITransport, AsyncClient

from apa.bot.menu import MAIN, handle_callback, view_for
from apa.chat import batch_flow
from apa.core.settings import get_settings
from apa.db.models.audit import AgentDebateTranscript
from apa.db.models.ops import InboundEvent, TelegramMessage
from apa.gateway.routers import inspect as inspect_router
from apa.inspector import timeline as tl
from apa.pipeline.chat import run_master
from tests.integration.conftest import HAVE_INFRA
from tests.integration.helpers import (
    TZ,
    FakeLLM,
    FakeNotifier,
    commit,
    new_event,
    parsed,
    raw_event,
    rows,
)
from tests.integration.test_batch import ArqLikeRedis, MasterLLM

pytestmark = pytest.mark.skipif(not HAVE_INFRA, reason="TEST_DATABASE_URL/TEST_REDIS_URL not set")


@pytest.fixture
async def redis(stack):
    import os

    r = ArqLikeRedis.from_url(os.environ["TEST_REDIS_URL"])
    r.jobs = []
    yield r
    await r.aclose()


async def paid(stack, text="kopi 18k", p=None, llm=None):
    eid = await new_event(stack, p or parsed(), source="telegram_shorthand")
    async with stack() as s:  # give it the text the owner would have typed
        ev = await s.get(InboundEvent, eid)
        ev.payload = {**ev.payload, "text": text}
        await s.commit()
    n = FakeNotifier()
    assert await commit(stack, eid, llm or FakeLLM(), n) == "committed"
    return eid


async def test_a_payment_shows_what_was_sent_the_answer_the_transaction_and_how_it_was_handled(stack):
    eid = await paid(stack)
    (x,) = await tl.timeline(stack, TZ, limit=5)
    assert (x.event_id, x.status, x.text) == (eid, "committed", "kopi 18k")
    assert x.route == "regex → debat"
    assert len(x.replies) == 1 and x.replies[0].kind == "summary" and "Dicatat" in x.replies[0].body
    assert x.tx.merchant == "KOPI KENANGAN" and x.tx.amount == 18000 and x.tx.category == "Food & Dining"
    assert x.latency_s is not None and x.latency_s >= 0


async def test_the_detail_shows_the_audit_row_and_every_agents_score_and_argument(stack):
    eid = await paid(stack)
    d = await tl.detail(stack, TZ, eid)
    assert any("ledger_commit" in a and "weighted_v1" in a for a in d.audit)
    assert len(d.turns) == 5 and {t.agent for t in d.turns} == {"A-02", "A-03", "A-04", "A-05", "A-07"}
    assert all(t.argument.endswith("looks fine") for t in d.turns)
    text = tl.render_detail(d, TZ)
    assert "Debat agen:" in text and "A-02 skor 0.800" in text and "Transaksi #1" in text and "Dicatat" in text


async def test_a_chat_exchange_links_the_question_to_its_answer_and_the_agent(stack, redis):
    eid = await raw_event(stack, "berapa pengeluaran saya?", status="needs_llm")
    n = FakeNotifier()
    await run_master(stack, MasterLLM(master={"intent": "ask", "agents": ["A-02"]}, answer="Rp 0."), n, redis, eid, tz=TZ)
    (x,) = await tl.timeline(stack, TZ)
    assert x.text == "berapa pengeluaran saya?" and x.route == "chat:A-02"
    assert [r.body for r in x.replies] == ["Rp 0."] and x.replies[0].kind == "reply" and x.tx is None


async def test_a_batch_draft_card_is_linked_to_the_message_that_made_it(stack, redis, monkeypatch):
    from apa.workers import settings as worker

    monkeypatch.setattr(worker, "SessionLocal", stack)
    eid = await raw_event(stack, "kopi 18k, roti 12k")
    n = FakeNotifier()
    await worker.process_inbound_event({"sessionmaker": stack, "redis": redis, "notifier": n}, eid)
    (x,) = await tl.timeline(stack, TZ)
    assert x.route == "daftar (kode)" and "Draft 2 pembayaran" in x.replies[0].body


async def test_a_fast_path_commit_says_so(stack):
    from datetime import timedelta
    from decimal import Decimal

    from sqlalchemy import update

    from apa.db.models.budget import MerchantCache
    from apa.ingestion.merchants import record_visit
    from tests.integration.helpers import T0

    async with stack() as s:
        for i, a in enumerate((18000, 17000, 19000)):
            await record_visit(s, "KOPI KENANGAN", Decimal(a), T0 - timedelta(days=5 + i), category="Food & Dining", agent="A-02")
        await s.execute(update(MerchantCache).values(last_verified_at=T0 - timedelta(days=1)))
        await s.commit()
    await paid(stack, llm=FakeLLM())
    (x,) = await tl.timeline(stack, TZ)
    assert x.route == "regex → jalur cepat"
    d = await tl.detail(stack, TZ, x.event_id)
    assert d.turns == [] and any("fast_path_v1" in a for a in d.audit)


async def test_an_event_that_went_nowhere_shows_its_reason_and_that_nothing_was_answered(stack):
    eid = await raw_event(stack, "halo", status="needs_review")
    async with stack() as s:
        ev = await s.get(InboundEvent, eid)
        ev.payload = {**ev.payload, "review_reason": "low_confidence"}
        await s.commit()
    (x,) = await tl.timeline(stack, TZ)
    assert x.reason == "low_confidence" and x.replies == []
    assert "(tidak ada balasan)" in tl.render_timeline([x], TZ) and "low_confidence" in tl.render_timeline([x], TZ)


async def test_paging_goes_backwards_by_event_id_and_is_capped(stack):
    ids = [await raw_event(stack, f"pesan {i}") for i in range(8)]
    first = await tl.timeline(stack, TZ, limit=5)
    assert [x.event_id for x in first] == ids[::-1][:5]
    older = await tl.timeline(stack, TZ, limit=5, before_id=first[-1].event_id)
    assert [x.event_id for x in older] == ids[::-1][5:]
    assert len(await tl.timeline(stack, TZ, limit=10_000)) == 8  # capped at 50, here fewer rows exist


async def test_private_session_text_cannot_appear_because_it_is_never_stored(stack):
    from apa.private_mode.session import PrivateManager
    from tests.integration.test_private import A06, FakeMessenger

    pm = PrivateManager(A06(), FakeMessenger(), stack, owner_id=1, tz=TZ)
    await pm.start(1, 1)
    await pm.message(1, 1, 5, "rahasia banget")
    await pm.end(1, "owner")
    assert await tl.timeline(stack, TZ) == [] and await rows(stack, TelegramMessage) == []


async def test_a_missing_event_has_no_detail(stack):
    assert await tl.detail(stack, TZ, 999) is None


async def test_long_text_is_trimmed_to_telegrams_limit(stack):
    for _ in range(6):
        await raw_event(stack, "x" * 3000)
    text = tl.render_timeline(await tl.timeline(stack, TZ, limit=6), TZ)
    assert len(text) <= tl.TELEGRAM_LIMIT


# ---- the Telegram view and the API --------------------------------------------------------------------------


async def test_the_log_view_lists_exchanges_with_detail_buttons_and_the_detail_opens(stack):
    eid = await paid(stack)
    v = await view_for(stack, "log", TZ)
    assert "kopi 18k" in v.text and f"l:d:{eid}" in [d for row in v.rows for _, d in row]
    d = await handle_callback(stack, None, f"l:d:{eid}", TZ)
    assert "Debat agen:" in d.text and d.rows == [[("⬅️ Riwayat", "m:log")]]
    assert "tidak ditemukan" in (await handle_callback(stack, None, "l:d:999", TZ)).text
    assert ("🗂️ Riwayat", "m:log") in [b for row in MAIN for b in row]


async def test_the_log_pages_back_in_the_telegram_view(stack):
    ids = [await raw_event(stack, f"pesan {i}") for i in range(9)]
    v = await view_for(stack, "log", TZ)
    assert f"l:p:{ids[-6]}" in [d for row in v.rows for _, d in row]  # six shown, an older page offered
    older = await handle_callback(stack, None, f"l:p:{ids[-6]}", TZ)
    assert "pesan 2" in older.text and "pesan 8" not in older.text


@pytest.fixture
async def api(stack, monkeypatch):
    monkeypatch.setenv("INGEST_API_KEY", "k")
    get_settings.cache_clear()
    monkeypatch.setattr(inspect_router, "SessionLocal", stack)
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(inspect_router.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def test_the_api_requires_the_key_and_returns_the_timeline_and_the_detail(stack, api):
    eid = await paid(stack)
    assert (await api.get("/inspect/timeline")).status_code == 401
    assert (await api.get("/inspect/timeline", headers={"x-api-key": "wrong"})).status_code == 401
    r = await api.get("/inspect/timeline", headers={"x-api-key": "k"})
    assert r.status_code == 200 and r.json()[0]["event_id"] == eid and r.json()[0]["replies"][0]["kind"] == "summary"
    d = await api.get(f"/inspect/events/{eid}", headers={"x-api-key": "k"})
    assert d.status_code == 200 and len(d.json()["turns"]) == 5
    assert (await api.get("/inspect/events/999", headers={"x-api-key": "k"})).status_code == 404
    assert (await api.get("/inspect/timeline?limit=0", headers={"x-api-key": "k"})).status_code == 422


async def test_the_api_has_no_way_to_change_anything(api):
    for method in ("post", "put", "delete", "patch"):
        r = await getattr(api, method)("/inspect/timeline", headers={"x-api-key": "k"})
        assert r.status_code == 405


async def test_debate_transcripts_are_untouched_by_reading(stack):
    await paid(stack)
    before = len(await rows(stack, AgentDebateTranscript))
    await tl.timeline(stack, TZ)
    await tl.detail(stack, TZ, 1)
    assert len(await rows(stack, AgentDebateTranscript)) == before == 5
    assert batch_flow is not None  # imported to keep the module graph honest for the worker path


# ---- the web dashboard's data ---------------------------------------------------------------------------------------------


@pytest.fixture
async def dash(stack, monkeypatch):
    from fastapi import FastAPI

    from apa.gateway.routers import dashboard as dashboard_router

    monkeypatch.setenv("INGEST_API_KEY", "k")
    get_settings.cache_clear()
    monkeypatch.setattr(dashboard_router, "SessionLocal", stack)
    app = FastAPI()
    app.include_router(dashboard_router.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def _seed_dashboard(stack):
    from datetime import UTC, datetime, timedelta
    from decimal import Decimal

    from apa.db.models.budget import BudgetRule
    from apa.db.models.health import SleepSession
    from apa.db.models.intel import CyberAdvisory
    from apa.db.models.ledger import Transaction

    now = datetime.now(UTC)
    async with stack() as s:
        for merchant, amount, status, direction in (("Warteg", 45000, "committed", "out"), ("Gaji", 5_000_000, "committed", "in"),
                                                    ("Salah", 99000, "rolled_back", "out"), ("Aneh", 3_000_000, "quarantined", "out")):
            s.add(Transaction(timestamp=now - timedelta(minutes=1), source_channel="manual_text", payment_wallet="cash_wallet",
                              merchant=merchant, direction=direction, total_paid_amount=Decimal(amount), net_user_expense=Decimal(amount),
                              primary_category="Food & Dining", execution_mode="consensus_auto", status=status))
        s.add(BudgetRule(category="Food & Dining", period_month=now.astimezone(TZ).date().replace(day=1), base_limit=Decimal(1_000_000),
                         current_limit=Decimal(1_000_000), spent_amount=Decimal(250_000)))
        s.add(SleepSession(sleep_date=now.date(), sleep_score=71, total_sleep_mins=380, deep_sleep_pct=Decimal("14")))
        s.add(CyberAdvisory(cve_id="CVE-2026-50001", summary="RCE", cvss=Decimal("9.8"), in_cisa_kev=True, tier="urgent", source="kev",
                            affected_products=["plex media server (< 1.43)"]))
        await s.commit()


async def test_the_dashboard_needs_the_key_and_every_endpoint_is_read_only(dash):
    for path in ("overview", "transactions", "decisions", "health", "security"):
        assert (await dash.get(f"/dashboard/{path}")).status_code == 401
        assert (await dash.get(f"/dashboard/{path}", headers={"x-api-key": "wrong"})).status_code == 401
        assert (await dash.post(f"/dashboard/{path}", headers={"x-api-key": "k"})).status_code == 405  # no way to write


async def test_the_overview_has_totals_budgets_recent_payments_waiting_work_sleep_and_cves(stack, dash):
    await _seed_dashboard(stack)
    o = (await dash.get("/dashboard/overview", headers={"x-api-key": "k"})).json()
    assert o["spent_today"] == 45_000 and o["spent_month"] == 45_000  # committed payments only: not the undone, not the held one
    assert o["income_month"] == 5_000_000 and o["quarantined"] == 1
    assert o["budgets"] == [{"category": "Food & Dining", "limit": 1_000_000, "base": 1_000_000, "spent": 250_000, "percent": 25.0, "essential": False}]
    assert len(o["recent"]) == 4 and o["sleep"] == {"date": o["sleep"]["date"], "minutes": 380, "score": 71}
    assert o["cve"] == {"total": 1, "critical": 1, "high": 0, "kev": 1}
    # today's budget is what is left of the month spread over the days that remain; weather is off in tests
    import calendar
    from datetime import datetime

    days_left = calendar.monthrange(datetime.now(TZ).year, datetime.now(TZ).month)[1] - datetime.now(TZ).day + 1
    assert o["daily"]["remaining"] == 750_000 and o["daily"]["days_left"] == days_left and o["daily"]["spent_today"] == 45_000
    assert o["daily"]["per_day"] == round(750_000 / days_left) and o["daily"]["categories"][0]["category"] == "Food & Dining"
    assert o["weather"] is None


async def test_transactions_page_backwards_filter_by_status_and_reject_bad_input(stack, dash):
    await _seed_dashboard(stack)
    h = {"x-api-key": "k"}
    allrows = (await dash.get("/dashboard/transactions", headers=h)).json()
    assert [t["merchant"] for t in allrows] == ["Aneh", "Salah", "Gaji", "Warteg"]  # newest first
    older = (await dash.get(f"/dashboard/transactions?limit=2&before_id={allrows[1]['id']}", headers=h)).json()
    assert [t["merchant"] for t in older] == ["Gaji", "Warteg"]
    assert [t["merchant"] for t in (await dash.get("/dashboard/transactions?status=rolled_back", headers=h)).json()] == ["Salah"]
    assert (await dash.get("/dashboard/transactions?status=nonsense", headers=h)).status_code == 422
    assert (await dash.get("/dashboard/transactions?limit=0", headers=h)).status_code == 422


async def test_health_decisions_and_security_endpoints_return_their_rows(stack, dash):
    await _seed_dashboard(stack)
    h = {"x-api-key": "k"}
    assert (await dash.get("/dashboard/health", headers=h)).json()[0]["score"] == 71
    sec = (await dash.get("/dashboard/security", headers=h)).json()
    assert sec["kev"] == 1 and sec["severe"][0]["id"] == "CVE-2026-50001" and sec["severe"][0]["cvss"] == 9.8
    assert sec["severe"][0]["products"] == ["plex media server (< 1.43)"]
    assert (await dash.get("/dashboard/decisions", headers=h)).json() == []  # nothing decided in this fixture


async def test_a_private_session_leaves_nothing_the_dashboard_can_show(stack, dash):
    from apa.private_mode.session import PrivateManager
    from tests.integration.test_private import A06, FakeMessenger

    pm = PrivateManager(A06(), FakeMessenger(), stack, owner_id=1, tz=TZ)
    await pm.start(1, 1)
    await pm.message(1, 1, 5, "rahasia-banget-12345")
    await pm.end(1, "owner")
    h = {"x-api-key": "k"}
    for path in ("overview", "transactions", "decisions", "health", "security"):
        assert "rahasia-banget-12345" not in (await dash.get(f"/dashboard/{path}", headers=h)).text
