"""The data steward: finds data that fell through and repairs it through the guarded pipeline. Strictly get / add /
edit: it never deletes, never acts twice forever, and reports what it could not fix."""

import json
import os
import re
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from apa.db.models.audit import ExplainabilityLog
from apa.db.models.budget import BudgetRule, MerchantCache
from apa.db.models.ledger import Transaction
from apa.db.models.ops import InboundEvent, TelegramMessage
from apa.ingestion.receipt_total import extract_total_only, printed_amount
from apa.integrations.evidence_store import LocalEvidenceStore
from apa.integrations.llm import LLMResult
from apa.pipeline.receipt import run_read_receipt
from apa.safety.audit_log import verify_log
from apa.steward.finder import find
from apa.steward.runner import LOCK_KEY, run_steward
from tests.integration.conftest import HAVE_INFRA
from tests.integration.helpers import (
    FakeLLM,
    FakeNotifier,
    commit,
    new_event,
    parsed,
    raw_event,
    rows,
    tx_for,
)
from tests.integration.test_batch import ArqLikeRedis

TZ = ZoneInfo("Asia/Jakarta")
FIX = Path(__file__).resolve().parents[1] / "fixtures"
NOW = datetime.now(TZ)
OLD = NOW - timedelta(hours=2)

infra = pytest.mark.skipif(not HAVE_INFRA, reason="TEST_DATABASE_URL/TEST_REDIS_URL not set")
SRC = Path(__file__).resolve().parents[2] / "src" / "apa"


# ---- pure ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "want"),
    [("Rp 58.300", 58300), ("58,300", 58300), ("Rp.58300,-", 58300), ("1.250.000", 1_250_000), ("58300", 58300),
     ("Rp 58.300 + tax", None), ("TOTAL", None), ("", None), ("5.8300", None), ("Rp -5", None)],
)
def test_the_printed_total_is_read_strictly(text, want):
    assert printed_amount(text) == (Decimal(want) if want is not None else None)


def test_the_steward_package_contains_no_way_to_delete():
    """Strictly get / add / edit: no delete() call, no sqlalchemy delete import, no DELETE statement in code."""
    import ast

    for path in [*(SRC / "steward").glob("*.py"), SRC / "ingestion" / "receipt_total.py"]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "attr", getattr(node.func, "id", ""))
                # the runner releases its own Redis lock: the only removal, and it is not data
                if name in ("delete", "truncate", "drop") and not (path.name == "runner.py" and name == "delete"):
                    raise AssertionError(f"{path.name}:{node.lineno} calls {name}()")
            if isinstance(node, ast.ImportFrom) and any(a.name in ("delete", "Delete") for a in node.names):
                raise AssertionError(f"{path.name}: imports delete")
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and re.match(r"\s*DELETE\s", node.value, re.I):
                raise AssertionError(f"{path.name}:{node.lineno} has a DELETE statement")


# ---- the second look at a photo --------------------------------------------------------------------------------


class Vision:
    def __init__(self, body):
        self.body, self.calls = body, 0

    async def complete(self, route, system, user, **kw):
        self.calls += 1
        assert kw.get("images") and route == "router-vision-ocr"
        return LLMResult(json.dumps(self.body), "fake-vision", 400, 60, 0)


def total_body(**over):
    return {"is_receipt": True, "merchant": "WARTEG BU SITI", "purchased_at": None, "total_printed": "Rp 58.300",
            "total": 58300, "payment_method": "qris", "confidence": 0.9, **over}


IMG = [(b"\x89PNG....", "image/png")]


async def total_only(body, caption=""):
    return await extract_total_only(Vision(body), IMG, caption, sent_at=NOW, tz=TZ)


async def test_a_clearly_printed_total_is_recorded_as_a_weaker_total_only_receipt():
    r = await total_only(total_body())
    assert r.status == "parsed" and r.parsed["amount"] == "58300" and r.parsed["merchant"] == "WARTEG BU SITI"
    assert r.parsed["confidence"] == 0.8  # capped below the normal reading's 0.95
    assert r.parsed["receipt"]["total_only"] is True and r.parsed["receipt"]["items"] == []
    assert r.parsed["extraction"]["method"] == "vision_total_only"


