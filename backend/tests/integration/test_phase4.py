"""Phase 4 against real Postgres: /undo, dismiss, release, lockdown clearing, Telegram commands."""

import asyncio
from datetime import timedelta
from decimal import Decimal

import pytest

from apa.bot.commands import handle_command, parse_command
from apa.db.models.audit import AgentDebateTranscript, ExplainabilityLog
from apa.db.models.budget import BudgetRule, MerchantCache, PolicyRule
from apa.db.models.ledger import CashWallet, Transaction, TransactionSplit
from apa.db.models.ops import InboundEvent, TelegramMessage
from apa.ingestion.merchants import get_stats, record_visit
from apa.pipeline.commit import DebateUnavailable, run_release
from apa.pipeline.undo import clear_lockdown, dismiss_quarantined, undo_transaction
from apa.safety.audit_log import verify_log
from tests.integration.conftest import HAVE_INFRA
from tests.integration.helpers import (
    T0,
    TZ,
    FakeLLM,
    FakeNotifier,
    commit,
    new_event,
    parsed,
    rows,
    tx_for,
)

pytestmark = pytest.mark.skipif(not HAVE_INFRA, reason="TEST_DATABASE_URL/TEST_REDIS_URL not set")

MONTH = T0.date().replace(day=1)


class FakeArq:
    def __init__(self):
        self.jobs: list[tuple] = []

    async def enqueue_job(self, name, *args):
        self.jobs.append((name, *args))


async def seed_cash(stack, balance):
    async with stack() as s:
        s.add(CashWallet(wallet_name="cash_wallet", current_balance=Decimal(balance)))
        await s.commit()


async def seed_budget(stack, category="Food & Dining", limit=1_000_000):
    async with stack() as s:
        s.add(BudgetRule(category=category, period_month=MONTH, base_limit=Decimal(limit),
                         current_limit=Decimal(limit)))
        await s.commit()


async def cash(stack):
    return (await rows(stack, CashWallet))[0].current_balance


async def spent(stack):
    return (await rows(stack, BudgetRule))[0].spent_amount


async def committed(stack, p=None, *, at=T0, llm=None, notifier=None, **kw):
    eid = await new_event(stack, p or parsed(), at=at, **kw)
    assert await commit(stack, eid, llm, notifier) == "committed"
    return await tx_for(stack, eid)


async def held(stack, p=None, *, at=T0):
    """A quarantined transaction (single-tx ceiling)."""
    eid = await new_event(stack, p or parsed(amount="3000000"), at=at)
    assert await commit(stack, eid) == "quarantined"
    return await tx_for(stack, eid)


async def undo(stack, tx_id, **kw):
    return await undo_transaction(stack, tx_id, tz=TZ, **kw)


async def token_of(stack, tx_id, kind):
    (tm,) = await rows(stack, TelegramMessage, TelegramMessage.transaction_id == tx_id,
                       TelegramMessage.kind == kind)
    return tm.undo_token


async def reload(stack, tx_id):
    return (await rows(stack, Transaction, Transaction.id == tx_id))[0]


async def audit_types(stack, tx_id):
    log = await rows(stack, ExplainabilityLog, ExplainabilityLog.transaction_id == tx_id)
    return [r.decision_type for r in sorted(log, key=lambda r: r.id)]


# ---- /undo -------------------------------------------------------------------------------------


