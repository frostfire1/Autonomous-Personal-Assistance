"""The owner's briefing preferences: language, voice, and the city the weather is for. Stored once on the server (in
`policy_rules`) so the Android app, the web dashboard and the scheduled job all read the same choice."""

import re
from dataclasses import dataclass

from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from apa.core.settings import get_settings
from apa.db.models.budget import PolicyRule

RULE_KEY = "briefing_prefs"


# language key -> (label shown to the owner, the language's name in English for the voice instruction)
LANGUAGES: dict[str, tuple[str, str]] = {
    "id": ("Bahasa Indonesia", "Indonesian"),
    "en": ("English", "English"),
    "ja": ("日本語 (Japanese)", "Japanese"),
    "zh": ("中文 (Chinese)", "Mandarin Chinese"),
    "ko": ("한국어 (Korean)", "Korean"),
    "de": ("Deutsch (German)", "German"),
    "fr": ("Français (French)", "French"),
    "es": ("Español (Spanish)", "Spanish"),
}

# Microsoft neural voice per language and gender. A Gemini voice that cannot be used falls back to the same gender here.
EDGE_VOICES: dict[str, dict[str, str]] = {
    "f": {"id": "id-ID-GadisNeural", "en": "en-US-AvaMultilingualNeural", "ja": "ja-JP-NanamiNeural", "zh": "zh-CN-XiaoxiaoNeural",
          "ko": "ko-KR-SunHiNeural", "de": "de-DE-KatjaNeural", "fr": "fr-FR-DeniseNeural", "es": "es-ES-ElviraNeural"},
    "m": {"id": "id-ID-ArdiNeural", "en": "en-US-AndrewMultilingualNeural", "ja": "ja-JP-KeitaNeural", "zh": "zh-CN-YunxiNeural",
          "ko": "ko-KR-InJoonNeural", "de": "de-DE-ConradNeural", "fr": "fr-FR-HenriNeural", "es": "es-ES-AlvaroNeural"},
}


@dataclass(frozen=True)
class Voice:
    engine: str  # "gemini" | "edge"
    name: str  # the engine's own voice name (for edge: the Indonesian one; other languages come from EDGE_VOICES)
    label: str
    gender: str = "f"  # "f" | "m", used to pick a matching Microsoft voice when Gemini is unavailable


VOICES: dict[str, Voice] = {
    "zephyr": Voice("gemini", "Zephyr", "Zephyr: bright, cheerful"),
    "puck": Voice("gemini", "Puck", "Puck: upbeat, energetic", "m"),
    "kore": Voice("gemini", "Kore", "Kore: firm, clear"),
    "leda": Voice("gemini", "Leda", "Leda: youthful, fresh"),
    "aoede": Voice("gemini", "Aoede", "Aoede: relaxed, light"),
    "charon": Voice("gemini", "Charon", "Charon: calm, informative", "m"),
    "fenrir": Voice("gemini", "Fenrir", "Fenrir: excitable", "m"),
    "orus": Voice("gemini", "Orus", "Orus: steady, authoritative", "m"),
    "gadis": Voice("edge", "id-ID-GadisNeural", "Microsoft female (Gadis, Ava, Nanami...)"),
    "ardi": Voice("edge", "id-ID-ArdiNeural", "Microsoft male (Ardi, Andrew, Keita...)", "m"),
}


# Gemini's prebuilt voices (every one speaks every supported language): name -> (gender, character)
_GEMINI = ("Zephyr:f:bright", "Puck:m:upbeat", "Charon:m:informative", "Kore:f:firm", "Fenrir:m:excitable", "Leda:f:youthful",
           "Orus:m:steady", "Aoede:f:easy-going", "Callirrhoe:f:easy-going", "Autonoe:f:bright", "Enceladus:m:breathy", "Iapetus:m:clear",
           "Umbriel:m:calm", "Algieba:m:smooth", "Despina:f:smooth", "Erinome:f:clear", "Algenib:m:gravelly", "Rasalgethi:m:informative",
           "Laomedeia:f:upbeat", "Achernar:f:soft", "Alnilam:m:firm", "Schedar:m:even", "Gacrux:f:mature", "Pulcherrima:f:forward",
           "Achird:m:friendly", "Zubenelgenubi:m:easy-going", "Vindemiatrix:f:soft", "Sadachbia:m:lively", "Sadaltager:m:knowledgeable",
           "Sulafat:f:warm")
GEMINI_VOICES: dict[str, tuple[str, str]] = {n: (g, c) for n, g, c in (x.split(":") for x in _GEMINI)}
VOICE_KEY = re.compile(r"^(gemini|edge|custom):[A-Za-z0-9_.-]{2,60}$")  # "engine:voice name", beside the short legacy keys


