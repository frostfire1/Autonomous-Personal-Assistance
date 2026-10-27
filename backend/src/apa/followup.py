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

# Fallback semantic patterns when LLM client is not provided (e.g. unit tests or offline)
_RE_IN_PROGRESS = re.compile(
    r"\b("
    r"still\s+(at|working|in|doing|busy|on|finishing|coding|debugging|studying)|"
    r"working\s+late|overtime|not\s+home\s+yet|at\s+the\s+office|"
    r"masih\s+(di\s+\w+|kerja|lembur|ngerjain|selesaiin|jalan)|"
    r"belum\s+(pulang|selesai|beres)|lagi\s+(lembur|ngerjain|di\s+\w+)"
    r")\b",
    re.I,
)
_RE_UPCOMING_SCHEDULE = re.compile(
    r"\b(tomorrow|tonight|later|besok|nanti|malam\s+ini)\b.*\b("
    r"leave|depart|flight|train|trip|meeting|schedule|agenda|wake\s+up|"
    r"berangkat|pergi|jadwal|kereta|pesawat|bangun|"
    r"\d{1,2}[:.]\d{2}|jam\s+\d{1,2}"
    r")\b|\b("
    r"berangkat\s+jam\s+\d{1,2}|leave\s+at\s+\d{1,2}|flight\s+at\s+\d{1,2}"
    r")\b",
    re.I,
)
_RE_WELLBEING = re.compile(
    r"\b("
    r"tired|exhausted|sleepy|drowsy|unwell|sick|headache|worn\s+out|sleep\s+deprived|"
    r"lelah|capek|ngantuk|pusing|kurang\s+tidur|lemas|sakit"
    r")\b",
    re.I,
)

FOLLOWUP_SYSTEM = (
    "You are APA, the owner's close friend and personal chief of staff. "
    "Earlier, the owner was in this situation: {hint}.\n\n"
    "Crucial Agentic Guidelines:\n"
    "- Check the LIVE CONTEXT (real-time laptop screen time, recent payments/rides, and sleep) below.\n"
    "- Synthesize their earlier situation with what they did in the meantime.\n"
    "- If their laptop is still actively running, acknowledge what they are doing lightly and gently "
    "remind them to wrap up or rest.\n"
    "- If they recorded a ride (Gojek/Grab) or meal, adapt naturally (e.g. ask if they arrived safely or "
    "enjoyed their meal).\n"
    "- If they have an early departure tomorrow, offer 1 concrete, actionable assistance (e.g. set a morning "
    "alarm, check weather/route).\n"
    "- Tone: warm, natural texting style of a trusted friend — never sound like a robotic bot, never use "
    "generic customer service phrases.\n"
    "- Write ONE short, caring message (1-2 sentences) in the owner's style (see STYLE). "
    "Reply with the message text only."
)


async def _last_owner_message(sm: async_sessionmaker) -> datetime | None:
    async with sm() as s:
        return (await s.execute(
            select(func.max(InboundEvent.received_at)).where(InboundEvent.source == "telegram_shorthand")
        )).scalar_one_or_none()