async def test_undo_reverses_wallet_budget_and_merchant_and_keeps_the_history(stack):
    await seed_cash(stack, 100_000)
    await seed_budget(stack)
    async with stack() as s:
        for i, a in enumerate((16000, 20000, 18000)):
            await record_visit(s, "warkop", Decimal(a), T0 - timedelta(days=i + 1))
        await s.commit()
        before = await get_stats(s, "warkop")

    tx = await committed(stack, parsed(wallet="cash_wallet", merchant="warkop", channel=None))
    assert (await cash(stack), await spent(stack)) == (Decimal(82_000), Decimal(18_000))

    r = await undo(stack, tx.id)
    assert (r.status, r.transaction_id) == ("ok", tx.id) and "Rp 18.000" in r.label

    after_tx = await reload(stack, tx.id)
    assert (after_tx.status, after_tx.rolled_back_via) == ("rolled_back", "telegram")
    assert after_tx.rolled_back_at is not None
    assert (await cash(stack), await spent(stack)) == (Decimal(100_000), Decimal(0))
    async with stack() as s:
        after = await get_stats(s, "warkop")
        assert await verify_log(s) is None
    assert (after.visit_count, after.avg_amount, after.stddev_amount) == (
        before.visit_count, before.avg_amount, before.stddev_amount)

    # nothing is deleted: the row, its five transcripts and the original audit row all remain
    assert len(await rows(stack, Transaction)) == 1
    assert len(await rows(stack, AgentDebateTranscript)) == 5
    assert await audit_types(stack, tx.id) == ["ledger_commit", "undo"]
    undo_row = (await rows(stack, ExplainabilityLog, ExplainabilityLog.decision_type == "undo"))[0]
    assert undo_row.verdict == "undo:rolled_back" and undo_row.debate_id is None


async def test_second_undo_is_refused_and_does_not_double_restore(stack):
    await seed_cash(stack, 100_000)
    tx = await committed(stack, parsed(wallet="cash_wallet", merchant="warkop", channel=None))
    assert (await undo(stack, tx.id)).status == "ok"
    assert (await undo(stack, tx.id)).status == "wrong_state"
    assert await cash(stack) == Decimal(100_000)
    assert await audit_types(stack, tx.id) == ["ledger_commit", "undo"]


async def test_undo_after_the_reversibility_window_is_refused(stack):
    await seed_cash(stack, 100_000)
    tx = await committed(stack, parsed(wallet="cash_wallet", merchant="warkop", channel=None))
    r = await undo(stack, tx.id, now=tx.reversibility_deadline + timedelta(seconds=1))
    assert r.status == "expired"
    assert (await reload(stack, tx.id)).status == "committed" and await cash(stack) == Decimal(82_000)
    # one second inside the window still works
    r = await undo(stack, tx.id, now=tx.reversibility_deadline - timedelta(seconds=1))
    assert r.status == "ok"


async def test_undo_of_an_unknown_transaction(stack):
    assert (await undo(stack, 999_999)).status == "not_found"


async def test_two_simultaneous_undo_taps_reverse_exactly_once(stack):
    await seed_cash(stack, 100_000)
    await seed_budget(stack)
    tx = await committed(stack, parsed(wallet="cash_wallet", merchant="warkop", channel=None))
    results = await asyncio.gather(undo(stack, tx.id), undo(stack, tx.id))
    assert sorted(r.status for r in results) == ["ok", "wrong_state"]
    assert (await cash(stack), await spent(stack)) == (Decimal(100_000), Decimal(0))
    async with stack() as s:
        assert await verify_log(s) is None


async def test_atm_withdrawal_with_a_fee_undoes_the_exact_cash_credited(stack):
    await seed_cash(stack, 20_000)
    tx = await committed(stack, parsed(amount="500000", fee="6500", wallet="bank_bca",
                                       channel="cash_withdrawal", merchant=None))
    assert (tx.total_paid_amount, tx.net_user_expense) == (Decimal(506_500), Decimal(6_500))
    assert await cash(stack) == Decimal(520_000)  # the cash drawn, not the fee
    assert (await undo(stack, tx.id)).status == "ok"
    assert await cash(stack) == Decimal(20_000)


async def test_income_into_the_cash_wallet_credits_it_and_undo_reverses_it(stack):
    await seed_cash(stack, 10_000)
    tx = await committed(stack, parsed(amount="50000", wallet="cash_wallet", direction="in",
                                       merchant="GAJI", channel=None))
    assert await cash(stack) == Decimal(60_000)
    assert (await undo(stack, tx.id)).status == "ok"
    assert await cash(stack) == Decimal(10_000)
    assert await rows(stack, MerchantCache) == []  # income never touched merchant history


async def test_undo_does_not_push_a_late_budget_below_zero(stack):
    tx = await committed(stack)  # no budget row existed, so nothing was counted
    await seed_budget(stack)
    assert (await undo(stack, tx.id)).status == "ok"
    assert await spent(stack) == Decimal(0)


