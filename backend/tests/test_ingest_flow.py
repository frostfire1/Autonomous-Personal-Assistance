"""Ingest endpoint + worker with in-memory stand-ins for Postgres and Redis.

These verify the code paths (auth, sanitize, dedupe, enqueue, parse, status transitions).
They do NOT prove the SQL works: that needs the real-database test in tests/integration/.
"""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apa.core.db import get_session
from apa.core.settings import get_settings
from apa.gateway.routers import ingest
from apa.ingestion.bank_parser import PACKAGE_WALLET
from apa.workers import settings as worker


class FakeArq:
    def __init__(self):
        self.jobs: list[tuple] = []

    async def enqueue_job(self, name, *args):
        self.jobs.append((name, *args))


class FakeResult:
    def __init__(self, value):
        self._v = value

    def scalar_one_or_none(self):
        return self._v


class FakeSession:
    """Mimics INSERT .. ON CONFLICT DO NOTHING RETURNING id keyed by idempotency_key."""

    seen: dict[str, int] = {}

    async def execute(self, stmt):
        key = stmt.compile().params["idempotency_key"]
        if key in self.seen:
            return FakeResult(None)
        self.seen[key] = len(self.seen) + 1
        return FakeResult(self.seen[key])

    async def commit(self):
        pass


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("INGEST_API_KEY", "test-key")
    get_settings.cache_clear()
    FakeSession.seen = {}
    app = FastAPI()
    app.include_router(ingest.router)
    app.state.arq = FakeArq()

    async def _session():
        yield FakeSession()

    app.dependency_overrides[get_session] = _session
    yield TestClient(app)
    get_settings.cache_clear()


BODY = {
    "idempotency_key": "abcdef123456",
    "package_name": "com.bca",
    "text": "Pembayaran QRIS Rp18.000 ke KOPI KENANGAN berhasil.",
    "posted_at": "2026-10-23T12:00:00+07:00",
}
HDR = {"x-api-key": "test-key"}


def test_rejects_missing_or_wrong_key(client):
    assert client.post("/ingest/notification", json=BODY).status_code == 401
    assert client.post("/ingest/notification", json=BODY, headers={"x-api-key": "nope"}).status_code == 401


def test_accepts_and_enqueues_once(client):
    r = client.post("/ingest/notification", json=BODY, headers=HDR)
    assert r.status_code == 202 and r.json() == {"accepted": True, "duplicate": False, "event_id": 1}
    assert client.app.state.arq.jobs == [("process_inbound_event", 1)]


def test_replay_is_deduplicated_and_not_re_enqueued(client):
    client.post("/ingest/notification", json=BODY, headers=HDR)
    r = client.post("/ingest/notification", json=BODY, headers=HDR)
    assert r.status_code == 202 and r.json()["duplicate"] is True
    assert len(client.app.state.arq.jobs) == 1


def test_validation_rejects_short_idempotency_key(client):
    r = client.post("/ingest/notification", json={**BODY, "idempotency_key": "x"}, headers=HDR)
    assert r.status_code == 422


# ---- worker -----------------------------------------------------------------------------


def _event(text, package="com.bca", status="received", source="android_listener"):
    return SimpleNamespace(
        status=status, source=source, payload={"text": text, "package": package}, processed_at=None,
        received_at=datetime(2026, 10, 24, 12, 0, tzinfo=UTC),  # the batch check reads when a message was sent
    )


def _patch_session(monkeypatch, event):
    class S:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def get(self, model, pk):
            return event

        async def commit(self):
            pass

    async def wallets(session):  # the stand-in session has no database behind it: the built-in list only
        return dict(PACKAGE_WALLET)

    async def no_templates(session, package, text, wallet_map):  # nothing learned in these stand-in tests
        return None

    monkeypatch.setattr(worker.templates, "apply", no_templates)
    monkeypatch.setattr(worker, "approved_wallets", wallets)
    monkeypatch.setattr(worker, "SessionLocal", lambda: S())


async def test_worker_parses_money_movement(monkeypatch):
    ev = _event("Pembayaran QRIS Rp18.000 ke KOPI KENANGAN berhasil.")
    _patch_session(monkeypatch, ev)
    assert await worker.process_inbound_event({}, 1) == "parsed"
    assert ev.payload["parsed"]["amount"] == "18000" and ev.processed_at is not None


async def test_worker_skips_otp(monkeypatch):
    ev = _event("Kode OTP 123456 jangan berikan ke siapapun")
    _patch_session(monkeypatch, ev)
    assert await worker.process_inbound_event({}, 1) == "skipped"
    assert ev.payload["skip_reason"] == "otp_or_credential"


async def test_worker_flags_unrecognised_for_llm(monkeypatch):
    ev = _event("Rp 75.000 untuk langganan bulanan Anda")  # amount but no direction wording
    _patch_session(monkeypatch, ev)
    assert await worker.process_inbound_event({}, 1) == "needs_llm"


