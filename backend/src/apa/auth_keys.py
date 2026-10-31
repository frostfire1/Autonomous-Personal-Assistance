"""Ingest API Key management and graceful key rotation with dual-key acceptance.

Protects ingestion and management endpoints while allowing seamless key rotation without
disrupting running devices (Android phones, Windows trackers, background workers).
"""

import secrets
import time
from datetime import UTC, datetime

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from apa.core.db import SessionLocal
from apa.core.logging import get_logger
from apa.core.settings import get_settings
from apa.db.models.budget import PolicyRule

log = get_logger(__name__)

RULE_KEY = "ingest_auth_keys"
CACHE_TTL_S = 3.0  # short cache for hot authentication path

# The known leaked key from past git commit to warn the user about
LEAKED_SAMPLE_KEYS = {"483cSAA0buZhtsTFoi7NMW7gMWpyfqz"}


class IngestKeysConfig(BaseModel):
    active_key: str = Field(default="")
    previous_key: str = ""
    dual_acceptance: bool = True
    rotated_at: datetime | None = None


_cache: tuple[float, IngestKeysConfig] | None = None


def generate_secure_key(prefix: str = "apa_") -> str:
    """Generate a high-entropy URL-safe key (256-bit entropy)."""
    return f"{prefix}{secrets.token_urlsafe(32)}"


async def load(sm: async_sessionmaker = SessionLocal) -> IngestKeysConfig:
    """Load key configuration from the database, falling back to environment settings."""
    env_default = get_settings().ingest_api_key
    try:
        async with sm() as s:
            row = (
                await s.execute(select(PolicyRule).where(PolicyRule.rule_key == RULE_KEY))
            ).scalar_one_or_none()
        if row is None or not isinstance(row.value_json, dict):
            return IngestKeysConfig(active_key=env_default, dual_acceptance=True)
        cfg = IngestKeysConfig(**row.value_json)
        if not cfg.active_key:
            cfg.active_key = env_default
        return cfg
    except Exception as e:
        log.warning("auth_keys_load_failed", error=str(e))
        return IngestKeysConfig(active_key=env_default, dual_acceptance=True)


async def save(cfg: IngestKeysConfig, sm: async_sessionmaker = SessionLocal) -> None:
    """Save key configuration to PolicyRule table and update in-memory cache."""
    global _cache
    data = cfg.model_dump(mode="json")
    async with sm() as s:
        stmt = insert(PolicyRule).values(
            rule_key=RULE_KEY,
            scope="global",
            scope_ref="",
            value_json=data,
            updated_by="owner",
            is_active=True,
        )
        await s.execute(
            stmt.on_conflict_do_update(
                index_elements=["rule_key", "scope", "scope_ref"],
                set_={"value_json": data, "updated_by": "owner"},
            )
        )
        await s.commit()
    _cache = (time.monotonic(), cfg)
    log.info("ingest_keys_saved", dual_acceptance=cfg.dual_acceptance, has_previous=bool(cfg.previous_key))


async def current(sm: async_sessionmaker = SessionLocal) -> IngestKeysConfig:
    """Get active key configuration with short TTL caching for the fast path."""
    global _cache
    now = time.monotonic()
    if _cache is not None and now - _cache[0] < CACHE_TTL_S:
        return _cache[1]
    cfg = await load(sm)
    _cache = (now, cfg)
    return cfg


def last() -> IngestKeysConfig:
    """Return cached key configuration synchronously; falls back to env setting."""
    if _cache is not None:
        return _cache[1]
    return IngestKeysConfig(active_key=get_settings().ingest_api_key, dual_acceptance=True)


def is_key_authorized_sync(key: str, env_default: str = "") -> bool:
    """Synchronous key verification using last known cache (for non-async callers like MCP)."""
    if not key:
        return False
    cfg = last()
    if cfg.active_key and secrets.compare_digest(key, cfg.active_key):
        return True
    if cfg.dual_acceptance and cfg.previous_key and secrets.compare_digest(key, cfg.previous_key):
        return True
    # Fallback to env setting only when no explicit active key was configured in policy
    if not cfg.active_key and env_default and secrets.compare_digest(key, env_default):
        return True
    return False


async def is_key_authorized(
    key: str, env_default: str = "", sm: async_sessionmaker = SessionLocal
) -> bool:
    """Check if key is authorized (active key, previous grace period key, or env default)."""
    if not key:
        return False
    cfg = await current(sm)
    # 1. Match current active key
    if cfg.active_key and secrets.compare_digest(key, cfg.active_key):
        return True
    # 2. Match previous key during rotation grace period
    if cfg.dual_acceptance and cfg.previous_key and secrets.compare_digest(key, cfg.previous_key):
        return True
    # 3. Match env fallback only when no active key has been set in policy
    if not cfg.active_key and env_default and secrets.compare_digest(key, env_default):
        return True
    return False


async def rotate_key(
    new_key: str | None = None, sm: async_sessionmaker = SessionLocal
) -> IngestKeysConfig:
    """Rotate the ingest key. The previous key is retained as fallback (dual acceptance) until revoked."""
    cfg = await load(sm)
    previous = cfg.active_key or get_settings().ingest_api_key
    fresh_key = new_key.strip() if new_key and new_key.strip() else generate_secure_key()

    new_cfg = IngestKeysConfig(
        active_key=fresh_key,
        previous_key=previous,
        dual_acceptance=True,
        rotated_at=datetime.now(UTC),
    )
    await save(new_cfg, sm)
    log.info("ingest_key_rotated", rotated_at=new_cfg.rotated_at.isoformat())
    return new_cfg


async def revoke_previous_key(sm: async_sessionmaker = SessionLocal) -> IngestKeysConfig:
    """Revoke the previous fallback key. Only the active key will be accepted afterwards."""
    cfg = await load(sm)
    new_cfg = IngestKeysConfig(
        active_key=cfg.active_key,
        previous_key="",
        dual_acceptance=False,
        rotated_at=cfg.rotated_at,
    )
    await save(new_cfg, sm)
    log.info("previous_ingest_key_revoked")
    return new_cfg


def mask_key(k: str) -> str:
    """Mask key for safe display (e.g. 'apa_a1b2...9z')."""
    if not k:
        return ""
    if len(k) <= 8:
        return "••••••••"
    return f"{k[:4]}••••••••{k[-4:]}"
