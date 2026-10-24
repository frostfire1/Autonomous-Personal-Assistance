"""Phase 3 pipeline against real Postgres: breakers -> debate -> Arbiter -> ledger + audit."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tests.integration.conftest import HAVE_INFRA
from tests.integration.helpers import (
    DEBATERS,
    T0,
    FakeLLM,
    FakeNotifier,
    commit,
    new_event,
    parsed,
    rows,
    tx_for,
)

pytestmark = pytest.mark.skipif(not HAVE_INFRA, reason="TEST_DATABASE_URL/TEST_REDIS_URL not set")

# ---- happy path --------------------------------------------------------------------------


async def test_expense_commits_with_debate_audit_merchant_and_summary(stack):
    from apa.db.models.audit import AgentDebateTranscript, ExplainabilityLog
    from apa.db.models.budget import MerchantCache
    from apa.db.models.ops import InboundEvent, TelegramMessage
    from apa.safety.audit_log import verify_log

    llm, notifier = FakeLLM(), FakeNotifier()
    eid = await new_event(stack, parsed())
    assert await commit(stack, eid, llm, notifier) == "committed"

    tx = await tx_for(stack, eid)
    assert (tx.status, tx.primary_category, tx.execution_mode, tx.direction) == (
        "committed", "Food & Dining", "consensus_auto", "out")
    assert tx.total_paid_amount == tx.net_user_expense == Decimal(18000) and tx.circuit_breaker_cleared
    assert tx.responsible_agent == "A-02" and tx.debate_id is not None
    assert timedelta(hours=23, minutes=59) < tx.reversibility_deadline - datetime.now(UTC) <= timedelta(hours=24)

    assert sorted(a for a, _ in llm.calls) == sorted(DEBATERS)  # all five, always
    turns = await rows(stack, AgentDebateTranscript, AgentDebateTranscript.debate_id == tx.debate_id)
    assert sorted(t.agent_id for t in turns) == sorted(DEBATERS) and all(t.subject_ref == tx.id for t in turns)

    (log,) = await rows(stack, ExplainabilityLog, ExplainabilityLog.transaction_id == tx.id)
    assert (log.decision_type, log.verdict, log.debate_id) == ("ledger_commit", "commit:Food & Dining", tx.debate_id)
    assert sorted(log.agents_invoked) == sorted(DEBATERS) and log.total_tokens == 5 * 120
    assert log.consensus_formula == "weighted_v1" and log.policy_version >= 1

    (m,) = await rows(stack, MerchantCache)
    assert (m.merchant_key, m.visit_count, m.amount_sum, m.default_category) == (
        "kopi kenangan", 1, Decimal(18000), "Food & Dining")

    (tm,) = await rows(stack, TelegramMessage)
    assert tm.kind == "summary" and tm.transaction_id == tx.id and tm.message_id == 101
    assert notifier.sent == [tm.body] and f"/undo_{tm.undo_token}" in tm.body
    assert "looks fine" not in tm.body  # debate content never reaches Telegram
    assert (await rows(stack, InboundEvent, InboundEvent.id == eid))[0].status == "committed"
    async with stack() as s:
        assert await verify_log(s) is None


async def test_debate_prompt_carries_history_budget_and_fences_untrusted_text(stack):
    from apa.db.models.budget import BudgetRule
    from apa.ingestion.merchants import record_visit

    async with stack() as s:
        for i, a in enumerate((20000, 22000, 18000)):
            await record_visit(s, "KOPI KENANGAN", Decimal(a), T0 - timedelta(days=7 * (3 - i)),
                               category="Food & Dining")
        s.add(BudgetRule(category="Food & Dining", period_month=T0.date().replace(day=1),
                         base_limit=Decimal(1500000), current_limit=Decimal(1500000),
                         spent_amount=Decimal(450000)))
        await s.commit()
    llm = FakeLLM()
    await commit(stack, await new_event(stack, parsed()), llm)
    prompt = llm.calls[0][1]
    assert "history: Merchant ini" in prompt and "Rp 20.000" in prompt
    assert "Rp 450.000 / Rp 1.500.000" in prompt
    assert prompt.index("untrusted text begins") < prompt.index("KOPI KENANGAN") < prompt.index("untrusted text ends")


async def test_cash_expense_debits_wallet_and_budget(stack):
    from apa.db.models.budget import BudgetRule
    from apa.db.models.ledger import CashWallet

    async with stack() as s:
        s.add(CashWallet(wallet_name="cash_wallet", current_balance=Decimal(100000)))
        s.add(BudgetRule(category="Food & Dining", period_month=T0.date().replace(day=1),
                         base_limit=Decimal(1000000), current_limit=Decimal(1000000)))
        await s.commit()
    eid = await new_event(stack, parsed(wallet="cash_wallet", channel=None, merchant=None,
                                        description="kopi susu"), source="telegram_shorthand")
    assert await commit(stack, eid) == "committed"
    (w,) = await rows(stack, CashWallet)
    (b,) = await rows(stack, BudgetRule)
    assert w.current_balance == Decimal(82000) and w.last_transaction_ref == (await tx_for(stack, eid)).id
    assert b.spent_amount == Decimal(18000)


async def test_untracked_cash_and_missing_budget_are_not_errors(stack):
    # no cash_wallets row and no budget rule: still commits (nothing to debit)
    eid = await new_event(stack, parsed(wallet="cash_wallet", merchant=None, description="parkir"),
                          source="telegram_shorthand")
    assert await commit(stack, eid) == "committed"


async def test_undo_window_comes_from_policy_rules(stack):
    from sqlalchemy import update

    from apa.db.models.budget import PolicyRule

    async with stack() as s:
        await s.execute(update(PolicyRule).where(PolicyRule.rule_key == "undo_window_hours")
                        .values(value_numeric=1))
        await s.commit()
    tx = await tx_for(stack, await _run(stack, parsed()))
    assert timedelta(minutes=59) < tx.reversibility_deadline - datetime.now(UTC) <= timedelta(hours=1)


async def _run(stack, p, **kw):
    eid = await new_event(stack, p, **kw)
    assert await commit(stack, eid) == "committed"
    return eid


# ---- non-spending movements: no debate ------------------------------------------------------


async def test_income_commits_without_a_debate(stack):
    from apa.db.models.audit import AgentDebateTranscript, ExplainabilityLog
    from apa.db.models.budget import MerchantCache

    llm, notifier = FakeLLM(), FakeNotifier()
    eid = await new_event(stack, parsed(direction="in", merchant="ANDI", channel="transfer"))
    assert await commit(stack, eid, llm, notifier) == "committed"
    tx = await tx_for(stack, eid)
    assert (tx.direction, tx.primary_category, tx.net_user_expense, tx.execution_mode) == (
        "in", "Income", Decimal(0), "deterministic_transfer")
    assert tx.total_paid_amount == Decimal(18000) and tx.debate_id is None
    assert llm.calls == [] and await rows(stack, AgentDebateTranscript) == []
    assert await rows(stack, MerchantCache) == []  # income does not teach spending history
    (log,) = await rows(stack, ExplainabilityLog)
    assert log.agents_invoked == [] and "without a debate" in log.justification
    assert "Pemasukan" in notifier.sent[0]


async def test_atm_withdrawal_credits_cash_wallet_and_is_not_an_expense(stack):
    from apa.db.models.ledger import CashWallet

    async with stack() as s:
        s.add(CashWallet(wallet_name="cash_wallet", current_balance=Decimal(20000)))
        await s.commit()
    llm = FakeLLM()
    eid = await new_event(stack, parsed(amount="500000", wallet="bank_bca", channel="cash_withdrawal",
                                        merchant=None))
    assert await commit(stack, eid, llm) == "committed"
    tx = await tx_for(stack, eid)
    assert (tx.primary_category, tx.net_user_expense, tx.direction) == ("Cash Withdrawal", Decimal(0), "out")
    assert (await rows(stack, CashWallet))[0].current_balance == Decimal(520000)
    assert llm.calls == []


# ---- circuit breakers -----------------------------------------------------------------------


async def _assert_quarantined(stack, eid, reason, llm, notifier):
    from apa.db.models.audit import ExplainabilityLog
    from apa.db.models.budget import MerchantCache
    from apa.ingestion.merchants import normalize_merchant_key

    tx = await tx_for(stack, eid)
    assert (tx.status, tx.execution_mode, tx.circuit_breaker_cleared) == ("quarantined", "circuit_override", False)
    assert tx.notes == f"quarantined: {reason}" and tx.reversibility_deadline is None
    assert llm.calls == [], "a quarantined event must not spend tokens"
    # held spend must not teach merchant history (other, committed merchants may exist)
    learned = {m.merchant_key for m in await rows(stack, MerchantCache)}
    assert normalize_merchant_key(tx.merchant) not in learned
    log = (await rows(stack, ExplainabilityLog, ExplainabilityLog.transaction_id == tx.id))[0]
    assert (log.decision_type, log.verdict) == ("quarantine", f"quarantine:{reason}")
    assert len(notifier.sent) == 1 and notifier.sent[0].startswith("🚨")


async def test_single_tx_ceiling_quarantines_without_lockdown(stack):
    from apa.safety.policy import lockdown_active

    llm, notifier = FakeLLM(), FakeNotifier()
    eid = await new_event(stack, parsed(amount="3000000"))
    assert await commit(stack, eid, llm, notifier) == "quarantined"
    await _assert_quarantined(stack, eid, "single_tx_ceiling", llm, notifier)
    async with stack() as s:
        assert not await lockdown_active(s)


async def test_cash_below_liquidity_floor_leaves_wallet_and_budget_untouched(stack):
    from apa.db.models.budget import BudgetRule
    from apa.db.models.ledger import CashWallet

    async with stack() as s:
        s.add(CashWallet(wallet_name="cash_wallet", current_balance=Decimal(10000)))
        s.add(BudgetRule(category="Food & Dining", period_month=T0.date().replace(day=1),
                         base_limit=Decimal(1000000), current_limit=Decimal(1000000)))
        await s.commit()
    llm, notifier = FakeLLM(), FakeNotifier()
    eid = await new_event(stack, parsed(wallet="cash_wallet", merchant="warkop"))
    assert await commit(stack, eid, llm, notifier) == "quarantined"
    await _assert_quarantined(stack, eid, "liquidity_floor", llm, notifier)
    assert (await rows(stack, CashWallet))[0].current_balance == Decimal(10000)
    assert (await rows(stack, BudgetRule))[0].spent_amount == 0


async def test_deviation_from_merchant_history_quarantines(stack):
    from apa.ingestion.merchants import record_visit

    async with stack() as s:
        for i, a in enumerate((19000, 21000, 20000, 20500, 19500, 20000)):
            await record_visit(s, "KOPI KENANGAN", Decimal(a), T0 - timedelta(days=i + 1))
        await s.commit()
    llm, notifier = FakeLLM(), FakeNotifier()
    eid = await new_event(stack, parsed(amount="200000"))
    assert await commit(stack, eid, llm, notifier) == "quarantined"
    tx = await tx_for(stack, eid)
    assert tx.notes == "quarantined: deviation" and llm.calls == []


async def test_velocity_trips_lockdown_which_then_holds_later_events(stack):
    from apa.db.models.audit import ExplainabilityLog
    from apa.db.models.budget import PolicyRule
    from apa.db.models.ledger import Transaction
    from apa.safety.audit_log import verify_log

    for i in range(5):  # five committed spends in the last 5 minutes
        eid = await new_event(stack, parsed(amount=str(10000 + i), merchant=f"toko {i}"),
                              at=T0 - timedelta(minutes=5 - i))
        assert await commit(stack, eid) == "committed"

    llm, notifier = FakeLLM(), FakeNotifier()
    sixth = await new_event(stack, parsed(amount="10005", merchant="toko 5"), at=T0)
    assert await commit(stack, sixth, llm, notifier) == "quarantined"
    await _assert_quarantined(stack, sixth, "velocity", llm, notifier)
    assert "LOCKDOWN" in notifier.sent[0]
    (flag,) = await rows(stack, PolicyRule, PolicyRule.rule_key == "lockdown_active")
    assert flag.value_numeric == 1 and flag.updated_by == "circuit_breaker"
    cfg = await rows(stack, ExplainabilityLog, ExplainabilityLog.decision_type == "config_change")
    assert [c.verdict for c in cfg] == ["lockdown_active=1"]

    # an hour later the velocity window is long clear, but the lockdown still holds everything
    later = await new_event(stack, parsed(amount="12000", merchant="toko baru"), at=T0 + timedelta(hours=1))
    llm2, notifier2 = FakeLLM(), FakeNotifier()
    assert await commit(stack, later, llm2, notifier2) == "quarantined"
    assert llm2.calls == [] and (await tx_for(stack, later)).notes == "quarantined: lockdown"
    assert len(await rows(stack, Transaction, Transaction.status == "committed")) == 5
    async with stack() as s:
        assert await verify_log(s) is None


async def test_income_still_commits_during_lockdown(stack):
    from apa.safety.policy import set_lockdown

    async with stack() as s:
        await set_lockdown(s, True, by="test")
        await s.commit()
    eid = await new_event(stack, parsed(direction="in", merchant="GAJI"))
    assert await commit(stack, eid) == "committed"


# ---- debate outage / low consensus ------------------------------------------------------------


async def test_total_debate_outage_retries_then_holds_the_transaction_on_the_last_try(stack):
    from apa.db.models.audit import AgentDebateTranscript, ExplainabilityLog
    from apa.db.models.budget import MerchantCache
    from apa.db.models.ops import InboundEvent, TelegramMessage
    from apa.pipeline.commit import DebateUnavailable

    down, notifier = FakeLLM(fail={"*"}), FakeNotifier()
    eid = await new_event(stack, parsed())
    for attempt in (1, 2):
        with pytest.raises(DebateUnavailable):
            await commit(stack, eid, down, notifier, job_try=attempt, max_tries=3)
        assert (await rows(stack, InboundEvent, InboundEvent.id == eid))[0].status == "processing"
    assert notifier.sent == []  # nothing is announced until it is decided

    # no quorum after the last try: HELD, never committed on zero votes
    assert await commit(stack, eid, down, notifier, job_try=3, max_tries=3) == "quarantined"
    tx = await tx_for(stack, eid)
    assert (tx.status, tx.notes, tx.circuit_breaker_cleared) == ("quarantined", "quarantined: no_quorum", False)
    assert await rows(stack, AgentDebateTranscript) == [] and await rows(stack, MerchantCache) == []
    (log,) = await rows(stack, ExplainabilityLog, ExplainabilityLog.transaction_id == tx.id)
    assert log.verdict == "quarantine:no_quorum" and "0/5 agents answered, quorum is 3" in log.justification
    (alert,) = await rows(stack, TelegramMessage, TelegramMessage.kind == "alert")
    assert f"/release_{alert.undo_token}" in notifier.sent[0] and "kuorum" in notifier.sent[0]


async def test_partial_outage_still_decides_from_the_agents_that_answered(stack):
    from apa.db.models.audit import AgentDebateTranscript

    eid = await new_event(stack, parsed())
    assert await commit(stack, eid, FakeLLM(fail={"A-03", "A-04"})) == "committed"
    assert len(await rows(stack, AgentDebateTranscript)) == 3
    assert (await tx_for(stack, eid)).primary_category == "Food & Dining"


async def test_low_consensus_is_committed_but_flagged(stack):
    notifier = FakeNotifier()
    eid = await new_event(stack, parsed())
    assert await commit(stack, eid, FakeLLM(score=0.1), notifier) == "committed"
    assert (await tx_for(stack, eid)).notes == "flagged: low consensus" and "⚠" in notifier.sent[0]


async def test_unexpected_error_marks_the_event_failed_and_reraises(stack, monkeypatch):
    from apa.db.models.ops import InboundEvent
    from apa.pipeline import commit as mod

    async def boom(*a, **k):
        raise ValueError("kaboom")

    monkeypatch.setattr(mod, "run_debate", boom)
    eid = await new_event(stack, parsed())
    with pytest.raises(ValueError):
        await commit(stack, eid)
    ev = (await rows(stack, InboundEvent, InboundEvent.id == eid))[0]
    assert ev.status == "failed" and "kaboom" in ev.error


# ---- idempotency, concurrency, audit integrity -------------------------------------------------


async def test_rerunning_a_finished_event_does_nothing(stack):
    from apa.db.models.ledger import Transaction

    notifier = FakeNotifier()
    eid = await new_event(stack, parsed())
    assert await commit(stack, eid, notifier=notifier) == "committed"
    assert await commit(stack, eid, notifier=notifier) == "committed"
    assert await commit(stack, eid, notifier=notifier, job_try=2) == "committed"
    assert len(await rows(stack, Transaction)) == 1 and len(notifier.sent) == 1


async def test_two_workers_racing_on_one_event_write_exactly_one_transaction(stack):
    from apa.db.models.ledger import Transaction

    eid = await new_event(stack, parsed())
    results = await asyncio.gather(commit(stack, eid), commit(stack, eid))
    assert sorted(results) == ["committed", "processing"]
    assert len(await rows(stack, Transaction)) == 1


async def test_concurrent_commits_keep_the_audit_chain_linear(stack):
    from apa.db.models.audit import ExplainabilityLog
    from apa.safety.audit_log import verify_log

    ids = [await new_event(stack, parsed(amount=str(10000 + i), merchant=f"toko {i}"),
                           at=T0 + timedelta(minutes=20 * i)) for i in range(6)]
    assert set(await asyncio.gather(*(commit(stack, i) for i in ids))) == {"committed"}
    log = await rows(stack, ExplainabilityLog)
    assert len(log) == 6
    assert len({r.prev_hash for r in log}) == 6, "two commits read the same prev_hash: the chain forked"
    async with stack() as s:
        assert await verify_log(s) is None


async def test_audit_verification_detects_tampering(stack):
    from sqlalchemy import text, update

    from apa.db.models.audit import ExplainabilityLog
    from apa.safety.audit_log import verify_log

    for i in range(3):
        await _run(stack, parsed(amount=str(10000 + i), merchant=f"toko {i}"), at=T0 + timedelta(minutes=20 * i))
    victim = (await rows(stack, ExplainabilityLog))[1]
    async with stack() as s:  # the trigger blocks this for everyone; the owner can still disable it
        await s.execute(text("ALTER TABLE explainability_log DISABLE TRIGGER explainability_log_append_only"))
        await s.execute(update(ExplainabilityLog).where(ExplainabilityLog.id == victim.id)
                        .values(verdict="commit:Rewritten"))
        await s.execute(text("ALTER TABLE explainability_log ENABLE TRIGGER explainability_log_append_only"))
        await s.commit()
    async with stack() as s:
        assert await verify_log(s) == victim.id


# ---- worker wiring -----------------------------------------------------------------------------


async def test_worker_end_to_end_from_raw_notification_text(stack, monkeypatch):
    from apa.core.sanitize import sanitize_untrusted
    from apa.db.models.ops import InboundEvent
    from apa.ingestion.intake import store_and_enqueue
    from apa.workers import settings as worker

    monkeypatch.setattr(worker, "SessionLocal", stack)

    class Q:
        jobs: list[tuple] = []

        async def enqueue_job(self, name, *args):
            self.jobs.append((name, *args))

    q = Q()
    raw = "Pembayaran QRIS Rp18.000 ke KOPI KENANGAN rek 1234567890 berhasil."
    async with stack() as s:
        eid = await store_and_enqueue(
            s, q, source="android_listener", idempotency_key="e2e-p3",
            payload={"package": "com.gojek.app", "text": sanitize_untrusted(raw).text,
                     "posted_at": T0.isoformat()}, clean=sanitize_untrusted(raw))
    assert q.jobs == [("process_inbound_event", eid)]

    ctx = {"redis": q, "sessionmaker": stack, "llm": FakeLLM(), "notifier": FakeNotifier()}
    assert await worker.process_inbound_event(ctx, eid) == "parsed"
    assert q.jobs[-1] == ("commit_event", eid)  # handed off only after the parse was committed
    assert await worker.commit_event(ctx, eid) == "committed"
    assert (await rows(stack, InboundEvent, InboundEvent.id == eid))[0].status == "committed"
    assert "1234567890" not in ctx["notifier"].sent[0]


async def test_worker_turns_a_debate_outage_into_an_arq_retry(stack):
    from arq import Retry

    from apa.workers import settings as worker

    eid = await new_event(stack, parsed())
    ctx = {"sessionmaker": stack, "llm": FakeLLM(fail={"*"}), "notifier": FakeNotifier(), "job_try": 1}
    with pytest.raises(Retry) as exc:
        await worker.commit_event(ctx, eid)
    assert exc.value.defer_score == worker.DEBATE_RETRY_DELAY_S * 1000  # arq stores ms
    ctx["job_try"] = worker.COMMIT_MAX_TRIES
    assert await worker.commit_event(ctx, eid) == "quarantined"  # out of retries, still no quorum
