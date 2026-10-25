"""telegram_photo event -> vision reading -> parsed (and on to commit_event) | skipped | needs_review.

Same claim / call-outside-the-transaction / re-lock-and-write shape as `run_extract`, so a redelivered
job or two racing workers produce one vision call and one outcome. Statuses: reading (in flight),
parsed, skipped, needs_review. A photo the user sent always gets an answer when it cannot be recorded.
"""

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apa.core.logging import get_logger
from apa.core.settings import get_settings
from apa.db.models.ops import InboundEvent, TelegramMessage
from apa.ingestion.llm_extract import Extraction, LLMUnavailable
from apa.ingestion.receipt_multi import extract_receipts
from apa.integrations.evidence_store import EvidenceError, EvidenceStore
from apa.integrations.llm import LLMClient
from apa.integrations.notify import Notifier
from apa.pipeline.commit import send_and_record

log = get_logger(__name__)

_REASON_ID = {
    "not_a_receipt": "itu tidak terbaca sebagai struk",
    "total_mismatch": "total tidak cocok dengan rincian barang",
    "item_line_mismatch": "ada baris yang hitungannya tidak cocok",
    "subtotal_mismatch": "subtotal tidak cocok dengan rincian barang",
    "no_items": "rincian barang tidak terbaca",
    "caption_total_mismatch": "nominal di keterangan berbeda dengan struk",
    "low_confidence": "foto kurang jelas",
    "invalid_output": "tidak bisa dipahami",
    "llm_unavailable": "layanan AI sedang tidak tersedia",
    "image_unavailable": "fotonya tidak bisa dibuka lagi",
    "no_receipt_page": "tidak ada foto struk, hanya bukti pembayaran",
    "no_total": "total akhir tidak terlihat di foto mana pun",
    "too_many_pages": "terlalu banyak halaman struk",
    "too_many_slips": "lebih dari satu bukti pembayaran",
    "slip_unreadable": "nominal di bukti pembayaran tidak terbaca",
    "slip_total_mismatch": "nominal bukti pembayaran tidak cocok dengan struk",
}


def build_reply(reason: str) -> str:
    why = _REASON_ID.get(reason, reason)
    return (
        f"❓ Struk belum bisa dicatat ({why}). "
        "Foto ulang yang lebih jelas, atau tulis manual: kopi susu 18k tunai"
    )


class AlbumStillArriving(Exception):
    """An album's photos have not stopped arriving. The worker retries after `wait` seconds."""

    def __init__(self, wait: float):
        super().__init__(f"album still arriving, {wait:.1f}s of quiet needed")
        self.wait = wait


async def _locked(s: AsyncSession, event_id: int) -> InboundEvent | None:
    return (
        await s.execute(select(InboundEvent).where(InboundEvent.id == event_id).with_for_update())
    ).scalar_one_or_none()


async def run_read_receipt(
    sm: async_sessionmaker,
    llm: LLMClient,
    notifier: Notifier,
    store: EvidenceStore,
    enqueuer: Any | None,
    event_id: int,
    *,
    job_try: int = 1,
    max_tries: int = 3,
    tz: ZoneInfo | None = None,
    settle_seconds: float | None = None,
) -> str:
    """Returns the event's resulting status. Raises LLMUnavailable while retries remain, and
    AlbumStillArriving while an album's photos are still coming in."""
    tz = tz or ZoneInfo(get_settings().timezone)
    settle = get_settings().album_settle_seconds if settle_seconds is None else settle_seconds
    async with sm() as s:
        ev = await _locked(s, event_id)
        if ev is None:
            return "missing"
        if not (ev.status == "received" or (ev.status == "reading" and job_try > 1)):
            return ev.status
        if ev.status == "received" and ev.payload.get("album") and job_try < max_tries:
            # Photos are still being appended: reading now would miss the late ones. Nothing is
            # claimed yet, so a late photo can still join. On the last try we read what we have.
            quiet = (datetime.now(UTC) - datetime.fromisoformat(ev.payload["last_append_at"])).total_seconds()
            if quiet < settle:
                raise AlbumStillArriving(settle - quiet)
        ev.status = "reading"
        payload = dict(ev.payload)
        await s.commit()

    meal_read = None
    try:
        attachments = payload["attachments"]
        images = [(await store.get(a["uri"]), a.get("mime", "image/jpeg")) for a in attachments]
        if not images:
            raise KeyError("attachments")
        result = await extract_receipts(
            llm, images, payload.get("caption") or "",
            sent_at=datetime.fromisoformat(payload["sent_at"]), tz=tz,
        )
        not_a_receipt = result.status == "skipped" and result.reason in ("not_a_receipt", "no_receipt_page")
        if not_a_receipt and not payload.get("steward"):
            from apa import meal

            # a photo of food goes to the assistant, not to the error reply
            meal_read = await meal.read(llm, images, payload.get("caption") or "")
    except (EvidenceError, KeyError):
        result = Extraction("needs_review", reason="image_unavailable")
    except LLMUnavailable:
        if job_try < max_tries:
            raise  # the event stays 'reading'; the retry re-enters
        result = Extraction("needs_review", reason="llm_unavailable")

    reply_id: int | None = None
    reply = None
    async with sm() as s:
        ev = await _locked(s, event_id)
        if ev.status != "reading":  # a concurrent worker finished first
            return ev.status
        out = dict(ev.payload)
        quiet = bool(out.get("steward"))  # a repair re-read: the owner was already told once, so a second
        # failure is the steward's to report, not another error message
        if result.status == "parsed":
            out["parsed"] = result.parsed
        elif quiet:
            out["skip_reason" if result.status == "skipped" else "review_reason"] = result.reason
            out["extraction"] = {"model": result.model, "tokens": result.tokens}
        elif meal_read is not None:
            out["skip_reason"] = result.reason
            out["extraction"] = {"model": result.model, "tokens": result.tokens}
            out["meal"] = meal_read
        else:
            out["skip_reason" if result.status == "skipped" else "review_reason"] = result.reason
            out["extraction"] = {"model": result.model, "tokens": result.tokens}
            reply = build_reply(result.reason or "invalid_output")
            tm = TelegramMessage(
                direction="out", chat_id=get_settings().telegram_owner_id, kind="reply", body=reply,
                inbound_event_id=event_id,
            )
            s.add(tm)
            await s.flush()
            reply_id = tm.id
        ev.payload, ev.status, ev.processed_at = out, result.status, datetime.now(UTC)
        await s.commit()

    log.info("receipt_read", event_id=event_id, status=result.status, reason=result.reason,
             model=result.model, tokens=result.tokens)
    # Enqueue only after the commit above so commit_event always sees status='parsed'.
    if result.status == "parsed" and enqueuer is not None:
        await enqueuer.enqueue_job("commit_event", event_id)
    if meal_read is not None and enqueuer is not None:
        await enqueuer.enqueue_job("meal_event", event_id)
    if reply_id is not None:
        await send_and_record(sm, notifier, reply_id, reply)
    return result.status
