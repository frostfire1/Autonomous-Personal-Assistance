"""The owner's AI settings, edited in the web dashboard instead of in files: which provider (an OpenAI-compatible base URL
and key) and which model each AI role and each debating agent uses, which of them are switched off, and which speech
engine and model reads the briefing. Stored once on the server (in `policy_rules`, key `llm_config`).

Anything not set here keeps the original behaviour: the LiteLLM proxy and its routes (infra/litellm/config.yaml).
A role or agent with a provider and a model set calls that provider directly (`integrations/llm.py`), so the models on
offer can be whatever the provider's `/models` lists, with no change to the proxy's file and no restart."""

import re
import time
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from apa.agents.personas import AGENTS, ARBITER, MACRO, MENTAL, RESEARCH, ROUTINE, VISION
from apa.core.db import SessionLocal
from apa.db.models.budget import PolicyRule

PROTECTED = frozenset(a.id for a in AGENTS.values() if a.protected)  # Master, Operator, Arbiter
MAX_CUSTOM = 12
RULE_KEY = "llm_config"
CACHE_S = 10.0  # other processes (API, worker, bot) see a change within this long

# the roles (LiteLLM routes) and what each is for, as the owner sees them
ROUTES: dict[str, str] = {
    ROUTINE: "Routine: extraction, classification, debate, news, translation",
    VISION: "Vision: read receipts and payment slips (must be able to read images)",
    ARBITER: "Arbiter: the final decision of a debate",
    MENTAL: "Mental health: /private sessions (what is said goes to the chosen provider)",
    MACRO: "Macro: economic analysis",
    RESEARCH: "Deep research: the /private research agent",
}
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,29}$")

# How much room a role or agent gets, as the owner sees it in the dashboard. "normal" is exactly what the code
# asked for before this setting existed, so a fresh install and every saved configuration behave as they always did.
# The numbers multiply the `max_tokens` of a call and the size of the chat context (recent turns and the data block);
# they do not change what the provider's model can do, only what we choose to spend on it.
Preset = Literal["thrifty", "normal", "generous", "maximum"]
PRESETS: dict[str, tuple[float, int, float]] = {  # preset -> (max_tokens x, chat turns kept, context chars x)
    "thrifty": (0.5, 6, 0.5),
    "normal": (1.0, 16, 1.0),
    "generous": (2.0, 32, 1.5),
    "maximum": (3.5, 48, 2.5),
}
PRESET_LABELS: dict[str, str] = {  # shown in the dashboard
    "thrifty": "Hemat (shorter answers, less history)",
    "normal": "Normal (as designed)",
    "generous": "Panjang (longer answers, more history)",
    "maximum": "Tanpa Potong / Full (uncut, complete detailed answers)",
}
TOKEN_FLOOR, TOKEN_CEILING = 16, 8000  # a multiplied max_tokens never leaves this range


class Provider(BaseModel):
    base_url: str
    api_key: str = ""

    @field_validator("base_url")
    @classmethod
    def _url(cls, v: str) -> str:
        v = v.strip().rstrip("/")
        if not re.match(r"^https?://[^\s/]+", v):
            raise ValueError("base_url must start with http:// or https://")
        return v


class Slot(BaseModel):
    enabled: bool = True
    provider: str = ""
    model: str = Field(default="", max_length=120)
    preset: Preset = "normal"  # how much output and context this role or agent is given

    @field_validator("model")
    @classmethod
    def _tidy(cls, v: str) -> str:
        return v.strip()


class CustomAgent(BaseModel):
    """An agent the owner made: a name, how it behaves, when the Master should send a question to it, which facts it may
    read, and (optionally) its own provider and model. It answers in chat; it only reads, like every chat agent."""

    name: str = Field(min_length=1, max_length=40)
    persona: str = Field(default="", max_length=800)
    hint: str = Field(default="", max_length=200)
    data: list[Literal["finance", "health"]] = Field(default_factory=lambda: ["finance"])
    enabled: bool = True
    provider: str = ""
    model: str = Field(default="", max_length=120)
    preset: Preset = "normal"

    @field_validator("name", "persona", "hint")
    @classmethod
    def _tidy(cls, v: str) -> str:
        return " ".join(v.split())