def valid_voice(key: str) -> bool:
    return key in VOICES or bool(VOICE_KEY.match(key))


def voice_gender(key: str) -> str:
    """"f" or "m", for choosing a matching backup voice."""
    if key in VOICES:
        return VOICES[key].gender
    engine, _, name = key.partition(":")
    if engine == "gemini":
        return GEMINI_VOICES.get(name, ("f", ""))[0]
    return "m" if name in EDGE_VOICES["m"].values() else "f"


def edge_name(gender: str, lang: str) -> str:
    return EDGE_VOICES["m" if gender == "m" else "f"].get(lang) or EDGE_VOICES["f"]["en"]


class Prefs(BaseModel):
    language: str = "id"
    voice: str = "zephyr"
    city: str = Field(default="", max_length=60)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    topics: list[str] = Field(default_factory=lambda: ["nasional", "cyber"])  # which news sections the briefing has
    keywords: list[str] = Field(default_factory=list)  # words to watch for in any headline (a "your interests" section)
    currency: str = "IDR"  # the ledger's one home currency (no conversion): how money is written and which amounts are read
    gps: bool = False  # the weather follows the phone's position (sent by the app) instead of a typed city

    @field_validator("currency")
    @classmethod
    def _known_currency(cls, v: str) -> str:
        from apa.currency import CURRENCIES

        v = v.strip().upper()
        if v not in CURRENCIES:
            raise ValueError(f"unknown currency; choose one of {', '.join(CURRENCIES)}")
        return v

    @field_validator("language")
    @classmethod
    def _known_language(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in LANGUAGES:
            raise ValueError(f"unknown language; choose one of {', '.join(LANGUAGES)}")
        return v

    @field_validator("topics")
    @classmethod
    def _known_topics(cls, v: list[str]) -> list[str]:
        bad = [x for x in v if not re.match(r"^[a-z0-9][a-z0-9_-]{1,23}$", x)]  # which ids exist is checked against the catalog
        if bad:
            raise ValueError(f"unknown topic {bad[0]!r}")
        return list(dict.fromkeys(v))  # no repeats, owner's order kept

    @field_validator("keywords")
    @classmethod
    def _tidy_keywords(cls, v: list[str]) -> list[str]:
        out = [" ".join(k.split())[:40] for k in v if k and k.strip()]
        return list(dict.fromkeys(out))[:12]

    @field_validator("voice")
    @classmethod
    def _known_voice(cls, v: str) -> str:
        v = v.strip()
        if not valid_voice(v.lower()) and not VOICE_KEY.match(v):
            raise ValueError("unknown voice; use one of the listed voices")
        return v.lower() if v.lower() in VOICES else v

    @field_validator("city")
    @classmethod
    def _tidy_city(cls, v: str) -> str:
        return " ".join(v.split())

    @model_validator(mode="after")
    def _both_coordinates_or_neither(self) -> "Prefs":
        if (self.lat is None) != (self.lon is None):
            raise ValueError("lat and lon go together")
        return self


def default_voice() -> str:
    """Gemini's Zephyr when a Gemini key exists, else the Microsoft female voice."""
    return "zephyr" if get_settings().gemini_api_key else "gadis"


def default_prefs() -> Prefs:
    return Prefs(voice=default_voice(), city=get_settings().weather_city)


async def load(sm: async_sessionmaker) -> Prefs:
    """The saved choice; anything missing or damaged falls back to the defaults, never to an error."""
    async with sm() as s:
        row = (await s.execute(select(PolicyRule).where(PolicyRule.rule_key == RULE_KEY))).scalar_one_or_none()
    base = default_prefs()
    if row is None or not isinstance(row.value_json, dict):
        return base
    try:
        return Prefs(**{**base.model_dump(), **row.value_json})
    except ValueError:
        return base


async def save(sm: async_sessionmaker, prefs: Prefs) -> None:
    data = prefs.model_dump()
    async with sm() as s:
        stmt = insert(PolicyRule).values(rule_key=RULE_KEY, scope="global", scope_ref="", value_json=data, updated_by="owner",
                                         is_active=True)
        await s.execute(stmt.on_conflict_do_update(index_elements=["rule_key", "scope", "scope_ref"],
                                                   set_={"value_json": data, "updated_by": "owner"}))
        await s.commit()


def voice_name(key: str, lang: str) -> tuple[str, str]:
    """(engine, the engine's voice name) for a voice key in a language. A short Microsoft key is the voice of that language
    and gender; "engine:name" keys name the voice exactly; Gemini voices speak every supported language."""
    if VOICE_KEY.match(key):
        engine, _, name = key.partition(":")
        return engine, name
    v = VOICES.get(key) or VOICES[default_voice()]
    return v.engine, (edge_name(v.gender, lang) if v.engine == "edge" else v.name)
