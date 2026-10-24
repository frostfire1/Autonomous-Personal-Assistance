"""The scheduled news and cyber jobs. Each is a plain function (testable) that the worker's cron entries call.

What reaches Telegram without being asked, by design (blueprint v2: urgent only; the rest goes into the
briefing): a story covered by 3+ outlets with a very high score, a CVE that is Critical and in KEV or on the
watchlist, and a quiet-source warning. Each is sent once (the digest delivery key makes it idempotent)."""

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from apa import prefs
from apa.core.logging import get_logger
from apa.core.settings import get_settings
from apa.news import briefing as brief
from apa.news import config as news_config
from apa.news.cyber import ingest_cyber
from apa.news.ingest import ingest_news, recent_articles
from apa.news.rank import rank
from apa.news.summarize import summarize
from apa.news.telegram import daily_telegram
from apa.reporting.digest import deliver
from apa.tts import voice

log = get_logger(__name__)

URGENT_SCORE = 65  # three outlets, fresh, and clearly central to government or the economy
URGENT_SOURCES = 3
MAX_URGENT_PER_RUN = 2


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(follow_redirects=False, limits=httpx.Limits(max_connections=10))


async def run_news_ingest(
    sm, llm: Any, notifier: Any, redis: Any | None, now: datetime, *, client=None
) -> dict:
    owned = client is None
    client = client or make_client()
    try:
        res = await ingest_news(sm, client, redis)
    finally:
        if owned:
            await client.aclose()
    for name in res["quiet"]:
        await deliver(
            sm,
            notifier,
            redis,
            f"src:{name}:{now:%Y-%m-%d}",
            f"⚠️ Sumber berita {name} diam: gagal dibaca beberapa kali berturut-turut.",
        )
    pushed = 0
    country = (await news_config.current(sm)).country
    for r in rank(await recent_articles(sm, now), now, topic="nasional", country=country):
        if pushed >= MAX_URGENT_PER_RUN:
            break
        if r.score >= URGENT_SCORE and len(r.cluster.sources) >= URGENT_SOURCES:
            if await deliver(
                sm, notifier, redis, f"news:{r.cluster.key}", "📰 " + await summarize(llm, r.cluster)
            ):
                pushed += 1
    return {**res, "pushed": pushed}


async def run_cyber_poll(
    sm, notifier: Any, redis: Any | None, now: datetime, *, client=None, nvd_key: str = ""
) -> dict:
    owned = client is None
    client = client or make_client()
    try:
        res = await ingest_cyber(sm, client, now, nvd_key=nvd_key)
    finally:
        if owned:
            await client.aclose()
    for cid in res["urgent"]:
        await deliver(
            sm,
            notifier,
            redis,
            f"cve:{cid}",
            f"🚨 {cid}: kerentanan kritis yang sudah dieksploitasi atau cocok dengan daftar pantauanmu.",
        )
    return res


def precompute_time(wake: str, lead_minutes: int) -> tuple[int, int]:
    """(hour, minute) to start preparing the briefing: `lead_minutes` before the wake time 'HH:MM'. A bad value means
    06:30 minus the lead rather than a worker that will not start."""
    try:
        h, m = (int(x) for x in wake.split(":"))
        if not (0 <= h < 24 and 0 <= m < 60):
            raise ValueError
    except ValueError:
        h, m = 6, 30
    h, m = divmod((h * 60 + m - max(lead_minutes, 0)) % 1440, 60)
    return h, m


async def run_briefing_precompute(
    sm, llm: Any, redis: Any | None, tz: ZoneInfo, now: datetime, *, notifier: Any | None = None,
    reuse_since: datetime | None = None,
) -> list[int]:
    """Ahead of the wake-up time: build today's short and full briefings, have the short one's voice clips ready so
    playback starts at once, and send the detailed text version to Telegram. Each is made once: a rerun (a restart, a
    second trigger) with `reuse_since` set reuses what this morning's run already made, text, voice and Telegram."""
    choice = await prefs.load(sm)
    ids = []
    for kind in ("short", "full"):
        row = await brief.get_or_build(sm, llm, redis, tz, now, kind=kind, fresh=reuse_since is None, reuse_since=reuse_since,
                                       lang=choice.language)
        ids.append(row.id)
        if kind == "short" and get_settings().tts_enabled:
            await voice.warm(redis, [row.text], voice=choice.voice, lang=row.lang)  # ONE clip for the whole short briefing (one voice call)
    if notifier is not None:
        for i, text in enumerate(
            await daily_telegram(sm, redis, tz, now, rebuild=reuse_since is None, lang=choice.language, llm=llm), start=1
        ):
            await deliver(sm, notifier, redis, f"briefing:{now.astimezone(tz):%Y-%m-%d}:{choice.language}:{i}", text)
    return ids
