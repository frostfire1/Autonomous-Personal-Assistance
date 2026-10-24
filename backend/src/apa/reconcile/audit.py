"""The weekly ledger self-check (A-11, Sunday night). It needs no statement: it looks for things that should never
be true in the ledger itself and tells the owner, once, only when it finds one. It changes nothing (no deletes, no
edits): a duplicate pair is reported with both ids so the owner (or the chat) can undo the wrong one.

  * two committed expenses with the same wallet, amount and counterparty within 5 minutes (a double count);
  * a physical cash wallet below zero (more cash spent than was ever recorded as withdrawn)."""

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from apa.core.format import rupiah
from apa.db.models.ledger import CashWallet, Transaction
from apa.ingestion.merchants import normalize_merchant_key
from apa.reporting.digest import deliver

WINDOW = timedelta(days=14)
GAP = timedelta(minutes=5)
MAX_LISTED = 5


async def find_duplicates(sm: async_sessionmaker, now: datetime) -> list[tuple[Transaction, Transaction]]:
    async with sm() as s:
        txs = (
            (
                await s.execute(
                    select(Transaction)
                    .where(
                        Transaction.status == "committed",
                        Transaction.direction == "out",
                        Transaction.timestamp >= now - WINDOW,
                    )
                    .order_by(Transaction.timestamp)
                )
            )
            .scalars()
            .all()
        )
    pairs: list[tuple[Transaction, Transaction]] = []
    for i, a in enumerate(txs):
        key = (a.payment_wallet, a.total_paid_amount, normalize_merchant_key(a.merchant or ""))
        for b in txs[i + 1 :]:
            if b.timestamp - a.timestamp > GAP:
                break
            if (
                b.payment_wallet,
                b.total_paid_amount,
                normalize_merchant_key(b.merchant or ""),
            ) == key and key[2]:
                pairs.append((a, b))
    return pairs


async def weekly_audit(sm: async_sessionmaker, notifier: Any, redis: Any | None, now: datetime) -> list[str]:
    """-> the lines reported (empty when the ledger is clean, in which case nothing is sent)."""
    lines = [
        f"• #{a.id} dan #{b.id}: {a.merchant or 'transaksi'} {rupiah(a.total_paid_amount)} dicatat dua kali dalam 5 menit"
        for a, b in (await find_duplicates(sm, now))[:MAX_LISTED]
    ]
    async with sm() as s:
        negative = (await s.execute(select(CashWallet).where(CashWallet.current_balance < 0))).scalars().all()
    lines += [
        f"• dompet {w.wallet_name} minus {rupiah(-w.current_balance)}: ada tarik tunai yang belum tercatat?"
        for w in negative
    ]
    if lines:
        year, week, _ = now.isocalendar()
        await deliver(
            sm,
            notifier,
            redis,
            f"audit:{year}-W{week:02d}",
            "🔎 Pemeriksaan mingguan buku kas:\n" + "\n".join(lines),
        )
    return lines
