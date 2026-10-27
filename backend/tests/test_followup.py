"""Tests for autonomous proactive task follow-up engine and research fallback."""
import asyncio
import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from apa import followup


class FakeRedis:
    def __init__(self):
        self.data = {}

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.data:
            return False
        self.data[key] = value
        return True

    async def delete(self, key):
        self.data.pop(key, None)

    async def lrange(self, key, start, stop):
        return []


class FakeNotifier:
    def __init__(self):
        self.sent = []

    async def send(self, text):
        self.sent.append(text)
        return 999


class FakeLlm:
    def __init__(self, reply="Joe, udah jam 21:30 nih. Masih di kantor?"):
        self.reply = reply
        self.calls = []

    async def complete(self, route, system, user, **kw):
        self.calls.append((route, system, user, kw))
        return type("R", (), {
            "content": self.reply,
            "prompt_tokens": 20,
            "completion_tokens": 15
        })()


def test_schedule_if_active_situation():
    redis = FakeRedis()
    tz = ZoneInfo("Asia/Jakarta")
    now = datetime.now(tz)

    # 1. Office late detection
    scheduled = asyncio.run(followup.schedule_if_active_situation(
        redis, "belum masih di kantor", "Oke, masih di kantor.", tz, now
    ))
    assert scheduled is True
    raw = asyncio.run(redis.get(followup.ACTIVE_KEY))
    assert raw is not None
    d = json.loads(raw)
    assert d["topic"] == "office_late"
    assert "working late" in d["context_hint"]

    # 2. Early morning commitment
    scheduled2 = asyncio.run(followup.schedule_if_active_situation(
        redis, "besok jam 6 ke NCSC harus siap", "Siap.", tz, now
    ))
    assert scheduled2 is True
    d2 = json.loads(asyncio.run(redis.get(followup.ACTIVE_KEY)))
    assert d2["topic"] == "early_morning_plan"

    # 3. Non-active question should not trigger
    scheduled3 = asyncio.run(followup.schedule_if_active_situation(
        redis, "berapa saldo bank jago sekarang?", "Saldo Rp 500.000.", tz, now
    ))
    # It overwrote with nothing since False
    assert scheduled3 is False

    # 4. Global / English in-progress situation
    scheduled4 = asyncio.run(followup.schedule_if_active_situation(
        redis, "still at the office debugging code", "Take your time.", tz, now
    ))
    assert scheduled4 is True
    d4 = json.loads(asyncio.run(redis.get(followup.ACTIVE_KEY)))
    assert d4["topic"] in ("office_late", "in_progress")

    # 5. Global / English upcoming travel/schedule
    scheduled5 = asyncio.run(followup.schedule_if_active_situation(
        redis, "flight tomorrow at 6am to Tokyo", "Have a great flight.", tz, now
    ))
    assert scheduled5 is True
    d5 = json.loads(asyncio.run(redis.get(followup.ACTIVE_KEY)))
    assert d5["topic"] in ("early_morning_plan", "upcoming_schedule")


def test_dismiss_pending():
    redis = FakeRedis()
    asyncio.run(redis.set(followup.ACTIVE_KEY, "test"))
    asyncio.run(followup.dismiss_pending(redis))
    assert asyncio.run(redis.get(followup.ACTIVE_KEY)) is None


def test_check_and_run_followup_not_due():
    redis = FakeRedis()
    notifier = FakeNotifier()
    llm = FakeLlm()
    tz = ZoneInfo("Asia/Jakarta")
    now = datetime.now(tz)

    # Set due in the future
    future_ts = (now + timedelta(minutes=30)).timestamp()
    asyncio.run(redis.set(followup.ACTIVE_KEY, json.dumps({
        "topic": "office_late",
        "due_at": future_ts,
        "context_hint": "User in office"
    })))

    res = asyncio.run(followup.check_and_run_followup(
        sm=None, llm=llm, redis=redis, notifier=notifier, tz=tz, now=now
    ))
    assert res == "not_due"
    assert len(notifier.sent) == 0


