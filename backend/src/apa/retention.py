"""Chat retention: keep the raw chat for a few days only, because the diary and the learned items keep what mattered.

The assistant's memory of a day lives in the diary entry and the learned items written from it; the raw messages are then just
more plain text to store (and to leak) and more rows to search. Every night, after the day's diary is written:

  1. days of the last 30 that have chat but no diary entry yet (a model outage, a day missed) are summarised first, up to 5
     a run, so nothing is thrown away before it was captured;
  2. the text of chat messages older than CHAT_KEEP_DAYS (default 7; 0 = keep for ever) is erased: the owner's messages that
     were chat (not payments) and the assistant's replies that were not a ledger confirmation.

What is never touched: payments and everything the ledger and its audit trail rest on (the event of a recorded transaction, the
confirmation message with its undo link), the diary, the learned items, the notes, and the private journal. /private chats are
never stored at all."""

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from apa import assistant_memory
from apa.core.logging import get_logger

log = get_logger(__name__)

CATCH_UP_DAYS = 30
CATCH_UP_PER_RUN = 5


async def catch_up(sm: async_sessionmaker, llm: Any, tz: ZoneInfo, today: date) -> int:
    """Write the diary entry of recent days that have chat but no entry. -> how many were written."""
    have = {e["date"] for e in await assistant_memory.episodes(sm)}
    written = 0
    for back in range(1, CATCH_UP_DAYS + 1):
        day = today - timedelta(days=back)
        if day.isoformat() in have:
            continue
        try:
            if await assistant_memory.summarize_day(sm, llm, day, tz):
                written += 1
        except Exception as exc:  # the model is down: stop here, tomorrow's run tries again (nothing is pruned for these days)
            log.warning("diary_catch_up_failed", error=type(exc).__name__)
            break
        if written >= CATCH_UP_PER_RUN:
            break
    return written


async def undigested_days(sm: async_sessionmaker, tz: ZoneInfo, cutoff: datetime) -> set[date]:
    """Local days before `cutoff` that still have at least two chat messages from the owner but no diary entry: their text is
    kept until a summary exists."""
    have = {e["date"] for e in await assistant_memory.episodes(sm)}
    async with sm() as s:
        rows = (await s.execute(text(
            "SELECT (received_at AT TIME ZONE :tz)::date AS d, count(*) FROM inbound_events "
            "WHERE source = 'telegram_shorthand' AND received_at < :cutoff AND payload ? 'text' "
            "GROUP BY d HAVING count(*) >= :n"), {"tz": str(tz), "cutoff": cutoff, "n": assistant_memory.MIN_OWNER_MESSAGES})).all()
    return {d for d, _ in rows if d.isoformat() not in have}


async def prune(sm: async_sessionmaker, tz: ZoneInfo, now: datetime, keep_days: int) -> dict:
    """Erase the text of chat older than `keep_days` (not of days still waiting for their diary entry). -> counts."""
    if keep_days <= 0:
        return {"events": 0, "replies": 0, "kept_for_diary": 0}
    cutoff = (now - timedelta(days=keep_days)).replace(hour=0, minute=0, second=0, microsecond=0)
    waiting = await undigested_days(sm, tz, cutoff)
    keep = [d.isoformat() for d in waiting] or ["1900-01-01"]
    async with sm() as s:
        events = await s.execute(text(
            "UPDATE inbound_events SET payload = (payload - 'text') || jsonb_build_object('text_pruned', true) "
            "WHERE source = 'telegram_shorthand' AND received_at < :cutoff AND payload ? 'text' "
            "AND (status IN ('skipped', 'needs_review') OR payload->>'chat_done' = 'true') "
            "AND (received_at AT TIME ZONE :tz)::date::text <> ALL(:keep)"), {"cutoff": cutoff, "tz": str(tz), "keep": keep})
        replies = await s.execute(text(
            "UPDATE telegram_messages SET body = NULL WHERE direction = 'out' AND kind = 'reply' AND transaction_id IS NULL "
            "AND body IS NOT NULL AND created_at < :cutoff AND (created_at AT TIME ZONE :tz)::date::text <> ALL(:keep)"),
            {"cutoff": cutoff, "tz": str(tz), "keep": keep})
        await s.commit()
    out = {"events": events.rowcount, "replies": replies.rowcount, "kept_for_diary": len(waiting)}
    log.info("chat_pruned", **out)
    return out
