"""News and CVE choices for the web dashboard (same key as /ingest): switch built-in feeds off, add any RSS or Atom feed (with
a test), make topics, and tune the CVE rule. Everything here is optional: the built-in feeds and the original CVE rule
(high and critical, plus anything CISA lists as exploited) work with no changes."""

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from apa.gateway.routers.ingest import reason, require_ingest_key
from apa.news import config as C
from apa.news.fetch import FetchError, fetch_bytes, parse_feed
from apa.news.sources import COUNTRIES, FEEDS, TOPICS

router = APIRouter(prefix="/news-config", tags=["news"], dependencies=[Depends(require_ingest_key)])


def _view(cfg: C.NewsConfig) -> dict:
    return {
        "config": cfg.model_dump(),
        "builtin_feeds": [{"name": f.name, "url": f.url, "topic": f.topic, "enabled": f.name not in cfg.disabled_feeds}
                          for f in cfg.builtin_feeds()],  # the home country's national feeds and the worldwide ones
        "countries": [{"key": k, "label": v} for k, v in COUNTRIES.items()],
        "topics": [{"key": k, "label": v[1], "builtin": k in TOPICS} for k, v in cfg.catalog().items()],
        "limits": {"feeds": C.MAX_FEEDS, "topics": C.MAX_TOPICS},
    }


@router.get("")
async def get_news_config() -> dict:
    return _view(await C.load())


class NewsIn(BaseModel):
    country: str = "id"
    disabled_feeds: list[str] = Field(default_factory=list)
    feeds: list[C.CustomFeed] = Field(default_factory=list)
    topics: dict[str, C.CustomTopic] = Field(default_factory=dict)
    cve: C.CveRule = Field(default_factory=C.CveRule)


@router.put("")
async def put_news_config(body: NewsIn) -> dict:
    known = {f.name for f in FEEDS}  # a switched-off name of any country is remembered
    try:
        cfg = C.NewsConfig(country=body.country, disabled_feeds=[n for n in body.disabled_feeds if n in known], feeds=body.feeds,
                           topics=body.topics, cve=body.cve)
    except ValueError as e:
        raise HTTPException(422, reason(e)) from None
    await C.save(cfg)
    return _view(cfg)


class FeedTest(BaseModel):
    url: str


@router.post("/test-feed")
async def test_feed(body: FeedTest) -> dict:
    """Read a feed once and say what is in it, before the owner adds it. Same safety as the real reader: public addresses
    only, a size cap, a time limit."""
    try:
        url = C.CustomFeed(name="test feed", url=body.url, topic="nasional").url
    except ValueError as e:
        return {"ok": False, "detail": reason(e)}
    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=20) as client:
            entries = parse_feed(await fetch_bytes(client, url))
    except FetchError as e:
        return {"ok": False, "detail": str(e)[:160]}
    return {"ok": bool(entries), "count": len(entries), "sample": [e.title for e in entries[:3]],
            "detail": "" if entries else "the address answered but has no articles in it"}
