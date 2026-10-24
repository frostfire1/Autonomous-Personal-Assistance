from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

from apa.agents.arbiter import AgentVote, resolve
from apa.pipeline.candidate import Candidate
from apa.pipeline.commit import (
    Gathered,
    build_alert,
    build_justification,
    build_summary,
    decide_breakers,
)
from apa.safety.breakers import BreakerPolicy

T = datetime(2026, 10, 23, 5, 0, tzinfo=UTC)


def cand(**kw):
    base = dict(source_channel="android_listener", wallet="gopay", direction="out", amount=Decimal(18000),
                fee=Decimal(0), merchant="KOPI KENANGAN", description=None, channel="qris", timestamp=T,
                parse_confidence=1.0)
    return Candidate(**{**base, **kw})


def gathered(**kw):
    base = dict(policy=BreakerPolicy(), policy_version=1, undo_hours=24, lockdown=False,
                recent_times=[T], cash_balance=None)
    return Gathered(**{**base, **kw})


def test_normal_expense_is_clear():
    assert not decide_breakers(cand(), gathered()).tripped


def test_lockdown_holds_expenses_and_withdrawals_but_never_income():
    g = gathered(lockdown=True)
    assert decide_breakers(cand(), g).reason == "lockdown"
    assert decide_breakers(cand(channel="cash_withdrawal"), g).reason == "lockdown"
    assert not decide_breakers(cand(direction="in"), g).tripped


def test_income_skips_even_a_huge_amount():
    assert not decide_breakers(cand(direction="in", amount=Decimal(50_000_000)), gathered()).tripped


def test_unknown_bank_balance_never_trips_the_liquidity_floor():
    g = gathered(policy=BreakerPolicy(liquidity_floor=Decimal(10**9)), cash_balance=None)
    assert not decide_breakers(cand(wallet="bank_bca"), g).tripped


def test_cash_expense_below_floor_is_rejected():
    g = gathered(cash_balance=Decimal(10000))
    r = decide_breakers(cand(wallet="cash_wallet", amount=Decimal(18000)), g)
    assert (r.reason, r.action) == ("liquidity_floor", "reject")


def test_cash_withdrawal_does_not_check_liquidity():
    g = gathered(cash_balance=Decimal(0))
    assert not decide_breakers(cand(wallet="bank_bca", channel="cash_withdrawal"), g).tripped


def test_withdrawal_over_the_ceiling_is_quarantined():
    r = decide_breakers(cand(channel="cash_withdrawal", amount=Decimal(3_000_000)), gathered())
    assert r.reason == "single_tx_ceiling"


def test_merchant_deviation_uses_history_only_when_provided():
    g = gathered(merchant_avg=Decimal(20000), merchant_std=Decimal(2000))
    assert decide_breakers(cand(amount=Decimal(40000)), g).reason == "deviation"
    assert not decide_breakers(cand(amount=Decimal(40000)), gathered()).tripped


def test_summary_expense_matches_blueprint_shape():
    s = build_summary(cand(), "Food & Dining", "A-02", "8f9a2b", flagged=False)
    assert s == "✅ Dicatat: KOPI KENANGAN · Rp 18.000 · Pos: Food & Dining (A-02) · Kasir: GoPay · Undo: /undo_8f9a2b"


def test_summary_variants():
    assert "Pemasukan" in build_summary(cand(direction="in"), "Income", None, "aaaaaa", False)
    assert "Tarik tunai" in build_summary(cand(channel="cash_withdrawal"), "Cash Withdrawal", None, "aaaaaa", False)
    assert "⚠ konsensus rendah" in build_summary(cand(), "Other", "A-02", "aaaaaa", flagged=True)


def test_alert_text_per_reason():
    assert "LOCKDOWN" in build_alert(cand(), "velocity")
    assert "lockdown aktif" in build_alert(cand(), "lockdown")
    assert "(single_tx_ceiling)" in build_alert(cand(), "single_tx_ceiling")


def test_arbiter_lead_agent_is_the_heaviest_voice_for_the_winning_category():
    votes = [AgentVote("A-02", Decimal("0.9"), Decimal("1"), "Food"),
             AgentVote("A-05", Decimal("0.9"), Decimal("1"), "Food"),
             AgentVote("A-07", Decimal("0.9"), Decimal("1"), "Savings")]
    c = resolve(votes, None)
    assert c.category == "Food" and c.lead_agent == "A-02"  # A-02 carries the largest weight


def test_justification_records_every_vote_and_flags():
    votes = [AgentVote("A-02", Decimal("0.1"), Decimal("1"), "X")]
    c = resolve(votes, None)
    turn = SimpleNamespace(agent_id="A-02", vote=votes[0], argument="too expensive")
    text = build_justification(c, [turn], "X")
    assert "A-02 score=0.1" in text and "too expensive" in text and "flagged" in text
    assert "no agent produced a valid vote" in build_justification(resolve([], None), [], "Uncategorized")
