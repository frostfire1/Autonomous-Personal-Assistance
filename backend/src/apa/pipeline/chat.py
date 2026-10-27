"""skipped (not a transaction) Telegram text -> a conversational answer from the right agent role.

Same claim / call-outside-the-transaction / re-lock-and-write shape as the other jobs: a redelivered job
or two racing workers send one answer."""

import asyncio
import re
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from apa.chat import batch, batch_flow, master
from apa.chat.agent import (
    CAPABILITIES,
    _history_block,
    answer,
    ask_agent,
    load_history,
    plan_action,
    remember,
)
from apa.chat.assistant import converse
from apa.chat.snapshot import finance_snapshot, health_snapshot
from apa.core.logging import get_logger
from apa.core.sanitize import sanitize_untrusted
from apa.core.settings import get_settings
from apa.db.models.ledger import Transaction
from apa.db.models.ops import InboundEvent, TelegramMessage
from apa.ingestion.llm_extract import LLMUnavailable
from apa.integrations.llm import LLMClient
from apa.integrations.notify import Notifier
from apa.pipeline.commit import send_and_record

log = get_logger(__name__)

# what "the model is unavailable" looks like from the client: the retry/fallback logic treats all of it alike
LLM_ERRORS = (LLMUnavailable, httpx.HTTPError, TimeoutError)

UNAVAILABLE = (
    "⚠️ Asisten AI sedang tidak tersedia, coba lagi sebentar. "
    "Mencatat dengan format «kopi susu 18k tunai» tetap bisa."
)
INJECTION = "Pesan itu mengandung instruksi yang saya abaikan. " + CAPABILITIES


# Typed text the extractor could not turn into a payment: the assistant talks to the owner about it (asks what they meant)
# instead of a canned error. An AI outage keeps its own message.
CHAT_REASONS = frozenset({"llm_not_a_transaction", "invalid_output", "no_digits_in_text", "low_confidence", "amount_not_in_text"})


def chat_eligible(source: str, reason: str | None) -> bool:
    """Only the owner's own typed text that was clearly not a transaction. Anything else (a notification we
    cannot read, a transaction we could not parse) keeps its own reply."""
    return source == "telegram_shorthand" and reason in CHAT_REASONS


async def _locked(s, event_id: int) -> InboundEvent | None:
    return (
        await s.execute(select(InboundEvent).where(InboundEvent.id == event_id).with_for_update())
    ).scalar_one_or_none()


async def run_chat(
    sm: async_sessionmaker,
    llm: LLMClient,
    notifier: Notifier,
    redis: Any | None,
    event_id: int,
    *,
    tz: ZoneInfo | None = None,
    job_try: int = 1,
    max_tries: int = 3,
) -> str:
    """Returns 'answered' | 'skipped' | 'missing'. Raises LLMUnavailable while retries remain."""
    tz = tz or ZoneInfo(get_settings().timezone)
    async with sm() as s:
        ev = await _locked(s, event_id)
        if ev is None:
            return "missing"
        reason = ev.payload.get("skip_reason") or ev.payload.get("review_reason")
        if ev.payload.get("chat_done") or not chat_eligible(ev.source, reason):
            return "skipped"
        text, reply_to = ev.payload.get("text", ""), ev.payload.get("reply_to")

    from apa import followup
    await followup.dismiss_pending(redis)

    clean = sanitize_untrusted(text)
    if clean.suspicious:
        reply, role, tokens = INJECTION, "injection", 0  # no model sees an instruction-bearing message
    else:
        try:
            a = await answer(sm, llm, clean.text, tz=tz, history=await load_history(redis), redis=redis, reply_to=reply_to)
            reply, role, tokens = a.text, a.role, a.tokens
        except LLMUnavailable:
            if job_try < max_tries:
                raise
            reply, role, tokens = UNAVAILABLE, "unavailable_ai", 0
        except LLM_ERRORS as e:
            if job_try < max_tries:
                raise LLMUnavailable(str(e)) from e
            reply, role, tokens = UNAVAILABLE, "unavailable_ai", 0

    if not await _finish(sm, notifier, redis, event_id, clean.text, reply, role, tokens, llm=llm):
        return "skipped"
    return "answered"


