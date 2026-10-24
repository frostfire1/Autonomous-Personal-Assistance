"""Where receipt photos are kept, chosen in the web dashboard instead of in .env: this server's disk, or any S3-compatible
store (Garage, MinIO, AWS S3, Cloudflare R2, Backblaze B2...). Saved once on the server (`policy_rules`, key
`storage_config`). "env" (the default) keeps the original behaviour: whatever EVIDENCE_BACKEND and MINIO_* say."""

import time
from typing import Literal

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from apa.core.db import SessionLocal
from apa.core.settings import Settings, get_settings
from apa.db.models.budget import PolicyRule

RULE_KEY = "storage_config"
CACHE_S = 10.0


class StorageCfg(BaseModel):
    backend: Literal["env", "local", "s3"] = "env"
    endpoint: str = Field(default="", max_length=200)  # host:port, no scheme (Garage: garage:3900)
    access_key: str = Field(default="", max_length=200)
    secret_key: str = Field(default="", max_length=200)
    bucket: str = Field(default="evidence", min_length=3, max_length=63)
    region: str = Field(default="", max_length=40)  # Garage: "garage"
    secure: bool = False  # https to the store

    @field_validator("endpoint")
    @classmethod
    def _host(cls, v: str) -> str:
        v = v.strip().removeprefix("https://").removeprefix("http://").rstrip("/")
        if v and ("/" in v or " " in v):
            raise ValueError("endpoint is host:port only, for example s3.example.com or garage:3900")
        return v

    def settings(self, base: Settings | None = None) -> Settings:
        """The process settings with this choice applied on top."""
        base = base or get_settings()
        if self.backend == "env":
            return base
        if self.backend == "local":
            return base.model_copy(update={"evidence_backend": "local"})
        return base.model_copy(update={
            "evidence_backend": "s3", "minio_endpoint": self.endpoint, "minio_access_key": self.access_key,
            "minio_secret_key": self.secret_key, "minio_bucket_evidence": self.bucket, "minio_region": self.region,
            "minio_secure": self.secure,
        })


def public(cfg: StorageCfg) -> dict:
    d = cfg.model_dump()
    k = d.pop("secret_key")
    d["secret_key_hint"] = "" if not k else ("…" + k[-4:] if len(k) > 8 else "…")
    return d


_cache: tuple[float, StorageCfg] | None = None


async def load(sm: async_sessionmaker = SessionLocal) -> StorageCfg:
    async with sm() as s:
        row = (await s.execute(select(PolicyRule).where(PolicyRule.rule_key == RULE_KEY))).scalar_one_or_none()
    if row is None or not isinstance(row.value_json, dict):
        return StorageCfg()
    try:
        return StorageCfg(**row.value_json)
    except ValueError:
        return StorageCfg()


async def save(cfg: StorageCfg, sm: async_sessionmaker = SessionLocal) -> None:
    global _cache
    data = cfg.model_dump()
    async with sm() as s:
        stmt = insert(PolicyRule).values(rule_key=RULE_KEY, scope="global", scope_ref="", value_json=data, updated_by="owner",
                                         is_active=True)
        await s.execute(stmt.on_conflict_do_update(index_elements=["rule_key", "scope", "scope_ref"],
                                                   set_={"value_json": data, "updated_by": "owner"}))
        await s.commit()
    _cache = (time.monotonic(), cfg)


async def current(sm: async_sessionmaker = SessionLocal) -> StorageCfg:
    global _cache
    if _cache is not None and time.monotonic() - _cache[0] < CACHE_S:
        return _cache[1]
    try:
        cfg = await load(sm)
    except Exception:  # a database hiccup must not stop a photo from being stored: last known, else the .env behaviour
        cfg = _cache[1] if _cache else StorageCfg()
    _cache = (time.monotonic(), cfg)
    return cfg
