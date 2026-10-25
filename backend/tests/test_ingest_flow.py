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


# ---- describing a payment without an amount ------------------------------------------------------------------------------------


def test_a_payment_described_without_an_amount_proposes_an_edit_of_the_recent_transaction(monkeypatch):
    import asyncio
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    from apa.chat import master
    from apa.pipeline import chat as pc

    seen = {}

    async def fake_tx(sm, reply_to, sent_at):
        seen["reply_to"] = reply_to
        return seen.get("tx", 77)

    async def fake_plan(sm, llm, text, history, tz, redis):
        seen["hint"] = text
        return "edit card", 0

    monkeypatch.setattr(pc, "_described_transaction", fake_tx)
    monkeypatch.setattr(pc, "plan_action", fake_plan)

    async def fake_converse(sm, llm, redis, text, history_block, tz, now, *, reply_to=None):
        seen["converse"] = text
        return "Maksudnya apa?", 0

    monkeypatch.setattr(pc, "converse", fake_converse)

    def run(text, reply_to=None):
        return asyncio.run(pc._execute(None, None, None, master.Plan("transaction"), text, [], None, ZoneInfo("Asia/Jakarta"),
                                       datetime.now(UTC), None, reply_to))

    assert run("beli makan nasgor sosis solaria", reply_to=15) == ("edit card", "action")
    assert "#77" in seen["hint"] and "edit_transaction" in seen["hint"] and seen["reply_to"] == 15
    assert run("kopi susu 18k tunai") == (None, "transaction")  # with an amount it is a new payment, as before
    seen["tx"] = None
    assert run("beli makan nasgor sosis solaria") == ("Maksudnya apa?", "general")  # nothing to describe: the assistant asks


def test_the_router_is_shown_the_recent_transactions_and_told_a_short_reference_to_them_is_an_edit():
    import asyncio

    from apa.chat import master

    seen = {}

    class Llm:
        async def complete(self, route, system, user, **kw):
            seen["system"], seen["user"], seen["agent"] = system, user, kw.get("agent")
            return type("R", (), {"content": '{"intent": "action", "agents": [], "reply": null, "topic": ""}',
                                  "prompt_tokens": 1, "completion_tokens": 1})()

    recent = "\n".join(["#7 - Rp 20.071 out 17:33", "#6 - Rp 20.071 out 17:31"])
    got = asyncio.run(master.plan(Llm(), "2 2 nya dari kunci AI", [], has_draft=False, recent=recent))
    assert got.intent == "action" and seen["agent"] == "A-00"
    assert "RECENT_TRANSACTIONS:" in seen["user"] and "#7 - Rp 20.071" in seen["user"]
    assert "labelling them" in seen["system"]
    asyncio.run(master.plan(Llm(), "hello", [], has_draft=False))
    assert "(none in the last 2 hours)" in seen["user"]


# ---- the assistant: context, look-ups, no dead ends ---------------------------------------------------------------------------


def _assistant(monkeypatch, steps, ran=None, proposed=None):
    """converse() with a scripted model (one reply per call) and fake look-ups; returns what the model was shown."""
    import asyncio
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    from apa.chat import actions, assistant

    shown = []

    class Llm:
        async def complete(self, route, system, user, **kw):
            shown.append((system, user, kw))
            return type("R", (), {"content": steps[len(shown) - 1], "prompt_tokens": 10, "completion_tokens": 5})()

    async def fake_context(sm, tz, now, history_block, reply_to):
        return f"CTX reply_to={reply_to}\n{history_block}"

    async def fake_read(sm, tz, now, name, args):
        (ran if ran is not None else []).append((name, args))
        return "RESULT-FOR-" + name

    async def fake_propose(sm, redis, raw):
        (proposed if proposed is not None else []).append(raw)
        return "CARD"

    monkeypatch.setattr(assistant, "_context", fake_context)
    monkeypatch.setattr(assistant, "run_read", fake_read)
    monkeypatch.setattr(actions, "propose", fake_propose)
    out = asyncio.run(assistant.converse(None, Llm(), None, "berapa makan minggu ini?", "user: hai", ZoneInfo("Asia/Jakarta"),
                                         datetime.now(UTC), reply_to=42))
    return out, shown


def test_the_assistant_looks_things_up_then_answers_from_the_result(monkeypatch):
    ran = []
    (reply, tokens), shown = _assistant(monkeypatch, [
        '{"tool": "spending", "args": {"period": "week", "by": "category"}}',
        '{"say": "Minggu ini makan Rp 120.000."}',
    ], ran)
    assert reply == "Minggu ini makan Rp 120.000." and tokens == 30
    assert ran == [("spending", {"period": "week", "by": "category"})]
    assert "RESULT-FOR-spending" in shown[1][1] and "CTX reply_to=42" in shown[0][1]  # the 2nd call sees the result; the 1st the context
    assert shown[0][2]["agent"] == "A-00"


