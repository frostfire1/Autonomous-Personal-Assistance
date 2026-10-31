"""/undo (soft reversal, decisions.md D3), dismissing a quarantined transaction, and clearing
the emergency lockdown. Used by the Telegram commands today and the Web UI later (`via`).

Nothing here deletes a row. A reversal flips the status, subtracts exactly what the commit
added, and appends an audit row, all in one transaction.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apa.core.format import rupiah
from apa.core.settings import get_settings
from apa.db.models.ledger import Transaction, TransactionSplit
from apa.db.models.ops import TelegramMessage
from apa.ingestion.merchants import remove_visit
from apa.pipeline.effects import apply_effects, tx_kind
from apa.safety.audit_log import append_explainability
from apa.safety.policy import lockdown_active, set_lockdown


@dataclass(frozen=True)
class UndoResult:
    status: str  # ok | not_found | wrong_state | expired
    transaction_id: int | None = None
    label: str | None = None  # "KOPI KENANGAN · Rp 18.000", for the reply text


def label_of(tx: Transaction) -> str:
    return f"{tx.merchant or 'Transaksi'} · {rupiah(tx.total_paid_amount)}"


async def resolve_token(s: AsyncSession, token: str) -> int | None:
    """Short `/undo_<token>`, `/release_<token>` or `/dismiss_<token>` id -> transaction id."""
    return (
        await s.execute(
            select(TelegramMessage.transaction_id).where(TelegramMessage.undo_token == token.lower())
        )
    ).scalar_one_or_none()


async def _locked_tx(s: AsyncSession, tx_id: int) -> Transaction | None:
    return (
        await s.execute(select(Transaction).where(Transaction.id == tx_id).with_for_update())
    ).scalar_one_or_none()


async def undo_transaction(
    sm: async_sessionmaker,
    tx_id: int,
    *,
    via: str = "telegram",
    tz: ZoneInfo | None = None,
    now: datetime | None = None,
) -> UndoResult:
    """Reverse a committed transaction inside its reversibility window."""
    tz = tz or ZoneInfo(get_settings().timezone)
    now = now or datetime.now(UTC)
    async with sm() as s:
        tx = await _locked_tx(s, tx_id)  # also serialises two concurrent /undo taps
        if tx is None:
            return UndoResult("not_found")
        if tx.status != "committed":
            return UndoResult("wrong_state", tx.id, label_of(tx))
        if tx.reversibility_deadline is None or now > tx.reversibility_deadline:
            return UndoResult("expired", tx.id, label_of(tx))

        tx.status, tx.rolled_back_at, tx.rolled_back_via = "rolled_back", now, via
        await s.flush()
        await apply_effects(s, tx, sign=-1, tz=tz, now=now)
        if (
            tx_kind(tx.direction, tx.execution_mode) == "expense"
            and tx.merchant
            and tx.execution_mode != "reconciliation_catchup"
        ):
            await remove_visit(s, tx.merchant, tx.net_user_expense)
        await s.execute(  # money the user fronted for others is no longer owed
            update(TransactionSplit)
            .where(TransactionSplit.transaction_id == tx.id, TransactionSplit.settlement_status == "pending")
            .values(settlement_status="waived")
        )
        await append_explainability(
            s,
            decision_type="undo",
            transaction_id=tx.id,
            verdict="undo:rolled_back",
            justification=f"/undo via {via}: reversed wallet, budget and merchant statistics, "
            f"waived pending splits",
        )
        label = label_of(tx)
        await s.commit()
        return UndoResult("ok", tx.id, label)


async def dismiss_quarantined(
    sm: async_sessionmaker, tx_id: int, *, via: str = "telegram", now: datetime | None = None
) -> UndoResult:
    """Discard a held transaction. Nothing was ever applied to wallets or budgets, so there is
    nothing to reverse: the row just becomes rolled_back."""
    now = now or datetime.now(UTC)
    async with sm() as s:
        tx = await _locked_tx(s, tx_id)
        if tx is None:
            return UndoResult("not_found")
        if tx.status != "quarantined":
            return UndoResult("wrong_state", tx.id, label_of(tx))
        was = (tx.notes or "").removeprefix("quarantined: ")
        tx.status, tx.rolled_back_at, tx.rolled_back_via = "rolled_back", now, via
        await append_explainability(
            s,
            decision_type="dismiss",
            transaction_id=tx.id,
            verdict="dismiss:quarantined",
            justification=f"quarantined transaction ({was}) dismissed by owner via {via}",
        )
        label = label_of(tx)
        await s.commit()
        return UndoResult("ok", tx.id, label)


async def clear_lockdown(sm: async_sessionmaker, *, by: str = "telegram") -> bool:
    """Switch the emergency lockdown off. False if it was not active (and nothing is written)."""
    async with sm() as s:
        if not await lockdown_active(s):
            return False
        await set_lockdown(s, False, by=by)
        await append_explainability(
            s,
            decision_type="config_change",
            verdict="lockdown_active=0",
            justification=f"emergency lockdown cleared by owner via {by}",
        )
        await s.commit()
        return True
