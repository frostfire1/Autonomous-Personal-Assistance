"""Collect: read every feed, store new articles once (dedup by hash of title and URL), and keep a heartbeat per
source so silent failure does not go unnoticed (blueprint v2 4.4 A2). One source failing never stops the others."""

import asyncio
import hashlib
from datetime import datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from apa.core.logging import get_logger
from apa.db.models.intel import NewsArticle
from apa.news import config as news_config
from apa.news.fetch import FetchError, fetch_bytes, parse_feed
from apa.news.rank import Article
from apa.news.sources import Feed

log = get_logger(__name__)

HEARTBEAT_KEY = "apa:news:src:"
QUIET_AFTER_FAILURES = 6  # consecutive failures before the owner hears about a source


def dedup_hash(title: str, url: str) -> str:
    return hashlib.sha256(f"{title.strip().lower()}|{url.strip()}".encode()).hexdigest()


async def _one(client: httpx.AsyncClient, feed: Feed) -> tuple[Feed, list | FetchError]:
    try:
        return feed, parse_feed(await fetch_bytes(client, feed.url))
    except FetchError as e:
        return feed, e


async def ingest_news(
    sm: async_sessionmaker, client: httpx.AsyncClient, redis: Any | None, *, feeds: tuple[Feed, ...] | None = None
) -> dict:
    """-> {'new': n, 'failed': [source names], 'quiet': [sources newly past the failure threshold]}."""
    feeds = feeds if feeds is not None else (await news_config.current(sm)).active_feeds()  # built-in minus off, plus the owner's
    results = await asyncio.gather(*[_one(client, f) for f in feeds])
    new, failed, quiet = 0, [], []
    for feed, got in results:
        if isinstance(got, FetchError):
            failed.append(feed.name)
            if redis is not None:
                n = await redis.incr(HEARTBEAT_KEY + feed.name)
                if n == QUIET_AFTER_FAILURES:
                    quiet.append(feed.name)
            log.warning("news_source_failed", source=feed.name, error=str(got))
            continue
        if redis is not None:
            await redis.delete(HEARTBEAT_KEY + feed.name)
        async with sm() as s:
            for e in got:
                res = await s.execute(
                    insert(NewsArticle)
                    .values(
                        url=e.url,
                        title=e.title,
                        dedup_hash=dedup_hash(e.title, e.url),
                        source=feed.name,
                        published_at=e.published,
                        body=e.summary or None,
                    )
                    .on_conflict_do_nothing(index_elements=["dedup_hash"])
                    .returning(NewsArticle.id)
                )
                new += 1 if res.scalar_one_or_none() is not None else 0
            await s.commit()
    log.info("news_ingested", new=new, failed=len(failed))
    return {"new": new, "failed": failed, "quiet": quiet}


async def recent_articles(sm: async_sessionmaker, now: datetime, *, hours: int = 36) -> list[Article]:
    """Articles of the window as the ranker wants them (the topic comes from the feed they were read from)."""
    topics = {f.name: f.topic for f in (await news_config.current(sm)).active_feeds()}
    async with sm() as s:
        rows = (
            (
                await s.execute(
                    select(NewsArticle)
                    .where(
                        NewsArticle.published_at >= now - timedelta(hours=hours),
                        NewsArticle.published_at <= now,
                    )
                    .order_by(NewsArticle.published_at.desc())
                    .limit(3000)
                )
            )
            .scalars()
            .all()
        )
    return [
        Article(r.source, r.title, r.url, r.published_at, r.body or "", topics[r.source])
        for r in rows
        if r.source in topics
    ]