def test_a_change_is_only_ever_a_proposal_the_owner_confirms(monkeypatch):
    proposed = []
    (reply, _), _ = _assistant(monkeypatch, ['{"tool": "edit_transaction", "args": {"tx_id": 7, "extra_tx_ids": [6], "merchant": "Kunci AI"}}'],
                               proposed=proposed)
    assert reply == "CARD" and '"edit_transaction"' in proposed[0] and "Kunci AI" in proposed[0]


def test_the_assistant_asks_instead_of_failing_and_never_loops_forever(monkeypatch):
    (reply, _), _ = _assistant(monkeypatch, ['{"say": "Yang mana maksudmu, yang Rp 20.071 atau Rp 50.500?"}'])
    assert reply.startswith("Yang mana")
    (reply, _), _ = _assistant(monkeypatch, ["{not json at all"])  # garbage: a polite request to rephrase, not a crash
    assert "belum paham" in reply
    looping = ['{"tool": "budgets", "args": {}}'] * 10
    (reply, _), shown = _assistant(monkeypatch, looping + ['{"say": "ok"}'] * 3)
    assert len(shown) <= 5 and ("belum bisa" in reply or reply == "ok")  # bounded number of look-ups


def test_the_assistant_reads_prose_as_a_reply_and_unknown_tools_are_refused(monkeypatch):
    from apa.chat.assistant import parse_step

    assert parse_step("Halo! Ada yang bisa dibantu?") == ("say", "Halo! Ada yang bisa dibantu?")
    assert parse_step('{"say": "ok"}') == ("say", "ok") and parse_step('```json\n{"say": "ok"}\n```') == ("say", "ok")
    assert parse_step('{"tool": "spending", "args": {"period": "week"}}') == ("tool", ("spending", {"period": "week"}))
    assert parse_step("{}")[0] == "bad" and parse_step("[1]")[0] == "bad"
    (reply, _), shown = _assistant(monkeypatch, ['{"tool": "drop_database", "args": {}}', '{"say": "maaf"}'])
    assert reply == "maaf" and "unknown tool" in shown[1][1]


def test_the_read_tools_validate_their_arguments_and_say_what_is_wrong():
    import asyncio
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    from apa.chat.tools import READ_TOOLS, run_read

    now, tz = datetime.now(UTC), ZoneInfo("Asia/Jakarta")
    assert {"transactions", "spending", "budgets", "balances", "sleep", "cve", "news", "recall", "profile", "activity"} == set(READ_TOOLS)
    for name, args in (("transactions", {"days": 9999}), ("spending", {"period": "decade"}), ("sleep", {"days": 0}),
                       ("news", {"limit": 99}), ("transactions", {"drop": "table"})):
        assert asyncio.run(run_read(None, tz, now, name, args)).startswith("Bad arguments")
    assert asyncio.run(run_read(None, tz, now, "nope", {})) == "Unknown tool 'nope'."


def test_the_assistant_remembers_and_forgets_when_asked_and_then_answers(monkeypatch):
    from apa import assistant_memory

    saved = []

    async def fake_remember(sm, fact):
        saved.append(("remember", fact))
        return f"Remembered: {fact}"

    async def fake_forget(sm, index):
        saved.append(("forget", index))
        return "Forgot: x"

    monkeypatch.setattr(assistant_memory, "remember", fake_remember)
    monkeypatch.setattr(assistant_memory, "forget", fake_forget)
    (reply, _), shown = _assistant(monkeypatch, [
        '{"tool": "remember", "args": {"fact": "vegetarian"}}',
        '{"tool": "forget", "args": {"index": 2}}',
        '{"tool": "forget", "args": {"index": "two"}}',  # not a number: refused, not executed
        '{"say": "Oke, saya ingat."}',
    ])
    assert reply == "Oke, saya ingat." and saved == [("remember", "vegetarian"), ("forget", 2)]
    assert "Remembered: vegetarian" in shown[1][1] and "No such item." in shown[3][1]


def test_what_is_remembered_is_scrubbed_of_long_numbers_and_capped():
    from apa.assistant_memory import MAX_CHARS, clean

    assert clean("my card 4111 1111 1111 1111 and rent is on the 5th") == "my card [number] and rent is on the 5th"
    assert clean("call 0812-3456-7890 please") == "call [number] please"
    assert len(clean("x " * 500)) <= MAX_CHARS


