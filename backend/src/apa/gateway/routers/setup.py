"""Setup from the web dashboard (same key as /ingest): where photos are stored (this server's disk or any S3 store) with a
connection test, and a checklist of what is set up and what is still missing, each with how to fix it."""

import asyncio
import io

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response
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

    await assistant_memory.encrypt_legacy(SessionLocal)  # rows from the older plaintext storage are encrypted on first sight
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


class JournalIn(BaseModel):
    text: str
    shared: bool = False  # private unless the owner says the assistant may read it
    day: str | None = None


class ShareIn(BaseModel):
    shared: bool


@router.get("/journal")
async def list_journal(limit: int = 50, offset: int = 0) -> dict:
    """The owner's private journal, decrypted. Owner only; it never reaches the AI unless an entry was marked shared."""
    from apa import assistant_memory

    return {"entries": await assistant_memory.journal_list(SessionLocal, min(max(limit, 1), 200), max(offset, 0))}


@router.post("/journal")
async def add_journal(body: JournalIn) -> dict:
    from datetime import date, datetime
    from zoneinfo import ZoneInfo

    from apa import assistant_memory
    from apa.core.secrets import SecretsUnavailable

    try:
        day = date.fromisoformat(body.day) if body.day else datetime.now(ZoneInfo(get_settings().timezone)).date()
    except ValueError:
        raise HTTPException(422, "day must be YYYY-MM-DD") from None
    if not body.text.strip():
        raise HTTPException(422, "write something first")
    try:
        entry_id = await assistant_memory.journal_add(SessionLocal, day, body.text, shared=body.shared)
    except SecretsUnavailable:
        raise HTTPException(503, "the server has no valid SECRETS_KEY, so the diary cannot be stored safely") from None
    return {"id": entry_id}


@router.put("/journal/{entry_id}/share")
async def share_journal(entry_id: int, body: ShareIn) -> dict:
    from apa import assistant_memory

    if not await assistant_memory.journal_set_shared(SessionLocal, entry_id, body.shared):
        raise HTTPException(404, "no such entry")
    return {"id": entry_id, "shared": body.shared}


@router.delete("/journal/{entry_id}")
async def delete_journal(entry_id: int) -> dict:
    from apa import assistant_memory

    return {"deleted": await assistant_memory.journal_delete(SessionLocal, entry_id)}


@router.post("/memory/prune")
async def prune_chat_now(request: Request) -> dict:
    """Write any missing diary days, then erase chat text older than the retention window (it also runs every night)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from apa import retention

    tz = ZoneInfo(get_settings().timezone)
    now = datetime.now(tz)
    written = await retention.catch_up(SessionLocal, request.app.state.llm, tz, now.date())
    pruned = await retention.prune(SessionLocal, tz, now, get_settings().chat_keep_days)
    return {"diary_days_written": written, "keep_days": get_settings().chat_keep_days, **pruned}


@router.get("/youtube")
async def youtube_status() -> dict:
    from apa import youtube_api

    return {"configured": youtube_api.configured(), "connected": await youtube_api.connected(SessionLocal)}


@router.post("/youtube/connect")
async def youtube_connect() -> dict:
    """Start connecting the YouTube account: returns a short code to type at google.com/device (no redirect needed)."""
    from apa import youtube_api

    try:
        async with httpx.AsyncClient(timeout=20) as http:
            return await youtube_api.start(http)
    except youtube_api.YouTubeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None


@router.post("/youtube/poll")
async def youtube_poll(device_code: str = Body(embed=True, max_length=300)) -> dict:
    """Has the owner entered the code yet? -> {"status": "pending" | "connected"}; connected also reads liked videos and follows."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from apa import youtube_api

    try:
        async with httpx.AsyncClient(timeout=20) as http:
            status = await youtube_api.poll(SessionLocal, http, device_code)
            items = await youtube_api.sync(SessionLocal, http, datetime.now(ZoneInfo(get_settings().timezone)).date()) if status == "connected" else 0
    except youtube_api.YouTubeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    return {"status": status, "items": items}


