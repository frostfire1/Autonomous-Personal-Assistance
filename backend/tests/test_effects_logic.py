from datetime import UTC, datetime
from decimal import Decimal

import pytest

from apa.pipeline.candidate import Candidate
from apa.pipeline.commit import build_alert
from apa.pipeline.effects import cash_delta, tx_kind

D = Decimal


@pytest.mark.parametrize(
    ("direction", "mode", "kind"),
    [
        ("out", "consensus_auto", "expense"),
        ("in", "deterministic_transfer", "income"),
        ("out", "deterministic_transfer", "withdrawal"),
    ],
)
def test_tx_kind_from_stored_columns(direction, mode, kind):
    assert tx_kind(direction, mode) == kind


@pytest.mark.parametrize(
    ("kind", "wallet", "total", "net", "delta"),
    [
        ("expense", "cash_wallet", 18000, 18000, -18000),
        ("expense", "gopay", 18000, 18000, 0),  # only the physical wallet is tracked
        ("income", "cash_wallet", 50000, 0, 50000),
        ("income", "bank_bca", 50000, 0, 0),
        ("withdrawal", "bank_bca", 506500, 6500, 500000),  # the fee is not cash in hand
        ("withdrawal", "bank_bca", 500000, 0, 500000),
    ],
)
def test_cash_delta(kind, wallet, total, net, delta):
    assert cash_delta(kind, wallet, D(total), D(net)) == D(delta)


def test_every_applied_effect_has_an_exact_inverse():
    # commit applies +delta, undo applies -delta: the pair must always cancel
    for kind, wallet, total, net in [
        ("expense", "cash_wallet", D(18000), D(18000)),
        ("income", "cash_wallet", D(50000), D(0)),
        ("withdrawal", "bank_bca", D(506500), D(6500)),
    ]:
        d = cash_delta(kind, wallet, total, net)
        assert d + (-d) == 0


def _c():
    return Candidate(
        source_channel="android_listener", wallet="gopay", direction="out", amount=D(18000), fee=D(0),
        merchant="KOPI", description=None, channel="qris", timestamp=datetime(2026, 10, 23, tzinfo=UTC),
        parse_confidence=1.0,
    )


def test_alert_with_token_offers_release_and_dismiss():
    text = build_alert(_c(), "single_tx_ceiling", "abc123")
    assert "/release_abc123" in text and "/dismiss_abc123" in text and "/unlock" not in text


def test_lockdown_alerts_also_offer_unlock():
    assert "/unlock" in build_alert(_c(), "velocity", "abc123")
    assert "/unlock" in build_alert(_c(), "lockdown", "abc123")


def test_alert_without_a_token_has_no_commands():
    assert "/release" not in build_alert(_c(), "deviation")