def test_the_nightly_digest_reply_is_read_into_a_summary_and_lasting_items():
    from apa.assistant_memory import MAX_PER_DAY, parse_digest

    summary, items = parse_digest(
        '{"summary": "Owner cerita kerjaan cyber security dan rencana beli vitamin.", "learned": ['
        '{"kind": "routine", "text": "Bekerja di bidang cyber security"}, {"kind": "nonsense", "text": "Suka kopi tanpa gula"},'
        '{"kind": "person", "text": "ab"}, {"text": 5}, "junk"]}'
    )
    assert summary.startswith("Owner cerita") and [i["kind"] for i in items] == ["routine", "other"]  # unknown kind -> other; too short or malformed dropped
    assert parse_digest("```json\n{\"summary\": \"Hari biasa saja kok.\", \"learned\": []}\n```") == ("Hari biasa saja kok.", [])
    assert parse_digest("Prosa biasa tentang hari ini.") == ("Prosa biasa tentang hari ini.", [])  # plain prose: a summary, nothing learned
    assert parse_digest("[1, 2]") == ("", []) and parse_digest("") == ("", [])
    many = '{"summary": "x y z a b c d e f", "learned": [' + ",".join(f'{{"kind": "goal", "text": "tujuan nomor {i}"}}' for i in range(20)) + "]}"
    assert len(parse_digest(many)[1]) == MAX_PER_DAY
    assert parse_digest('{"summary": "kartu 4111 1111 1111 1111 ok ya", "learned": []}')[0] == "kartu [number] ok ya"  # numbers scrubbed


# ---- the profile: patterns computed from the owner's data -----------------------------------------------------------------------


def _day(n, h=12, m=0):
    from datetime import UTC, datetime, timedelta

    return datetime(2026, 10, 1, h, m, tzinfo=UTC) + timedelta(days=n)


def test_sleep_is_summarised_from_the_nights_and_not_claimed_from_too_few():
    from datetime import date

    from apa.profile import sleep_trait

    assert sleep_trait([(date(2026, 10, d), 400, 80) for d in range(1, 4)]) is None  # three nights: no claim
    nights = [(date(2026, 10, d), 330 if date(2026, 10, d).weekday() < 5 else 450, 70 + d) for d in range(1, 15)]
    t = sleep_trait(nights)
    assert t.key == "sleep.duration" and t.evidence == 14
    assert "weekdays 5h30" in t.summary and "weekends 7h30" in t.summary and "under 6 hours" in t.summary
    assert t.data["min"] == 330 and t.data["max"] == 450 and 330 < t.data["avg_min"] < 450


def test_activity_hours_are_inferred_from_the_first_and_last_thing_done_each_day():
    from zoneinfo import ZoneInfo

    from apa.profile import activity_trait

    tz = ZoneInfo("Asia/Jakarta")
    # UTC 00:30 is 07:30 in Jakarta; UTC 16:20 is 23:20; a day with a single event is ignored
    stamps = [s for d in range(8) for s in (_day(d, 0, 30), _day(d, 5, 0), _day(d, 16, 20))] + [_day(20, 3, 0)]
    t = activity_trait(stamps, tz)
    assert t.evidence == 8 and "07:30" in t.summary and "23:20" in t.summary and "inferred" in t.summary
    assert activity_trait([_day(d, 1, 0) for d in range(3)], tz) is None


def test_food_habits_name_the_usual_meal_times_places_and_foods():
    from zoneinfo import ZoneInfo

    from apa.profile import food_trait

    tz = ZoneInfo("Asia/Jakarta")
    meals = [(_day(d, 5, 0), "Warteg Bahari", 25000) for d in range(6)] + [(_day(d, 13, 0), "Kopi Kenangan", 18000) for d in range(3)]
    t = food_trait(meals, ["nasi goreng"] * 4 + ["es kopi susu"] * 2, tz, [180.0, 220.0], [30.0])
    assert t.evidence == 9 and "lunch (6)" in t.summary and "dinner (3)" in t.summary  # UTC 05:00 is 12:00 in Jakarta, UTC 13:00 is 20:00
    assert "Warteg Bahari (6x)" in t.summary and "nasi goreng (4x)" in t.summary and "caffeine about 200 mg" in t.summary
    assert food_trait(meals[:3], [], tz, [], []) is None


