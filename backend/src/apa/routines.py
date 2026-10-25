"""Routines: things the assistant does by itself on a schedule, which the owner sets up once (dashboard: Routines, or by asking in chat).

Three kinds:
- **remind**: a reminder in the owner's own style ("time to log lunch"), at a time of day on chosen days, or every N minutes;
- **ask**: the assistant is given a task and does it like in a chat, with its look-ups and skills, and tells the owner what came of it
  (any change it wants to make still needs the owner's tap);
- **tool**: one skill (a tool of a connected MCP server, e.g. a smart-home action) called with fixed arguments. The owner approved it
  when creating the routine, so it runs without asking, but only while that tool is still allowed on the Skills page.

A tick every minute (the worker) runs what is due, once. Limits: at most `MAX_PER_DAY` runs a day in all, an interval of at least 15
minutes (and only between 08:00 and 21:30), and every run that fails is reported to the owner."""

import json
import re
import time
from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from apa.core.db import SessionLocal
from apa.core.logging import get_logger
from apa.core.settings import get_settings
from apa.db.models.budget import PolicyRule

log = get_logger(__name__)

RULE_KEY = "routines"
MAX_ROUTINES = 20
MAX_PER_DAY = 60
MIN_INTERVAL = 15
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


class Routine(BaseModel):
    id: str = Field(min_length=2, max_length=30)
    name: str = Field(min_length=1, max_length=60)
    kind: Literal["remind", "ask", "tool"]
    enabled: bool = True
    at: str = ""  # "HH:MM", every selected day
    days: list[int] = Field(default_factory=list)  # 0 = Monday .. 6 = Sunday; empty = every day
    every_min: int = 0  # or every N minutes (15 or more) between 08:00 and 21:30
    text: str = Field(default="", max_length=400)  # the reminder, or the task for the assistant
    server: str = ""
    tool: str = ""
    arguments: dict = Field(default_factory=dict)
    notify: Literal["always", "error"] = "always"
    last_run: float = 0.0

    @field_validator("at")
    @classmethod
    def _clock(cls, v: str) -> str:
        if v and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v):
            raise ValueError("time is HH:MM")
        return v

    @field_validator("days")
    @classmethod
    def _days(cls, v: list[int]) -> list[int]:
        return sorted({d for d in v if 0 <= d <= 6})

    def when(self) -> str:
        days = "every day" if not self.days or len(self.days) == 7 else ", ".join(DAYS[d] for d in self.days)
        return f"every {self.every_min} min" if self.every_min else f"{self.at} · {days}"


def check(r: Routine) -> Routine:
    """Reject a routine that cannot run: no schedule, a too-short interval, or no text/tool for its kind."""
    if not r.at and r.every_min < MIN_INTERVAL:
        raise ValueError(f"Give a time of day, or an interval of at least {MIN_INTERVAL} minutes.")
    if r.at and r.every_min:
        raise ValueError("Use a time of day or an interval, not both.")
    if r.kind in ("remind", "ask") and len(r.text.strip()) < 3:
        raise ValueError("Say what to remind about, or what the assistant should do.")
    if r.kind == "tool" and not (r.server and r.tool):
        raise ValueError("Pick the skill (server and tool) to call.")
    return r


def is_due(r: Routine, now: datetime, now_ts: float) -> bool:
    """Pure: should this routine run in the minute of `now`? (The tick also remembers the run so it happens once.)"""
    if not r.enabled:
        return False
    if r.every_min:
        if not (8 <= now.hour + now.minute / 60 <= 21.5):
            return False
        return now_ts - r.last_run >= r.every_min * 60 - 20
    if r.days and now.weekday() not in r.days:
        return False
    return f"{now:%H:%M}" == r.at


# ---- storage ---------------------------------------------------------------------------------------------------------------------------


async def load(sm: async_sessionmaker = SessionLocal) -> list[Routine]:
    async with sm() as s:
        row = (await s.execute(select(PolicyRule).where(PolicyRule.rule_key == RULE_KEY))).scalar_one_or_none()
    out = []
    for d in (row.value_json.get("routines", []) if row is not None and isinstance(row.value_json, dict) else []):
        try:
            out.append(Routine(**d))
        except ValueError:
            continue
    return out


async def save(routines: list[Routine], sm: async_sessionmaker = SessionLocal) -> None:
    data = {"routines": [r.model_dump() for r in routines]}
    async with sm() as s:
        stmt = insert(PolicyRule).values(rule_key=RULE_KEY, scope="global", scope_ref="", value_json=data, updated_by="owner", is_active=True)
        await s.execute(stmt.on_conflict_do_update(index_elements=["rule_key", "scope", "scope_ref"], set_={"value_json": data, "updated_by": "owner"}))
        await s.commit()


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:30]