@router.post("/youtube/sync")
async def youtube_sync_now() -> dict:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from apa import youtube_api

    try:
        async with httpx.AsyncClient(timeout=30) as http:
            return {"items": await youtube_api.sync(SessionLocal, http, datetime.now(ZoneInfo(get_settings().timezone)).date())}
    except youtube_api.YouTubeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None


@router.delete("/youtube")
async def youtube_disconnect() -> dict:
    from apa import youtube_api

    await youtube_api.disconnect(SessionLocal)
    return {"ok": True}


@router.post("/import/youtube")
async def import_youtube(items: list[dict] = Body(max_length=50000)) -> dict:
    """The owner's Google Takeout `watch-history.json` (YouTube and YouTube Music > History, JSON). Keeps the channel and title of
    what was watched or searched in the last 90 days, encrypted, for the profile. Importing again replaces those days."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from apa import assistant_memory, youtube
    from apa.core.secrets import SecretsUnavailable

    tz = ZoneInfo(get_settings().timezone)
    by_day = youtube.parse(items, tz, datetime.now(tz))
    try:
        stored = await assistant_memory.add_watch(SessionLocal, by_day)
    except SecretsUnavailable:
        raise HTTPException(status_code=503, detail="SECRETS_KEY is not set, so nothing can be stored safely") from None
    return {"ok": True, "items": stored, "days": len(by_day), "watched": sum(1 for v in by_day.values() for i in v if i["k"] == "watched")}


@router.get("/usage")
async def usage_summary(days: int = 14) -> dict:
    """Screen time per day and the most used apps over the last days, from the devices that report (ActivityWatch)."""
    from collections import defaultdict

    from apa import usage

    days = max(1, min(days, 90))
    rows = await usage.recent(SessionLocal, days)
    per_day: dict[str, dict] = defaultdict(lambda: defaultdict(int))
    apps: dict[str, int] = defaultdict(int)
    devices: dict[str, str] = {}
    for r in rows:
        per_day[r.day.isoformat()][r.device] += r.seconds
        apps[r.app] += r.seconds
        devices[r.device] = r.kind
    return {"devices": devices, "days": {d: dict(v) for d, v in sorted(per_day.items())},
            "top_apps": sorted(({"app": a, "seconds": s, "category": usage.category(a)} for a, s in apps.items()),
                               key=lambda x: -x["seconds"])[:15]}


@router.post("/profile/content")
async def profile_content(request: Request) -> dict:
    """Have the AI group what you watch, listen to and like (YouTube) into topics now; it also runs every night."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from apa import content

    today = datetime.now(ZoneInfo(get_settings().timezone)).date()
    return {"result": await content.digest(SessionLocal, request.app.state.llm, today, force=True)}


@router.get("/about")
async def about_card() -> dict:
    """The whole dataset about the owner, grouped by domain, for the About me page."""
    from apa import about

    entries = await about.card(SessionLocal)
    return {"domains": list(about.DOMAINS), "entries": entries}


@router.post("/about/fact")
async def about_add(domain: str = Body(max_length=20), key: str = Body(max_length=60), value: str = Body(max_length=400),
                    locked: bool = Body(default=True)) -> dict:
    """The owner writes a fact (locked by default: the assistant can read it but never change it)."""
    from apa import about

    done = await about.set_fact(SessionLocal, domain, key, value, src="owner", conf=100, locked=locked)
    if done == "off":
        raise HTTPException(status_code=503, detail="SECRETS_KEY is not set, so nothing can be stored safely")
    if done != "saved":
        raise HTTPException(status_code=400, detail="a key and a value are needed")
    return {"ok": True}


@router.put("/about/item")
async def about_edit(ref: str = Body(max_length=40), value: str = Body(max_length=400), locked: bool | None = Body(default=None)) -> dict:
    from apa import about

    if not await about.update_item(SessionLocal, ref, value, locked):
        raise HTTPException(status_code=404, detail="no such editable entry")
    return {"ok": True}


