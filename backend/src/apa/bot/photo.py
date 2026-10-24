"""Receipt photos from Telegram. Duck-typed on the message and bot so it can be tested without aiogram."""

import io
from collections import deque
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker

from apa.core.settings import get_settings
from apa.ingestion.photo_intake import intake_photo
from apa.integrations.evidence_store import EvidenceStore

DUPLICATE_NOTICE = "Foto struk ini sudah pernah dikirim, jadi tidak dicatat dua kali."
LATE_NOTICE = "Foto album ini datang terlambat (struk sudah dibaca). Kirim ulang seluruh albumnya."


def too_many_notice(limit: str | int) -> str:
    return f"Maksimal {limit} foto per struk (struk + bukti bayar, atau beberapa halaman)."


# One notice per album and kind, not one per photo in it: a bounded memory of recent (album, kind).
_notified: deque[tuple[str, str]] = deque(maxlen=400)


def _once(group: str | None, kind: str) -> bool:
    """True the first time `kind` is reported for this album; always True for a lone photo."""
    if not group:
        return True
    if (group, kind) in _notified:
        return False
    _notified.append((group, kind))
    return True


async def handle_photo(
    sm: async_sessionmaker, arq: Any, store: EvidenceStore, bot: Any, message: Any
) -> str | None:
    """Returns the reply to send, or None when the photo was queued: the one-line summary the
    pipeline sends when it commits is the answer."""
    group = getattr(message, "media_group_id", None)
    photo = message.photo[-1]  # Telegram lists sizes ascending; the last is the largest
    limit = get_settings().max_image_bytes
    if photo.file_size and photo.file_size > limit:
        return f"Fotonya terlalu besar ({photo.file_size // 1024} KB, batas {limit // 1024} KB)."

    buf = io.BytesIO()
    await bot.download(photo, destination=buf)
    result = await intake_photo(
        sm, arq, store,
        chat_id=message.chat.id, message_id=message.message_id,
        data=buf.getvalue(), mime="image/jpeg",  # Telegram re-encodes every photo as JPEG
        caption=message.caption, sent_at=message.date, media_group_id=group,
    )
    if result.status == "duplicate":
        return DUPLICATE_NOTICE if _once(group, "dup") else None
    if result.status == "late":
        return LATE_NOTICE if _once(group, "late") else None
    if result.status == "too_many":
        return too_many_notice(result.detail) if _once(group, "many") else None
    if result.status == "rejected":
        return f"Foto tidak bisa diproses: {result.detail}"
    return None  # queued, or a redelivered update that is already queued