async def _finish(
    sm,
    notifier,
    redis,
    event_id: int,
    user_text: str,
    reply: str,
    role: str,
    tokens: int,
    *,
    reason: str | None = None,
    llm: Any = None,
) -> bool:
    """Record the reply once (a racing worker loses), then send it. False if someone else already answered."""
    async with sm() as s:
        ev = await _locked(s, event_id)
        if ev.payload.get("chat_done"):  # a concurrent worker answered first
            return False
        ev.payload = {
            **ev.payload,
            "chat_done": True,
            "chat_role": role,
            **({"skip_reason": reason} if reason else {}),
        }
        if reason:
            ev.status = "skipped"
        tm = TelegramMessage(
            direction="out", chat_id=get_settings().telegram_owner_id, kind="reply", body=reply,
            inbound_event_id=event_id,
        )
        s.add(tm)
        await s.flush()
        tm_id = tm.id
        await s.commit()
    log.info("chat_answered", event_id=event_id, role=role, tokens=tokens)
    if role not in ("injection", "unavailable_ai"):
        await remember(redis, user_text, reply)
        from zoneinfo import ZoneInfo

        from apa import followup
        s_tz = ZoneInfo(get_settings().timezone)
        await followup.schedule_if_active_situation(redis, user_text, reply, s_tz, datetime.now(s_tz), llm=llm)
    await send_and_record(sm, notifier, tm_id, reply)
    return True


async def run_master(
    sm: async_sessionmaker,
    llm: LLMClient,
    notifier: Notifier,
    redis: Any | None,
    event_id: int,
    *,
    tz: ZoneInfo | None = None,
    job_try: int = 1,
    max_tries: int = 3,
) -> str:
    """The owner's free text that code could not classify. One small model call decides what it is; then
    only the agents that are needed run. Returns 'answered' | 'handoff' (a payment: the extractor continues)
    | 'skipped' | 'missing'. Raises LLMUnavailable while retries remain."""
    tz = tz or ZoneInfo(get_settings().timezone)
    async with sm() as s:
        ev = await _locked(s, event_id)
        if ev is None:
            return "missing"
        if ev.status != "needs_llm" or ev.source != "telegram_shorthand" or ev.payload.get("master_done"):
            return "skipped"
        text, sent_raw, received = ev.payload.get("text", ""), ev.payload.get("sent_at"), ev.received_at
        reply_to = ev.payload.get("reply_to")
    sent_at = datetime.fromisoformat(sent_raw) if sent_raw else received

    clean = sanitize_untrusted(text)
    if clean.suspicious:
        await _finish(sm, notifier, redis, event_id, clean.text, INJECTION, "injection", 0, reason="chat")
        return "answered"  # no model sees an instruction-bearing message

    tokens = 0
    try:
        history, draft = await load_history(redis), await batch.load_draft(redis)
        cfg = await llm.config() if hasattr(llm, "config") else None
        custom = cfg.active_custom() if cfg is not None else {}
        recent = await _recent_lines(sm, sent_at)
        p = await master.plan(llm, clean.text, history, has_draft=draft is not None, custom=custom, recent=recent)
        tokens = p.tokens
        reply, role = await _execute(sm, llm, redis, p, clean.text, history, draft, tz, sent_at, custom, reply_to)
    except LLM_ERRORS as e:
        if job_try < max_tries:
            raise LLMUnavailable(str(e)) from e
        reply, role = UNAVAILABLE, "unavailable_ai"

    if reply is None:  # a payment (or a list the model could not ground): the existing extractor takes over
        async with sm() as s:
            ev = await _locked(s, event_id)
            if ev.payload.get("master_done"):
                return "skipped"
            ev.payload = {**ev.payload, "master_done": True}
            await s.commit()
        if redis is not None:
            await redis.enqueue_job("extract_event", event_id)
        return "handoff"
    ok = await _finish(sm, notifier, redis, event_id, clean.text, reply, role, tokens, reason="chat")
    return "answered" if ok else "skipped"


async def _recent_lines(sm, sent_at: datetime) -> str:
    """The owner's committed transactions of the last 2 hours as '#id merchant amount time' lines, so the router can tell
    that a short message is about them."""
    from apa.core.format import rupiah

    async with sm() as s:
        txs = (await s.execute(
            select(Transaction).where(
                Transaction.status == "committed", Transaction.timestamp >= sent_at - timedelta(hours=2),
                Transaction.timestamp <= sent_at + timedelta(minutes=5)).order_by(Transaction.timestamp.desc()).limit(5)
        )).scalars().all()
    return "\n".join(
        f"#{t.id} {(t.merchant or '-')[:30]} {rupiah(t.total_paid_amount)} {t.direction} {t.timestamp:%H:%M}" for t in txs
    )