@pytest.mark.parametrize(
    ("over", "reason"),
    [({"total": 58000}, "total_not_as_printed"),  # the number is not what is printed
     ({"total_printed": None}, "no_total"), ({"total": None}, "no_total"), ({"total": 0, "total_printed": "0"}, "no_total"),
     ({"confidence": 0.6}, "low_confidence"), ({"total_printed": "Rp 58.300 + tax"}, "total_not_as_printed"),
     ({"is_receipt": False}, None)],
)
async def test_a_total_that_cannot_be_checked_against_the_print_is_refused(over, reason):
    r = await total_only(total_body(**over))
    assert r.status != "parsed" and (reason is None or r.reason == reason)


async def test_a_typed_caption_amount_must_still_match_the_total():
    assert (await total_only(total_body(), "bayar 58rb")).status == "parsed"
    assert (await total_only(total_body(), "bayar 35rb")).reason == "caption_total_mismatch"


# ---- the finder ---------------------------------------------------------------------------------------------------


@pytest.fixture
async def redis(stack):
    r = ArqLikeRedis.from_url(os.environ["TEST_REDIS_URL"])
    r.jobs = []
    yield r
    await r.aclose()


async def make_event(stack, status, *, source="telegram_shorthand", received=OLD, payload=None):
    eid = await raw_event(stack, "x", source=source, status=status)
    async with stack() as s:
        ev = await s.get(InboundEvent, eid)
        ev.received_at = received
        ev.payload = {**ev.payload, **(payload or {})}
        await s.commit()
    return eid


@infra
async def test_the_finder_lists_only_what_is_really_stuck_or_unrepaired(stack):
    stuck = await make_event(stack, "processing")
    await make_event(stack, "processing", received=NOW - timedelta(minutes=3))  # still fresh: a debate takes seconds
    await make_event(stack, "committed")
    await make_event(stack, "skipped")
    photo = await make_event(stack, "needs_review", source="telegram_photo",
                             payload={"review_reason": "total_mismatch", "attachments": []})
    await make_event(stack, "needs_review", source="android_listener", payload={"review_reason": "low_confidence"})
    tickets = await find(stack, NOW)
    assert {(t.kind, t.event_id) for t in tickets} == {("stuck", stuck), ("image", photo)}


@infra
async def test_a_ticket_that_already_used_its_attempts_is_not_listed_again(stack):
    await make_event(stack, "processing", payload={"steward": {"attempts": {"stuck": 3}}})
    assert await find(stack, NOW) == []


@infra
async def test_uncategorized_spending_and_ai_down_holds_are_listed_but_ordinary_holds_are_not(stack):
    eid = await new_event(stack, parsed(), at=OLD)
    assert await commit(stack, eid, FakeLLM(cat="Uncategorized")) == "committed"
    held = await new_event(stack, parsed(amount="3000000", merchant="BIG"), at=OLD)
    assert await commit(stack, held) == "quarantined"  # a real hold: the single-transaction ceiling
    tickets = await find(stack, NOW)
    assert [t.kind for t in tickets] == ["uncategorized"]  # the ceiling hold is the owner's call, never the steward's
    async with stack() as s:  # now make one look like an AI outage
        tx = await s.get(Transaction, (await tx_for(stack, held)).id)
        tx.notes = "quarantined: no_quorum"
        await s.commit()
    assert sorted(t.kind for t in await find(stack, NOW)) == ["held", "uncategorized"]


# ---- repairs ----------------------------------------------------------------------------------------------------------


@infra
async def test_a_stuck_message_goes_back_to_where_its_job_picks_it_up(stack, redis):
    cases = [("processing", "parsed", "commit_event"), ("parsed", "parsed", "commit_event"),
             ("extracting", "needs_llm", "extract_event"), ("needs_llm", "needs_llm", "master_event"),
             ("received", "received", "process_inbound_event")]
    for status, _, _ in cases:
        await make_event(stack, status)
    report = await run_steward(stack, FakeLLM(), None, FakeNotifier(), redis, tz=TZ, now=NOW)
    assert sorted(j[0] for j in redis.jobs) == sorted(c[2] for c in cases)
    events = {e.status for e in await rows(stack, InboundEvent)}
    assert events == {"parsed", "needs_llm", "received"}  # claimed/in-flight states were reset to enterable ones
    assert len(report.done) == 5 and "diproses ulang" in report.done[0]


