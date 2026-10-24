"""Read-only inspector API: what was sent, what the system answered, and why. Same key as /ingest."""

from dataclasses import asdict
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query

from apa.core.db import SessionLocal
from apa.core.settings import get_settings
from apa.gateway.routers.ingest import require_ingest_key
from apa.inspector import timeline as tl

router = APIRouter(prefix="/inspect", tags=["inspect"], dependencies=[Depends(require_ingest_key)])


@router.get("/timeline")
async def get_timeline(
    limit: int = Query(10, ge=1, le=50), before_id: int | None = Query(None, ge=1)
) -> list[dict]:
    """Newest first. Page backwards with `before_id` = the last event id you received."""
    tz = ZoneInfo(get_settings().timezone)
    rows = await tl.timeline(SessionLocal, tz, limit=limit, before_id=before_id)
    return [asdict(r) for r in rows]


@router.get("/events/{event_id}")
async def get_event(event_id: int) -> dict:
    """One exchange with the audit trail and the agents' arguments behind its transaction."""
    d = await tl.detail(SessionLocal, ZoneInfo(get_settings().timezone), event_id)
    if d is None:
        raise HTTPException(404, "no such event")
    return asdict(d)
