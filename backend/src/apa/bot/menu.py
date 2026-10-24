"""The owner's interactive menu: views (text + button rows) and button handling. aiogram-free, so it is
testable on its own; bot/main.py only turns a View into a Telegram message."""

import re
from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from apa.bot.commands import act_on_transaction, handle_command
from apa.chat.actions import resolve
from apa.core.format import rupiah
from apa.db.models.budget import BudgetRule
from apa.db.models.ledger import Transaction
from apa.inspector.timeline import detail, render_detail, render_timeline, timeline
from apa.integrations.notify import Buttons

BACK: Buttons = [[("⬅️ Menu", "m:menu")]]

MAIN: Buttons = [
    [("📊 Hari ini", "m:today"), ("🧾 Terakhir", "m:recent")],
    [("⏳ Ditahan", "m:held"), ("💰 Anggaran", "m:budget")],
    [("🗂️ Riwayat", "m:log"), ("🔓 Cabut lockdown", "m:unlock")],
    [("❓ Bantuan", "m:help")],
]

# the command menu Telegram shows next to the text box (name, description)
BOT_COMMANDS = [
    ("menu", "Menu utama"),
    ("hari_ini", "Ringkasan hari ini"),
    ("terakhir", "Transaksi terakhir"),
    ("ditahan", "Transaksi yang ditahan"),
    ("anggaran", "Anggaran bulan ini"),
    ("bantuan", "Cara pakai"),
    ("log", "Riwayat pesan, jawaban dan alasannya"),
    ("unlock", "Cabut lockdown"),
    ("private", "Sesi curhat privat (dihapus otomatis)"),
    ("selesai", "Akhiri sesi privat dan hapus pesannya"),
]

HELP = (
    "❓ Cara pakai\n"
    "• Ketik pengeluaran, mis. «kopi susu 18k tunai», atau «gaji 5jt masuk bca».\n"
    "• Kirim foto struk (boleh beberapa foto: struk + slip EDC, atau struk panjang).\n"
    "• Tiap pencatatan punya tombol ↩️ Undo (24 jam).\n"
    "• Transaksi yang ditahan bisa dilepas atau dibuang dari ⏳ Ditahan.\n"
    "• Butuh ruang bercerita? /private membuka sesi yang hanya ada di memori dan dihapus saat selesai.\n"
    "• Menu: /menu"
)


@dataclass
class View:
    text: str
    rows: Buttons
    replace: bool = True  # True: edit the tapped message in place; False: send a new message


# /hari_ini, /today, /menu@MyBot ... -> view key
_ALIAS = {
    "start": "menu",
    "menu": "menu",
    "hari_ini": "today",
    "today": "today",
    "terakhir": "recent",
    "recent": "recent",
    "ditahan": "held",
    "held": "held",
    "anggaran": "budget",
    "budget": "budget",
    "bantuan": "help",
    "help": "help",
    "log": "log",
    "riwayat": "log",
}
_SLASH = re.compile(r"^/(?P<name>[a-z_]+)(?:@\w+)?\s*$", re.I)
_ACTION_CMD = re.compile(r"^/(?P<name>confirm|cancel)_(?P<nonce>[0-9a-f]{12})(?:@\w+)?\s*$", re.I)


def parse_action_command(text: str) -> tuple[bool, str] | None:
    """'/confirm_<nonce>' / '/cancel_<nonce>' -> (accept, nonce)."""
    m = _ACTION_CMD.match(text.strip())
    return (m["name"].lower() == "confirm", m["nonce"].lower()) if m else None


def menu_key(text: str) -> str | None:
    m = _SLASH.match(text.strip())
    return _ALIAS.get(m["name"].lower()) if m else None


def _day_start(tz: ZoneInfo) -> datetime:
    return datetime.combine(datetime.now(tz).date(), time.min, tzinfo=tz)


async def today_view(sm: async_sessionmaker, tz: ZoneInfo) -> View:
    start = _day_start(tz)
    async with sm() as s:
        rows = (
            await s.execute(
                select(
                    Transaction.direction,
                    Transaction.primary_category,
                    func.count(),
                    func.sum(Transaction.total_paid_amount),
                )
                .where(Transaction.status == "committed", Transaction.timestamp >= start)
                .group_by(Transaction.direction, Transaction.primary_category)
            )
        ).all()
    spent = sum((r[3] for r in rows if r[0] == "out"), Decimal(0))
    earned = sum((r[3] for r in rows if r[0] == "in"), Decimal(0))
    n = sum(r[2] for r in rows)
    if not n:
        return View("📊 Hari ini belum ada transaksi.", BACK)
    top = sorted((r for r in rows if r[0] == "out"), key=lambda r: r[3], reverse=True)[:5]
    lines = [f"📊 Hari ini · {n} transaksi", f"Keluar: {rupiah(spent)}"]
    if earned:
        lines.append(f"Masuk: {rupiah(earned)}")
    lines += [f"  • {r[1] or 'Tanpa kategori'}: {rupiah(r[3])}" for r in top]
    return View("\n".join(lines), BACK)