@infra
async def test_every_repair_is_one_audited_row_and_the_chain_still_verifies(stack, redis):
    await make_event(stack, "processing")
    await make_event(stack, "reading", source="telegram_photo")
    await run_steward(stack, FakeLLM(), None, FakeNotifier(), redis, tz=TZ, now=NOW)
    repairs = await rows(stack, ExplainabilityLog, ExplainabilityLog.decision_type == "repair")
    assert len(repairs) == 2 and all(r.verdict.startswith("repair:") for r in repairs)
    async with stack() as s:
        assert await verify_log(s) is None


@infra
async def test_the_steward_reports_in_one_message_and_says_nothing_when_all_is_well(stack, redis):
    n = FakeNotifier()
    assert (await run_steward(stack, FakeLLM(), None, n, redis, tz=TZ, now=NOW)).text() is None
    assert n.sent == []
    await make_event(stack, "processing")
    await make_event(stack, "parsed")
    await run_steward(stack, FakeLLM(), None, n, redis, tz=TZ, now=NOW)
    assert len(n.sent) == 1 and n.sent[0].startswith("🛠️ Perbaikan otomatis:") and n.sent[0].count("•") == 2


@infra
async def test_a_message_that_stays_stuck_is_tried_three_times_and_then_left_alone(stack, redis):
    eid = await make_event(stack, "needs_llm")
    for _ in range(5):
        async with stack() as s:  # the job "never runs": it is stuck again on the next pass
            ev = await s.get(InboundEvent, eid)
            ev.status = "needs_llm"
            await s.commit()
        await run_steward(stack, FakeLLM(), None, FakeNotifier(), redis, tz=TZ, now=NOW)
    assert [j for j in redis.jobs if j == ("master_event", eid)] == [("master_event", eid)] * 3


@infra
async def test_one_pass_at_a_time_and_a_bounded_number_of_actions(stack, redis):
    for _ in range(14):
        await make_event(stack, "parsed")
    await redis.set(LOCK_KEY, "1", ex=60)  # another pass is running
    assert (await run_steward(stack, FakeLLM(), None, FakeNotifier(), redis, tz=TZ, now=NOW)).done == []
    await redis.delete(LOCK_KEY)
    report = await run_steward(stack, FakeLLM(), None, FakeNotifier(), redis, tz=TZ, now=NOW)
    assert len(report.done) == 10  # a backlog is worked off over several passes
    assert await redis.get(LOCK_KEY) is None  # and the lock is released


@infra
async def test_a_failed_commit_is_requeued_twice_at_most(stack, redis):
    p = {"parsed": parsed()}
    eid = await make_event(stack, "failed", payload=p)
    async with stack() as s:
        (await s.get(InboundEvent, eid)).error = "boom"
        await s.commit()
    await run_steward(stack, FakeLLM(), None, FakeNotifier(), redis, tz=TZ, now=NOW)
    assert redis.jobs == [("commit_event", eid)]
    assert (await rows(stack, InboundEvent))[0].status == "parsed"


@infra
async def test_uncategorized_spending_is_fixed_from_merchant_memory_without_a_model_call(stack, redis):
    month = NOW.date().replace(day=1)
    async with stack() as s:
        for c in ("Food & Dining", "Groceries"):
            s.add(BudgetRule(category=c, period_month=month, base_limit=Decimal(1_000_000),
                             current_limit=Decimal(1_000_000)))
        await s.commit()
    eid = await new_event(stack, parsed(), at=OLD)
    assert await commit(stack, eid, FakeLLM(cat="Uncategorized")) == "committed"
    async with stack() as s:  # the commit learned the merchant but not a category (it was Uncategorized)
        row = (await s.execute(select(MerchantCache))).scalar_one()
        row.default_category = "Food & Dining"
        await s.commit()
    llm = FakeLLM()
    report = await run_steward(stack, llm, None, FakeNotifier(), redis, tz=TZ, now=NOW)
    assert llm.calls == [] and "dikategorikan Food & Dining" in report.done[0]
    tx = (await rows(stack, Transaction))[0]
    assert tx.primary_category == "Food & Dining"
    budgets = {b.category: b.spent_amount for b in await rows(stack, BudgetRule)}
    assert budgets["Food & Dining"] == Decimal(18_000)  # and the budget now carries the spend
    again = await run_steward(stack, llm, None, FakeNotifier(), redis, tz=TZ, now=NOW)
    assert again.done == []  # once only


