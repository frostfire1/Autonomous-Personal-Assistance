import time

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from apa import auth_keys
from apa.core.settings import get_settings
from apa.gateway.routers import devices as devices_router
from apa.gateway.routers import ingest as ingest_router


@pytest.fixture(autouse=True)
def mock_auth_keys_storage(monkeypatch):
    store = {"cfg": None}

    async def mock_load(sm=None):
        if store["cfg"] is not None:
            return store["cfg"]
        return auth_keys.IngestKeysConfig(
            active_key=get_settings().ingest_api_key,
            dual_acceptance=True,
        )

    async def mock_save(cfg, sm=None):
        store["cfg"] = cfg
        auth_keys._cache = (time.monotonic(), cfg)

    monkeypatch.setattr(auth_keys, "load", mock_load)
    monkeypatch.setattr(auth_keys, "save", mock_save)
    auth_keys._cache = None
    yield
    auth_keys._cache = None


@pytest.fixture
def clean_env_key(monkeypatch):
    monkeypatch.setenv("INGEST_API_KEY", "env_test_secret_key_123")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_auth_keys_env_default(clean_env_key):
    # Empty DB fallback
    assert await auth_keys.is_key_authorized("env_test_secret_key_123", "env_test_secret_key_123") is True
    assert await auth_keys.is_key_authorized("wrong_key", "env_test_secret_key_123") is False
    assert await auth_keys.is_key_authorized("", "env_test_secret_key_123") is False


@pytest.mark.asyncio
async def test_key_rotation_and_dual_acceptance(clean_env_key):
    # Rotate with custom key
    cfg = await auth_keys.rotate_key("new_rotated_key_456")
    assert cfg.active_key == "new_rotated_key_456"
    assert cfg.previous_key == "env_test_secret_key_123"
    assert cfg.dual_acceptance is True

    # Both keys MUST be authorized during dual acceptance
    assert await auth_keys.is_key_authorized("new_rotated_key_456", "env_test_secret_key_123") is True
    assert await auth_keys.is_key_authorized("env_test_secret_key_123", "env_test_secret_key_123") is True
    assert await auth_keys.is_key_authorized("random_intruder", "env_test_secret_key_123") is False

    # Revoke previous key
    revoked = await auth_keys.revoke_previous_key()
    assert revoked.active_key == "new_rotated_key_456"
    assert revoked.previous_key == ""
    assert revoked.dual_acceptance is False

    # Now only the new key is authorized (and env fallback if same)
    assert await auth_keys.is_key_authorized("new_rotated_key_456", "") is True
    assert await auth_keys.is_key_authorized("env_test_secret_key_123", "") is False


def test_mask_key():
    assert auth_keys.mask_key("apa_1234567890abcdef") == "apa_••••••••cdef"
    assert auth_keys.mask_key("short") == "••••••••"
    assert auth_keys.mask_key("") == ""


@pytest.fixture
async def app_client(clean_env_key, monkeypatch):
    app = FastAPI()
    app.include_router(ingest_router.router)
    app.include_router(devices_router.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_devices_endpoints(app_client):
    # 401 when no key
    res = await app_client.get("/devices")
    assert res.status_code == 401

    # 200 with valid key
    res = await app_client.get("/devices", headers={"x-api-key": "env_test_secret_key_123"})
    assert res.status_code == 200
    data = res.json()
    assert "summary" in data
    assert "key_status" in data
    assert "devices" in data

    # Rotate key via endpoint
    rot = await app_client.post(
        "/devices/rotate-key",
        json={"new_key": "endpoint_rotated_key_789"},
        headers={"x-api-key": "env_test_secret_key_123"},
    )
    assert rot.status_code == 200
    r_data = rot.json()
    assert r_data["ok"] is True
    assert r_data["active_key"] == "endpoint_rotated_key_789"
    assert r_data["dual_acceptance"] is True

    # Test that BOTH keys work on /devices
    res_new = await app_client.get("/devices", headers={"x-api-key": "endpoint_rotated_key_789"})
    assert res_new.status_code == 200
    res_old = await app_client.get("/devices", headers={"x-api-key": "env_test_secret_key_123"})
    assert res_old.status_code == 200

    # Revoke previous key via endpoint
    rev = await app_client.post(
        "/devices/revoke-previous-key",
        headers={"x-api-key": "endpoint_rotated_key_789"},
    )
    assert rev.status_code == 200
    assert rev.json()["dual_acceptance"] is False

    # Old key is now rejected
    res_old_revoked = await app_client.get("/devices", headers={"x-api-key": "env_test_secret_key_123"})
    assert res_old_revoked.status_code == 401
    # New key still works
    res_new_still = await app_client.get("/devices", headers={"x-api-key": "endpoint_rotated_key_789"})
    assert res_new_still.status_code == 200
