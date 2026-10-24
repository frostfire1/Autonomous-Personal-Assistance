"""One-way ledger mirror into Google Sheets (blueprint v2 4.2). Postgres stays the source of truth; the sheet is a view the
owner can chart, filter and share. Nothing is ever read back into the database.

Self-healing and idempotent: the sheet's own first column (the transaction id) tells what is already there, so there is
no cursor to lose. Each run (1) appends transactions the sheet has never seen, (2) corrects rows whose status, category
or amount changed since (an undo, an edit, a category fix), and (3) refreshes the budget tab at most every 15 minutes.
Values are sent RAW, so text such as '=HYPERLINK(...)' stays text and cannot run as a formula."""

from datetime import datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from apa.core.logging import get_logger
from apa.db.models.budget import BudgetRule
from apa.db.models.ledger import Transaction
from apa.sheets.client import SheetsClient

log = get_logger(__name__)

TX_TAB = "Transaksi"
BUDGET_TAB = "Anggaran"
TX_HEADER = ["ID", "Waktu", "Merchant", "Arah", "Jumlah", "Kategori", "Dompet", "Status", "Sumber", "Catatan"]
BUDGET_HEADER = ["Bulan", "Kategori", "Batas dasar", "Batas saat ini", "Terpakai", "Sisa", "Persen terpakai"]
WINDOW = 300  # newest transactions checked for changes on each run
MAX_APPEND = 500  # per run, so a first sync of a long history catches up over several runs instead of one huge call
BUDGET_EVERY_S = 900
BUDGET_KEY = "apa:sheets:budget_synced"


def _num(d: Decimal | None) -> int | float:
    d = d or Decimal(0)
    return int(d) if d == d.to_integral_value() else float(d)


def tx_row(t: Transaction, tz: ZoneInfo) -> list[Any]:
    return [t.id, t.timestamp.astimezone(tz).strftime("%Y-%m-%d %H:%M"), t.merchant or "", t.direction, _num(t.total_paid_amount),
            t.primary_category or "", t.payment_wallet, t.status, t.source_channel, (t.notes or "")[:200]]


def _same(a: list[Any], b: list[Any]) -> bool:
    return [str(x) for x in a] == [str(x) for x in b]


async def sync_transactions(sm: async_sessionmaker, client: SheetsClient, tz: ZoneInfo) -> dict:
    await client.ensure_tab(TX_TAB)
    sheet = await client.values(f"{TX_TAB}!A1:J")
    if not sheet:
        await client.update(f"{TX_TAB}!A1:J1", [TX_HEADER])
        sheet = [TX_HEADER]
    at: dict[int, int] = {}  # transaction id -> 1-based row
    for i, row in enumerate(sheet[1:], start=2):
        if row and str(row[0]).isdigit():
            at[int(row[0])] = i
    newest = max(at) if at else 0

    async with sm() as s:
        fresh = list((await s.execute(select(Transaction).where(Transaction.id > newest).order_by(Transaction.id).limit(MAX_APPEND))).scalars())
        recent = list((await s.execute(select(Transaction).order_by(Transaction.id.desc()).limit(WINDOW))).scalars())

    fixed = 0
    for t in recent:
        row_no = at.get(t.id)
        if row_no is None or t.id > newest:
            continue
        want = tx_row(t, tz)
        have = sheet[row_no - 1] if row_no - 1 < len(sheet) else []
        if not _same(want[:9], (have + [""] * 10)[:9]):
            await client.update(f"{TX_TAB}!A{row_no}:J{row_no}", [want])
            fixed += 1
    if fresh:
        await client.append(f"{TX_TAB}!A:J", [tx_row(t, tz) for t in fresh])
    return {"appended": len(fresh), "corrected": fixed}


async def sync_budgets(sm: async_sessionmaker, client: SheetsClient, now: datetime) -> int:
    month = now.date().replace(day=1)
    async with sm() as s:
        rules = list((await s.execute(select(BudgetRule).where(BudgetRule.period_month == month).order_by(BudgetRule.category))).scalars())
    rows = [[month.isoformat(), r.category, _num(r.base_limit), _num(r.current_limit), _num(r.spent_amount),
             _num(r.current_limit - r.spent_amount),
             round(float(r.spent_amount / r.current_limit * 100), 1) if r.current_limit else 0] for r in rules]
    await client.ensure_tab(BUDGET_TAB)
    await client.clear(f"{BUDGET_TAB}!A:G")
    await client.update(f"{BUDGET_TAB}!A1:G{len(rows) + 1}", [BUDGET_HEADER, *rows])
    return len(rows)


async def run_sync(sm: async_sessionmaker, client: SheetsClient, redis: Any | None, tz: ZoneInfo, now: datetime) -> dict:
    out = await sync_transactions(sm, client, tz)
    if redis is None or await redis.set(BUDGET_KEY, "1", nx=True, ex=BUDGET_EVERY_S):
        out["budgets"] = await sync_budgets(sm, client, now)
    if out["appended"] or out["corrected"]:
        log.info("sheets_synced", **out)
    return out
