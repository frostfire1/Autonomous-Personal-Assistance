"""Staple prices pushed from anywhere (a script, a scraper, the owner's phone). Same key as /ingest. Bounded and validated."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from apa.core.db import SessionLocal
from apa.gateway.routers.ingest import require_ingest_key
from apa.prices.check import Observation, ingest_observations

router = APIRouter(prefix="/prices", tags=["prices"], dependencies=[Depends(require_ingest_key)])


class PricesIn(BaseModel):
    observations: list[Observation] = Field(min_length=1, max_length=500)


@router.post("/ingest")
async def ingest_prices(body: PricesIn) -> dict:
    """Unknown commodities and implausible prices are skipped; the same day and region is overwritten, so retries are safe."""
    return {"ok": True, "stored": await ingest_observations(SessionLocal, body.observations)}
