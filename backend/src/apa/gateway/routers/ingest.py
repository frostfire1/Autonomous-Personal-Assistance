import hashlib
import hmac
import secrets
import time
from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from apa.core.db import get_session
from apa.core.sanitize import sanitize_untrusted
from apa.core.settings import get_settings
from apa.domain.schemas import NotificationIn
from apa.ingestion.intake import store_and_enqueue
from apa.ingestion.packages import AppIn, register

router = APIRouter(prefix="/ingest", tags=["ingest"])

HEARTBEAT_KEY = "apa:phone:heartbeat"


SIGNATURE_WINDOW_S = 300
RATE_KEY = "apa:ratelimit:ingest:"


def sign(secret: str, timestamp: str, method: str, path: str, body: bytes) -> str:
    """The signature the app sends: HMAC-SHA256 over 'timestamp.METHOD.path.' and the exact body bytes."""
    msg = f"{timestamp}.{method.upper()}.{path}.".encode() + body
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


async def _rate_limited(request: Request, limit: int) -> bool:
    """A one-minute window across the API key. Fails open: a Redis hiccup must not drop real payments."""
    redis = getattr(request.app.state, "arq", None) if hasattr(request.app, "state") else None
    if redis is None or limit <= 0:
        return False
    key = f"{RATE_KEY}{int(time.time() // 60)}"
    try:
        n = await redis.incr(key)
        if n == 1:
            await redis.expire(key, 120)
    except Exception:
        return False
    return n > limit


async def require_ingest_key(request: Request, x_api_key: str = Header(default="")) -> None:
    """The shared key; a fresh signature over the request when INGEST_HMAC_SECRET is set; a per-minute cap."""
    s = get_settings()
    if not s.ingest_api_key or not secrets.compare_digest(x_api_key, s.ingest_api_key):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad api key")
    if s.ingest_hmac_secret:
        ts = request.headers.get("x-timestamp", "")
        sig = request.headers.get("x-signature", "")
        try:
            fresh = abs(time.time() - int(ts)) <= SIGNATURE_WINDOW_S
        except ValueError:
            fresh = False
        want = sign(s.ingest_hmac_secret, ts, request.method, request.url.path, await request.body())
        if not (fresh and secrets.compare_digest(sig, want)):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad signature")
    if await _rate_limited(request, s.ingest_rate_per_min):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "slow down", headers={"Retry-After": "60"})


@router.api_route("/test", methods=["GET", "POST"], dependencies=[Depends(require_ingest_key)])
async def test_auth() -> dict[str, object]:
    """Test endpoint for desktop/laptop trackers to verify connection, API key, & HMAC signature."""
    return {"ok": True, "message": "Authentication successful", "ts": time.time()}


class PackagesIn(BaseModel):
    apps: list[AppIn] = Field(max_length=100)


@router.post("/packages", dependencies=[Depends(require_ingest_key)])
async def register_packages(body: PackagesIn, session: AsyncSession = Depends(get_session)) -> dict[str, object]:
    """The phone sends the banking and wallet apps the owner picked (the full current selection). Built-in apps and
    anything that is not a valid package name are dropped; the answer lists what is now listened to."""
    picks = await register(session, body.apps)
    return {"ok": True, "listening_extra": picks}


class HeartbeatIn(BaseModel):
    listener: bool  # the phone's notification listener is connected to the system
    pending: int = Field(default=0, ge=0, le=100000)  # notifications waiting in the phone's queue
    version: str = Field(default="", max_length=20)


@router.post("/heartbeat", dependencies=[Depends(require_ingest_key)])
async def heartbeat(body: HeartbeatIn, request: Request) -> dict[str, object]:
    """The app says it is alive and whether its notification listener is connected. If the phone goes quiet, or the listener
    is disconnected, the owner is told in Telegram (see workers phone_watch)."""
    import json

    await request.app.state.arq.set(
        HEARTBEAT_KEY, json.dumps({"ts": time.time(), "listener": body.listener, "pending": body.pending, "version": body.version}),
        ex=14 * 86400,
    )
    return {"ok": True}


class UsageApp(BaseModel):
    app: str = Field(min_length=1, max_length=120)
    seconds: int = Field(gt=0, le=86400)
    first_at: datetime | None = None
    last_at: datetime | None = None


