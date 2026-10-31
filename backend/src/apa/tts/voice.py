"""Human-sounding speech for the briefing, generated here on the server and cached, so the phone only plays an audio
file and any phone sounds the same. Two engines, tried in order:

  1. Google Gemini speech (when GEMINI_API_KEY is set): expressive, takes a plain-language style instruction
     ("cheerful, energetic morning host"), returns WAV.
  2. Microsoft neural voices through `edge-tts` (no key): returns MP3.
  3. If both fail, callers get `TtsError` and the app reads the text with the phone's own voice.

PRIVACY: the text of a briefing section (including the money and health lines) is sent to Google and/or Microsoft.
That was chosen by the owner for the voice quality. `TTS_ENABLED=false` turns it all off. The edge-tts endpoint is
unofficial, and Gemini speech models are preview-named (set `GEMINI_TTS_MODEL` if Google renames them); a change on
either side fails the call cleanly and the next engine takes over."""

import asyncio
import base64
import hashlib
import io
import re
import time
import wave
from typing import Any

import httpx

from apa import llm_config
from apa.core.logging import get_logger
from apa.core.settings import get_settings
from apa.prefs import LANGUAGES, edge_name, voice_gender
from apa.tts.speakable import plain, speakable

log = get_logger(__name__)

CACHE_KEY = "apa:tts:"
CACHE_TTL_S = 3 * 86400
BACKUP_TTL_S = 600  # audio from a backup engine is kept briefly, so the chosen engine is tried again soon
GEMINI_COOLDOWN_S = 120  # after a quota answer (429) Gemini is not asked again for this long
_gemini_blocked_until = 0.0
MAX_CHARS = 4500  # one clip (the whole short briefing, or one section of the full one); longer is cut at a sentence
TIMEOUT_S = 40


class TtsError(Exception):
    """No audio could be produced. Safe to log; the app falls back to the phone's voice."""


def cap(text: str) -> str:
    text = " ".join(text.split())
    if len(text) <= MAX_CHARS:
        return text
    cut = text[:MAX_CHARS]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return cut[: end + 1] if end > MAX_CHARS // 2 else cut


def sniff(audio: bytes) -> str:
    return "audio/wav" if audio[:4] == b"RIFF" else "audio/mpeg"