async def test_undo_waives_pending_splits_but_not_settled_ones(stack):
    tx = await committed(stack)
    async with stack() as s:
        s.add_all([
            TransactionSplit(transaction_id=tx.id, debtor_name="Budi", share_amount=Decimal(6000)),
            TransactionSplit(transaction_id=tx.id, debtor_name="Siti", share_amount=Decimal(6000),
                             settlement_status="settled"),
        ])
        await s.commit()
    assert (await undo(stack, tx.id)).status == "ok"
    status = {r.debtor_name: r.settlement_status for r in await rows(stack, TransactionSplit)}
    assert status == {"Budi": "waived", "Siti": "settled"}


async def test_an_undone_transaction_stops_counting_toward_velocity(stack):
    txs = [await committed(stack, parsed(amount=str(10_000 + i), merchant=f"toko {i}"),
                           at=T0 - timedelta(minutes=5 - i)) for i in range(5)]
    assert (await undo(stack, txs[0].id)).status == "ok"
    # five in the window, one undone: the sixth makes five live ones, which is not "more than five"
    sixth = await new_event(stack, parsed(amount="10005", merchant="toko 5"), at=T0)
    assert await commit(stack, sixth) == "committed"


# ---- dismiss a quarantined transaction ------------------------------------------------------------


async def test_dismiss_discards_a_held_transaction_without_touching_balances(stack):
    await seed_budget(stack)
    tx = await held(stack)
    r = await dismiss_quarantined(stack, tx.id)
    assert r.status == "ok"
    after = await reload(stack, tx.id)
    assert (after.status, after.rolled_back_via) == ("rolled_back", "telegram")
    assert await spent(stack) == 0
    assert await audit_types(stack, tx.id) == ["quarantine", "dismiss"]
    assert (await dismiss_quarantined(stack, tx.id)).status == "wrong_state"
    async with stack() as s:
        assert await verify_log(s) is None


async def test_each_operation_refuses_the_wrong_state(stack):
    done = await committed(stack)
    pending = await held(stack, parsed(amount="3000000", merchant="lain"), at=T0 + timedelta(hours=2))
    assert (await dismiss_quarantined(stack, done.id)).status == "wrong_state"  # not held
    assert (await undo(stack, pending.id)).status == "wrong_state"  # never committed
    assert await run_release(stack, FakeLLM(), FakeNotifier(), done.id, tz=TZ) == "committed"  # no-op


# ---- release a quarantined transaction --------------------------------------------------------------


async def test_release_commits_the_same_row_with_a_real_debate_and_full_effects(stack):
    await seed_budget(stack)
    tx = await held(stack)
    llm, notifier = FakeLLM(), FakeNotifier()
    assert await run_release(stack, llm, notifier, tx.id, tz=TZ) == "committed"

    all_tx = await rows(stack, Transaction)
    assert len(all_tx) == 1, "release must update the held row, not insert a second one"
    done = all_tx[0]
    assert (done.id, done.status, done.execution_mode, done.circuit_breaker_cleared) == (
        tx.id, "committed", "consensus_auto", True)
    assert done.primary_category == "Food & Dining" and done.debate_id is not None
    assert done.notes == "released via telegram (was single_tx_ceiling)"
    assert done.reversibility_deadline > tx.timestamp  # a fresh undo window starts at release

    assert await spent(stack) == Decimal(3_000_000)
    assert (await rows(stack, MerchantCache))[0].visit_count == 1
    turns = await rows(stack, AgentDebateTranscript)
    assert len(turns) == 5 and {t.subject_ref for t in turns} == {tx.id}
    assert await audit_types(stack, tx.id) == ["quarantine", "release"]
    rel = (await rows(stack, ExplainabilityLog, ExplainabilityLog.decision_type == "release"))[0]
    assert rel.verdict == "release:Food & Dining" and "released from quarantine" in rel.justification
    assert (await rows(stack, InboundEvent))[0].status == "committed"
    assert "/undo_" in notifier.sent[0]  # the normal one-line summary is the confirmation

    # a released transaction is a normal committed one: it can be undone and releasing twice is a no-op
    calls = len(llm.calls)
    assert await run_release(stack, llm, notifier, tx.id, tz=TZ) == "committed"
    assert len(llm.calls) == calls and len(notifier.sent) == 1
    assert (await undo(stack, tx.id)).status == "ok" and await spent(stack) == 0
    async with stack() as s:
        assert await verify_log(s) is None