@router.delete("/about/item")
async def about_delete(ref: str) -> dict:
    from apa import about

    if not await about.delete_item(SessionLocal, ref):
        raise HTTPException(status_code=404, detail="no such deletable entry")
    return {"ok": True}


@router.get("/about/export")
async def about_export(format: str = "jsonl") -> Response:
    """The dataset as JSON lines (domain, key, value, source, confidence, locked, evidence, updated): one stable shape to feed a
    retrieval index or to train a model later. `format=card` gives the same as the text block the assistant reads."""
    from apa import about

    if format == "card":
        return Response(await about.person_card(SessionLocal, 20000), media_type="text/plain; charset=utf-8")
    return Response(about.export_jsonl(await about.export_rows(SessionLocal)), media_type="application/x-ndjson")


@router.get("/days")
async def days_list(n: int = 30) -> dict:
    """The snapshots of the last days (what the owner was like each day), newest first."""
    from apa import assistant_memory

    snaps = await assistant_memory.days(SessionLocal, max(1, min(n, 400)))
    return {"days": [{k: v for k, v in d.items() if k != "facts"} for d in reversed(snaps)]}


@router.put("/days/{day}")
async def days_edit(day: str, diary: str = Body(embed=True, max_length=1500)) -> dict:
    """The owner corrects the diary entry of a day. An edited entry is never rewritten by the nightly job."""
    from datetime import date

    from apa import assistant_memory

    try:
        d = date.fromisoformat(day)
    except ValueError:
        raise HTTPException(status_code=400, detail="day must be YYYY-MM-DD") from None
    snap = await assistant_memory.get_day(SessionLocal, d)
    if snap is None or len(diary.strip()) < 3:
        raise HTTPException(status_code=404, detail="no such day")
    await assistant_memory.add_day(SessionLocal, d, {**snap, "diary": " ".join(diary.split()), "edited": True})
    return {"ok": True}


@router.delete("/days/{day}")
async def days_delete(day: str) -> dict:
    from datetime import date

    from apa import assistant_memory

    try:
        d = date.fromisoformat(day)
    except ValueError:
        raise HTTPException(status_code=400, detail="day must be YYYY-MM-DD") from None
    if not await assistant_memory.delete_day(SessionLocal, d):
        raise HTTPException(status_code=404, detail="no such day")
    return {"ok": True}


@router.post("/days/build")
async def days_build(request: Request, day: str | None = Body(default=None, embed=True)) -> dict:
    """Write the snapshot of a day now (default: today), and refresh the long-term personality. They also run every night."""
    from datetime import date, datetime
    from zoneinfo import ZoneInfo

    from apa import daily

    tz = ZoneInfo(get_settings().timezone)
    try:
        d = date.fromisoformat(day) if day else datetime.now(tz).date()
    except ValueError:
        raise HTTPException(status_code=400, detail="day must be YYYY-MM-DD") from None
    built = await daily.build_day(SessionLocal, request.app.state.llm, tz, d, force=True)
    return {"built": built, "day": d.isoformat(), "persona": await daily.persona(SessionLocal, request.app.state.llm, d, force=True)}


@router.get("/profile")
async def get_profile() -> dict:
    """The patterns computed from the owner's data (sleep, activity, food, spending, steps, stress, interests)."""
    from apa import profile

    return {"traits": await profile.traits(SessionLocal)}


@router.post("/profile/refresh")
async def refresh_profile() -> dict:
    """Recompute the profile now (it also runs every night)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from apa import profile

    tz = ZoneInfo(get_settings().timezone)
    done = await profile.refresh(SessionLocal, tz, datetime.now(tz))
    return {"traits": [t.key for t in done], "all": await profile.traits(SessionLocal)}


@router.get("/memory/export")
async def export_memory() -> dict:
    """Everything the assistant keeps about the owner (notes, what it learned, the diary), as JSON."""
    from apa import assistant_memory, profile

    return {**await assistant_memory.export_all(SessionLocal), "profile": await profile.traits(SessionLocal)}


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
