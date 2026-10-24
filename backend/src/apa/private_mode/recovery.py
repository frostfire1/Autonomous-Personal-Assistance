"""Autonomous stress-recovery allocation (blueprint 4.3 step 2): when the mental-health agent judges a session to
be acute stress, a small amount moves from a discretionary budget to the Mental Care budget. No transcript and
no narrative reach the database: the audit rows say only that a stress-recovery reallocation happened.

Bounds: never more than 15% of the source's base limit (the elastic band, blueprint 4.4b), never below what the
source has already spent, never more than RECOVERY_CAP in one session."""

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from apa.core.format import rupiah
from apa.db.models.budget import BudgetRebalanceLog, BudgetRule
from apa.safety.audit_log import append_explainability

RECOVERY_CATEGORY = "Mental Care"
SOURCES = ("Entertainment", "Social", "Shopping")  # discretionary, in the order they give first
RECOVERY_CAP = Decimal(200_000)


async def reallocate(
    sm: async_sessionmaker, requested: Decimal, tz: ZoneInfo, now: datetime | None = None
) -> tuple[Decimal, str] | None:
    """-> (amount moved, source category), or None when nothing could move (no discretionary budget with room)."""
    want = min(Decimal(requested), RECOVERY_CAP)
    if want <= 0:
        return None
    month = (now or datetime.now(tz)).astimezone(tz).date().replace(day=1)
    async with sm() as s:
        for name in SOURCES:
            src = (
                await s.execute(
                    select(BudgetRule)
                    .where(
                        BudgetRule.category == name,
                        BudgetRule.period_month == month,
                        BudgetRule.is_sinking_fund.is_(False),
                        BudgetRule.is_essential.is_(False),
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if src is None:
                continue
            floor = src.base_limit * (1 - src.elastic_band_pct / 100)
            room = min(src.current_limit - floor, src.current_limit - src.spent_amount)
            amount = min(want, room).quantize(Decimal("1"))
            if amount <= 0:
                continue
            dst = (
                await s.execute(
                    select(BudgetRule)
                    .where(BudgetRule.category == RECOVERY_CATEGORY, BudgetRule.period_month == month)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if dst is None:  # a sinking fund: it is allowed to grow beyond the elastic band
                dst = BudgetRule(
                    category=RECOVERY_CATEGORY,
                    period_month=month,
                    base_limit=Decimal(0),
                    current_limit=Decimal(0),
                    spent_amount=Decimal(0),
                    is_sinking_fund=True,
                )
                s.add(dst)
                await s.flush()
            before_src, before_dst = src.current_limit, dst.current_limit
            src.current_limit -= amount
            dst.current_limit += amount
            log = await append_explainability(
                s,
                decision_type="budget_rebalance",
                verdict="rebalance:stress_recovery",
                # neutral by design: the reason is a category of trigger, never what the owner said
                justification=f"stress recovery reallocation {rupiah(amount)}: {name} -> {RECOVERY_CATEGORY}",
            )
            s.add(
                BudgetRebalanceLog(
                    trigger_type="stress_shield",
                    from_category=name,
                    to_category=RECOVERY_CATEGORY,
                    period_month=month,
                    amount=amount,
                    from_limit_before=before_src,
                    from_limit_after=src.current_limit,
                    to_limit_before=before_dst,
                    to_limit_after=dst.current_limit,
                    trigger_ref=None,
                    explainability_id=log.id,
                )
            )
            await s.commit()
            return amount, name
    return None