async def test_release_of_a_held_withdrawal_needs_no_debate(stack):
    await seed_cash(stack, 0)
    tx = await held(stack, parsed(amount="3000000", wallet="bank_bca", channel="cash_withdrawal",
                                  merchant=None))
    llm = FakeLLM()
    assert await run_release(stack, llm, FakeNotifier(), tx.id, tz=TZ) == "committed"
    done = await reload(stack, tx.id)
    assert (done.primary_category, done.execution_mode) == ("Cash Withdrawal", "deterministic_transfer")
    assert llm.calls == [] and await cash(stack) == Decimal(3_000_000)


async def test_release_works_during_lockdown_and_does_not_clear_it(stack):
    from apa.safety.policy import lockdown_active, set_lockdown

    async with stack() as s:
        await set_lockdown(s, True, by="test")
        await s.commit()
    eid = await new_event(stack, parsed(amount="12000"))
    assert await commit(stack, eid) == "quarantined"
    tx = await tx_for(stack, eid)
    assert tx.notes == "quarantined: lockdown"
    assert await run_release(stack, FakeLLM(), FakeNotifier(), tx.id, tz=TZ) == "committed"
    async with stack() as s:
        assert await lockdown_active(s)


async def test_release_with_the_debate_down_stays_held_and_tells_the_owner(stack):
    tx = await held(stack)
    down, notifier = FakeLLM(fail={"*"}), FakeNotifier()
    for attempt in (1, 2):
        with pytest.raises(DebateUnavailable):
            await run_release(stack, down, notifier, tx.id, job_try=attempt, tz=TZ)
        assert (await reload(stack, tx.id)).status == "quarantined"  # still held, nothing announced
    assert notifier.sent == []

    assert await run_release(stack, down, notifier, tx.id, job_try=3, tz=TZ) == "quarantined"
    still = await reload(stack, tx.id)
    assert still.status == "quarantined" and still.notes == "quarantined: single_tx_ceiling"  # untouched
    (reply,) = await rows(stack, TelegramMessage, TelegramMessage.kind == "reply")
    assert "/release belum bisa" in reply.body and "kuorum" in reply.body and "0 dari 5" in reply.body
    assert notifier.sent == [reply.body] and reply.message_id == 101

    # the LLM comes back: the same /release now goes through
    assert await run_release(stack, FakeLLM(), notifier, tx.id, tz=TZ) == "committed"
    assert (await reload(stack, tx.id)).status == "committed"


async def test_release_of_an_unknown_transaction(stack):
    assert await run_release(stack, FakeLLM(), FakeNotifier(), 999_999, tz=TZ) == "not_found"


# ---- clearing the lockdown ----------------------------------------------------------------------------


async def trip_velocity(stack):
    for i in range(5):
        await committed(stack, parsed(amount=str(10_000 + i), merchant=f"toko {i}"),
                        at=T0 - timedelta(minutes=5 - i))
    sixth = await new_event(stack, parsed(amount="10005", merchant="toko 5"), at=T0)
    assert await commit(stack, sixth) == "quarantined"


async def test_clearing_lockdown_lets_expenses_commit_again_and_is_audited(stack):
    await trip_velocity(stack)
    held_later = await new_event(stack, parsed(amount="12000", merchant="baru"), at=T0 + timedelta(hours=1))
    assert await commit(stack, held_later) == "quarantined"  # lockdown holds it

    assert await clear_lockdown(stack, by="telegram") is True
    (flag,) = await rows(stack, PolicyRule, PolicyRule.rule_key == "lockdown_active")
    assert (flag.value_numeric, flag.updated_by, flag.version) == (0, "telegram", 2)
    cleared = [r for r in await rows(stack, ExplainabilityLog) if r.verdict == "lockdown_active=0"]
    assert len(cleared) == 1 and cleared[0].decision_type == "config_change"

    fresh = await new_event(stack, parsed(amount="13000", merchant="baru lagi"), at=T0 + timedelta(hours=2))
    assert await commit(stack, fresh) == "committed"
    async with stack() as s:
        assert await verify_log(s) is None


