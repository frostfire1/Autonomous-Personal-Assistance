"""Setup from the web dashboard (same key as /ingest): where photos are stored (this server's disk or any S3 store) with a
connection test, and a checklist of what is set up and what is still missing, each with how to fix it."""

import asyncio
import io

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import text

from apa import llm_config, storage_config
from apa.core.db import SessionLocal
from apa.core.settings import get_settings
from apa.gateway.routers.ingest import reason, require_ingest_key

router = APIRouter(tags=["setup"], dependencies=[Depends(require_ingest_key)])


@router.get("/storage")
async def get_storage() -> dict:
    cfg = await storage_config.load()
    s = get_settings()
    return {"config": storage_config.public(cfg), "env_backend": s.evidence_backend, "env_endpoint": s.minio_endpoint}


class StorageIn(BaseModel):
    backend: str = "env"
    endpoint: str = ""
    access_key: str = ""
    secret_key: str | None = None  # None keeps the saved secret
    bucket: str = "evidence"
    region: str = ""
    secure: bool = False


def _build(body: StorageIn, old: storage_config.StorageCfg) -> storage_config.StorageCfg:
    secret = body.secret_key if body.secret_key is not None else old.secret_key
    try:
        cfg = storage_config.StorageCfg(**{**body.model_dump(), "secret_key": secret})
    except ValueError as e:
        raise HTTPException(422, reason(e)) from None
    if cfg.backend == "s3" and not (cfg.endpoint and cfg.access_key and cfg.secret_key):
        raise HTTPException(422, "S3 needs an endpoint, an access key and a secret key")
    return cfg


def _probe(cfg: storage_config.StorageCfg) -> str:
    """Make sure the bucket exists (creating it if the key may) and that a small object can be written, read and removed.
    Returns a short sentence; raises on failure."""
    from minio import Minio

    s = cfg.settings()
    if s.evidence_backend == "local":
        import pathlib
        root = pathlib.Path(s.evidence_dir)
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".apa-write-test"
        probe.write_bytes(b"ok")
        probe.unlink()
        return f"folder {root} is writable"
    c = Minio(s.minio_endpoint, s.minio_access_key, s.minio_secret_key, secure=s.minio_secure, region=s.minio_region or None)
    made = False
    if not c.bucket_exists(s.minio_bucket_evidence):
        c.make_bucket(s.minio_bucket_evidence)
        made = True
    c.put_object(s.minio_bucket_evidence, "apa-setup-test", io.BytesIO(b"ok"), 2)
    c.get_object(s.minio_bucket_evidence, "apa-setup-test").read()
    c.remove_object(s.minio_bucket_evidence, "apa-setup-test")
    return f"bucket {s.minio_bucket_evidence} {'created and ' if made else ''}works (write, read, delete)"


@router.put("/storage")
async def put_storage(body: StorageIn) -> dict:
    """Save the choice. An S3 choice is tested first, so a wrong address or key is refused instead of saved."""
    old = await storage_config.load()
    cfg = _build(body, old)
    if cfg.backend != "env":
        try:
            await asyncio.to_thread(_probe, cfg)
        except Exception as e:
            raise HTTPException(422, f"cannot use this storage ({type(e).__name__}: {str(e)[:120]})") from None
    await storage_config.save(cfg)
    return await get_storage()


@router.post("/storage/test")
async def test_storage(body: StorageIn) -> dict:
    cfg = _build(body, await storage_config.load())
    try:
        return {"ok": True, "detail": await asyncio.to_thread(_probe, cfg)}
    except Exception as e:
        return {"ok": False, "detail": f"{type(e).__name__}: {str(e)[:160]}"}


async def _item(key: str, label: str, ok: bool, detail: str, fix: str = "") -> dict:
    return {"key": key, "label": label, "ok": ok, "detail": detail, "fix": fix}


@router.get("/templates")
async def learned_formats() -> dict:
    """How many notification formats were learned per app (the model read each once; later ones need no model)."""
    from apa.ingestion import templates

    async with SessionLocal() as s:
        data = await templates._row(s)
    return {pkg: {"formats": len(e), "skip": sum(1 for x in e if x.get("dir") == "skip"), "retired": sum(1 for x in e if x.get("bad"))}
            for pkg, e in data.items()}


@router.delete("/templates")
async def clear_formats(package: str | None = None) -> dict:
    """Forget what was learned for one app (?package=...), or for all. The model reads the next notification again; use it
    when a learned format turned out wrong."""
    from apa.ingestion import templates

    async with SessionLocal() as s:
        data = await templates._row(s, lock=True)
        removed = len(data) if package is None else int(data.pop(package, None) is not None)
        data = {} if package is None else data
        from sqlalchemy import update

        from apa.db.models.budget import PolicyRule

        await s.execute(update(PolicyRule).where(PolicyRule.rule_key == templates.RULE_KEY).values(value_json=data))
        await s.commit()
    return {"cleared": removed}