def test_check_and_run_followup_due_and_sent(monkeypatch):
    redis = FakeRedis()
    notifier = FakeNotifier()
    llm = FakeLlm("Joe, udah jam 21:45 nih. Masih di kantor atau udah mau jalan pulang?")
    tz = ZoneInfo("Asia/Jakarta")
    now = datetime.now(tz)

    # Past due
    past_ts = (now - timedelta(minutes=5)).timestamp()
    asyncio.run(redis.set(followup.ACTIVE_KEY, json.dumps({
        "topic": "office_late",
        "due_at": past_ts,
        "context_hint": "User in office late"
    })))

    # Monkeypatch last owner message: 30 minutes ago (idle)
    async def fake_last_owner(sm):
        return (now - timedelta(minutes=30)).astimezone(UTC)

    monkeypatch.setattr(followup, "_last_owner_message", fake_last_owner)

    # Monkeypatch profile style guide
    from apa import profile
    async def fake_style(sm):
        return "Santai dan perhatian"
    monkeypatch.setattr(profile, "style_guide", fake_style)

    # Mock DB commit
    class FakeSession:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        def add(self, item):
            pass
        async def commit(self):
            pass

    class FakeSM:
        def __call__(self):
            return FakeSession()

    res = asyncio.run(followup.check_and_run_followup(
        sm=FakeSM(), llm=llm, redis=redis, notifier=notifier, tz=tz, now=now
    ))
    assert res == "sent"
    assert len(notifier.sent) == 1
    assert "Masih di kantor" in notifier.sent[0]
    # Active key should be cleared
    assert asyncio.run(redis.get(followup.ACTIVE_KEY)) is None
    # Cooldown key should be set
    assert asyncio.run(redis.get(followup.LAST_SENT_KEY)) is not None


def test_research_run_fallback_on_failure(monkeypatch):
    """When research.run fails, research_run falls back to search_web & maps without erroring."""
    from apa import research
    from apa.workers import settings as ws

    notifier = FakeNotifier()
    redis = FakeRedis()
    llm = FakeLlm("Hasil riset: Kamu bisa naik Mikrotrans JakLingko atau TransSemarang (Rp 3.500).")

    class FakeSession:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        def add(self, item):
            pass
        async def commit(self):
            pass

    class FakeSM:
        def __call__(self):
            return FakeSession()

    async def fail_run(topic):
        raise research.ResearchError("the research service returned no report")

    monkeypatch.setattr(research, "run", fail_run)

    async def fake_search_web(topic, max_results=5):
        return {"summary": "NCSC Semarang dapat diakses via rute TransSemarang.", "links": ["https://ncsc.id"]}

    async def fake_route(orig, dest, mode="transit"):
        return "Rute dari Bonsari ke NCSC: 8 km via Jalan Pemuda, estimasi tarif Rp 3.500."

    monkeypatch.setattr("apa.search.search_web", fake_search_web)
    monkeypatch.setattr("apa.maps.route_directions", fake_route)

    from apa import profile
    async def fake_style(sm):
        return "Santai"
    monkeypatch.setattr(profile, "style_guide", fake_style)

    ctx = {
        "sessionmaker": FakeSM(),
        "notifier": notifier,
        "redis": redis,
        "llm": llm,
    }

    status = asyncio.run(ws.research_run(ctx, "cara termurah dari Bonsari ke NCSC"))
    assert status == "sent"
    assert len(notifier.sent) == 1
    # Verify the message sent is informative, NOT "Maaf, aku belum berhasil..."
    assert "belum berhasil" not in notifier.sent[0]
    assert "Mikrotrans" in notifier.sent[0] or "TransSemarang" in notifier.sent[0]


def test_dynamic_situation_eval_and_live_context():
    tz = ZoneInfo("Asia/Jakarta")
    now = datetime.now(tz)
    redis = FakeRedis()

    # Dynamic LLM evaluation returning JSON
    eval_json = '{"active": true, "topic": "work_late", "delay_minutes": 25, "hint": "Debugging backend issues."}'
    llm = FakeLlm(reply=eval_json)

    scheduled = asyncio.run(followup.schedule_if_active_situation(
        redis, "still debugging code", "Keep going.", tz, now, llm=llm
    ))
    assert scheduled is True
    raw = asyncio.run(redis.get(followup.ACTIVE_KEY))
    assert raw is not None
    data = json.loads(raw)
    assert data["topic"] == "work_late"
    assert data["context_hint"] == "Debugging backend issues."

    # Test live context gathering fails open safely
    live_ctx = asyncio.run(followup._gather_live_context(None, tz, now))
    assert "desktop" in live_ctx
    assert "transactions" in live_ctx
    assert "sleep" in live_ctx