async def _gather_live_context(sm: async_sessionmaker | None, tz: ZoneInfo, now: datetime) -> dict[str, str]:
    """Gather real-time laptop screen time, recent payments/rides, and sleep context."""
    out = {
        "desktop": "No desktop activity recorded today",
        "transactions": "No recent payments in the last 3 hours",
        "sleep": "Normal baseline",
    }
    if sm is None:
        return out

    # 1. Desktop / Laptop Activity
    try:
        from apa.db.models.intel import AppUsage
        async with sm() as s:
            usages = (
                await s.execute(
                    select(AppUsage)
                    .where(AppUsage.day == now.date())
                    .order_by(AppUsage.last_at.desc().nullslast())
                    .limit(5)
                )
            ).scalars().all()
            if usages:
                top = usages[0]
                if top.last_at:
                    mins_ago = max(0, int((now - top.last_at.astimezone(tz)).total_seconds() / 60))
                    status_str = "Actively running" if mins_ago <= 15 else f"Last active {mins_ago}m ago"
                    out["desktop"] = (
                        f"{status_str}: {top.app} on {top.device} (total {top.seconds // 60}m today)"
                    )
                else:
                    out["desktop"] = f"Top app today: {top.app} ({top.seconds // 60}m)"
    except Exception as exc:
        log.debug("gather_desktop_context_failed", error=str(exc))

    # 2. Recent Transactions (rides, food, deliveries)
    try:
        from apa.db.models.ledger import Transaction
        cutoff = now - timedelta(hours=3)
        async with sm() as s:
            txs = (
                await s.execute(
                    select(Transaction)
                    .where(Transaction.timestamp >= cutoff)
                    .order_by(Transaction.timestamp.desc())
                    .limit(3)
                )
            ).scalars().all()
            if txs:
                items = []
                for t in txs:
                    m = t.merchant or "Payment"
                    t_time = t.timestamp.astimezone(tz) if t.timestamp.tzinfo else t.timestamp
                    is_transport = bool(re.search(
                        r"\b(gojek|grab|taxi|bluebird|mrt|krl|transjakarta|tiket)\b", m, re.I
                    ))
                    flag = " [RIDE / TRANSPORT DETECTED]" if is_transport else ""
                    items.append(f"{m} (Rp {int(t.total_paid_amount):,} at {t_time:%H:%M}){flag}")
                out["transactions"] = "; ".join(items)
    except Exception as exc:
        log.debug("gather_tx_context_failed", error=str(exc))

    # 3. Sleep / Wellbeing
    try:
        from apa.db.models.health import SleepSession
        async with sm() as s:
            sleep = (
                await s.execute(
                    select(SleepSession)
                    .order_by(SleepSession.sleep_date.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if sleep:
                h, m = divmod(sleep.total_sleep_mins or 0, 60)
                fatigue = (
                    " (Fatigue defense active / sleep-deprived!)" if sleep.fatigue_defense_active else ""
                )
                out["sleep"] = f"Last recorded sleep: {h}h {m}m (Score: {sleep.sleep_score or '-'}){fatigue}"
    except Exception as exc:
        log.debug("gather_sleep_context_failed", error=str(exc))

    return out


async def evaluate_active_situation_llm(
    llm: Any,
    user_text: str,
    assistant_reply: str,
    now: datetime,
) -> dict[str, Any] | None:
    """Use LLM to dynamically evaluate if an interaction leaves an open situation needing follow-up."""
    if llm is None:
        return None

    system_eval = (
        "You are an executive personal assistant analyzing a conversation turn. "
        "Determine if the owner is currently in an ongoing, pending, or active real-world situation "
        "that warrants a proactive check-in or follow-up within the next 20-60 minutes "
        "(e.g., working late/overtime, travelling tomorrow morning, feeling unwell/exhausted, in-transit).\n"
        'Return JSON only: {"active": true, "topic": "topic_slug", "delay_minutes": 30, '
        '"hint": "detailed description of situation and what to check on"} or {"active": false}'
    )
    user_content = f"User: {user_text}\nAssistant: {assistant_reply}\nCurrent time: {now:%A %H:%M}"
    try:
        res = await llm.complete(ROUTINE, system_eval, user_content, max_tokens=150, temperature=0.2)
        raw = (res.content or "").strip()
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1:
            data = json.loads(raw[start:end + 1])
            if data.get("active"):
                return {
                    "topic": data.get("topic", "active_situation"),
                    "delay_minutes": int(data.get("delay_minutes", 30)),
                    "hint": data.get("hint", user_text[:100]),
                }
    except Exception as exc:
        log.debug("dynamic_eval_failed", error=str(exc))
    return None


async def schedule_if_active_situation(
    redis: Any,
    user_text: str,
    assistant_reply: str,
    tz: ZoneInfo,
    now: datetime,
    *,
    llm: Any = None,
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

    # 1. LLM-driven dynamic evaluation (if LLM is available and topic wasn't forced)
    if not topic and llm is not None:
        eval_result = await evaluate_active_situation_llm(llm, user_text, assistant_reply, now)
        if eval_result:
            topic = eval_result["topic"]
            delay = eval_result["delay_minutes"]
            context_hint = eval_result["hint"]

    # 2. Semantic fallback when LLM is offline or not supplied
    if not topic:
        if _RE_IN_PROGRESS.search(combined):
            is_office = bool(re.search(r"\b(office|kantor|work|kerja|lembur)\b", combined, re.I))
            topic = "office_late" if is_office else "in_progress"
            delay = 30
            context_hint = (
                f"Owner mentioned an ongoing activity or working late ('{user_text[:100]}'). "
                "Check if they are wrapping up, remind them to eat and rest."
            )
        elif _RE_UPCOMING_SCHEDULE.search(combined):
            is_morning = bool(re.search(r"\b(morning|pagi|jam\s+[4-9]|at\s+[4-9])\b", combined, re.I))
            topic = "early_morning_plan" if is_morning else "upcoming_schedule"
            delay = 40
            context_hint = (
                f"Owner mentioned an upcoming departure or schedule ('{user_text[:100]}'). "
                "Check if everything is prepared and offer to set an alarm or check travel."
            )
        elif _RE_WELLBEING.search(combined):
            topic = "health_fatigue"
            delay = 35
            context_hint = (
                f"Owner mentioned feeling tired or unwell ('{user_text[:100]}'). "
                "Check in warmly on how they are feeling and encourage rest."
            )

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

    # Dynamic Live Context Gathering
    live_ctx = await _gather_live_context(sm, tz, now)
    desktop_info = live_ctx.get("desktop", "None")
    tx_info = live_ctx.get("transactions", "None")
    sleep_info = live_ctx.get("sleep", "None")

    from apa import profile
    from apa.chat import agent

    hint = data.get("context_hint", data.get("topic", "active task"))
    guide = await profile.style_guide(sm)
    system = FOLLOWUP_SYSTEM.format(hint=hint)
    recent_history = agent._history_block(await agent.load_history(redis))

    user_prompt = (
        f"STYLE:\n{guide}\n\n"
        f"ORIGINAL CONTEXT:\n{hint}\n\n"
        f"LIVE CONTEXT (OBSERVED AS OF {now:%A %H:%M} {tz}):\n"
        f"- Laptop/Desktop Screen Time: {desktop_info}\n"
        f"- Recent Transactions / Commute: {tx_info}\n"
        f"- Sleep / Wellbeing: {sleep_info}\n\n"
        f"RECENT CONVERSATION:\n{recent_history}\n\n"
        "Draft the single proactive check-in message now:"
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
