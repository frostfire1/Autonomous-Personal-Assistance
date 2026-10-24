"""Receipt photo intake: store the image, refuse a photo that was already counted, queue the reading.

Only the URI and checksum travel through the queue and the events table, never the image bytes.

An ALBUM (a receipt + its payment slip, or the pages of a long receipt) arrives as one Telegram
message per photo, handled concurrently. They all append to ONE event keyed on the album id:
the first photo creates it (an upsert, so two photos racing to be first cannot make two events), every
photo appends under a row lock, and the pages are ordered by Telegram message id, not by arrival order.
The reading job waits until the photos have stopped arriving.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apa.core.sanitize import sanitize_untrusted
from apa.core.settings import get_settings
from apa.db.models.evidence import EvidenceFile
from apa.db.models.ops import InboundEvent
from apa.ingestion.intake import Enqueuer, store_and_enqueue
from apa.integrations.evidence_store import EvidenceError, EvidenceStore

# An earlier event with the same image in one of these states means the photo is already being
# (or was) counted. A rejected or failed one does not: a resend may deserve another try.
_LIVE = ("received", "reading", "parsed", "processing", "committed", "quarantined")


@dataclass(frozen=True)
class PhotoIntake:
    status: str  # queued | duplicate | rejected | replayed | late | too_many
    detail: str | None = None
    event_id: int | None = None


async def already_counted(s: AsyncSession, sha256: str, *, exclude_key: str | None = None) -> bool:
    """True if this exact image already backs a transaction or has a live event in the pipeline.
    `exclude_key` is the album's own event, which of course contains the photo being added."""
    on_a_transaction = (
        await s.execute(
            select(EvidenceFile.id)
            .where(
                EvidenceFile.sha256_checksum == sha256,
                EvidenceFile.file_role.in_(("receipt_primary", "receipt_continuation", "payment_proof_slip")),
            )
            .limit(1)
        )
    ).first()
    if on_a_transaction:
        return True
    q = select(InboundEvent.id).where(
        InboundEvent.payload.contains({"attachments": [{"sha256": sha256}]}),
        InboundEvent.status.in_(_LIVE),
    )
    if exclude_key:
        q = q.where(InboundEvent.idempotency_key != exclude_key)
    return bool((await s.execute(q.limit(1))).first())


async def intake_photo(
    sm: async_sessionmaker,
    arq: Enqueuer,
    store: EvidenceStore,
    *,
    chat_id: int,
    message_id: int,
    data: bytes,
    mime: str,
    caption: str | None,
    sent_at: datetime,
    media_group_id: str | None = None,
) -> PhotoIntake:
    try:
        stored = await store.put(data, mime)
    except EvidenceError as e:
        return PhotoIntake("rejected", str(e))
    clean = sanitize_untrusted(caption or "")
    attachment = {
        "uri": stored.uri, "sha256": stored.sha256, "mime": stored.mime, "size": stored.size,
        "message_id": message_id,
    }
    if media_group_id:
        return await _intake_album_photo(sm, arq, chat_id, media_group_id, attachment, clean, sent_at)

    async with sm() as s:
        if await already_counted(s, stored.sha256):
            return PhotoIntake("duplicate")
        event_id = await store_and_enqueue(
            s,
            arq,
            source="telegram_photo",
            idempotency_key=f"tg:{chat_id}:{message_id}",
            payload={"caption": clean.text, "sent_at": sent_at.isoformat(), "attachments": [attachment]},
            clean=clean,
            job="read_receipt",
        )
    # None: Telegram redelivered the same update, which is already queued
    return PhotoIntake("replayed") if event_id is None else PhotoIntake("queued", event_id=event_id)


async def _intake_album_photo(sm, arq, chat_id, group, attachment, clean, sent_at) -> PhotoIntake:
    settings = get_settings()
    key = f"tg:{chat_id}:album:{group}"
    now = datetime.now(UTC).isoformat()
    async with sm() as s:
        if await already_counted(s, attachment["sha256"], exclude_key=key):
            return PhotoIntake("duplicate")
        # The first photo creates the event; a photo that loses the race to be first finds it instead.
        created_id = (
            await s.execute(
                insert(InboundEvent)
                .values(
                    idempotency_key=key,
                    source="telegram_photo",
                    status="received",
                    injection_flags=[],
                    payload={"caption": "", "sent_at": sent_at.isoformat(), "attachments": [], "album": True,
                             "last_append_at": now},
                )
                .on_conflict_do_nothing(index_elements=["idempotency_key"])
                .returning(InboundEvent.id)
            )
        ).scalar_one_or_none()
        ev = (
            await s.execute(select(InboundEvent).where(InboundEvent.idempotency_key == key).with_for_update())
        ).scalar_one()
        event_id = ev.id  # read it now: after a rollback the ORM object is expired and cannot be touched
        if ev.status != "received":  # the reading already started: this photo missed the album
            await s.rollback()
            return PhotoIntake("late", event_id=event_id)
        payload = dict(ev.payload)
        attachments = list(payload["attachments"])
        if any(a["sha256"] == attachment["sha256"] for a in attachments):
            await s.rollback()
            return PhotoIntake("replayed", event_id=event_id)  # the same photo delivered twice
        if len(attachments) >= settings.max_album_images:
            await s.rollback()
            return PhotoIntake("too_many", detail=str(settings.max_album_images), event_id=event_id)
        attachments.append(attachment)
        # Handlers run concurrently, so arrival order is arbitrary. Telegram numbers the messages of an
        # album in send order, and the page order matters for stitching.
        payload["attachments"] = sorted(attachments, key=lambda a: a["message_id"])
        payload["last_append_at"] = now
        if clean.text and not payload.get("caption"):
            payload["caption"] = clean.text  # Telegram puts the caption on only one message of the album
        ev.payload = payload
        ev.injection_flags = [*(ev.injection_flags or []), *clean.injection_flags]
        await s.commit()
    if created_id is not None:  # exactly one photo creates the event, so exactly one job is queued
        await arq.enqueue_job(
            "read_receipt", event_id,
            _job_id=f"read_receipt:{event_id}", _defer_by=settings.album_settle_seconds,
        )
    return PhotoIntake("queued", event_id=event_id)
