"""Autonomous proactive task follow-up engine.

Ensures the assistant doesn't abandon tasks or leave ongoing situations hanging.
When the owner mentions an ongoing situation (working late at the office, early morning commitments,
waiting on research, health fatigue), this engine tracks the active context in Redis.
If the owner goes quiet for 20-45 minutes, the assistant proactively reaches out on Telegram
with a caring, relevant follow-up before the user has to restart the chat.
"""

import json
import re
import time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from apa.agents.personas import ROUTINE
from apa.core.logging import get_logger
from apa.core.settings import get_settings
from apa.db.models.ops import InboundEvent, TelegramMessage

log = get_logger(__name__)

ACTIVE_KEY = "apa:followup:pending"
LAST_SENT_KEY = "apa:followup:last_sent"
COOLDOWN_SECONDS = 45 * 60  # At least 45 minutes between autonomous follow-ups
MIN_IDLE_MINUTES = 15      # Owner must be idle for at least 15 minutes before we ping

# Detection regexes
_RE_OFFICE_LATE = re.compile(r"\b(masih di kantor|lembur|masih kerja|belum pulang|di kantor|kerjaan)\b", re.I)
_RE_EARLY_MORNING = re.compile(r"\b(besok.*(jam \d|ke \w+|pagi)|berangkat jam \d|ke ncsc|jadwal besok)\b", re.I)
_RE_TIRED = re.compile(r"\b(lemas|capek|pusing|kurang tidur|ngantuk|lelah)\b", re.I)

FOLLOWUP_SYSTEM = (
    "You are APA, the owner's close friend and personal chief of staff. "
    "Earlier, the owner was in this situation: {hint}. "
    "Some time has passed and they have been quiet. Write ONE short, natural, caring message "
    "(1-2 sentences) in the owner's style (see STYLE). "
    "Check in on their status, gently bring up the next practical step or reminder, and offer concrete help. "
    "Sound like a real friend texting on Telegram — no robotic greetings like 'Halo aku APA', "
    "no generic customer-service phrases. If they are working late, remind them gently about getting rest. "
    "Reply with the message text only."
)


async def _last_owner_message(sm: async_sessionmaker) -> datetime | None:
    async with sm() as s:
        return (await s.execute(
            select(func.max(InboundEvent.received_at)).where(InboundEvent.source == "telegram_shorthand")
        )).scalar_one_or_none()


async def schedule_if_active_situation(
    redis: Any,
    user_text: str,
    assistant_reply: str,
    tz: ZoneInfo,
    now: datetime,
    *,
    force_topic: str | None = None,
    delay_minutes: int | None = None,
    hint: str | None = None,
) -> bool:
    """Schedule a proactive follow-up if user_text or context represents an open situation."""
    if redis is None:
        return False

    topic = force_topic
    delay = delay_minutes or 30
    context_hint = hint

    combined = f"{user_text} {assistant_reply}"
    hour = now.hour + now.minute / 60

    if not topic:
        if _RE_OFFICE_LATE.search(combined):
            topic = "office_late"
            delay = 30
            context_hint = "Owner is still working late at the office. Check if they are heading home, remind them to eat and rest."
        elif _RE_EARLY_MORNING.search(combined):
            topic = "early_morning_plan"
            delay = 40
            context_hint = "Owner has an early morning departure/schedule tomorrow. Check if everything is prepared and offer to set an alarm."
        elif _RE_TIRED.search(combined):
            topic = "health_fatigue"
            delay = 35
            context_hint = "Owner mentioned feeling tired or unwell. Check in warmly on how they are feeling."

    if not topic:
        return False

    due_at = now + timedelta(minutes=delay)
    payload = {
        "topic": topic,
        "due_at": due_at.timestamp(),
        "created_at": now.timestamp(),
        "user_text": user_text[:300],
        "context_hint": context_hint or topic,
    }

    try:
        await redis.set(ACTIVE_KEY, json.dumps(payload, ensure_ascii=False), ex=6 * 3600)
        log.info("followup_scheduled", topic=topic, delay_minutes=delay, due_at=str(due_at))
        return True
    except Exception as exc:
        log.warning("followup_schedule_failed", error=str(exc))
        return False