class CategoryLLM:
    def __init__(self, body):
        self.body, self.calls = body, 0

    async def complete(self, route, system, user, **kw):
        self.calls += 1
        assert "Allowed categories" in user
        return LLMResult(json.dumps(self.body), "fake", 30, 10, 0)


@infra
async def test_an_unknown_merchant_is_categorised_by_the_model_only_when_it_is_confident(stack, redis):
    eid = await new_event(stack, parsed(merchant="WARUNG BARU"), at=OLD)
    assert await commit(stack, eid, FakeLLM(cat="Uncategorized")) == "committed"
    llm = CategoryLLM({"category": "groceries", "confidence": 0.9})
    report = await run_steward(stack, llm, None, FakeNotifier(), redis, tz=TZ, now=NOW)
    assert llm.calls == 1 and "dikategorikan Groceries" in report.done[0]


@infra
async def test_a_model_that_invents_a_category_changes_nothing_and_is_reported_once(stack, redis):
    eid = await new_event(stack, parsed(merchant="WARUNG BARU"), at=OLD)
    assert await commit(stack, eid, FakeLLM(cat="Uncategorized")) == "committed"
    n = FakeNotifier()
    invented = CategoryLLM({"category": "Made Up", "confidence": 0.95})
    report = await run_steward(stack, invented, None, n, redis, tz=TZ, now=NOW)
    assert "kategori belum jelas" in report.needs_you[0]
    assert (await rows(stack, Transaction))[0].primary_category == "Uncategorized"
    again = await run_steward(stack, CategoryLLM({"category": "Groceries", "confidence": 0.9}), None, n, redis, tz=TZ, now=NOW)
    assert again.done == [] and again.needs_you == []  # the one attempt is spent: no endless retry, no repeat nag


@infra
async def test_a_model_that_is_unsure_does_not_guess(stack, redis):
    eid = await new_event(stack, parsed(merchant="WARUNG BARU"), at=OLD)
    assert await commit(stack, eid, FakeLLM(cat="Uncategorized")) == "committed"
    report = await run_steward(stack, CategoryLLM({"category": "Groceries", "confidence": 0.4}), None, FakeNotifier(), redis,
                               tz=TZ, now=NOW)
    assert report.done == [] and (await rows(stack, Transaction))[0].primary_category == "Uncategorized"


@infra
async def test_a_hold_caused_only_by_an_ai_outage_is_released_through_the_normal_job_a_few_times(stack, redis):
    eid = await new_event(stack, parsed(), at=OLD)
    assert await commit(stack, eid, FakeLLM(fail={"*"}), job_try=3, max_tries=3) == "quarantined"  # no quorum, last try
    tx = await tx_for(stack, eid)
    assert tx.notes == "quarantined: no_quorum"
    for _ in range(5):
        await run_steward(stack, FakeLLM(), None, FakeNotifier(), redis, tz=TZ, now=NOW)
    assert redis.jobs == [("release_quarantined", tx.id)] * 3  # three attempts, then the owner's call
    audit = await rows(stack, ExplainabilityLog, ExplainabilityLog.verdict == "repair:release_requeued")
    assert len(audit) == 3
    assert (await rows(stack, Transaction, Transaction.id == tx.id))[0].status == "quarantined"  # the debate decides


@infra
async def test_a_hold_for_a_real_risk_is_never_touched_by_the_steward(stack, redis):
    eid = await new_event(stack, parsed(amount="3000000"), at=OLD)
    assert await commit(stack, eid, FakeLLM()) == "quarantined"
    await run_steward(stack, FakeLLM(), None, FakeNotifier(), redis, tz=TZ, now=NOW)
    assert redis.jobs == []


# ---- photos ------------------------------------------------------------------------------------------------------------