class VoiceCfg(BaseModel):
    engine: Literal["auto", "gemini", "edge", "custom"] = "auto"
    gemini_model: str = Field(default="", max_length=120)  # empty = GEMINI_TTS_MODEL from the environment
    # "custom": any provider above that serves the OpenAI speech API (POST {base_url}/audio/speech)
    custom_provider: str = ""
    custom_model: str = Field(default="", max_length=120)
    custom_voices: list[str] = Field(default_factory=list, max_length=40)  # the voice names that model offers


class LLMConfig(BaseModel):
    providers: dict[str, Provider] = Field(default_factory=dict)
    routes: dict[str, Slot] = Field(default_factory=dict)
    agents: dict[str, Slot] = Field(default_factory=dict)  # built-in agents by id (A-02 ...)
    custom: dict[str, CustomAgent] = Field(default_factory=dict)  # the owner's own agents, ids like C-budget
    voice: VoiceCfg = Field(default_factory=VoiceCfg)
    chat_max_tokens: int = Field(default=2500, ge=400, le=16000)
    chat_max_reply_chars: int = Field(default=15000, ge=1000, le=50000)
    chat_split_long_messages: bool = True

    @field_validator("providers")
    @classmethod
    def _names(cls, v: dict[str, Provider]) -> dict[str, Provider]:
        for name in v:
            if not _NAME.match(name):
                raise ValueError(f"provider name {name!r}: use lowercase letters, digits, - or _ (up to 30)")
        return v

    @model_validator(mode="after")
    def _consistent(self) -> "LLMConfig":
        for key in self.routes:
            if key not in ROUTES:
                raise ValueError(f"unknown role {key}")
        for key, slot in self.agents.items():
            if key not in AGENTS or AGENTS[key].kind == "engine" and key not in PROTECTED:
                raise ValueError(f"unknown agent {key}")
            if key in PROTECTED and not slot.enabled:
                raise ValueError(f"{AGENTS[key].name} is part of the core and cannot be switched off")
        if len(self.custom) > MAX_CUSTOM:
            raise ValueError(f"at most {MAX_CUSTOM} custom agents")
        for key, agent in self.custom.items():
            if not re.match(r"^C-[a-z0-9]{1,12}$", key):
                raise ValueError(f"custom agent id {key!r}: use C- and up to 12 lowercase letters or digits")
            if bool(agent.provider) != bool(agent.model):
                raise ValueError(f"agent {agent.name}: provider and model go together")
            if agent.provider and agent.provider not in self.providers:
                raise ValueError(f"agent {agent.name}: provider {agent.provider!r} is not defined")
        v = self.voice
        if v.custom_provider and v.custom_provider not in self.providers:
            raise ValueError(f"voice: provider {v.custom_provider!r} is not defined")
        if v.engine == "custom" and not (v.custom_provider and v.custom_model):
            raise ValueError("voice: the custom engine needs a provider and a model")
        for where, slots in (("role", self.routes), ("agent", self.agents)):
            for key, slot in slots.items():
                if bool(slot.provider) != bool(slot.model):
                    raise ValueError(f"{where} {key}: provider and model go together")
                if slot.provider and slot.provider not in self.providers:
                    raise ValueError(f"{where} {key}: provider {slot.provider!r} is not defined")
        return self

    def slot_for(self, route: str, agent: str | None = None) -> Slot | None:
        """The setting that applies to a call: the agent's own, else its role's, else None (the original behaviour)."""
        if agent and agent in self.custom:
            c = self.custom[agent]
            return Slot(enabled=c.enabled, provider=c.provider, model=c.model, preset=c.preset)
        if agent and agent in self.agents:
            return self.agents[agent]
        return self.routes.get(route)

    def preset_for(self, route: str, agent: str | None = None) -> str:
        """The preset that applies to a call. A slot that only sets a preset (no provider, still on the proxy)
        counts too, so the choice works whether or not the owner picked their own provider."""
        slot = self.slot_for(route, agent)
        name = slot.preset if slot is not None else "normal"
        return name if name in PRESETS else "normal"

    def budget(self, route: str, agent: str | None = None) -> tuple[float, int, float]:
        """(max_tokens multiplier, chat turns to keep, context characters multiplier) for a call."""
        return PRESETS[self.preset_for(route, agent)]

    def agent_enabled(self, agent_id: str) -> bool:
        if agent_id in PROTECTED:
            return True
        if agent_id in self.custom:
            return self.custom[agent_id].enabled
        s = self.agents.get(agent_id)
        return s.enabled if s else True

    def active_custom(self) -> dict[str, CustomAgent]:
        return {k: v for k, v in self.custom.items() if v.enabled}


