"""Huawei Health Kit cloud sync (blueprint 2d). **Switches on when HUAWEI_CLIENT_ID and HUAWEI_CLIENT_SECRET are in
.env and the owner has authorised once** (`scripts/huawei_authorize.py` stores the tokens, encrypted).

HONEST STATUS: written from Huawei's documented REST shape and tested only against a scripted transport. The
endpoints and data-type names below have NOT been exercised against the real service because no credentials
existed when this was built. The first real run may need small adjustments to `fetch_samples` / `to_health`;
everything around it (token refresh, encryption, scheduling, ingest, the failure note) is independent of that."""

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from apa.core import secrets
from apa.core.logging import get_logger
from apa.core.settings import get_settings
from apa.db.models.ops import OAuthToken
from apa.health.ingest import HealthIn, MetricIn, ingest

log = get_logger(__name__)

TOKEN_URL = "https://oauth-login.cloud.huawei.com/oauth2/v3/token"
API_BASE = "https://health-api.cloud.huawei.com/healthkit/v1"
PROVIDER = "huawei"
STEPS = "com.huawei.continuous.steps.delta"
CALORIES = "com.huawei.continuous.calories.burnt"
DISTANCE = "com.huawei.continuous.distance.delta"
HEART_RATE = "com.huawei.instantaneous.heart_rate"
REFRESH_MARGIN = timedelta(minutes=5)


class HuaweiError(Exception):
    """The Health Kit could not be reached or answered something unusable. Safe to log."""


def configured() -> bool:
    s = get_settings()
    return bool(s.huawei_client_id and s.huawei_client_secret and s.secrets_key)


async def save_tokens(
    sm: async_sessionmaker, access: str, refresh: str, expires_in: int, scope: str | None
) -> None:
    expires = datetime.now(UTC) + timedelta(seconds=expires_in)
    async with sm() as s:
        row = (
            await s.execute(select(OAuthToken).where(OAuthToken.provider == PROVIDER))
        ).scalar_one_or_none()
        enc = dict(
            access_token_enc=secrets.encrypt(access),
            refresh_token_enc=secrets.encrypt(refresh),
            expires_at=expires,
            scope=scope,
        )
        if row is None:
            s.add(OAuthToken(provider=PROVIDER, **enc))
        else:
            for k, v in enc.items():
                setattr(row, k, v)
        await s.commit()


async def exchange_code(http: httpx.AsyncClient, code: str, redirect_uri: str) -> dict:
    """One-time authorisation: the code from Huawei's consent page -> tokens (used by the authorize script)."""
    s = get_settings()
    r = await http.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": s.huawei_client_id,
            "client_secret": s.huawei_client_secret,
        },
    )
    if r.status_code != 200:
        raise HuaweiError(f"token exchange http {r.status_code}")
    return r.json()


async def access_token(sm: async_sessionmaker, http: httpx.AsyncClient) -> str:
    """A valid access token, refreshed (and the new pair stored encrypted) when it is about to expire."""
    async with sm() as s:
        row = (
            await s.execute(select(OAuthToken).where(OAuthToken.provider == PROVIDER))
        ).scalar_one_or_none()
    if row is None:
        raise HuaweiError("not authorised yet (run scripts/huawei_authorize.py once)")
    try:
        if row.expires_at - datetime.now(UTC) > REFRESH_MARGIN:
            return secrets.decrypt(row.access_token_enc)
        refresh = secrets.decrypt(row.refresh_token_enc)
    except secrets.SecretsUnavailable as e:
        raise HuaweiError(str(e)) from e
    s = get_settings()
    r = await http.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": s.huawei_client_id,
            "client_secret": s.huawei_client_secret,
        },
    )
    if r.status_code != 200:
        raise HuaweiError(f"token refresh http {r.status_code}")
    body = r.json()
    await save_tokens(
        sm,
        body["access_token"],
        body.get("refresh_token", refresh),
        int(body.get("expires_in", 3600)),
        body.get("scope"),
    )
    return body["access_token"]


async def fetch_samples(http: httpx.AsyncClient, token: str, start: datetime, end: datetime) -> dict:
    """Daily aggregates for the window (UNVERIFIED against the live service, see the module note)."""
    body = {
        "polymerizeWith": [{"dataTypeName": t} for t in (STEPS, CALORIES, DISTANCE)],
        "startTime": int(start.timestamp() * 1000),
        "endTime": int(end.timestamp() * 1000),
        "groupByTime": {"duration": 86_400_000},
    }
    r = await http.post(
        f"{API_BASE}/sampleSet:polymerize",
        json=body,
        headers={"Authorization": f"Bearer {token}", "x-client-id": get_settings().huawei_client_id},
    )
    if r.status_code != 200:
        raise HuaweiError(f"sampleSet http {r.status_code}")
    return r.json()


def to_health(raw: dict[str, Any], device_id: str = "huawei-cloud") -> HealthIn:
    """Tolerant parse of polymerized groups into daily metrics. Unknown shapes yield no rows, never a crash."""
    metrics: dict[datetime, dict] = {}
    for group in raw.get("group", []) or []:
        if not isinstance(group, dict):
            continue
        start_ms = group.get("startTime")
        if not start_ms:
            continue
        when = datetime.fromtimestamp(int(start_ms) / 1000, UTC)
        for sset in group.get("sampleSet", []) or []:
            if not isinstance(sset, dict):
                continue
            for point in sset.get("samplePoints", []) or []:
                if not isinstance(point, dict):
                    continue
                name = point.get("dataTypeName", "")
                vals = [v for v in point.get("value", []) or [] if isinstance(v, dict)]
                if not vals:
                    continue
                v = vals[0].get("integerValue", vals[0].get("floatValue"))
                if v is None:
                    continue
                m = metrics.setdefault(when, {"recorded_at": when})
                if name == STEPS:
                    m["step_count"] = int(v)
                elif name == CALORIES:
                    m["active_calories"] = int(v)
                elif name == DISTANCE:
                    m["daily_distance_m"] = v
    return HealthIn(device_id=device_id, metrics=[MetricIn(**m) for m in metrics.values() if len(m) > 1])


async def sync(sm: async_sessionmaker, http: httpx.AsyncClient, now: datetime) -> dict[str, int]:
    token = await access_token(sm, http)
    raw = await fetch_samples(http, token, now - timedelta(days=2), now)
    return await ingest(sm, to_health(raw), source="cloud_api")
