"""Today's budget, worked out from the monthly budgets: what is left in each, spread over the days that remain (today
included). Pure SQL and arithmetic; no model. Sinking funds are savings, not spending room, so they are left out."""

import calendar
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from apa.db.models.budget import BudgetRule
from apa.db.models.ledger import Transaction


@dataclass(frozen=True)
class DailyBudget:
    per_day: Decimal  # all categories together
    remaining: Decimal  # left this month, all categories
    days_left: int  # including today
    spent_today: Decimal
    categories: list[tuple[str, Decimal, Decimal]]  # (category, per day, left this month), most room first


async def daily_budget(sm: async_sessionmaker, tz: ZoneInfo, now: datetime) -> DailyBudget | None:
    """None when there are no budgets this month."""
    local = now.astimezone(tz)
    month = local.date().replace(day=1)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    days_left = calendar.monthrange(local.year, local.month)[1] - local.day + 1
    async with sm() as s:
        rules = (
            await s.execute(select(BudgetRule).where(BudgetRule.period_month == month, BudgetRule.is_sinking_fund.is_(False)))
        ).scalars().all()
        spent_today = (
            await s.execute(
                select(func.coalesce(func.sum(Transaction.total_paid_amount), 0)).where(
                    Transaction.status == "committed", Transaction.direction == "out",
                    Transaction.timestamp >= start, Transaction.timestamp < start + timedelta(days=1),
                )
            )
        ).scalar_one()
    if not rules:
        return None
    left = [(r.category, max(r.current_limit - r.spent_amount, Decimal(0))) for r in rules]
    cats = sorted(((c, rem / days_left, rem) for c, rem in left if rem > 0), key=lambda x: -x[1])
    remaining = sum((rem for _, rem in left), Decimal(0))
    return DailyBudget((remaining / days_left).quantize(Decimal("1")), remaining, days_left, Decimal(spent_today), cats)
