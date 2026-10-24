"""A-11 writes: the audit rows (reconciliation_log) and the entries a reconciliation creates. Entries are flagged so
they can always be told apart from what the owner or the listener recorded: source 'reconciliation', execution
mode 'reconciliation_catchup', auto_reconciled true. Each is committed in one transaction with its wallet/budget
effects and an audit row, exactly like any other ledger write."""

import re
import uuid
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from apa.core.format import rupiah
from apa.db.models.audit import ReconciliationLog
from apa.db.models.ledger import Transaction
from apa.ingestion.merchants import find_merchant
from apa.pipeline.effects import apply_effects
from apa.reconcile.match import Entry, Line, Plan
from apa.safety.audit_log import append_explainability

FEES_CATEGORY = "Bank Fees"
INCOME_CATEGORY = "Income"
UNDO_HOURS = 24


def ledger_entries(txs: list[Transaction], tz: ZoneInfo) -> list[Entry]:
    return [
        Entry(t.id, t.timestamp.astimezone(tz).date(), t.total_paid_amount, t.direction, t.merchant)
        for t in txs
    ]


async def ledger_window(sm: async_sessionmaker, wallet: str, start, end, tz: ZoneInfo) -> list[Transaction]:
    """Committed transactions of the wallet in [start - 2 days, end + 2 days]: the tolerance both sides."""
    lo = datetime.combine(start - timedelta(days=2), time.min, tzinfo=tz)
    hi = datetime.combine(end + timedelta(days=3), time.min, tzinfo=tz)
    async with sm() as s:
        return list(
            (
                await s.execute(
                    select(Transaction).where(
                        Transaction.payment_wallet == wallet,
                        Transaction.status == "committed",
                        Transaction.timestamp >= lo,
                        Transaction.timestamp < hi,
                    )
                )
            ).scalars()
        )


def clean_name(description: str) -> str:
    """'TRSF E-BANKING DB 2310/FTSCY/WS95031 KOPI KENANGAN' -> a short readable counterparty."""
    t = re.sub(r"\b\d[\d/.\-]*\b", " ", description)
    t = re.sub(
        r"(?i)\b(trsf|trf|transfer|e-?banking|bi-?fast|ftscy|ws\d*|db|cr|dr|qris|tarik tunai|pembayaran|payment|"
        r"pmt|dari|ke|kepada|sdr|sdri)\b",
        " ",
        t,
    )
    t = re.sub(r"[^\w &'.-]+", " ", t)  # the slashes and codes that held the numbers
    return " ".join(t.split())[:60] or "Mutasi bank"


async def _new_entry(
    sm,
    *,
    wallet: str,
    line: Line,
    amount: Decimal,
    direction: str,
    merchant: str,
    category: str,
    reason: str,
    tz: ZoneInfo,
    note: str,
) -> int:
    now = datetime.now(UTC)
    async with sm() as s:
        tx = Transaction(
            timestamp=datetime.combine(line.on, time(12, 0), tzinfo=tz),
            source_channel="reconciliation",
            payment_wallet=wallet,
            merchant=merchant,
            direction=direction,
            total_paid_amount=amount,
            net_user_expense=amount if direction == "out" else Decimal(0),
            primary_category=category,
            responsible_agent="A-11",
            execution_mode="reconciliation_catchup",
            circuit_breaker_cleared=True,
            auto_reconciled=True,
            status="committed",
            reversibility_deadline=now + timedelta(hours=UNDO_HOURS),
            notes=note,
        )
        s.add(tx)
        await s.flush()
        await apply_effects(s, tx, sign=+1, tz=tz, now=now)
        await append_explainability(
            s,
            decision_type="reconciliation",
            transaction_id=tx.id,
            verdict=f"reconcile:{reason}",
            justification=note,
        )
        await s.commit()
        return tx.id


