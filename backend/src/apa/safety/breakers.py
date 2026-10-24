"""Hard-stop circuit breakers (blueprint 4.5). Pure functions so they are trivially testable.

Thresholds come from policy_rules at call time; the defaults below are the blueprint seeds.
Breakers run BEFORE the debate (decisions.md D2) so quarantined events spend no tokens.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal


@dataclass(frozen=True)
class BreakerPolicy:
    velocity_max_tx: int = 5
    velocity_window_min: int = 10
    single_tx_ceiling: Decimal = Decimal("2500000")
    liquidity_floor: Decimal = Decimal("0")
    deviation_sigma: Decimal = Decimal("3")


@dataclass(frozen=True)
class BreakerResult:
    tripped: bool
    reason: str | None = None  # 'velocity' | 'single_tx_ceiling' | 'liquidity_floor' | 'deviation'
    action: str | None = None  # 'lockdown' | 'quarantine' | 'reject'

    @classmethod
    def ok(cls) -> "BreakerResult":
        return cls(False)


def check_velocity(recent_tx_times: list[datetime], now: datetime, p: BreakerPolicy) -> BreakerResult:
    """More than `velocity_max_tx` transactions inside the window -> emergency lockdown.

    `recent_tx_times` should include the candidate transaction being evaluated.
    """
    window_start = now - timedelta(minutes=p.velocity_window_min)
    n = sum(1 for t in recent_tx_times if window_start <= t <= now)
    if n > p.velocity_max_tx:
        return BreakerResult(True, "velocity", "lockdown")
    return BreakerResult.ok()


def check_single_tx_ceiling(amount: Decimal, p: BreakerPolicy) -> BreakerResult:
    if amount > p.single_tx_ceiling:
        return BreakerResult(True, "single_tx_ceiling", "quarantine")
    return BreakerResult.ok()


def check_liquidity_floor(
    balance_after: Decimal, p: BreakerPolicy, *, is_expense: bool = True
) -> BreakerResult:
    if is_expense and balance_after < p.liquidity_floor:
        return BreakerResult(True, "liquidity_floor", "reject")
    return BreakerResult.ok()


def check_deviation(
    amount: Decimal, avg: Decimal | None, stddev: Decimal | None, p: BreakerPolicy
) -> BreakerResult:
    """Flag amounts more than `deviation_sigma` standard deviations above the merchant mean.

    Needs merchant history; with no (or zero-variance) history the check is skipped.
    """
    if avg is None or stddev is None or stddev <= 0:
        return BreakerResult.ok()
    if (amount - avg) / stddev > p.deviation_sigma:
        return BreakerResult(True, "deviation", "quarantine")
    return BreakerResult.ok()


def evaluate(
    *,
    amount: Decimal,
    balance_after: Decimal,
    recent_tx_times: list[datetime],
    now: datetime,
    merchant_avg: Decimal | None = None,
    merchant_stddev: Decimal | None = None,
    policy: BreakerPolicy = BreakerPolicy(),
    is_expense: bool = True,
) -> BreakerResult:
    """First tripped breaker wins; order is severity: lockdown, reject, quarantine."""
    for result in (
        check_velocity(recent_tx_times, now, policy),
        check_liquidity_floor(balance_after, policy, is_expense=is_expense),
        check_single_tx_ceiling(amount, policy),
        check_deviation(amount, merchant_avg, merchant_stddev, policy),
    ):
        if result.tripped:
            return result
    return BreakerResult.ok()
