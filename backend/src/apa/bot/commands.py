"""Telegram owner commands. aiogram-free so the parsing and handling are testable on their own."""

import re
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker

from apa.db.models.ledger import Transaction
from apa.pipeline.undo import (
    UndoResult,
    clear_lockdown,
    dismiss_quarantined,
    label_of,
    resolve_token,
    undo_transaction,
)

# "/undo_8f9a2b", "/undo 8f9a2b", "/undo_8f9a2b@MyBot": the token is the 6 hex chars the summary
# line carries. Numeric ids are deliberately not accepted here: a token like "123456" would be
# ambiguous with an id. (The Web UI undoes by id through `undo_transaction`.)
_CMD = re.compile(
    r"^/(?P<name>undo|release|dismiss|unlock)(?:@\w+)?"  # "/undo@MyBot 8f9a2b"
    r"(?:[_ ]+(?P<arg>[0-9a-fA-F]{6}))?"
    r"(?:@\w+)?\s*$"  # Telegram appends "@MyBot" to the whole tapped command: "/undo_8f9a2b@MyBot"
)


def parse_command(text: str) -> tuple[str, str | None] | None:
    m = _CMD.match(text.strip())
    return (m["name"], m["arg"].lower() if m["arg"] else None) if m else None


_UNDO_FAIL = {
    "not_found": "Token tidak dikenal.",
    "wrong_state": "Transaksi ini sudah dibatalkan atau belum tercatat.",
    "expired": "Batas waktu /undo sudah lewat.",
}


def _fail(r: UndoResult, extra: str = "") -> str:
    return _UNDO_FAIL[r.status] + (f" {r.label}" if r.label and r.status != "not_found" else "") + extra


async def handle_command(sm: async_sessionmaker, arq: Any, name: str, arg: str | None) -> str | None:
    """Run one command. Returns the reply text, or None when the answer is the normal
    Telegram summary a worker will send (so `/release` stays silent on success)."""
    if name == "unlock":
        return "🔓 Lockdown dicabut." if await clear_lockdown(sm) else "Lockdown tidak aktif."

    if arg is None:
        return f"Format: /{name}_<token> (token ada di pesan pencatatan)."
    async with sm() as s:
        tx_id = await resolve_token(s, arg)
    if tx_id is None:
        return _UNDO_FAIL["not_found"]
    return await act_on_transaction(sm, arq, name, tx_id)


async def act_on_transaction(sm: async_sessionmaker, arq: Any, name: str, tx_id: int) -> str | None:
    """undo / dismiss / release one transaction by id (the token commands and the buttons both land here)."""
    if name == "undo":
        r = await undo_transaction(sm, tx_id)
        return f"↩️ Dibatalkan: {r.label}" if r.status == "ok" else _fail(r)
    if name == "dismiss":
        r = await dismiss_quarantined(sm, tx_id)
        return f"🗑️ Dibuang: {r.label}" if r.status == "ok" else _fail(r)

    # release: only a held transaction can be released; check now so a stale tap gets an answer
    async with sm() as s:
        tx = await s.get(Transaction, tx_id)
    if tx is None or tx.status != "quarantined":
        return _fail(UndoResult("wrong_state", tx_id, label_of(tx) if tx else None))
    # the debate runs in the worker, which sends the usual summary when it commits
    await arq.enqueue_job("release_quarantined", tx_id)
    return None
