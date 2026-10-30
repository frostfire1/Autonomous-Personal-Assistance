"""The data steward (A-14, an addition to the blueprint's roster): every few minutes it looks for data that fell
through, repairs what it safely can, and tells the owner in ONE message what it did and what it could not.

Strictly get / add / edit: `finder` reads, `fixers` add or edit through the guarded pipeline, nothing deletes."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import async_sessionmaker

from apa.core.logging import get_logger
from apa.pipeline.commit import record_reply
from apa.steward import fixers
from apa.steward.finder import find

log = get_logger(__name__)

MAX_ACTIONS = 10  # per run: a backlog is worked off over several runs, never all at once
LOCK_KEY = "apa:steward:lock"
LOCK_TTL_S = 540


@dataclass
class Report:
    done: list[str] = field(default_factory=list)
    needs_you: list[str] = field(default_factory=list)
    scanned: int = 0

    def text(self) -> str | None:
        if not self.done and not self.needs_you:
            return None
        lines = []
        if self.done:
            lines += ["🛠️ Perbaikan otomatis:"] + [f"• {d}" for d in self.done]
        if self.needs_you:
            header = (
                "⚠️ Perlu dilihat (tidak bisa diperbaiki otomatis):"
                if self.done
                else "⚠️ Perlu dilihat:"
            )
            lines += [header] + [f"• {n}" for n in self.needs_you]
        return "\n".join(lines)


async def run_steward(
    sm: async_sessionmaker,
    llm: Any,
    store: Any,
    notifier: Any,
    redis: Any,
    *,
    tz: ZoneInfo,
    now: datetime | None = None,
) -> Report:
    """One pass. Safe to call concurrently: a Redis lock lets only one pass run at a time."""
    now = now or datetime.now(tz)
    if redis is not None and not await redis.set(LOCK_KEY, "1", nx=True, ex=LOCK_TTL_S):
        return Report()
    report = Report()
    try:
        tickets = await find(sm, now)
        report.scanned = len(tickets)
        acted = 0
        for t in tickets:
            if acted >= MAX_ACTIONS:
                break
            try:
                if t.kind == "stuck":
                    fix = await fixers.fix_stuck(sm, redis, t)
                elif t.kind == "failed_commit":
                    fix = await fixers.fix_failed_commit(sm, redis, t)
                elif t.kind == "image":
                    fix = await fixers.fix_image(sm, llm, store, redis, t, tz)
                elif t.kind == "uncategorized":
                    fix = await fixers.fix_category(sm, llm, t, tz, now)
                else:
                    fix = await fixers.fix_held(sm, redis, t)
            except Exception as e:  # one bad ticket must not stop the rest, or the next run
                log.warning("steward_ticket_failed", kind=t.kind, error=type(e).__name__)
                continue
            if fix is None or not fix.line:
                continue
            acted += 1
            (report.needs_you if fix.unfixable else report.done).append(fix.line)
        text = report.text()
        if text:
            needs_hash = None
            if report.needs_you and redis is not None:
                import hashlib

                needs_hash = hashlib.sha256("\n".join(sorted(report.needs_you)).encode()).hexdigest()
                last_hash = await redis.get("apa:steward:last_needs_hash")
                if isinstance(last_hash, bytes):
                    last_hash = last_hash.decode("utf-8", errors="replace")
                if last_hash == needs_hash:
                    log.info("steward_suppressed_duplicate_needs_you", count=len(report.needs_you))
                    report.needs_you = []
                    text = report.text()
            if text:
                await record_reply(sm, notifier, text)
                if needs_hash and redis is not None:
                    await redis.set("apa:steward:last_needs_hash", needs_hash, ex=43200)
        log.info(
            "steward_ran", scanned=report.scanned, done=len(report.done), needs_you=len(report.needs_you)
        )
    finally:
        if redis is not None:
            await redis.delete(LOCK_KEY)
    return report