async def add(sm: async_sessionmaker, **fields: Any) -> Routine:
    routines = await load(sm)
    if len(routines) >= MAX_ROUTINES:
        raise ValueError(f"At most {MAX_ROUTINES} routines.")
    r = Routine(id=slug(str(fields.get("name", ""))) or "routine", **{k: v for k, v in fields.items() if k != "id"})
    if any(x.id == r.id for x in routines):
        r.id = f"{r.id[:24]}_{int(time.time()) % 10000}"
    check(r)
    await save([*routines, r], sm)
    return r


async def remove(sm: async_sessionmaker, routine_id: str) -> bool:
    routines = await load(sm)
    kept = [r for r in routines if r.id != routine_id and r.name.lower() != routine_id.lower()]
    if len(kept) == len(routines):
        return False
    await save(kept, sm)
    return True


async def toggle(sm: async_sessionmaker, routine_id: str, enabled: bool) -> bool:
    routines = await load(sm)
    hit = [r for r in routines if r.id == routine_id]
    for r in hit:
        r.enabled = enabled
    if hit:
        await save(routines, sm)
    return bool(hit)


def summary_lines(routines: list[Routine], limit: int = 10) -> str:
    """For the assistant: what already exists, so it does not create the same routine twice."""
    return "\n".join(f"- {r.name} ({r.kind}, {r.when()}){'' if r.enabled else ' [off]'}" for r in routines[:limit])


# ---- running ---------------------------------------------------------------------------------------------------------------------------

REMIND_SYSTEM = (
    "You are the owner's close friend. Remind them of the thing below in ONE short chat message (at most 2 sentences), in exactly their way "
    "of writing (see STYLE), friendly and without nagging. Keep any time, number or name exactly. Reply with the message text only."
)


async def _remind_text(llm: Any, guide: str, text: str) -> str:
    from apa.agents.personas import ROUTINE

    try:
        res = await llm.complete(ROUTINE, REMIND_SYSTEM, f"STYLE:\n{guide}\n\nREMIND ABOUT: {text}", max_tokens=100, temperature=0.6, agent="A-00")
        out = " ".join((res.content or "").split())
        if 8 <= len(out) <= 300:
            return out
    except Exception as exc:
        log.warning("routine_remind_failed", error=type(exc).__name__)
    return f"⏰ {text}"


async def execute(ctx: dict, r: Routine, now: datetime, tz: ZoneInfo) -> str:
    """Run one routine now. -> "sent", "ran" (a tool, quietly) or "failed"."""
    from apa import mcp_client, profile
    from apa.chat import agent
    from apa.chat.assistant import converse

    sm = ctx.get("sessionmaker", SessionLocal)
    notifier, llm, redis = ctx["notifier"], ctx.get("llm"), ctx.get("redis")
    try:
        if r.kind == "remind":
            await notifier.send(await _remind_text(llm, await profile.style_guide(sm), r.text))
            return "sent"
        if r.kind == "ask":
            hist = agent._history_block(await agent.load_history(redis))
            reply, _ = await converse(sm, llm, redis, f"[Scheduled task «{r.name}»] {r.text}", hist, tz, now)
            await notifier.send(reply)
            await agent.remember_assistant(redis, reply)
            return "sent"
        text = await mcp_client.run(sm, r.server, r.tool, r.arguments)
        if r.notify == "always":
            await notifier.send(f"✅ {r.name}: {text[:300]}")
        return "ran"
    except Exception as exc:
        log.warning("routine_failed", routine=r.id, error=type(exc).__name__)
        try:
            await notifier.send(f"⚠️ Rutinitas «{r.name}» gagal: {str(exc)[:200]}")
        except Exception:
            pass
        return "failed"


async def tick(ctx: dict, now: datetime | None = None) -> list[str]:
    """Run what is due this minute. -> the ids that ran."""
    s = get_settings()
    tz = ZoneInfo(s.timezone)
    now = now or datetime.now(tz)
    sm = ctx.get("sessionmaker", SessionLocal)
    redis = ctx.get("redis")
    routines = await load(sm)
    ran: list[str] = []
    changed = False
    for r in routines:
        if not is_due(r, now, time.time()):
            continue
        if redis is not None:
            if not await redis.set(f"apa:routine:{r.id}:{now:%Y%m%d%H%M}", "1", nx=True, ex=3600):
                continue  # another tick already ran it this minute
            n = await redis.incr(f"apa:routine:count:{now:%Y%m%d}")
            await redis.expire(f"apa:routine:count:{now:%Y%m%d}", 2 * 86400)
            if n > MAX_PER_DAY:
                break
        r.last_run = time.time()
        changed = True
        await execute(ctx, r, now, tz)
        ran.append(r.id)
    if changed:
        await save(routines, sm)
    return ran


# ---- the confirmation card for a routine the assistant proposes in chat ----------------------------------------------------------------


def describe(r: Routine) -> str:
    what = {"remind": f"ingatkan: {r.text}", "ask": f"asisten mengerjakan: {r.text}", "tool": f"jalankan skill {r.server}/{r.tool} {json.dumps(r.arguments, ensure_ascii=False)[:120]}"}[r.kind]
    return f"Buat rutinitas «{r.name}»: {what} · {r.when()}"
