"""Shared fixtures for tests that need a real Postgres + Redis (see test_ingest_e2e.py)."""

import os

import pytest

DB = os.environ.get("TEST_DATABASE_URL")
REDIS = os.environ.get("TEST_REDIS_URL")
HAVE_INFRA = bool(DB and REDIS)


@pytest.fixture
async def stack(monkeypatch):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    monkeypatch.setenv("DATABASE_URL", DB)
    monkeypatch.setenv("REDIS_URL", REDIS)
    monkeypatch.setenv("INGEST_API_KEY", "test-key")
    monkeypatch.setenv("WEATHER_ENABLED", "false")  # tests never reach Open-Meteo unless they say so
    from apa.core.settings import get_settings

    get_settings.cache_clear()

    # Queued jobs outlive a test, so reset Redis too. FLUSHDB is destructive: only allow it on a
    # non-default database index, never on db 0 where a real instance would live.
    from urllib.parse import urlparse

    from redis.asyncio import Redis

    if urlparse(REDIS).path in ("", "/", "/0"):
        pytest.fail("TEST_REDIS_URL must name a dedicated database (e.g. /15), not db 0")
    redis = Redis.from_url(REDIS)
    await redis.flushdb()
    await redis.aclose()

    engine = create_async_engine(DB)
    async with engine.begin() as c:  # clean slate
        await c.execute(text("DROP SCHEMA public CASCADE"))
        await c.execute(text("CREATE SCHEMA public"))

    cfg = Config(os.path.join(os.path.dirname(__file__), "..", "..", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(os.path.dirname(__file__), "..", "..", "alembic"))
    # env.py is async and calls asyncio.run, so migrate from a worker thread
    import asyncio

    await asyncio.to_thread(command.upgrade, cfg, "head")

    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()
    get_settings.cache_clear()