async def photo_event(stack, store, reason="total_mismatch"):
    sha = await store.put((FIX / "receipt_warteg.png").read_bytes(), "image/png")
    eid = await make_event(
        stack, "needs_review", source="telegram_photo",
        payload={"review_reason": reason, "caption": "", "sent_at": NOW.isoformat(),
                 "attachments": [{"uri": sha.uri, "mime": "image/png", "sha256": sha.sha256, "size": sha.size}]},
    )
    return eid


@infra
async def test_a_photo_with_no_transaction_is_read_again_quietly_first(stack, redis, tmp_path):
    store = LocalEvidenceStore(tmp_path)
    eid = await photo_event(stack, store)
    n = FakeNotifier()
    report = await run_steward(stack, Vision(total_body()), store, n, redis, tz=TZ, now=NOW)
    assert redis.jobs == [("read_receipt", eid)] and "dibaca ulang" in report.done[0]
    assert (await rows(stack, InboundEvent))[0].status == "received"


@infra
async def test_a_quiet_reread_that_fails_again_sends_the_owner_no_second_error(stack, redis, tmp_path):
    store = LocalEvidenceStore(tmp_path)
    eid = await photo_event(stack, store)
    await run_steward(stack, Vision(total_body()), store, FakeNotifier(), redis, tz=TZ, now=NOW)
    n = FakeNotifier()
    bad = Vision({"is_receipt": True, "merchant": "X", "items": [{"name": "a", "quantity": 1, "unit_price": 1, "total_price": 1}],
                  "total": 99999, "subtotal": None, "discount": 0, "tax": 0, "service_charge": 0,
                  "payment_method": "cash", "confidence": 0.95})
    assert await run_read_receipt(stack, bad, n, store, redis, eid, tz=TZ) == "needs_review"
    assert n.sent == []  # no repeat "struk belum bisa dicatat"
    assert not [m for m in await rows(stack, TelegramMessage) if "Struk belum bisa dicatat" in (m.body or "")]
    assert (await rows(stack, InboundEvent))[0].payload["review_reason"] == "total_mismatch"


@infra
async def test_the_second_rung_records_the_printed_total_through_the_normal_commit(stack, redis, tmp_path):
    store = LocalEvidenceStore(tmp_path)
    eid = await photo_event(stack, store)
    async with stack() as s:
        ev = await s.get(InboundEvent, eid)
        ev.payload = {**ev.payload, "steward": {"attempts": {"image": 1}}}
        await s.commit()
    n = FakeNotifier()
    report = await run_steward(stack, Vision(total_body()), store, n, redis, tz=TZ, now=NOW)
    assert redis.jobs == [("commit_event", eid)] and "hanya total Rp 58.300 yang dicatat" in report.done[0]
    assert await commit(stack, eid, FakeLLM()) == "committed"  # breakers, debate and quorum still run
    (tx,) = await rows(stack, Transaction)
    assert (tx.merchant, tx.total_paid_amount) == ("WARTEG BU SITI", Decimal(58300))
    assert "receipt lines unreadable: total only" in tx.notes
    audit = await rows(stack, ExplainabilityLog, ExplainabilityLog.verdict == "repair:image_total_only")
    assert len(audit) == 1 and "58.300" in audit[0].justification


@infra
async def test_when_even_the_total_is_unreadable_the_steward_gives_up_reports_once_and_stops(stack, redis, tmp_path):
    store = LocalEvidenceStore(tmp_path)
    eid = await photo_event(stack, store)
    async with stack() as s:
        ev = await s.get(InboundEvent, eid)
        ev.payload = {**ev.payload, "steward": {"attempts": {"image": 1}}}
        await s.commit()
    n = FakeNotifier()
    vision = Vision(total_body(total_printed=None, total=None))
    report = await run_steward(stack, vision, store, n, redis, tz=TZ, now=NOW)
    assert "no_total" in report.needs_you[0] and redis.jobs == []
    ev = (await rows(stack, InboundEvent))[0]
    assert ev.payload["steward"]["gave_up"] and ev.status == "needs_review"  # untouched, not deleted
    again = await run_steward(stack, vision, store, n, redis, tz=TZ, now=NOW)
    assert again.needs_you == [] and vision.calls == 1 and len(n.sent) == 1  # told once, never nagged