def test_spending_movement_stress_and_interests_are_computed_not_guessed():
    from zoneinfo import ZoneInfo

    from apa.profile import interest_trait, spending_trait, steps_trait, stress_trait

    tz = ZoneInfo("Asia/Jakarta")
    spends = [(_day(d, 3, 0), "Food & Dining", 20000) for d in range(6)] + [(_day(7, 3, 0), "Transport", 60000)]
    t = spending_trait(spends, tz, 30)
    assert "Food & Dining" in t.summary and "largest payment" in t.summary and t.data["total"] == 180000
    assert steps_trait([4000, 8000, 6000, 0, 5000, 7000], [60, 62]).data["avg"] == 6000  # a zero day is a missing day
    assert steps_trait([4000, 8000], []) is None
    assert stress_trait([(_day(0, 3, 0), 40)] * 3, tz) is None  # too few readings
    i = interest_trait(["lagi chaos kerjaan cyber security", "kerjaan cyber banyak", "cyber lagi ramai kerjaan"], ["bitcoin"],
                       ["Suka kopi tanpa gula"], [("Entertainment", 10.0), ("Food & Dining", 5.0)])
    assert "cyber (3x)" in i.summary and "kerjaan (3x)" in i.summary and "follows bitcoin" in i.summary and "Entertainment" in i.summary
    assert "lagi" not in i.summary.split("learned")[0]  # stop words are not interests
    assert interest_trait([], [], [], []) is None


# ---- Telegram /diary: private, saved at once, the message removed ----------------------------------------------------------------


def _diary_run(monkeypatch, text, *, fail_with=None):
    import asyncio
    from datetime import UTC, datetime

    from apa import assistant_memory
    from apa.bot import main as bot

    saved, said, deleted = [], [], []

    async def fake_add(sm, day, body, *, shared=False):
        if fail_with:
            raise fail_with
        saved.append((body, shared))
        return 1

    class Msg:
        def __init__(self):
            self.text = text
            self.date = datetime.now(UTC)

        async def answer(self, reply, **kw):
            said.append(reply)

        async def delete(self):
            deleted.append(True)

    monkeypatch.setattr(assistant_memory, "journal_add", fake_add)
    asyncio.run(bot._diary(Msg()))
    return saved, said, deleted


def test_the_diary_command_saves_privately_removes_the_message_and_never_repeats_it(monkeypatch):
    saved, said, deleted = _diary_run(monkeypatch, "/diary hari ini aku sedih karena kerjaan")
    assert saved == [("hari ini aku sedih karena kerjaan", False)]  # private: not shared with the AI
    assert deleted == [True] and len(said) == 1 and "sedih" not in said[0] and "private diary" in said[0]


def test_an_empty_diary_command_explains_itself_and_a_missing_key_saves_nothing(monkeypatch):
    saved, said, deleted = _diary_run(monkeypatch, "/diary")
    assert saved == [] and deleted == [] and "followed by your entry" in said[0]
    from apa.core.secrets import SecretsUnavailable

    saved, said, deleted = _diary_run(monkeypatch, "/diary rahasia", fail_with=SecretsUnavailable("no key"))
    assert saved == [] and deleted == [] and "SECRETS_KEY" in said[0] and "rahasia" not in said[0]  # not saved, not deleted: nothing lost


# ---- app usage (ActivityWatch), moods and likes in the profile -------------------------------------------------------------------


def test_app_usage_is_categorised_and_becomes_a_screen_time_trait():
    from datetime import UTC, date, datetime, timedelta
    from zoneinfo import ZoneInfo

    from apa import profile, usage

    assert usage.category("com.whatsapp") == "messaging" and usage.category("YouTube") == "video" and usage.category("Zorg") == "other"
    tz = ZoneInfo("Asia/Jakarta")
    rows = []
    for i in range(6):
        day = date(2026, 1, 1) + timedelta(days=i)
        late = datetime(2026, 1, 1 + i, 17, 30, tzinfo=UTC)  # 00:30 next day in Jakarta: up late
        rows += [(day, "pc", "desktop", "Code", 7200, None), (day, "phone", "android", "YouTube", 3600, late if i < 3 else None)]
    t = profile.usage_trait(rows, tz)
    assert t and t.key == "screen_time" and t.evidence == 6 and "3.0 h a day" in t.summary and "Code" in t.summary
    assert "after 23:00 on 3 of 6 days" in t.summary and t.data["late_days"] == 3
    assert profile.usage_trait(rows[:4], tz) is None  # too few days: no claim


def test_mood_and_likes_traits_need_enough_data_and_compare_with_sleep():
    from datetime import date, timedelta

    from apa import profile

    days = [date(2026, 1, 1) + timedelta(days=i) for i in range(6)]
    moods = [(d, 2 if i < 3 else 4) for i, d in enumerate(days)]
    sleeps = [(d, 300 if i < 3 else 450) for i, d in enumerate(days)]
    t = profile.mood_trait(moods, sleeps)
    assert t and "3.0 of 5" in t.summary and "under 6.5 h of sleep 2.0, after more 4.0" in t.summary
    assert profile.mood_trait(moods[:3], sleeps) is None
    likes = profile.likes_trait([("like", "kopi"), ("like", "Kopi"), ("dislike", "macet"), ("meal", "nasi")])
    assert likes and "Likes: kopi" in likes.summary and "Dislikes: macet" in likes.summary
    assert profile.likes_trait([("like", "kopi")]) is None


