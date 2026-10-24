"""A statement arrives the way a receipt does: the owner sends the file. A PDF document is always a statement; a photo
is one only when its caption says so (otherwise it is a receipt). No command is involved."""

import re
from datetime import datetime
from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from apa.core.sanitize import sanitize_untrusted
from apa.db.models.ops import InboundEvent
from apa.integrations.evidence_store import EvidenceError, EvidenceStore

STATEMENT_WORDS = re.compile(r"(?i)\b(mutasi|rekening koran|rek koran|e-?statement|statement)\b")
IMPORT_WORDS = re.compile(r"(?i)\b(catat semua|import|impor)\b")
MAX_PDF_BYTES = 15 * 1024 * 1024


def looks_like_statement(caption: str | None) -> bool:
    return bool(caption and STATEMENT_WORDS.search(caption))


def wants_full_import(caption: str | None) -> bool:
    """«catat semua»: the owner says everything on this statement is new, so the safety stop for a statement
    that mostly does not match is lifted."""
    return bool(caption and IMPORT_WORDS.search(caption))


async def intake_statement(
    sm: async_sessionmaker,
    arq: Any,
    store: EvidenceStore,
    *,
    chat_id: int,
    message_id: int,
    data: bytes,
    mime: str,
    caption: str | None,
    sent_at: datetime,
) -> str:
    """Store the file and queue its reading. -> 'queued' | 'duplicate' | 'rejected: <why>'."""
    if mime == "application/pdf" and len(data) > MAX_PDF_BYTES:
        return f"rejected: file terlalu besar (batas {MAX_PDF_BYTES // 1024 // 1024} MB)"
    try:
        stored = await store.put(data, mime)
    except EvidenceError as e:
        return f"rejected: {e}"
    clean = sanitize_untrusted(caption or "")
    payload = {
        "attachments": [{"uri": stored.uri, "mime": mime, "sha256": stored.sha256, "size": stored.size}],
        "caption": clean.text,
        "sent_at": sent_at.isoformat(),
        "import_all": wants_full_import(caption),
    }
    async with sm() as s:
        event_id = (
            await s.execute(
                insert(InboundEvent)
                .values(
                    idempotency_key=f"tgstmt:{chat_id}:{message_id}",
                    source="telegram_statement",
                    payload=payload,
                    status="received",
                    injection_flags=clean.injection_flags,
                )
                .on_conflict_do_nothing(index_elements=["idempotency_key"])
                .returning(InboundEvent.id)
            )
        ).scalar_one_or_none()
        await s.commit()
    if event_id is None:
        return "duplicate"
    await arq.enqueue_job("reconcile_event", event_id)
    return "queued"