def scale_tokens(max_tokens: int | None, mult: float) -> int | None:
    """`max_tokens` after the preset, kept sane: never below a usable answer, never above the ceiling.
    None (the caller let the model decide) stays None."""
    if max_tokens is None or mult == 1.0:
        return max_tokens
    return max(TOKEN_FLOOR, min(TOKEN_CEILING, round(max_tokens * mult)))


def masked(key: str) -> str:
    """What the dashboard may show of a saved key: the last four characters."""
    return "" if not key else ("…" + key[-4:] if len(key) > 8 else "…")


def public(cfg: LLMConfig) -> dict:
    """The configuration for the dashboard: keys never leave the server, only a hint of their end."""
    d = cfg.model_dump()
    for p in d["providers"].values():
        p["api_key_hint"] = masked(p.pop("api_key"))
    return d


def merge_keys(new: LLMConfig, old: LLMConfig, sent_blank: set[str]) -> LLMConfig:
    """A provider saved without a key keeps the key it had, so the dashboard never needs to hold or resend it."""
    for name, p in new.providers.items():
        if not p.api_key and name in old.providers and name not in sent_blank:
            p.api_key = old.providers[name].api_key
    return new


_cache: tuple[float, LLMConfig] | None = None


async def load(sm: async_sessionmaker = SessionLocal) -> LLMConfig:
    """The saved settings; damaged or missing ones fall back to the defaults (everything as before), never to an error."""
    async with sm() as s:
        row = (await s.execute(select(PolicyRule).where(PolicyRule.rule_key == RULE_KEY))).scalar_one_or_none()
    if row is None or not isinstance(row.value_json, dict):
        return LLMConfig()
    try:
        return LLMConfig(**row.value_json)
    except ValueError:
        return LLMConfig()


async def save(cfg: LLMConfig, sm: async_sessionmaker = SessionLocal) -> None:
    global _cache
    data = cfg.model_dump()
    async with sm() as s:
        stmt = insert(PolicyRule).values(rule_key=RULE_KEY, scope="global", scope_ref="", value_json=data, updated_by="owner",
                                         is_active=True)
        await s.execute(stmt.on_conflict_do_update(index_elements=["rule_key", "scope", "scope_ref"],
                                                   set_={"value_json": data, "updated_by": "owner"}))
        await s.commit()
    _cache = (time.monotonic(), cfg)


async def current(sm: async_sessionmaker = SessionLocal) -> LLMConfig:
    """`load` with a short cache, for the hot path (every model call asks)."""
    global _cache
    if _cache is not None and time.monotonic() - _cache[0] < CACHE_S:
        return _cache[1]
    try:
        cfg = await load(sm)
    except Exception:  # a database hiccup must not stop a model call: use what was last known, else the defaults
        cfg = _cache[1] if _cache else LLMConfig()
    _cache = (time.monotonic(), cfg)
    return cfg


def last() -> LLMConfig:
    """The most recently loaded settings without waiting (synchronous callers); defaults before the first load."""
    return _cache[1] if _cache else LLMConfig()


def reset_cache() -> None:
    global _cache
    _cache = None