async def test_clearing_an_inactive_lockdown_writes_nothing(stack):
    assert await clear_lockdown(stack) is False
    assert await rows(stack, ExplainabilityLog) == [] and await rows(stack, PolicyRule, PolicyRule.rule_key == "lockdown_active") == []


# ---- Telegram commands ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/undo_8f9a2b", ("undo", "8f9a2b")),
        ("/undo 8F9A2B", ("undo", "8f9a2b")),
        ("/undo_8f9a2b@MyBot", ("undo", "8f9a2b")),
        ("/release_000abc", ("release", "000abc")),
        ("/dismiss_00ff00", ("dismiss", "00ff00")),
        ("/unlock", ("unlock", None)),
        ("/undo", ("undo", None)),
        ("  /unlock  ", ("unlock", None)),
        ("/undo_nothex", None),
        ("/undo 12", None),
        ("/start", None),
        ("undo_8f9a2b", None),
    ],
)
def test_parse_command(text, expected):
    assert parse_command(text) == expected


async def test_undo_command_by_token(stack):
    await seed_cash(stack, 100_000)
    tx = await committed(stack, parsed(wallet="cash_wallet", merchant="warkop", channel=None))
    token = await token_of(stack, tx.id, "summary")
    arq = FakeArq()
    assert await handle_command(stack, arq, "undo", token) == "↩️ Dibatalkan: warkop · Rp 18.000"
    assert await cash(stack) == Decimal(100_000)
    assert "sudah dibatalkan" in await handle_command(stack, arq, "undo", token)
    assert await handle_command(stack, arq, "undo", "deadbe") == "Token tidak dikenal."
    assert "Format" in await handle_command(stack, arq, "undo", None)
    assert arq.jobs == []


async def test_quarantine_alert_carries_tokens_that_dismiss_and_release(stack):
    tx = await held(stack)
    alert = (await rows(stack, TelegramMessage, TelegramMessage.kind == "alert"))[0]
    assert alert.transaction_id == tx.id and alert.undo_token
    assert f"/release_{alert.undo_token}" in alert.body and f"/dismiss_{alert.undo_token}" in alert.body

    arq = FakeArq()
    # release is silent on success: the worker sends the normal summary when it commits
    assert await handle_command(stack, arq, "release", alert.undo_token) is None
    assert arq.jobs == [("release_quarantined", tx.id)]
    assert "sudah dibatalkan atau belum tercatat" in await handle_command(stack, arq, "undo", alert.undo_token)

    assert (await handle_command(stack, arq, "dismiss", alert.undo_token)).startswith("🗑️ Dibuang")
    assert (await reload(stack, tx.id)).status == "rolled_back"


async def test_release_command_on_a_committed_transaction_is_answered_not_enqueued(stack):
    tx = await committed(stack)
    token = await token_of(stack, tx.id, "summary")
    arq = FakeArq()
    reply = await handle_command(stack, arq, "release", token)
    assert reply and "sudah dibatalkan atau belum tercatat" in reply and arq.jobs == []


async def test_unlock_command(stack):
    await trip_velocity(stack)
    assert await handle_command(stack, FakeArq(), "unlock", None) == "🔓 Lockdown dicabut."
    assert await handle_command(stack, FakeArq(), "unlock", None) == "Lockdown tidak aktif."


async def test_velocity_alert_offers_unlock(stack):
    await trip_velocity(stack)
    alert = (await rows(stack, TelegramMessage, TelegramMessage.kind == "alert"))[0]
    assert "/unlock" in alert.body and "LOCKDOWN" in alert.body


# ---- worker job -------------------------------------------------------------------------------------------


async def test_release_job_maps_an_outage_to_a_retry_then_stays_held(stack):
    from arq import Retry

    from apa.workers import settings as worker

    tx = await held(stack)
    ctx = {"sessionmaker": stack, "llm": FakeLLM(fail={"*"}), "notifier": FakeNotifier(), "job_try": 1}
    with pytest.raises(Retry):
        await worker.release_quarantined(ctx, tx.id)
    ctx["job_try"] = worker.COMMIT_MAX_TRIES
    assert await worker.release_quarantined(ctx, tx.id) == "quarantined"  # no quorum: stays held