def test_digest_observations_are_validated():
    import json

    from apa import assistant_memory as mem

    raw = json.dumps({"observations": [
        {"type": "meal", "text": "nasi goreng", "at": "19:30"}, {"type": "mood", "text": "capek", "mood": 2},
        {"type": "like", "text": "kopi", "mood": 5}, {"type": "Skin Care!", "text": "serum"}, {"type": "meal", "text": "sate", "at": "25:99"}]})
    got = mem.parse_observations(raw)
    assert [(o["type"], o["at"], o["mood"]) for o in got] == [("meal", "19:30", None), ("mood", "", 2), ("like", "", None), ("skincare", "", None), ("meal", "", None)]
    assert mem.parse_observations("not json") == [] and mem.parse_observations('{"summary": "x"}') == []


# ---- YouTube Takeout import and the daily usage trend --------------------------------------------------------------------------


def test_the_youtube_export_is_parsed_into_watched_and_searched_items_and_a_trait():
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    from apa import profile, youtube

    tz = ZoneInfo("Asia/Jakarta")
    now = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)
    items = [{"title": f"Watched Belajar python bagian {i}", "titleUrl": "https://youtu.be/x", "subtitles": [{"name": "Kelas Kode"}],
              "time": "2026-03-09T10:00:00.000Z"} for i in range(8)]
    items += [{"title": "Watched Resep nasi goreng python", "subtitles": [{"name": "Dapur Ani"}], "time": "2026-03-08T10:00:00Z"},
              {"title": "Watched python tips", "subtitles": [{"name": "Kelas Kode"}], "time": "2026-03-08T11:00:00Z"},
              {"title": "Searched for kopi arabika", "time": "2026-03-08T09:00:00Z"},
              {"title": "Watched https://www.youtube.com/watch?v=gone", "time": "2026-03-08T09:00:00Z"},  # removed video
              {"title": "Watched too old", "subtitles": [{"name": "X"}], "time": "2025-01-01T00:00:00Z"},
              {"title": "Visited a page", "time": "2026-03-08T09:00:00Z"}, {"nonsense": True}, "text"]
    by_day = youtube.parse(items, tz, now)
    flat = [i for v in by_day.values() for i in v]
    assert len(flat) == 11 and sum(i["k"] == "searched" for i in flat) == 1
    t = profile.watch_trait([(i["k"], i["c"], i["t"]) for i in flat])
    assert t and t.key == "youtube" and "Kelas Kode (9)" in t.summary and "python" in t.summary and "kopi arabika" in t.summary
    assert profile.watch_trait([("watched", "A", "satu dua")]) is None


def test_the_usage_trait_reports_this_weeks_trend():
    from datetime import date, timedelta
    from zoneinfo import ZoneInfo

    from apa import profile

    rows = [(date(2026, 1, 1) + timedelta(days=i), "p", "android", "Chrome", 3600 if i < 7 else 7200, None) for i in range(14)]
    t = profile.usage_trait(rows, ZoneInfo("Asia/Jakarta"))
    assert t and "The last 7 days average 2.0 h, up 100% on the week before" in t.summary and len(t.data["daily"]) == 14


def test_youtube_account_items_are_shaped_and_reach_the_trait():
    from apa import profile, youtube_api

    liked = [{"snippet": {"title": f"Video {i} tentang python", "channelTitle": "Kelas Kode"}} for i in range(5)] + [{"snippet": {}}]
    subs = [{"snippet": {"title": n}} for n in ("Kelas Kode", "Dapur Ani", "Gadget Lab", "Musik Santai", "Berita Pagi")]
    items = youtube_api.shape(liked, subs)
    assert len(items) == 10 and items[0] == {"k": "liked", "c": "Kelas Kode", "t": "Video 0 tentang python"} and items[-1]["k"] == "subscribed"
    t = profile.watch_trait([(i["k"], i["c"], i["t"]) for i in items])
    assert t and "Follows 5 channels" in t.summary and "Liked videos come mostly from Kelas Kode (5)" in t.summary and t.data["subscriptions"] == 5