@infra
async def test_a_photo_that_is_not_a_receipt_or_contradicts_its_caption_is_not_guessed_at(stack, redis, tmp_path):
    store = LocalEvidenceStore(tmp_path)
    for reason in ("not_a_receipt", "caption_total_mismatch", "too_many_pages"):
        await photo_event(stack, store, reason)
    vision = Vision(total_body())
    report = await run_steward(stack, vision, store, FakeNotifier(), redis, tz=TZ, now=NOW)
    assert len(report.needs_you) == 3 and vision.calls == 0 and redis.jobs == []


@infra
async def test_a_model_outage_during_the_second_look_costs_no_attempt(stack, redis, tmp_path):
    store = LocalEvidenceStore(tmp_path)
    eid = await photo_event(stack, store)
    async with stack() as s:
        ev = await s.get(InboundEvent, eid)
        ev.payload = {**ev.payload, "steward": {"attempts": {"image": 1}}}
        await s.commit()

    class Down:
        async def complete(self, *a, **k):
            raise RuntimeError("down")

    assert (await run_steward(stack, Down(), store, FakeNotifier(), redis, tz=TZ, now=NOW)).done == []
    ev = (await rows(stack, InboundEvent))[0]
    assert ev.payload["steward"]["attempts"]["image"] == 1 and not ev.payload["steward"].get("gave_up")


@infra
async def test_a_photo_whose_file_is_gone_is_reported_not_crashed_on(stack, redis, tmp_path):
    store = LocalEvidenceStore(tmp_path)
    eid = await photo_event(stack, store)
    async with stack() as s:
        ev = await s.get(InboundEvent, eid)
        ev.payload = {**ev.payload, "steward": {"attempts": {"image": 1}},
                      "attachments": [{"uri": "local:zz/missing.png", "mime": "image/png"}]}
        await s.commit()
    report = await run_steward(stack, Vision(total_body()), store, FakeNotifier(), redis, tz=TZ, now=NOW)
    assert "image_unavailable" in report.needs_you[0]


@infra
async def test_the_steward_never_removes_anything(stack, redis, tmp_path):
    store = LocalEvidenceStore(tmp_path)
    await photo_event(stack, store)
    await make_event(stack, "processing")
    before = {t.__tablename__: len(await rows(stack, t)) for t in (InboundEvent, Transaction)}
    await run_steward(stack, Vision(total_body()), store, FakeNotifier(), redis, tz=TZ, now=NOW)
    await run_steward(stack, Vision(total_body()), store, FakeNotifier(), redis, tz=TZ, now=NOW)
    after = {t.__tablename__: len(await rows(stack, t)) for t in (InboundEvent, Transaction)}
    assert after == before  # rows are edited in place, none removed, none duplicated


@infra
async def test_a_ticket_that_went_stale_is_not_acted_on_if_the_hold_is_not_an_ai_outage(stack, redis):
    """The fixer re-checks under the row lock: the finder's view may be minutes old."""
    from apa.steward import fixers
    from apa.steward.finder import Ticket

    eid = await new_event(stack, parsed(amount="3000000"), at=OLD)
    assert await commit(stack, eid, FakeLLM()) == "quarantined"  # a real risk hold
    tx = await tx_for(stack, eid)
    assert await fixers.fix_held(stack, redis, Ticket("held", tx_id=tx.id)) is None and redis.jobs == []


@infra
async def test_recategorizing_a_cash_expense_does_not_touch_the_wallet_again(stack, redis):
    from apa.db.models.ledger import CashWallet

    async with stack() as s:
        s.add(CashWallet(wallet_name="cash_wallet", current_balance=Decimal(100_000)))
        await s.commit()
    eid = await new_event(stack, parsed(wallet="cash_wallet", merchant="WARUNG BARU", channel=None), at=OLD)
    assert await commit(stack, eid, FakeLLM(cat="Uncategorized")) == "committed"
    assert (await rows(stack, CashWallet))[0].current_balance == Decimal(82_000)
    report = await run_steward(stack, CategoryLLM({"category": "Groceries", "confidence": 0.9}), None,
                               FakeNotifier(), redis, tz=TZ, now=NOW)
    assert "dikategorikan Groceries" in report.done[0]
    assert (await rows(stack, CashWallet))[0].current_balance == Decimal(82_000)  # reversed, then applied once
