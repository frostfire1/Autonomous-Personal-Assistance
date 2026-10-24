"""needs_llm event -> LLM extraction -> parsed (and on to commit_event) | skipped | needs_review.

Same claim / call-outside-the-transaction / re-lock-and-write shape as `run_commit`, so a
redelivered job or two racing workers produce one LLM call and one outcome.

Statuses written to the event: extracting (in flight), parsed, skipped, needs_review.
`needs_review` is a dead end by design: nothing is committed on a guess. Missed money still
surfaces later through statement reconciliation (blueprint 4.6, tier 3).
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apa.core.logging import get_logger
from apa.core.settings import get_settings
from apa.db.models.ops import InboundEvent, TelegramMessage
from apa.ingestion import templates
from apa.ingestion.llm_extract import Extraction, LLMUnavailable, extract
from apa.ingestion.packages import approved_wallets
from apa.integrations.llm import LLMClient
from apa.integrations.notify import Notifier
from apa.pipeline.chat import chat_eligible
from apa.pipeline.commit import send_and_record

log = get_logger(__name__)

_REASON_ID = {
    "llm_not_a_transaction": "itu tidak terbaca sebagai transaksi",
    "low_confidence": "kurang yakin",
    "amount_not_in_text": "nominal tidak cocok dengan teks",
    "fee_not_in_text": "biaya tidak cocok dengan teks",
    "no_digits_in_text": "nominal harus ditulis dengan angka",
    "invalid_output": "tidak bisa dipahami",
    "llm_unavailable": "layanan AI sedang tidak tersedia",
}


def build_reply(text: str, reason: str) -> str:
    """Answer to text the user typed that could not be turned into a transaction. Only sent for
    Telegram entries: a bank notification we cannot read is not worth a ping."""
    why = _REASON_ID.get(reason, reason)
    return f"❓ Belum bisa mencatat «{text[:80]}» ({why}). Coba tulis seperti: kopi susu 18k tunai"


async def _locked(s: AsyncSession, event_id: int) -> InboundEvent | None:
    return (
        await s.execute(select(InboundEvent).where(InboundEvent.id == event_id).with_for_update())
    ).scalar_one_or_none()


async def run_extract(
    sm: async_sessionmaker,
    llm: LLMClient,
    notifier: Notifier,
    enqueuer: Any | None,
    event_id: int,
    *,
    job_try: int = 1,
    max_tries: int = 3,
) -> str:
    """Returns the event's resulting status. Raises LLMUnavailable while retries remain."""
    async with sm() as s:
        ev = await _locked(s, event_id)
        if ev is None:
            return "missing"
        if not (ev.status == "needs_llm" or (ev.status == "extracting" and job_try > 1)):
            return ev.status
        ev.status = "extracting"
        source, text, package = ev.source, ev.payload.get("text", ""), ev.payload.get("package", "")
        await s.commit()

    known_wallet = None
    if source == "android_listener":
        async with sm() as s:
            known_wallet = (await approved_wallets(s)).get(package)
    try:
        result = await extract(llm, source=source, text=text, known_wallet=known_wallet)
    except LLMUnavailable:
        if job_try < max_tries:
            raise  # the event stays 'extracting'; the retry re-enters
        result = Extraction("needs_review", reason="llm_unavailable")

    reply_id: int | None = None
    async with sm() as s:
        ev = await _locked(s, event_id)
        if ev.status != "extracting":  # a concurrent worker finished first
            return ev.status
        payload = dict(ev.payload)
        meta = {"model": result.model, "tokens": result.tokens}
        if result.status == "parsed":
            payload["parsed"] = result.parsed
        else:
            payload["skip_reason" if result.status == "skipped" else "review_reason"] = result.reason
            payload["extraction"] = meta
        ev.payload, ev.status, ev.processed_at = payload, result.status, datetime.now(UTC)
        reply = None
        to_chat = (
            enqueuer is not None and result.status == "skipped" and chat_eligible(source, result.reason)
        )  # a question or request, not a transaction: the conversational agent answers instead
        if result.status != "parsed" and source == "telegram_shorthand" and not to_chat:
            reply = build_reply(text, result.reason or "invalid_output")
            tm = TelegramMessage(
                direction="out", chat_id=get_settings().telegram_owner_id, kind="reply", body=reply,
                inbound_event_id=event_id,
            )
            s.add(tm)
            await s.flush()
            reply_id = tm.id
        await s.commit()

    if result.status == "parsed" and source == "android_listener":
        try:  # remember how this wording reads; never let that get in the way of the payment itself
            async with sm() as s:
                if await templates.learn(s, package, text, result.parsed):
                    await s.commit()
        except Exception as exc:
            log.warning("template_learning_failed", error=type(exc).__name__)
    log.info("event_extracted", event_id=event_id, status=result.status, reason=result.reason,
             model=result.model, tokens=result.tokens)
    # Enqueue only after the commit above so commit_event always sees status='parsed'.
    if result.status == "parsed" and enqueuer is not None:
        await enqueuer.enqueue_job("commit_event", event_id)
    if to_chat:
        await enqueuer.enqueue_job("chat_event", event_id)
    if reply_id is not None:
        await send_and_record(sm, notifier, reply_id, reply)
    return result.status