@router.get("/memory")
async def get_memory() -> dict:
    """What the assistant has been asked to remember about the owner."""
    from apa import assistant_memory

    return {"facts": await assistant_memory.load(SessionLocal), "diary": await assistant_memory.episodes(SessionLocal),
            "learned": await assistant_memory.learned(SessionLocal)}


@router.delete("/memory/{index}")
async def forget_memory(index: int) -> dict:
    from apa import assistant_memory

    return {"result": await assistant_memory.forget(SessionLocal, index), "facts": await assistant_memory.load(SessionLocal)}


@router.delete("/memory/learned/{index}")
async def delete_learned_item(index: int) -> dict:
    from apa import assistant_memory

    return {"deleted": await assistant_memory.delete_learned(SessionLocal, index)}


@router.get("/memory/export")
async def export_memory() -> dict:
    """Everything the assistant keeps about the owner (notes, what it learned, the diary), as JSON."""
    from apa import assistant_memory

    return await assistant_memory.export_all(SessionLocal)


@router.delete("/memory/diary/{day}")
async def delete_diary_day(day: str) -> dict:
    from apa import assistant_memory

    return {"deleted": await assistant_memory.delete_episode(SessionLocal, day)}


@router.post("/memory/diary/today")
async def write_diary_today(request: Request) -> dict:
    """Write today's diary entry now (the job does it every night at 23:55)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from apa import assistant_memory

    tz = ZoneInfo(get_settings().timezone)
    written = await assistant_memory.summarize_day(SessionLocal, request.app.state.llm, datetime.now(tz).date(), tz)
    return {"written": written}


@router.get("/setup/status")
async def status(request: Request) -> dict:
    """What is working and what is not yet, with the way to fix each. Core items first."""
    s = get_settings()
    items = []
    try:
        async with SessionLocal() as db:
            await db.execute(text("select 1"))
        items.append(await _item("db", "Database", True, "connected"))
    except Exception:
        items.append(await _item("db", "Database", False, "not connected", "check the postgres container"))
    try:
        await request.app.state.arq.ping()
        items.append(await _item("redis", "Queue (Redis)", True, "connected"))
    except Exception:
        items.append(await _item("redis", "Queue (Redis)", False, "not connected", "check the redis container"))
    try:
        async with httpx.AsyncClient(timeout=5) as http:
            r = await http.get(s.litellm_base_url.removesuffix("/v1") + "/health/liveliness")
        items.append(await _item("litellm", "AI model gateway (LiteLLM)", r.status_code == 200, f"HTTP {r.status_code}",
                                 "check the litellm container and infra/litellm/config.yaml"))
    except Exception:
        items.append(await _item("litellm", "AI model gateway (LiteLLM)", False, "unreachable",
                                 "check the litellm container and infra/litellm/config.yaml"))
    cfg = await llm_config.load()
    custom = len(cfg.providers)
    items.append(await _item("providers", "Extra AI providers", custom > 0,
                             f"{custom} provider(s)" if custom else "none yet (using the LiteLLM defaults)",
                             "AI Models page: add a provider with a URL and key"))
    st = await storage_config.load()
    where = {"env": f"from .env ({s.evidence_backend})", "local": "this server's disk", "s3": f"S3 {st.endpoint}/{st.bucket}"}[st.backend]
    items.append(await _item("storage", "Receipt photo storage", True, where, "Storage page"))
    items.append(await _item("telegram", "Telegram bot", bool(s.telegram_bot_token and s.telegram_owner_id),
                             "token and owner id set" if s.telegram_bot_token else "not set",
                             "set TELEGRAM_BOT_TOKEN and TELEGRAM_OWNER_ID in .env, then restart"))
    items.append(await _item("gemini", "Gemini voice", bool(s.gemini_api_key),
                             "key set" if s.gemini_api_key else "none (using the Microsoft voice)",
                             "set GEMINI_API_KEY in .env (free key from aistudio.google.com/apikey)"))
    items.append(await _item("search", "Web search for /private", bool(s.firecrawl_api_key or s.brave_api_key),
                             "key set" if (s.firecrawl_api_key or s.brave_api_key) else "none (using Wikipedia)",
                             "set FIRECRAWL_API_KEY or BRAVE_API_KEY in .env"))
    items.append(await _item("sheets", "Google Sheets", bool(s.sheets_url or s.sheets_spreadsheet_id), "set" if (s.sheets_url or s.sheets_spreadsheet_id) else "not set (optional)",
                             "set SHEETS_URL and the credentials file in .env"))
    return {"items": items, "ready": all(i["ok"] for i in items[:3])}