def test_connecting_youtube_needs_credentials_and_reports_pending_until_the_code_is_entered(monkeypatch):
    import asyncio

    import httpx

    from apa import youtube_api
    from apa.core.settings import get_settings

    def handler(req):
        if "device/code" in str(req.url):
            return httpx.Response(200, json={"user_code": "ABCD-EFGH", "verification_url": "https://www.google.com/device", "device_code": "dc", "interval": 5, "expires_in": 1800})
        return httpx.Response(428, json={"error": "authorization_pending"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            try:
                await youtube_api.start(http)
                raise AssertionError("must refuse without credentials")
            except youtube_api.YouTubeError as e:
                assert "YOUTUBE_CLIENT_ID" in str(e)
            monkeypatch.setenv("YOUTUBE_CLIENT_ID", "cid")
            monkeypatch.setenv("YOUTUBE_CLIENT_SECRET", "sec")
            get_settings.cache_clear()
            d = await youtube_api.start(http)
            assert d["user_code"] == "ABCD-EFGH" and d["device_code"] == "dc"
            assert await youtube_api.poll(None, http, "dc") == "pending"

    monkeypatch.delenv("YOUTUBE_CLIENT_ID", raising=False)
    monkeypatch.delenv("YOUTUBE_CLIENT_SECRET", raising=False)
    get_settings.cache_clear()
    asyncio.run(run())
    get_settings.cache_clear()


def test_the_activity_feed_dates_likes_and_replaces_the_undated_copy():
    from apa import youtube_api

    liked = [{"snippet": {"title": "Belajar python", "channelTitle": "Kelas Kode"}}, {"snippet": {"title": "Lagu santai", "channelTitle": "Musik"}}]
    acts = [{"snippet": {"type": "like", "title": "Belajar python", "publishedAt": "2026-03-02T10:00:00Z", "channelTitle": "ME"}},
            {"snippet": {"type": "playlistItem", "title": "Resep", "publishedAt": "2026-03-03T10:00:00Z"}},
            {"snippet": {"type": "subscription", "title": "ignored", "publishedAt": "2026-03-03T10:00:00Z"}}, {"snippet": {"type": "like"}}]
    items = youtube_api.shape(liked, [], acts)
    assert items == [{"k": "liked", "c": "Kelas Kode", "t": "Belajar python", "d": "2026-03-02"},
                     {"k": "saved", "c": "", "t": "Resep", "d": "2026-03-03"}, {"k": "liked", "c": "Musik", "t": "Lagu santai"}]


def test_music_from_youtube_music_is_listened_and_the_ai_reply_is_validated():
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    from apa import content, profile, youtube

    now = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)
    items = [{"header": "YouTube Music", "title": f"Watched Lagu {i}", "subtitles": [{"name": "Tulus - Topic"}], "time": "2026-03-09T10:00:00Z"} for i in range(5)]
    items += [{"header": "YouTube", "title": "Watched Resep", "subtitles": [{"name": "Dapur Ani"}], "time": "2026-03-09T10:00:00Z"}]
    flat = [i for v in youtube.parse(items, ZoneInfo("Asia/Jakarta"), now).values() for i in v]
    assert [i["k"] for i in flat].count("listened") == 5 and flat[0]["c"] == "Tulus"
    t = profile.watch_trait([(i["k"], i["c"], i["t"]) for i in flat])
    assert t and "Listens to (YouTube Music): Tulus (5)" in t.summary

    lines = content.build_input([("listened", "Tulus", "Lagu 1")] * 3 + [("watched", "Dapur Ani", "Resep")])
    assert lines.splitlines()[0] == "[listened x3] Tulus | Lagu 1"
    good = '{"summary": "Watches cooking and listens to Indonesian pop.", "topics": [{"name": "cooking", "share": 140}, {"x": 1}], "music": ["Tulus", 5], "likes": ["recipes"]}'
    out = content.parse_reply(good)
    assert out and out["topics"] == [{"name": "cooking", "share": 100}] and out["music"] == ["Tulus"] and out["likes"] == ["recipes"]
    assert content.parse_reply("nonsense") is None and content.parse_reply('{"summary": "x"}') is None


# ---- writing style, hobbies, care check-ins -------------------------------------------------------------------------------------


def test_the_owners_writing_style_is_measured_and_turned_into_guidance():
    from apa import profile

    msgs = ["gak jadi beli yang itu deh, mahal banget", "udah makan belum nih kamu hari ini 😄", "besok aku ke kantor aja ya dong",
            "tadi pagi kopi doang sih jadi lemes"] * 5
    t = profile.style_trait(msgs)
    assert t and t.key == "style" and t.data["lang"] == "Indonesian" and t.data["casual"] and 0.2 <= t.data["emoji"] <= 0.3
    assert "casual" in t.summary and "Indonesian" in t.summary
    assert profile.style_trait(msgs[:6]) is None and profile.style_trait(["ok", "ya"] * 20) is None  # too little, or too short to judge


def test_hobbies_come_from_app_time_and_watched_topics():
    from apa import profile

    t = profile.hobby_trait([("games", 0.4), ("video", 0.25), ("work", 0.2), ("social", 0.03)], ["Mobile Legends", "Magic Chess", "Telegram"], ["gaming", "tech"])
    assert t and "games 40%" in t.summary and "video 25%" in t.summary and "social" not in t.summary and "watches gaming, tech" in t.summary
    assert profile.hobby_trait([("work", 0.9)], [], []) is None


def test_care_speaks_up_only_for_a_reason_and_never_at_night():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from apa import care

    tz = ZoneInfo("Asia/Jakarta")
    base = dict(sleep_mins=None, meals_today=1, food_habit=True, up_late_at=None, caffeine_mg=0.0, stress_avg=None)

    def at(h, m=0, **kw):
        return [s.key for s in care.decide(now=datetime(2026, 3, 10, h, m, tzinfo=tz), **{**base, **kw})]

    assert at(10) == [] and at(10, sleep_mins=290) == ["sleep"] and at(15, sleep_mins=290) == []  # short sleep is for the morning
    assert at(15, meals_today=0) == ["food"] and at(10, meals_today=0) == [] and at(15, meals_today=0, food_habit=False) == []
    assert at(10, up_late_at=datetime(2026, 3, 10, 1, 40, tzinfo=tz)) == ["late"] and at(16, caffeine_mg=380) == ["caffeine"]
    assert at(12, stress_avg=82) == ["stress"] and at(23, sleep_mins=200, stress_avg=90) == [] and at(6, sleep_mins=200) == []


def test_the_companion_only_starts_chats_in_the_morning_midday_and_evening():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from apa import companion

    tz = ZoneInfo("Asia/Jakarta")
    at = lambda h, m=0: companion.slot_for(datetime(2026, 3, 10, h, m, tzinfo=tz))  # noqa: E731
    assert at(9)[0] == "morning" and at(13)[0] == "midday" and at(20)[0] == "evening"
    assert at(7) is None and at(11) is None and at(16) is None and at(22) is None and at(2) is None


# ---- the snapshot of a day -------------------------------------------------------------------------------------------------------


def test_a_day_gets_plain_tags_from_its_facts_and_the_ai_reply_is_validated():
    from apa import daily

    f = {"spend": {"count": 7}, "usage": {"hours": 9.1, "late": True, "categories": {"games": 0.55, "video": 0.2}},
         "health": {"steps": 9100, "sleep_mins": 300, "stress": 80}}
    assert daily.tags(f) == ["gaming day", "up late", "heavy screen day", "many purchases", "on the move", "short sleep", "stressed"]
    assert daily.tags({"spend": {"count": 0}, "youtube": {"counts": {"watched": 3}}}) == ["quiet wallet"]
    good = ('{"headline": "A long gaming day, up late", "personality": ["restless", "playful", 3], "mood": 4, "energy": "high", '
            '"focus": "ranked matches", "note": "Spent the day gaming."}')
    out = daily.parse_day(good)
    assert out and out["personality"] == ["restless", "playful"] and out["mood"] == 4 and out["energy"] == "high"
    assert daily.parse_day('{"headline": "x"}') is None and daily.parse_day("nonsense") is None
    assert daily.parse_day('{"headline": "A calm day", "mood": 9, "energy": "wild"}')["mood"] is None


# ---- About me: one dataset ---------------------------------------------------------------------------------------------------------


def test_the_card_text_groups_by_domain_and_marks_what_the_owner_set():
    from apa import about

    entries = [
        {"ref": "fact:1", "domain": "comfort", "key": "language", "value": "Indonesian, casual", "locked": True},
        {"ref": "item:2", "domain": "tastes", "key": "learned_2", "value": "likes spicy food", "locked": False},
        {"ref": "trait:ai.persona", "domain": "personality", "key": "ai.persona", "value": "Playful and curious.", "locked": False},
    ]
    text = about.render(entries)
    assert text.splitlines() == ["[comfort]", "- language: Indonesian, casual (set by the owner)", "[tastes]", "- likes spicy food", "[personality]", "- Playful and curious."]
    assert about.slug("Favourite Game!") == "favourite_game" and about._dom("nonsense") == "other"


# ---- web research (GPT Researcher) ---------------------------------------------------------------------------------------------------


def test_a_research_topic_is_kept_short_and_generic_and_links_are_collected_without_repeats():
    from apa import research

    assert research.clean_topic("best sleep habits for programmers") == "best sleep habits for programmers"
    assert research.clean_topic("my account 1234567890 is hacked") == "" and research.clean_topic("mail me at a@b.com about it") == ""
    assert research.clean_topic("ab") == "" and research.clean_topic(None) == ""
    info = {"source_urls": ["https://a.example/x", "https://b.example/y"]}
    report = "Findings [a](https://a.example/x) and [c](https://c.example/z). See https://c.example/z. http://localhost:8000/x"
    assert research.links(report, info) == ["https://a.example/x", "https://b.example/y", "https://c.example/z"]


def test_the_research_service_answer_is_read_and_a_failure_is_explained(monkeypatch):
    import asyncio

    import httpx

    from apa import research
    from apa.core.settings import get_settings

    monkeypatch.setenv("RESEARCH_URL", "http://gptr:8000")
    get_settings.cache_clear()
    seen = {}

    def handler(req):
        seen["body"] = req.read().decode()
        if "fail" in seen["body"]:
            return httpx.Response(500)
        return httpx.Response(200, json={"report": "# Sleep\n" + "Adults need 7 hours of sleep. " * 8 + "[src](https://sleep.example/a)", "research_information": {"source_urls": ["https://sleep.example/b"]}})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            out = await research.run("sleep needs of adults", http)
            assert out["links"] == ["https://sleep.example/b", "https://sleep.example/a"] and "7 hours" in out["report"]
            assert '"generate_in_background":false' in seen["body"].replace(" ", "")
            try:
                await research.run("please fail this", http)
                raise AssertionError("must raise")
            except research.ResearchError as e:
                assert "500" in str(e)

    asyncio.run(go())
    get_settings.cache_clear()


# ---- GIFs and natural notices -----------------------------------------------------------------------------------------------------------


def test_a_gif_marker_is_split_off_and_only_provider_domains_are_ever_sent():
    from apa import gif

    assert gif.split("Mantap! [[gif:https://media.giphy.com/media/abc/giphy.gif]]") == ("Mantap!", "https://media.giphy.com/media/abc/giphy.gif")
    assert gif.split("Mantap!") == ("Mantap!", None)
    assert gif.split("Hi [[gif:https://evil.example/x.gif]]") == ("Hi", None)  # not a provider: the marker is dropped
    assert gif.split("Hi [[gif:http://media.giphy.com/x.gif]]") == ("Hi", None)  # https only
    assert gif.allowed("https://media.tenor.com/x.gif") and not gif.allowed("https://giphy.com.evil.example/x.gif")
    assert gif.clean_query("  cat   sleepy! ") == "cat sleepy" and gif.clean_query("1234567890") == "" and gif.clean_query(None) == ""


def test_a_gif_is_found_on_giphy_with_the_strictest_rating_and_a_failure_is_just_no_gif(monkeypatch):
    import asyncio

    import httpx

    from apa import gif
    from apa.core.settings import get_settings

    monkeypatch.setenv("GIPHY_API_KEY", "k")
    get_settings.cache_clear()
    seen = {}

    def handler(req):
        seen["url"] = str(req.url)
        if "fail" in seen["url"]:
            return httpx.Response(500)
        return httpx.Response(200, json={"data": [{"images": {"original": {"url": "https://media.giphy.com/media/1/giphy.gif"}}},
                                                  {"images": {"original": {"url": "https://evil.example/x.gif"}}}]})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            assert await gif.search("celebrate", http) == "https://media.giphy.com/media/1/giphy.gif"
            assert "rating=g" in seen["url"] and "q=celebrate" in seen["url"]
            assert await gif.search("fail please", http) is None

    asyncio.run(go())
    monkeypatch.delenv("GIPHY_API_KEY")
    get_settings.cache_clear()
    assert not gif.enabled()


def test_a_notice_rewrite_may_not_change_a_number_and_the_undo_link_is_kept():
    from apa import natural

    original = "✅ Dicatat: Kopi · Rp25.000 · Pos: Makan · Kasir: GoPay · ⚠ konsensus rendah · Undo: /undo_8f9a2b"
    assert natural.kind_of(original) == "summary" and natural.kind_of("🚨 LOCKDOWN darurat") is None and natural.kind_of("\U0001F4F5 HP belum melapor 3 jam") == "phone"
    body, flagged, token = natural.split_summary(original)
    assert body == "Kopi · Rp25.000 · Pos: Makan · Kasir: GoPay" and flagged and token == "/undo_8f9a2b"
    assert natural.keeps_numbers(body, "Kopi Rp25.000 udah kucatat di Makan") and not natural.keeps_numbers(body, "Kopi Rp52.000 udah kucatat")


def test_the_day_reply_carries_a_first_person_diary_entry():
    from apa import daily

    out = daily.parse_day('{"headline": "A quiet day of games", "diary": "Hari ini aku main game seharian dan makan sate malamnya.", "mood": 4}')
    assert out and out["diary"].startswith("Hari ini aku main game") and out["mood"] == 4
    assert daily.parse_day('{"headline": "A quiet day"}')["diary"] == ""