async def dismiss_pending(redis: Any) -> None:
    """Clear any pending follow-up when the owner actively resumes conversation."""
    if redis is None:
        return
    try:
        await redis.delete(ACTIVE_KEY)
    except Exception as exc:
        log.warning("followup_dismiss_failed", error=str(exc))


async def check_and_run_followup(
    sm: async_sessionmaker,
    llm: Any,
    redis: Any,
    notifier: Any,
    tz: ZoneInfo,
    now: datetime,
) -> str:
    """Run pending proactive follow-up if due and owner has been idle. -> status string."""
    if redis is None or notifier is None or llm is None:
        return "no_service"

    try:
        raw = await redis.get(ACTIVE_KEY)
    except Exception as exc:
        log.warning("followup_get_failed", error=str(exc))
        return "redis_error"

    if not raw:
        return "none"

    try:
        data = json.loads(raw)
    except Exception:
        await redis.delete(ACTIVE_KEY)
        return "bad_payload"

    due_at = float(data.get("due_at", 0))
    if now.timestamp() < due_at:
        return "not_due"

    # Check if owner has been actively chatting lately
    try:
        last_owner = await _last_owner_message(sm)
        if last_owner is not None:
            idle_delta = now - last_owner.astimezone(tz)
            if idle_delta < timedelta(minutes=MIN_IDLE_MINUTES):
                log.info("followup_deferred_owner_active", idle_minutes=idle_delta.total_seconds() / 60)
                return "user_active"
    except Exception as exc:
        log.warning("followup_last_message_check_failed", error=str(exc))

    # Cooldown check: don't bombard user with multiple follow-ups
    try:
        last_sent = await redis.get(LAST_SENT_KEY)
        if last_sent and (time.time() - float(last_sent)) < COOLDOWN_SECONDS:
            return "too_soon"
    except Exception:
        pass

    # Generate personalized message
    from apa import profile
    from apa.chat import agent

    hint = data.get("context_hint", data.get("topic", "active task"))
    guide = await profile.style_guide(sm)
    system = FOLLOWUP_SYSTEM.format(hint=hint)
    recent_history = agent._history_block(await agent.load_history(redis))

    user_prompt = (
        f"STYLE:\n{guide}\n\n"
        f"RECENT CONVERSATION:\n{recent_history}\n\n"
        f"TIME: {now:%A %H:%M}\n"
        f"TASK/SITUATION: {hint}\n"
        "Draft the single friendly follow-up message now:"
    )

    try:
        res = await llm.complete(ROUTINE, system, user_prompt, max_tokens=250, temperature=0.5, agent="A-00")
        msg_text = " ".join((res.content or "").split())
    except Exception as exc:
        log.warning("followup_llm_failed", error=type(exc).__name__)
        return "llm_failed"

    if len(msg_text) < 10 or msg_text.upper().startswith("NOTHING"):
        return "empty_reply"

    s = get_settings()
    try:
        message_id = await notifier.send(msg_text)
        await agent.remember_assistant(redis, msg_text)
        async with sm() as db:
            db.add(TelegramMessage(
                direction="out",
                chat_id=s.telegram_owner_id or 0,
                message_id=message_id,
                kind="reply",
                body=msg_text,
            ))
            await db.commit()

        await redis.delete(ACTIVE_KEY)
        await redis.set(LAST_SENT_KEY, str(time.time()), ex=86400)
        log.info("followup_sent", topic=data.get("topic"), text=msg_text[:80])
        return "sent"
    except Exception as exc:
        log.warning("followup_send_failed", error=str(exc))
        return "send_failed"
