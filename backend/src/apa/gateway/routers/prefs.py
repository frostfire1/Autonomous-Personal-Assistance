"""The owner's briefing preferences, shared by the Android app and the web dashboard: language, voice, and where the
weather is for (a typed city, or the phone's GPS position). Same key as /ingest."""

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from apa import currency, weather
from apa import prefs as P
from apa.core.db import SessionLocal
from apa.gateway.routers.ingest import reason, require_ingest_key
from apa.news import config as news_config
from apa.tts import voice

router = APIRouter(prefix="/prefs", tags=["prefs"], dependencies=[Depends(require_ingest_key)])

SAMPLE = {
    "id": "Selamat pagi! Ini contoh suaraku. Semoga harimu menyenangkan dan penuh semangat.",
    "en": "Good morning! This is a sample of my voice. I hope you have a wonderful, energetic day.",
    "ja": "おはようございます！これは私の声のサンプルです。素敵な一日をお過ごしください。",
    "zh": "早上好！这是我的声音示例。祝你今天愉快，充满活力。",
    "ko": "좋은 아침입니다! 제 목소리 샘플이에요. 활기차고 멋진 하루 보내세요.",
    "de": "Guten Morgen! Das ist eine Hörprobe meiner Stimme. Ich wünsche dir einen schönen, energiegeladenen Tag.",
    "fr": "Bonjour ! Voici un échantillon de ma voix. Je te souhaite une belle journée pleine d'énergie.",
    "es": "¡Buenos días! Esta es una muestra de mi voz. Que tengas un día maravilloso y lleno de energía.",
}


_edge_cache: dict[str, tuple[float, list[dict]]] = {}


async def voices_for(lang: str) -> list[dict]:
    """Every voice that can read `lang`: Gemini's (all speak every language), Microsoft's for that language (the live list,
    kept for 12 hours), and the custom provider's voices when one is configured."""
    import time

    from apa import llm_config

    out = [{"key": f"gemini:{n}", "label": f"Gemini {n}: {c}", "engine": "gemini", "gender": g}
           for n, (g, c) in P.GEMINI_VOICES.items()]
    hit = _edge_cache.get(lang)
    if hit is None or time.monotonic() - hit[0] > 43200:
        rows: list[dict] = []
        try:
            import edge_tts

            for v in await edge_tts.list_voices():
                if v["Locale"].split("-")[0].lower() == lang or (lang == "zh" and v["Locale"].startswith("zh")):
                    rows.append({"key": f"edge:{v['ShortName']}", "label": f"Microsoft {v['ShortName'].split('-', 2)[2].removesuffix('Neural')} ({v['LocaleName']})",
                                 "engine": "edge", "gender": "m" if v["Gender"] == "Male" else "f"})
        except Exception:  # offline: the two known voices of the language
            rows = [{"key": f"edge:{P.edge_name(g, lang)}", "label": f"Microsoft {P.edge_name(g, lang)}", "engine": "edge", "gender": g}
                    for g in ("f", "m")]
        _edge_cache[lang] = hit = (time.monotonic(), rows)
    out += hit[1]
    cfg = (await llm_config.current()).voice
    if cfg.custom_provider and cfg.custom_model:
        out += [{"key": f"custom:{n}", "label": f"{cfg.custom_provider}/{cfg.custom_model}: {n}", "engine": "custom", "gender": "f"}
                for n in cfg.custom_voices]
    return out


async def _view_async(p: P.Prefs) -> dict:
    catalog = (await news_config.current()).catalog()
    return {**_view(p), "voices": await voices_for(p.language),
            "topics": [{"key": k, "label": v[1]} for k, v in catalog.items()]}


@router.get("/voices")
async def get_voices(lang: str = Query("id", pattern="^(id|en|ja|zh|ko|de|fr|es)$")) -> dict:
    """The voices for a language (the app asks again when the language changes)."""
    return {"voices": await voices_for(lang)}


def _view(p: P.Prefs) -> dict:
    return {
        "prefs": p.model_dump(),
        "languages": [{"key": k, "label": v[0]} for k, v in P.LANGUAGES.items()],
        "currencies": [{"key": c.code, "label": f"{c.code}: {c.name}"} for c in currency.CURRENCIES.values()],
    }


@router.get("")
async def get_prefs() -> dict:
    return await _view_async(await P.load(SessionLocal))


class PrefsIn(BaseModel):
    language: str | None = None
    voice: str | None = None
    city: str | None = Field(default=None, max_length=60)
    gps: bool | None = None
    topics: list[str] | None = None
    keywords: list[str] | None = None
    currency: str | None = None


@router.put("")
async def put_prefs(body: PrefsIn) -> dict:
    """Change any of language, voice, city or whether the weather follows the phone's GPS. A typed city is looked up
    once and its coordinates stored; an unknown city is refused rather than guessed."""
    cur = await P.load(SessionLocal)
    data = cur.model_dump()
    if body.topics is not None:
        catalog = (await news_config.current()).catalog()
        unknown = [x for x in body.topics if x not in catalog]
        if unknown:
            raise HTTPException(422, f"unknown topic {unknown[0]}")
    for key in ("language", "voice", "topics", "keywords", "currency"):
        if (v := getattr(body, key)) is not None:
            data[key] = v
    if body.gps is not None:
        data["gps"] = body.gps
        if body.gps is False and body.city is None and cur.gps:  # leaving GPS mode: forget the position
            data["lat"] = data["lon"] = None
    if body.city is not None:
        city = " ".join(body.city.split())
        if city:
            found = await weather.geocode(city)
            if found is None:
                raise HTTPException(422, "city not found")
            data["city"], data["lat"], data["lon"], data["gps"] = found[0], found[1], found[2], False
        else:
            data["city"], data["lat"], data["lon"] = "", None, None
    try:
        new = P.Prefs(**data)
    except ValueError as e:
        raise HTTPException(422, reason(e)) from None
    await P.save(SessionLocal, new)
    currency.set_active(new.currency)  # this process now; the others within a minute
    return await _view_async(new)


class LocationIn(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


@router.put("/location")
async def put_location(body: LocationIn) -> dict:
    """The phone's coarse GPS position, sent by the app. Stored to about 1 km (two decimals) and used only for the
    weather, and only while the owner has GPS mode on."""
    cur = await P.load(SessionLocal)
    if not cur.gps:
        return {"ok": True, "stored": False}  # GPS mode is off: the position is not kept
    new = cur.model_copy(update={"lat": round(body.lat, 2), "lon": round(body.lon, 2)})
    await P.save(SessionLocal, new)
    return {"ok": True, "stored": True}


@router.get("/preview")
async def preview(request: Request, voice_key: str = Query("zephyr", alias="voice"), lang: str = Query("id", pattern="^(id|en|ja|zh|ko|de|fr|es)$")) -> Response:
    """A short sample in a voice and language, so the owner can hear a choice before saving it."""
    if not P.valid_voice(voice_key):
        raise HTTPException(422, "unknown voice")
    try:
        audio, engine = await voice.audio_with_engine(getattr(request.app.state, "arq", None), SAMPLE[lang], voice=voice_key, lang=lang)
    except voice.TtsError:
        raise HTTPException(503, "voice unavailable") from None
    wanted = P.voice_name(voice_key, lang)[0]
    # the phone shows "backup voice" when the engine that spoke is not the one chosen (a Gemini quota, for instance)
    return Response(audio, media_type=voice.sniff(audio), headers={
        "Cache-Control": "private, max-age=86400" if engine == wanted else "no-store",
        "X-Voice-Engine": engine, "X-Voice-Wanted": wanted,
    })
