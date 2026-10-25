from contextlib import AsyncExitStack, asynccontextmanager

from arq import create_pool
from fastapi import FastAPI

from apa import currency, mcp_server
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
    async with AsyncExitStack() as stack:
        await mcp_server.run_lifespan(stack, _mcp)  # its session manager lives as long as the app
        yield
    refresher.cancel()
    await app.state.llm.aclose()
    await app.state.arq.aclose()


app = FastAPI(title="Autonomous Personal Assistance", version="0.1.0", lifespan=lifespan)
_mcp = mcp_server.mount(app)  # None unless MCP_ENABLED=true
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
