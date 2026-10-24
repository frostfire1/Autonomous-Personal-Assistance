"""Wallet and budget effects of a committed transaction, applied with sign +1 on commit and
sign -1 on /undo. One function for both directions keeps a reversal exactly symmetric with the
original, and the commit/undo paths take row locks in the same order (transaction, wallet, budget,
merchant, then the audit-chain advisory lock last) so they cannot deadlock each other.
"""

from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession

from apa.db.models.budget import BudgetRule
from apa.db.models.ledger import CashWallet, Transaction
from apa.pipeline.candidate import CASH_WALLET


def month_start(ts: datetime, tz: ZoneInfo) -> date:
    return ts.astimezone(tz).date().replace(day=1)


def tx_kind(direction: str, execution_mode: str) -> str:
    """Recover 'expense' | 'income' | 'withdrawal' from a stored row. Only meaningful for
    committed rows: quarantined ones were never applied."""
    if execution_mode != "deterministic_transfer":
        return "expense"
    return "income" if direction == "in" else "withdrawal"


def cash_delta(kind: str, wallet: str, total: Decimal, net: Decimal) -> Decimal:
    """Change to the physical cash wallet when the transaction is applied.

    A withdrawal stores its bank fee as `net_user_expense`, so the cash drawn is `total - net`.
    """
    if kind == "withdrawal":
        return total - net
    if wallet != CASH_WALLET:
        return Decimal(0)
    return -total if kind == "expense" else total


async def apply_effects(
    s: AsyncSession, tx: Transaction, *, sign: int, tz: ZoneInfo, now: datetime
) -> None:
    """sign=+1 applies the transaction, sign=-1 reverses it. Needs tx.id (flush first)."""
    kind = tx_kind(tx.direction, tx.execution_mode)
    delta = cash_delta(kind, tx.payment_wallet, tx.total_paid_amount, tx.net_user_expense) * sign
    if delta != 0:  # no-op when cash is not tracked (no cash_wallets row)
        await s.execute(
            update(CashWallet)
            .where(CashWallet.wallet_name == CASH_WALLET)
            .values(
                current_balance=CashWallet.current_balance + delta,
                last_transaction_ref=tx.id,
                updated_at=now,
            )
        )
    if kind == "expense" and tx.net_user_expense:
        spent = BudgetRule.spent_amount + tx.net_user_expense * sign
        await s.execute(
            update(BudgetRule)
            .where(
                BudgetRule.category == tx.primary_category,
                BudgetRule.period_month == month_start(tx.timestamp, tz),
            )
            # A budget created after the spend never counted it, so a reversal must not push
            # the running total below zero.
            .values(spent_amount=func.greatest(spent, 0) if sign < 0 else spent)
        )


async def recategorize(
    s: AsyncSession, tx: Transaction, new_category: str, *, tz: ZoneInfo, now: datetime
) -> None:
    """Move a committed expense between budgets: out of the old category, change it, into the new.
    The caller holds the transaction's row lock and writes the audit row."""
    await apply_effects(s, tx, sign=-1, tz=tz, now=now)
    tx.primary_category = new_category
    await s.flush()
    await apply_effects(s, tx, sign=+1, tz=tz, now=now)