async def _described_transaction(sm, reply_to: int | None, sent_at: datetime) -> int | None:
    """The transaction a no-amount message is about: the one whose Telegram message the owner replied to, else the
    latest committed one of the last hour."""
    async with sm() as s:
        if reply_to is not None:
            tx = (await s.execute(
                select(TelegramMessage.transaction_id).where(
                    TelegramMessage.message_id == reply_to, TelegramMessage.transaction_id.is_not(None))
            )).scalar_one_or_none()
            if tx is not None:
                return tx
        return (await s.execute(
            select(Transaction.id).where(
                Transaction.status == "committed", Transaction.timestamp >= sent_at - timedelta(hours=2),
                Transaction.timestamp <= sent_at + timedelta(minutes=5)).order_by(Transaction.timestamp.desc()).limit(1)
        )).scalar_one_or_none()


async def _execute(
    sm, llm, redis, p: master.Plan, text: str, history: list[dict], draft, tz: ZoneInfo, sent_at: datetime,
    custom: dict | None = None,
    reply_to: int | None = None,
) -> tuple[str | None, str]:
    """(reply, role); reply None = hand the message to the payment extractor."""
    if p.intent == "transaction":
        if not re.search(r"\d", text):  # no amount: this describes a payment already recorded, it is not a new one
            # Duplicate / double-entry complaint: route to inspect + undo, not a new record
            _dup_words = re.compile(r"\b(double|duplikat|dobel|sama|dua kali|2x|kok ada \d|ada \d.{0,10}(sama|ganda))\b", re.I)
            if _dup_words.search(text):
                hint = ("[The owner is reporting a possible duplicate transaction. Look at the last 5 committed transactions "
                        "in DATA, identify which look identical (same amount, same time window), and propose undo_transaction "
                        f"on the duplicate one. Do not add a new transaction.] {text}")
                reply, _ = await plan_action(sm, llm, hint, history, tz, redis)
                return reply, "action"
            tx = await _described_transaction(sm, reply_to, sent_at)
            if tx is not None:
                hint = (f"[The owner is describing transaction #{tx} (the one they replied to, or their latest). Propose "
                        f"edit_transaction on #{tx} with the merchant, category or notes they describe; do not add a new "
                        f"transaction.] {text}")
                reply, _ = await plan_action(sm, llm, hint, history, tz, redis)
                return reply, "action"
            reply, _ = await converse(sm, llm, redis, text, _history_block(history), tz, datetime.now(tz), reply_to=reply_to)
            return reply, "general"  # nothing recent to describe and no amount: ask what the owner means
        return None, "transaction"
    if p.intent == "batch":
        items = await batch_flow.items_from_model(llm, text, sent_at, tz)
        if len(items) < 2:
            return None, "transaction"  # not really a list: let the single-payment extractor look at it
        return await batch_flow.start(sm, redis, items, tz), "batch"
    if p.intent == "draft_edit":
        return await batch_flow.edit_with_model(sm, redis, llm, draft, text, tz), "draft_edit"
    if p.intent == "ask" and p.agents:
        from apa.agents.personas import AGENTS

        facts_fin = await finance_snapshot(sm, tz)
        facts_health = await health_snapshot(sm, tz)

        async def facts_for(role: str) -> str:
            if custom and role in custom:
                parts = [await (finance_snapshot if d == "finance" else health_snapshot)(sm, tz) for d in custom[role].data]
                return "\n\n".join(parts) or facts_fin
            if role == "A-03":
                return facts_health
            return facts_fin

        # Specialists consult internally with Master. They NEVER chat directly to Telegram.
        answers = await asyncio.gather(*[ask_agent(llm, r, text, await facts_for(r), history, custom) for r in p.agents])
        specialist_reports = []
        for r, a in zip(p.agents, answers, strict=True):
            agent_obj = AGENTS.get(r)
            agent_name = custom[r].name if (custom and r in custom) else (agent_obj.name if agent_obj else r)
            specialist_reports.append(f"INPUT DARI {agent_name} ({r}) UNTUK MASTER:\n{a[0]}")
        internal_brief = "\n\n".join(specialist_reports)

        # Master is the single voice chatting to Telegram:
        prompt_with_brief = f"{text}\n\n[Catatan internal dari spesialis untuk Master (sintesiskan saran ini ke dalam jawabanmu, jangan kirim chat terpisah atau format mentah ke owner):\n{internal_brief}]"
        reply, _ = await converse(sm, llm, redis, prompt_with_brief, _history_block(history), tz, datetime.now(tz), reply_to=reply_to)
        return reply, "A-00"
    if p.intent == "action":
        reply, _ = await plan_action(sm, llm, text, history, tz, redis)
        return reply, "action"
    # everything else, a question, a greeting or "what can you do": the one assistant answers, with its look-ups, memory and skills
    reply, _ = await converse(sm, llm, redis, text, _history_block(history), tz, datetime.now(tz), reply_to=reply_to)
    return reply, "general"