class UsageIn(BaseModel):
    device: str = Field(min_length=1, max_length=40)
    kind: str = Field(default="other", max_length=12)  # android | desktop | ios | web | other
    day: date
    apps: list[UsageApp] = Field(max_length=300)


@router.post("/usage", dependencies=[Depends(require_ingest_key)])
async def ingest_usage(body: UsageIn) -> dict[str, object]:
    """A device's active time per app for one day (from ActivityWatch). Replaces what that device sent for that day, so a
    re-push is harmless. Only app names and seconds are kept: no window titles, no URLs."""
    from apa import usage
    from apa.core.db import SessionLocal

    kept = await usage.store(SessionLocal, body.device, body.kind, body.day, [a.model_dump() for a in body.apps])
    return {"ok": True, "apps": kept}


class MediaItem(BaseModel):
    app: str = Field(min_length=1, max_length=60)
    title: str = Field(min_length=2, max_length=200)
    artist: str | None = Field(default=None, max_length=120)
    at: datetime


class MediaIn(BaseModel):
    device: str = Field(min_length=1, max_length=40)
    items: list[MediaItem] = Field(max_length=50)


@router.post("/media", dependencies=[Depends(require_ingest_key)])
async def ingest_media(body: MediaIn) -> dict[str, object]:
    """What the phone's media notifications said was playing (YouTube, Spotify...): title, artist or channel, app and time. Stored
    encrypted as watched or listened items; the assistant uses it for "what did I last watch or listen to"."""
    from zoneinfo import ZoneInfo

    from apa import assistant_memory
    from apa.core.db import SessionLocal

    try:
        stored = await assistant_memory.add_playing(SessionLocal, [i.model_dump() for i in body.items], ZoneInfo(get_settings().timezone))
    except assistant_memory.SecretsUnavailable:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "SECRETS_KEY is not set") from None
    return {"ok": True, "stored": stored}


class AWPayloadIn(BaseModel):
    hostname: str = "desktop"
    afk_events: list[dict[str, Any]] = Field(default_factory=list)
    win_events: list[dict[str, Any]] = Field(default_factory=list)
    web_events: list[dict[str, Any]] = Field(default_factory=list)


@router.post("/activitywatch", dependencies=[Depends(require_ingest_key)])
async def ingest_activitywatch(body: AWPayloadIn, request: Request) -> dict[str, object]:
    """Ingest ActivityWatch events from desktop tracker, deduplicate, update live activity,
    and save YouTube media items."""
    from zoneinfo import ZoneInfo

    from apa import activitywatch
    from apa.core.db import SessionLocal

    redis = getattr(request.app.state, "arq", None) if hasattr(request.app, "state") else None
    tz = ZoneInfo(get_settings().timezone)

    raw = {
        "hostname": body.hostname,
        "afk_events": body.afk_events,
        "win_events": body.win_events,
        "web_events": body.web_events,
    }
    live = await activitywatch.process_aw_payload(SessionLocal, redis, tz, raw)
    return {"ok": True, "live": live}


@router.post(
    "/notification", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(require_ingest_key)]
)
async def ingest_notification(
    body: NotificationIn, request: Request, session: AsyncSession = Depends(get_session)
) -> dict[str, object]:
    """Store the sanitized event and enqueue it. Must stay fast (Phase 1 gate: < 200 ms)."""
    clean = sanitize_untrusted(f"{body.title or ''}\n{body.text}")
    event_id = await store_and_enqueue(
        session,
        request.app.state.arq,
        source="android_listener",
        idempotency_key=body.idempotency_key,
        payload={
            "package": body.package_name,
            "text": clean.text,
            "posted_at": body.posted_at.isoformat(),
        },
        clean=clean,
    )
    if event_id is None:  # replay from the offline buffer: already stored
        return {"accepted": True, "duplicate": True}
    return {"accepted": True, "duplicate": False, "event_id": event_id}


def reason(e: Exception) -> str:
    """The first plain reason of a validation error (not pydantic's "1 validation error for ..." heading)."""
    errors = getattr(e, "errors", None)
    if callable(errors) and (found := errors()):
        return str(found[0].get("msg", "")).removeprefix("Value error, ")
    return str(e).splitlines()[0]