async def apply_plan(
    sm: async_sessionmaker,
    plan: Plan,
    *,
    wallet: str,
    uri: str,
    sha256: str,
    tz: ZoneInfo,
    run_id: uuid.UUID | None = None,
) -> dict:
    """Write the plan. -> counts and the Rp totals, for the owner's one-line report."""
    run = run_id or uuid.uuid4()
    rows: list[ReconciliationLog] = []

    def log(line: Line, tier, kind, *, matched=None, fee=None, created=None, diff=None) -> None:
        rows.append(
            ReconciliationLog(
                run_id=run,
                wallet=wallet,
                statement_storage_uri=uri,
                statement_sha256=sha256,
                statement_line={
                    "date": line.on.isoformat(),
                    "description": line.description,
                    "amount": str(line.amount),
                    "direction": line.direction,
                },
                statement_date=line.on,
                statement_amount=line.amount,
                match_tier=tier,
                match_type=kind,
                matched_transaction_id=matched,
                fee_amount=fee,
                created_transaction_id=created,
                date_diff_days=diff,
            )
        )

    out = {
        "exact": len(plan.matched) - len(plan.folded_fees),
        "folded_fees": 0,
        "fee_lines": 0,
        "catchup": 0,
        "unmatched": len(plan.unmatched),
        "catchup_total": Decimal(0),
        "fee_total": Decimal(0),
    }

    for m in plan.matched:
        if m.fee is None:
            log(m.line, m.tier, "exact", matched=m.entry_id, diff=m.date_diff)
            continue
        eid = await _new_entry(
            sm,
            wallet=wallet,
            line=m.line,
            amount=m.fee,
            direction="out",
            merchant="Biaya bank",
            category=FEES_CATEGORY,
            reason="fee_split",
            tz=tz,
            note=f"bank fee {rupiah(m.fee)} split out of a statement line ({m.line.on}) that was recorded without it",
        )
        log(m.line, 2, "fuzzy_fee", matched=m.entry_id, fee=m.fee, created=eid, diff=m.date_diff)
        out["folded_fees"] += 1
        out["fee_total"] += m.fee

    for f in plan.fees:
        eid = await _new_entry(
            sm,
            wallet=wallet,
            line=f.line,
            amount=f.line.amount,
            direction="out",
            merchant="Biaya bank",
            category=FEES_CATEGORY,
            reason="fee_line",
            tz=tz,
            note=f"bank fee line on the statement ({f.line.on})",
        )
        log(f.line, 2, "fuzzy_fee", matched=f.near_entry_id, fee=f.line.amount, created=eid)
        out["fee_lines"] += 1
        out["fee_total"] += f.line.amount

    for line in plan.catchup:
        income = line.direction == "credit"
        name = clean_name(line.description)
        async with sm() as s:
            known = await find_merchant(s, name) if not income else None
            category = (
                INCOME_CATEGORY
                if income
                else ((known.default_category if known else None) or "Uncategorized")
            )
        eid = await _new_entry(
            sm,
            wallet=wallet,
            line=line,
            amount=line.amount,
            direction="in" if income else "out",
            merchant=name,
            category=category,
            reason="catchup",
            tz=tz,
            note=f"found on the bank statement, missed by the listener and manual entry: «{line.description[:80]}»",
        )
        log(line, 3, "synthetic_catchup", created=eid)
        out["catchup"] += 1
        out["catchup_total"] += line.amount

    for line in plan.unmatched:
        log(line, None, "unmatched")
    async with sm() as s:
        s.add_all(rows)
        await s.commit()
    return out


async def already_reconciled(sm: async_sessionmaker, sha256: str, wallet: str) -> bool:
    async with sm() as s:
        return bool(
            (
                await s.execute(
                    select(ReconciliationLog.id)
                    .where(ReconciliationLog.statement_sha256 == sha256, ReconciliationLog.wallet == wallet)
                    .limit(1)
                )
            ).first()
        )


def report(bank: str, lines: int, plan: Plan, counts: dict, start, end) -> str:
    parts = [f"🧾 Rekonsiliasi {bank} {start:%d/%m}-{end:%d/%m}: {lines} baris"]
    parts.append(f"{counts['exact']} cocok")
    if counts["folded_fees"] + counts["fee_lines"]:
        parts.append(
            f"{counts['folded_fees'] + counts['fee_lines']} biaya bank dicatat ({rupiah(counts['fee_total'])})"
        )
    if counts["catchup"]:
        parts.append(
            f"{counts['catchup']} transaksi terlewat ditambahkan ({rupiah(counts['catchup_total'])})"
        )
    text = " · ".join(parts)
    if plan.suspicious:
        text += (
            f"\n⚠️ {counts['unmatched']} baris tidak cocok dengan catatan (terlalu banyak, jadi tidak ditambahkan "
            "otomatis). Kalau memang semuanya belum tercatat, kirim ulang dengan keterangan «catat semua»."
        )
    elif counts["unmatched"]:
        text += (
            f"\n⚠️ {counts['unmatched']} baris di luar batas {40} per proses, kirim ulang untuk melanjutkan."
        )
    return text
