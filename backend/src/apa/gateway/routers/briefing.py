"""The voice briefing for the Android app (same key as /ingest): it reads `text` aloud and reports back."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select

from apa import prefs
from apa.core.db import SessionLocal
from apa.core.settings import get_settings
from apa.db.models.intel import Briefing
from apa.gateway.routers.ingest import require_ingest_key
from apa.news import briefing as brief
from apa.tts import voice

TELEGRAM_DEBOUNCE_KEY = "apa:briefing_tg_manual"
router = APIRouter(prefix="/briefing", tags=["briefing"], dependencies=[Depends(require_ingest_key)])


def _out(b: Briefing) -> dict:
    return {"id": b.id, "kind": b.kind, "lang": b.lang, "created_at": b.created_at, "text": b.text, "sections": b.sections}


async def _get(request: Request, kind: str, fresh: bool) -> dict:
    tz = ZoneInfo(get_settings().timezone)
    llm = getattr(request.app.state, "llm", None)
    row = await brief.get_or_build(
        SessionLocal, llm, request.app.state.arq, tz, datetime.now(tz), kind=kind, fresh=fresh
    )
    return _out(row)


@router.get("/short")
async def short(request: Request, fresh: bool = False) -> dict:
    """About a minute of speech: the one to play after the alarm."""
    return await _get(request, "short", fresh)


@router.get("/full")
async def full(request: Request, fresh: bool = False) -> dict:
    return await _get(request, "full", fresh)


@router.get("/{briefing_id}/audio")
async def audio_whole(briefing_id: int, request: Request) -> Response:
    """The whole briefing as ONE MP3 (one synthesis, so one call to the voice engine instead of one per topic): what the app plays
    after the alarm. Cached like a section; 503 means "speak it yourself"."""
    async with SessionLocal() as s:
        row = (await s.execute(select(Briefing).where(Briefing.id == briefing_id))).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "no such briefing")
    try:
        choice = await prefs.load(SessionLocal)
        mp3 = await voice.audio_for(getattr(request.app.state, "arq", None), row.text, voice=choice.voice, lang=row.lang)
    except voice.TtsError:
        raise HTTPException(503, "voice unavailable") from None
    return Response(mp3, media_type=voice.sniff(mp3), headers={"Cache-Control": "private, max-age=86400"})


@router.get("/{briefing_id}/audio/{index}")
async def audio(briefing_id: int, index: int, request: Request) -> Response:
    """One section as MP3 in a natural voice. 503 means "speak it yourself": the app then uses its
    own voice, so a Microsoft outage never silences the morning briefing."""
    async with SessionLocal() as s:
        row = (await s.execute(select(Briefing).where(Briefing.id == briefing_id))).scalar_one_or_none()
    if row is None or not (0 <= index < len(row.sections)):
        raise HTTPException(404, "no such briefing section")
    try:
        choice = await prefs.load(SessionLocal)
        mp3 = await voice.audio_for(getattr(request.app.state, "arq", None), row.sections[index]["text"], voice=choice.voice, lang=row.lang)
    except voice.TtsError:
        raise HTTPException(503, "voice unavailable") from None
    return Response(mp3, media_type=voice.sniff(mp3), headers={"Cache-Control": "private, max-age=86400"})


@router.post("/telegram")
async def send_to_telegram(request: Request) -> dict:
    """The app's "play now" buttons also ask for the full text in Telegram. The worker, which owns the bot,
    sends the day's saved text. Taps within two minutes of each other send once."""
    arq = getattr(request.app.state, "arq", None)
    if arq is None:
        raise HTTPException(503, "queue unavailable")
    if not await arq.set(TELEGRAM_DEBOUNCE_KEY, "1", nx=True, ex=120):
        return {"ok": True, "queued": False}
    await arq.enqueue_job("briefing_telegram")
    return {"ok": True, "queued": True}


@router.post("/{briefing_id}/played")
async def played(briefing_id: int) -> dict:
    async with SessionLocal() as s:
        row = (await s.execute(select(Briefing).where(Briefing.id == briefing_id))).scalar_one_or_none()
        if row is None:
            raise HTTPException(404, "no such briefing")
        row.played_at = row.played_at or datetime.now(UTC)
        await s.commit()
    return {"ok": True}
