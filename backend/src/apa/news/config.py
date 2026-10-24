"""The owner's news and CVE choices, edited in the web dashboard after setup. The built-in feeds, the topics and the CVE rule
(high and critical, plus anything in CISA's exploited list) are only the starting point: here the owner switches built-in
feeds off, adds any RSS or Atom feed, makes their own topics, and tunes the CVE rule (minimum score, a watch list of
vendors and products that always get through, how far back to look). Stored once on the server (`policy_rules`, key
`news_config`); the ingest job, the briefing and the Telegram text all read it, within seconds of a change."""

import re
import time

from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from apa.core.db import SessionLocal
from apa.db.models.budget import PolicyRule
from apa.news.sources import COUNTRIES, FEEDS, TOPICS, Feed

RULE_KEY = "news_config"
CACHE_S = 10.0
MAX_FEEDS = 40
MAX_TOPICS = 12
SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{1,23}$")


class CustomFeed(BaseModel):
    name: str = Field(min_length=2, max_length=40)
    url: str
    topic: str
    every_min: int = Field(default=60, ge=5, le=1440)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return " ".join(v.split())

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        v = v.strip()
        if not re.match(r"^https?://[^\s/]+", v):
            raise ValueError("a feed address must start with http:// or https://")
        return v


class CustomTopic(BaseModel):
    label: str = Field(min_length=2, max_length=30)

    @field_validator("label")
    @classmethod
    def _label(cls, v: str) -> str:
        return " ".join(v.split())


class CveRule(BaseModel):
    """Which CVEs reach the briefing and Telegram. Defaults are the original rule."""

    min_cvss: float = Field(default=7.0, ge=0, le=10)
    kev_always: bool = True  # anything CISA lists as exploited gets through, whatever its score
    watch: list[str] = Field(default_factory=list, max_length=40)  # vendor or product words: always get through
    hours: int = Field(default=24, ge=6, le=168)  # how far back "new" reaches

    @field_validator("watch")
    @classmethod
    def _watch(cls, v: list[str]) -> list[str]:
        out = [" ".join(w.split()).lower()[:50] for w in v if w and w.strip()]
        return list(dict.fromkeys(out))


class NewsConfig(BaseModel):
    country: str = "id"  # whose national news the built-in national feeds and the national ranking are for
    disabled_feeds: list[str] = Field(default_factory=list)  # names of built-in feeds switched off
    feeds: list[CustomFeed] = Field(default_factory=list, max_length=MAX_FEEDS)
    topics: dict[str, CustomTopic] = Field(default_factory=dict)
    cve: CveRule = Field(default_factory=CveRule)

    @field_validator("country")
    @classmethod
    def _country(cls, v: str) -> str:
        if v not in COUNTRIES:
            raise ValueError(f"unknown country {v!r}; choose one of {', '.join(COUNTRIES)}")
        return v

    @model_validator(mode="after")
    def _consistent(self) -> "NewsConfig":
        if len(self.topics) > MAX_TOPICS:
            raise ValueError(f"at most {MAX_TOPICS} custom topics")
        for key in self.topics:
            if not SLUG.match(key) or key in TOPICS or key == "minat":
                raise ValueError(f"topic id {key!r}: lowercase letters, digits, - or _ (2 to 24), not a built-in name")
        known = {*TOPICS, *self.topics}
        names = {f.name.lower() for f in FEEDS}
        for f in self.feeds:
            if f.topic not in known:
                raise ValueError(f"feed {f.name}: unknown topic {f.topic!r}")
            if f.name.lower() in names:
                raise ValueError(f"feed name {f.name!r} is already used")
            names.add(f.name.lower())
        return self

    def catalog(self) -> dict[str, tuple[str, str]]:
        """Every topic the owner can switch on: key -> (label, English title). Custom topics use one label for both."""
        return {**TOPICS, **{k: (t.label, t.label) for k, t in self.topics.items()}}

    def builtin_feeds(self) -> tuple[Feed, ...]:
        """The built-in feeds for the home country: every worldwide feed, and only this country's national feeds."""
        return tuple(f for f in FEEDS if not f.country or f.country == self.country)

    def active_feeds(self) -> tuple[Feed, ...]:
        built = tuple(f for f in self.builtin_feeds() if f.name not in self.disabled_feeds)
        return built + tuple(Feed(f.name, f.url, f.topic, f.every_min) for f in self.feeds)


_cache: tuple[float, NewsConfig] | None = None


async def load(sm: async_sessionmaker = SessionLocal) -> NewsConfig:
    async with sm() as s:
        row = (await s.execute(select(PolicyRule).where(PolicyRule.rule_key == RULE_KEY))).scalar_one_or_none()
    if row is None or not isinstance(row.value_json, dict):
        return NewsConfig()
    try:
        return NewsConfig(**row.value_json)
    except ValueError:
        return NewsConfig()


async def save(cfg: NewsConfig, sm: async_sessionmaker = SessionLocal) -> None:
    global _cache
    data = cfg.model_dump()
    async with sm() as s:
        stmt = insert(PolicyRule).values(rule_key=RULE_KEY, scope="global", scope_ref="", value_json=data, updated_by="owner",
                                         is_active=True)
        await s.execute(stmt.on_conflict_do_update(index_elements=["rule_key", "scope", "scope_ref"],
                                                   set_={"value_json": data, "updated_by": "owner"}))
        await s.commit()
    _cache = (time.monotonic(), cfg)


async def current(sm: async_sessionmaker = SessionLocal) -> NewsConfig:
    """`load` with a short cache; a database hiccup gives the last known choices, else the defaults (never an error)."""
    global _cache
    if _cache is not None and time.monotonic() - _cache[0] < CACHE_S:
        return _cache[1]
    try:
        cfg = await load(sm)
    except Exception:
        cfg = _cache[1] if _cache else NewsConfig()
    _cache = (time.monotonic(), cfg)
    return cfg