def pcm_to_wav(pcm: bytes, rate: int = 24000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


GEMINI_STYLE_EN = (
    "Read this in English in a cheerful, warm, energetic morning-host voice, with lively intonation and a natural pace:"
)


def _style(lang: str) -> str:
    s = get_settings()
    if lang == "id":
        return s.gemini_tts_style
    if lang == "en":
        return GEMINI_STYLE_EN
    name = LANGUAGES.get(lang, ("", "English"))[1]
    return f"Read this in {name} in a cheerful, warm, energetic morning-host voice, with lively intonation and a natural pace:"


def _gemini_model() -> str:
    return llm_config.last().voice.gemini_model or get_settings().gemini_tts_model


async def _gemini(text: str, name: str | None = None, lang: str = "id") -> bytes:
    global _gemini_blocked_until
    s = get_settings()
    if time.monotonic() < _gemini_blocked_until:
        raise TtsError("gemini cooling down after a quota answer")
    style = _style(lang)
    body = {
        "contents": [{"parts": [{"text": f"{style} {text}"}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": name or s.gemini_tts_voice}}},
        },
    }
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as http:
            r = await http.post(GEMINI_URL.format(model=_gemini_model()), json=body,
                                headers={"x-goog-api-key": s.gemini_api_key})
        if r.status_code == 429:
            _gemini_blocked_until = time.monotonic() + GEMINI_COOLDOWN_S
        if r.status_code != 200:
            raise TtsError(f"gemini HTTP {r.status_code}")
        part = r.json()["candidates"][0]["content"]["parts"][0]["inlineData"]
        pcm = base64.b64decode(part["data"])
        m = re.search(r"rate=(\d+)", part.get("mimeType", ""))
        audio = pcm_to_wav(pcm, int(m.group(1)) if m else 24000)
    except TtsError:
        raise
    except Exception as e:  # network, a changed response shape, an unknown model: the next engine takes over
        raise TtsError(f"gemini {type(e).__name__}") from None
    if len(pcm) < 2000:
        raise TtsError("gemini returned no audio")
    return audio


async def _edge(text: str, name: str | None = None, lang: str = "id") -> bytes:
    s = get_settings()
    import edge_tts

    voice_name = name or (edge_name("f", lang) if lang != "id" else s.tts_voice)

    async def run() -> bytes:
        out = bytearray()
        comm = edge_tts.Communicate(text, voice_name, rate=s.tts_rate, pitch=s.tts_pitch)
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                out += chunk["data"]
        return bytes(out)

    try:
        audio = await asyncio.wait_for(run(), TIMEOUT_S)
    except Exception as e:  # network, an unknown voice, a changed endpoint: all the same to the caller
        raise TtsError(type(e).__name__) from None
    if len(audio) < 500:
        raise TtsError("no audio returned")
    return audio


async def _custom(text: str, name: str | None = None, lang: str = "id") -> bytes:
    """An OpenAI-compatible speech endpoint the owner added as a provider in the dashboard."""
    v = llm_config.last().voice
    prov = llm_config.last().providers.get(v.custom_provider)
    if prov is None or not v.custom_model:
        raise TtsError("custom voice not configured")
    headers = {"Authorization": f"Bearer {prov.api_key}"} if prov.api_key else {}
    body = {"model": v.custom_model, "input": text, "voice": name or (v.custom_voices[0] if v.custom_voices else "alloy"),
            "response_format": "mp3"}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as http:
            r = await http.post(f"{prov.base_url}/audio/speech", json=body, headers=headers)
    except Exception as e:
        raise TtsError(f"custom {type(e).__name__}") from None
    if r.status_code != 200 or len(r.content) < 500:
        raise TtsError(f"custom HTTP {r.status_code}")
    return r.content


def engines() -> list[str]:
    s = get_settings()
    cfg = llm_config.last().voice
    custom = ["custom"] if cfg.custom_provider and cfg.custom_model else []
    chosen = cfg.engine
    engine = s.tts_engine if chosen == "auto" else chosen  # the dashboard's choice wins over the environment
    if engine == "custom":
        return [*custom, "edge"]
    if engine == "edge":
        return ["edge"]
    if engine == "gemini":
        return ["gemini"] if s.gemini_api_key else []
    return (["gemini"] if s.gemini_api_key else []) + custom + ["edge"]  # auto


def plan(voice: str | None, lang: str) -> list[tuple[str, str | None]]:
    """[(engine, voice name)] in the order to try. With no chosen voice: the configured engines and voices (the original
    behaviour). With one: its engine first, then whatever else is configured as a fallback in its own default voice."""
    available = engines()
    if voice is None:
        return [(e, None) for e in available]
    from apa.prefs import voice_name

    engine, name = voice_name(voice, lang)
    first = [(engine, name)] if engine in available else []
    gender = voice_gender(voice)
    # a backup Microsoft voice is the same gender and language as the chosen one; a backup Gemini voice keeps its default
    return first + [(e, edge_name(gender, lang) if e == "edge" else None) for e in available if e != engine]


async def synthesize(text: str, *, voice: str | None = None, lang: str = "id") -> bytes:
    """Audio bytes (WAV or MP3, see `sniff`) for `text` in the chosen voice and language: the first engine that works."""
    return (await synthesize_ex(text, voice=voice, lang=lang))[0]


async def synthesize_ex(text: str, *, voice: str | None = None, lang: str = "id") -> tuple[bytes, str]:
    """`synthesize` that also says which engine spoke."""
    s = get_settings()
    await llm_config.current()  # picks up the dashboard's engine and model choice
    if not s.tts_enabled:
        raise TtsError("disabled")
    text = plain(cap(text)) if lang == "en" else speakable(cap(text))
    if not text:
        raise TtsError("empty")
    last: TtsError | None = None
    for engine, name in plan(voice, lang):
        extra = {} if (name is None and lang == "id") else {"name": name, "lang": lang}
        try:
            fn = {"gemini": _gemini, "custom": _custom}.get(engine, _edge)
            return await fn(text, **extra), engine
        except TtsError as e:
            log.warning("tts_engine_failed", engine=engine, error=str(e))
            last = e
    raise last or TtsError("no engine configured")


def _key(text: str, voice: str | None = None, lang: str = "id") -> str:
    s = get_settings()
    chosen = "|".join(f"{e}:{n}" for e, n in plan(voice, lang))
    h = hashlib.sha256(
        f"v5|{lang}|{chosen}|{s.tts_voice}|{s.tts_rate}|{s.tts_pitch}|{_gemini_model()}|{s.gemini_tts_voice}|"
        f"{s.gemini_tts_style}|{cap(text)}".encode()
    ).hexdigest()[:24]
    return CACHE_KEY + h


async def audio_with_engine(redis: Any | None, text: str, *, voice: str | None = None, lang: str = "id") -> tuple[bytes, str]:
    """Cached by content, voice, language and style, so the morning precompute and the phone's request share one
    synthesis and a changed setting never serves old audio. Also says which engine spoke: audio from a backup engine
    (the chosen one was out of quota, say) is kept for ten minutes only, so the chosen engine is tried again soon."""
    if not get_settings().tts_enabled:
        raise TtsError("disabled")
    await llm_config.current()
    key = _key(text, voice, lang)
    if redis is not None:
        try:
            if hit := await redis.get(key):
                eng = await redis.get(key + ":e")
                return hit, (eng.decode() if isinstance(eng, bytes) else eng) or "unknown"
        except Exception:
            pass
    audio, engine = await synthesize_ex(text, voice=voice, lang=lang)
    first = (plan(voice, lang) or [("", None)])[0][0]
    ttl = CACHE_TTL_S if engine == first else BACKUP_TTL_S
    if redis is not None:
        try:
            await redis.set(key, audio, ex=ttl)
            await redis.set(key + ":e", engine, ex=ttl)
        except Exception:
            pass
    return audio, engine


async def audio_for(redis: Any | None, text: str, *, voice: str | None = None, lang: str = "id") -> bytes:
    return (await audio_with_engine(redis, text, voice=voice, lang=lang))[0]


async def warm(redis: Any | None, texts: list[str], *, voice: str | None = None, lang: str = "id") -> int:
    """Pre-generate audio (the morning precompute). Failures are counted, not raised. -> clips ready."""
    ready = 0
    for t in texts:
        try:
            await audio_for(redis, t, voice=voice, lang=lang)
            ready += 1
        except TtsError as e:
            log.warning("tts_warm_failed", error=str(e))
    return ready
