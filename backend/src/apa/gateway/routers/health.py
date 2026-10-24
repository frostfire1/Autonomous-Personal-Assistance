"""Health readings from the Android app (Health Connect). Same key as /ingest. Validated, idempotent."""

from fastapi import APIRouter, Depends

from apa.core.db import SessionLocal
from apa.gateway.routers.ingest import require_ingest_key
from apa.health.ingest import HealthIn, ingest

router = APIRouter(prefix="/health", tags=["health"], dependencies=[Depends(require_ingest_key)])


@router.post("/ingest")
async def ingest_health(body: HealthIn) -> dict:
    """Resending the same payload adds nothing, so the app may retry freely."""
    return {"ok": True, "added": await ingest(SessionLocal, body, source="health_connect_bridge")}
