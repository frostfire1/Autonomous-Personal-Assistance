from contextlib import asynccontextmanager

from arq import create_pool
from fastapi import FastAPI

from apa import currency
from apa.core.db import SessionLocal
from apa.core.logging import configure_logging
from apa.core.redis import arq_redis_settings
from apa.gateway.routers import briefing, dashboard, health, ingest, inspect, llm, news, prefs, prices, setup
from apa.integrations.llm import LLMClient


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    app.state.arq = await create_pool(arq_redis_settings())
    app.state.llm = LLMClient()
    refresher = currency.start_refresher(SessionLocal)
    yield
    refresher.cancel()
    await app.state.llm.aclose()
    await app.state.arq.aclose()


app = FastAPI(title="Autonomous Personal Assistance", version="0.1.0", lifespan=lifespan)
app.include_router(ingest.router)
app.include_router(inspect.router)
app.include_router(briefing.router)
app.include_router(health.router)
app.include_router(prices.router)
app.include_router(dashboard.router)
app.include_router(prefs.router)
app.include_router(llm.router)
app.include_router(setup.router)
app.include_router(news.router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