async def test_worker_is_idempotent_on_redelivery(monkeypatch):
    ev = _event("Pembayaran Rp 1.000 ke X berhasil", status="parsed")
    _patch_session(monkeypatch, ev)
    assert await worker.process_inbound_event({}, 1) == "parsed"
    assert "parsed" not in ev.payload  # untouched


async def test_worker_parses_telegram_shorthand(monkeypatch):
    ev = _event("kopi susu 18k tunai", source="telegram_shorthand")
    _patch_session(monkeypatch, ev)
    assert await worker.process_inbound_event({}, 1) == "parsed"
    p = ev.payload["parsed"]
    assert (p["amount"], p["wallet"], p["description"]) == ("18000", "cash_wallet", "kopi susu")


async def test_worker_routes_ambiguous_shorthand_to_llm(monkeypatch):
    ev = _event("makan siang di warteg 30rb sama es teh 5rb", source="telegram_shorthand")  # two amounts, no separator
    _patch_session(monkeypatch, ev)
    assert await worker.process_inbound_event({}, 1) == "needs_llm"


async def test_shorthand_text_is_not_parsed_as_bank_notification(monkeypatch):
    # a manual entry must never be run through the bank parser (no package -> would be dropped)
    ev = _event("pembayaran qris 18k ke warkop", source="telegram_shorthand")
    _patch_session(monkeypatch, ev)
    assert await worker.process_inbound_event({}, 1) == "parsed"


# ---- hand-off to the next job (needs ctx['redis']) and the unknown-package guard ---------------


class _Q:
    def __init__(self):
        self.jobs: list[tuple] = []

    async def enqueue_job(self, name, *args):
        self.jobs.append((name, *args))


async def test_unknown_package_is_skipped_not_sent_to_the_llm(monkeypatch):
    # a shopping app's "Rp 50.000" must never be mistaken for a bank transaction
    ev = _event("Pembayaran Rp 18.000 ke TOKO berhasil", package="com.random.shop")
    _patch_session(monkeypatch, ev)
    q = _Q()
    assert await worker.process_inbound_event({"redis": q}, 1) == "skipped"
    assert ev.payload["skip_reason"] == "unknown_package" and q.jobs == []


async def test_parsed_events_go_to_commit_and_unrecognised_ones_to_extraction(monkeypatch):
    for text, source, expect in [
        ("Pembayaran QRIS Rp18.000 ke KOPI berhasil.", "android_listener", "commit_event"),
        ("Rp 75.000 untuk langganan bulanan Anda", "android_listener", "extract_event"),
        ("makan siang di warteg 30rb sama es teh 5rb", "telegram_shorthand", "master_event"),
    ]:
        ev = _event(text, source=source)
        _patch_session(monkeypatch, ev)
        q = _Q()
        await worker.process_inbound_event({"redis": q}, 7)
        assert q.jobs == [(expect, 7)], text


async def test_skipped_events_enqueue_nothing(monkeypatch):
    ev = _event("Kode OTP 123456 jangan berikan ke siapapun")
    _patch_session(monkeypatch, ev)
    q = _Q()
    assert await worker.process_inbound_event({"redis": q}, 1) == "skipped" and q.jobs == []


# ---- the phone watch: the owner is told when the phone stops collecting ---------------------------------------------------------


def _watch(heartbeat, *, told=False):
    import asyncio
    import json
    import time

    from apa.workers.settings import phone_watch

    class Redis:
        def __init__(self):
            self.kv = {"apa:phone:alert:" + k: "1" for k in (["silent", "listener"] if told else [])}
            if heartbeat is not None:
                self.kv["apa:phone:heartbeat"] = json.dumps({**heartbeat, "ts": time.time() - heartbeat.get("age_h", 0) * 3600})

        async def get(self, key):
            return self.kv.get(key)

        async def set(self, key, value, ex=None, nx=False):
            if nx and key in self.kv:
                return None
            self.kv[key] = value
            return True

    class Notifier:
        sent: list = []

        async def send(self, text):
            self.sent.append(text)

    n = Notifier()
    n.sent = []
    result = asyncio.run(phone_watch({"redis": Redis(), "notifier": n}))
    return result, n.sent


def test_phone_watch_is_silent_when_the_phone_is_fine_or_has_never_reported():
    assert _watch(None) == ("never_seen", [])
    assert _watch({"listener": True, "pending": 0, "age_h": 0.2}) == ("ok", [])


def test_phone_watch_tells_the_owner_once_when_the_phone_goes_quiet_or_the_listener_drops():
    result, sent = _watch({"listener": True, "pending": 3, "age_h": 6})
    assert result == "silent" and "belum melapor" in sent[0] and "6 jam" in sent[0]
    result, sent = _watch({"listener": False, "pending": 0, "age_h": 0.1})
    assert result == "listener" and "terputus" in sent[0]
    assert _watch({"listener": False, "pending": 0, "age_h": 0.1}, told=True) == ("already_told", [])  # one message per 12 hours