async def recent_view(sm: async_sessionmaker, tz: ZoneInfo) -> View:
    async with sm() as s:
        txs = (
            (
                await s.execute(
                    select(Transaction)
                    .where(Transaction.status == "committed")
                    .order_by(Transaction.id.desc())
                    .limit(5)
                )
            )
            .scalars()
            .all()
        )
    if not txs:
        return View("🧾 Belum ada transaksi.", BACK)
    lines, rows = ["🧾 5 transaksi terakhir (tap untuk membatalkan):"], []
    for t in txs:
        sign = "+" if t.direction == "in" else "-"
        name = t.merchant or "Transaksi"
        when = t.timestamp.astimezone(tz).strftime("%d/%m %H:%M")
        lines.append(f"• {when} {name} {sign}{rupiah(t.total_paid_amount)} · {t.primary_category or '-'}")
        rows.append([(f"↩️ {name[:20]} {rupiah(t.total_paid_amount)}", f"t:undo:{t.id}")])
    return View("\n".join(lines), rows + BACK)


async def held_view(sm: async_sessionmaker) -> View:
    async with sm() as s:
        txs = (
            (
                await s.execute(
                    select(Transaction)
                    .where(Transaction.status == "quarantined")
                    .order_by(Transaction.id.desc())
                    .limit(5)
                )
            )
            .scalars()
            .all()
        )
    if not txs:
        return View("⏳ Tidak ada transaksi yang ditahan.", BACK)
    lines, rows = ["⏳ Ditahan, menunggu keputusan:"], []
    for t in txs:
        why = (t.notes or "").removeprefix("quarantined: ") or "-"
        lines.append(f"• {t.merchant or 'Transaksi'} {rupiah(t.total_paid_amount)} ({why})")
        rows.append([(f"✅ Lepas {t.id}", f"t:release:{t.id}"), (f"🗑️ Buang {t.id}", f"t:dismiss:{t.id}")])
    return View("\n".join(lines), rows + BACK)


async def budget_view(sm: async_sessionmaker, tz: ZoneInfo) -> View:
    month = datetime.now(tz).date().replace(day=1)
    async with sm() as s:
        rules = (
            (
                await s.execute(
                    select(BudgetRule).where(BudgetRule.period_month == month).order_by(BudgetRule.category)
                )
            )
            .scalars()
            .all()
        )
    if not rules:
        return View("💰 Belum ada anggaran untuk bulan ini.", BACK)
    lines = [f"💰 Anggaran {month:%m/%Y}"]
    for r in rules:
        pct = int(r.spent_amount / r.current_limit * 100) if r.current_limit else 0
        flag = "🔴" if pct >= 100 else "🟡" if pct >= 80 else "🟢"
        lines.append(f"{flag} {r.category}: {rupiah(r.spent_amount)} / {rupiah(r.current_limit)} ({pct}%)")
    return View("\n".join(lines), BACK)


async def log_view(sm: async_sessionmaker, tz: ZoneInfo, before_id: int | None = None) -> View:
    """The last exchanges (what you sent, what came back, how it was handled); each has a detail button."""
    rows = await timeline(sm, tz, limit=6, before_id=before_id)
    buttons: Buttons = [[(f"🔍 #{r.event_id}", f"l:d:{r.event_id}")] for r in rows]
    if len(rows) == 6:
        buttons.append([("⏪ Lebih lama", f"l:p:{rows[-1].event_id}")])
    return View(render_timeline(rows, tz), buttons + BACK)


async def view_for(sm: async_sessionmaker, key: str, tz: ZoneInfo) -> View:
    if key == "today":
        return await today_view(sm, tz)
    if key == "recent":
        return await recent_view(sm, tz)
    if key == "held":
        return await held_view(sm)
    if key == "budget":
        return await budget_view(sm, tz)
    if key == "help":
        return View(HELP, BACK)
    if key == "log":
        return await log_view(sm, tz)
    return View("📋 Menu utama", MAIN)


_ACTIONS = ("undo", "release", "dismiss")
_RELEASED = "⏳ Dilepas, sedang diproses ulang."


async def handle_callback(
    sm: async_sessionmaker, arq: Any, data: str, tz: ZoneInfo, actor: int | None = None
) -> View | None:
    """What a button tap does. None for an unknown button (ignored)."""
    parts = data.split(":")
    if parts[0] == "m" and len(parts) == 2:
        if parts[1] == "unlock":
            return View(await handle_command(sm, arq, "unlock", None) or "", BACK)
        return await view_for(sm, parts[1], tz)
    if parts[0] == "l" and len(parts) == 3 and parts[2].isdigit():  # inspector: a detail, or an older page
        if parts[1] == "p":
            return await log_view(sm, tz, before_id=int(parts[2]))
        d = await detail(sm, tz, int(parts[2]))
        back: Buttons = [[("⬅️ Riwayat", "m:log")]]
        return View(render_detail(d, tz) if d else "Pesan itu tidak ditemukan.", back)
    if parts[0] == "a" and len(parts) == 3 and parts[1] in ("ok", "no"):  # confirm / cancel a proposed change
        return View(await resolve(sm, arq, parts[2], parts[1] == "ok", actor=actor, tz=tz), [], replace=False)
    if parts[0] == "c" and len(parts) == 3 and parts[1] in _ACTIONS:
        # a button on a summary/alert message: reply separately, the original stays as the record
        reply = await handle_command(sm, arq, parts[1], parts[2])
        return View(reply or _RELEASED, [], replace=False)
    if parts[0] == "t" and len(parts) == 3 and parts[1] in _ACTIONS and parts[2].isdigit():
        reply = await act_on_transaction(sm, arq, parts[1], int(parts[2]))
        return View(reply or _RELEASED, BACK)
    return None
