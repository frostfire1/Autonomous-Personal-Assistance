"""Files the owner sends in Telegram (as documents): read their text and give it to the assistant like a message.

Text-like files (txt, md, csv, json, log, code...) are decoded; a PDF is read with `pypdf` when it is installed (the `docs` extra) and has
a text layer. Images sent as files go the photo way (see bot/main.py). The text is sanitised, cut to 8,000 characters and stored on an event
(`telegram_file`); the worker's `file_event` job then lets the assistant answer. Anything else is refused with a plain sentence."""

import io
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker

from apa.core.logging import get_logger
from apa.core.sanitize import sanitize_untrusted
from apa.ingestion.intake import store_and_enqueue

log = get_logger(__name__)

MAX_BYTES = 8 * 1024 * 1024
MAX_CHARS = 8000
MAX_PDF_PAGES = 15
TEXT_EXT = (".txt", ".md", ".csv", ".tsv", ".json", ".log", ".xml", ".yaml", ".yml", ".html", ".htm", ".sql", ".py", ".js", ".ts", ".ini", ".toml", ".conf", ".eml")


def kind_of(name: str, mime: str) -> str:
    """'pdf', 'text' or 'other'."""
    n = (name or "").lower()
    if mime == "application/pdf" or n.endswith(".pdf"):
        return "pdf"
    if mime.startswith("text/") or mime in ("application/json", "application/xml", "application/x-yaml") or n.endswith(TEXT_EXT):
        return "text"
    return "other"


def extract_text(data: bytes, name: str, mime: str) -> tuple[str, str | None]:
    """(text, problem). `problem` is a sentence for the owner when nothing could be read."""
    kind = kind_of(name, mime)
    if kind == "text":
        return data.decode("utf-8", errors="replace"), None
    if kind == "pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            return "", "Aku belum bisa membaca PDF di server ini (paket pypdf belum terpasang)."
        try:
            reader = PdfReader(io.BytesIO(data))
            pages = [(p.extract_text() or "") for p in reader.pages[:MAX_PDF_PAGES]]
        except Exception as exc:
            log.warning("pdf_read_failed", error=type(exc).__name__)
            return "", "PDF itu tidak bisa dibuka (rusak atau diproteksi)."
        text = "\n".join(pages).strip()
        return (text, None) if text else ("", "PDF itu tidak punya teks yang bisa dibaca (mungkin hasil scan). Kirim sebagai foto saja.")
    return "", f"Aku belum bisa membaca file jenis ini ({mime or name.rsplit('.', 1)[-1]}). Yang bisa: foto, teks, CSV, JSON, dan PDF yang punya teks."


_STATEMENT_WORDS = ("mutasi", "saldo", "rekening koran", "statement", "periode", "transaction history", "e-statement")


def looks_like_bank_statement(name: str, text: str) -> bool:
    """A PDF that is a bank statement (by its file name or its text), so it is reconciled instead of summarised."""
    low = f"{name} {text[:3000]}".lower()
    return sum(w in low for w in _STATEMENT_WORDS) >= 2 or any(w in name.lower() for w in ("mutasi", "statement", "rekening"))


async def handle_file(sm: async_sessionmaker, arq: Any, *, chat_id: int, message_id: int, name: str, mime: str, data: bytes, caption: str | None, sent_at: Any) -> str | None:
    """-> a sentence to answer with at once (too big, unreadable, duplicate), or None when it was queued for the assistant."""
    if not data:
        return "File itu kosong."
    if len(data) > MAX_BYTES:
        return f"File terlalu besar ({len(data) // 1024} KB, batas {MAX_BYTES // 1024} KB)."
    text, problem = extract_text(data, name, mime)
    if problem:
        return problem
    clean = sanitize_untrusted(text, max_len=MAX_CHARS)
    cap = sanitize_untrusted(caption or "").text if caption else ""
    async with sm() as session:
        event_id = await store_and_enqueue(
            session, arq, source="telegram_file", idempotency_key=f"tgfile:{chat_id}:{message_id}",
            payload={"name": name[:120], "mime": mime[:80], "size": len(data), "text": clean.text, "caption": cap, "message_id": message_id,
                     "sent_at": sent_at.isoformat(), "truncated": len(text) > MAX_CHARS},
            clean=clean, job="file_event")
    return None if event_id is not None else "File itu sudah pernah dikirim."
