"""The ingest guard: the shared key, the optional HMAC signature over the request, and the per-minute cap."""

import os
import time

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis

from apa.core.settings import get_settings
from apa.gateway.routers import ingest as ingest_mod
from apa.gateway.routers.ingest import require_ingest_key, sign
from tests.integration.conftest import HAVE_INFRA

infra = pytest.mark.skipif(not HAVE_INFRA, reason="TEST_DATABASE_URL/TEST_REDIS_URL not set")
SECRET = "s3cret-for-tests"


def make_app(redis=None):
    from fastapi import Depends

    app = FastAPI()
    if redis is not None:
        app.state.arq = redis

    @app.post("/x", dependencies=[Depends(require_ingest_key)])
    async def x(body: dict) -> dict:
        return {"ok": True}

    return app


async def post(app, body=b'{"a":1}', headers=None):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        return await c.post("/x", content=body, headers={"content-type": "application/json", **(headers or {})})


def signed(body=b'{"a":1}', ts=None, secret=SECRET, path="/x"):
    ts = str(int(time.time()) if ts is None else ts)
    return {"x-api-key": "k", "x-timestamp": ts, "x-signature": sign(secret, ts, "POST", path, body)}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("INGEST_API_KEY", "k")
    monkeypatch.delenv("INGEST_HMAC_SECRET", raising=False)
    monkeypatch.setenv("INGEST_RATE_PER_MIN", "0")
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


async def test_without_a_secret_only_the_key_is_needed(env):
    assert (await post(make_app(), headers={"x-api-key": "k"})).status_code == 200
    assert (await post(make_app(), headers={"x-api-key": "wrong"})).status_code == 401
    assert (await post(make_app())).status_code == 401


async def test_with_a_secret_a_valid_fresh_signature_is_required(env):
    env.setenv("INGEST_HMAC_SECRET", SECRET)
    get_settings.cache_clear()
    app = make_app()
    assert (await post(app, headers=signed())).status_code == 200
    assert (await post(app, headers={"x-api-key": "k"})).status_code == 401  # unsigned


async def test_a_signature_covers_the_body_the_path_and_the_secret(env):
    env.setenv("INGEST_HMAC_SECRET", SECRET)
    get_settings.cache_clear()
    app = make_app()
    assert (await post(app, body=b'{"a":2}', headers=signed(b'{"a":1}'))).status_code == 401  # body changed
    assert (await post(app, headers=signed(path="/other"))).status_code == 401  # signed for another path
    assert (await post(app, headers=signed(secret="not-it"))).status_code == 401
    bad = signed()
    bad["x-signature"] = bad["x-signature"][:-1] + ("0" if bad["x-signature"][-1] != "0" else "1")
    assert (await post(app, headers=bad)).status_code == 401


@pytest.mark.parametrize("skew", [301, -301, 100000])
async def test_a_stale_or_future_signature_is_a_replay(env, skew):
    env.setenv("INGEST_HMAC_SECRET", SECRET)
    get_settings.cache_clear()
    assert (await post(make_app(), headers=signed(ts=int(time.time()) - skew))).status_code == 401


async def test_a_garbage_timestamp_is_refused_not_a_crash(env):
    env.setenv("INGEST_HMAC_SECRET", SECRET)
    get_settings.cache_clear()
    h = signed()
    h["x-timestamp"] = "yesterday"
    assert (await post(make_app(), headers=h)).status_code == 401


async def test_a_valid_key_without_a_signature_never_gets_through_even_with_the_signature_header_empty(env):
    env.setenv("INGEST_HMAC_SECRET", SECRET)
    get_settings.cache_clear()
    assert (await post(make_app(), headers={"x-api-key": "k", "x-signature": "", "x-timestamp": ""})).status_code == 401


def test_the_signature_format_is_fixed_so_the_phone_can_match_it():
    # Android builds "$ts.POST./ingest/notification." + body with HmacSHA256 and hex; this pins the server side of that.
    assert sign("k", "1700000000", "post", "/p", b"{}") == sign("k", "1700000000", "POST", "/p", b"{}")
    assert len(sign("k", "1", "POST", "/p", b"")) == 64


@pytest.fixture
async def redis():
    r = Redis.from_url(os.environ["TEST_REDIS_URL"])
    for k in await r.keys(ingest_mod.RATE_KEY + "*"):
        await r.delete(k)
    yield r
    await r.aclose()


@infra
async def test_calls_beyond_the_per_minute_cap_get_429_with_retry_after(env, redis):
    env.setenv("INGEST_RATE_PER_MIN", "3")
    get_settings.cache_clear()
    app = make_app(redis)
    codes = [(await post(app, headers={"x-api-key": "k"})).status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]
    r = await post(app, headers={"x-api-key": "k"})
    assert r.headers["retry-after"] == "60"


@infra
async def test_a_wrong_key_does_not_use_up_the_owners_allowance(env, redis):
    env.setenv("INGEST_RATE_PER_MIN", "2")
    get_settings.cache_clear()
    app = make_app(redis)
    for _ in range(5):
        assert (await post(app, headers={"x-api-key": "bad"})).status_code == 401
    assert [(await post(app, headers={"x-api-key": "k"})).status_code for _ in range(2)] == [200, 200]


@infra
async def test_redis_failing_lets_the_owner_through(env):
    class Broken:
        async def incr(self, *a):
            raise ConnectionError("down")

    env.setenv("INGEST_RATE_PER_MIN", "1")
    get_settings.cache_clear()
    assert (await post(make_app(Broken()), headers={"x-api-key": "k"})).status_code == 200


async def test_a_server_with_no_key_configured_accepts_nothing(env):
    env.setenv("INGEST_API_KEY", "")
    get_settings.cache_clear()
    assert (await post(make_app())).status_code == 401  # an empty header must not match an empty key
    assert (await post(make_app(), headers={"x-api-key": ""})).status_code == 401
