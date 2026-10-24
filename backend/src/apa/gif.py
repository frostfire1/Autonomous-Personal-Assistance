"""GIFs for the chat: a reaction when it truly fits (a celebration, a joke, a bit of comfort), found on Giphy or Tenor.

Needs GIPHY_API_KEY (developers.giphy.com, free) or TENOR_API_KEY in .env; without one the assistant says GIFs are not set up. The
search is made with a few plain words from the assistant (never anything about the owner), with the strictest content rating. The
chosen link is put into the reply as `[[gif:URL]]`; the Telegram notifier turns that into an animation, and only addresses on the
providers' own domains are ever sent (see `allowed`)."""

import random
import re
from urllib.parse import urlparse

import httpx

from apa.core.logging import get_logger
from apa.core.settings import get_settings

log = get_logger(__name__)

HOSTS = ("giphy.com", "tenor.com", "tenor.googleapis.com", "klipy.com")
MARK = re.compile(r"\s*\[\[gif:([^\s\]]+)\]\]\s*$")  # any address: one that is not allowed is removed, never shown


def allowed(url: str) -> bool:
    """Only https links on a GIF provider's own domain."""
    try:
        u = urlparse(url)
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    return u.scheme == "https" and any(host == h or host.endswith("." + h) for h in HOSTS)


def split(text: str) -> tuple[str, str | None]:
    """(the reply without the marker, the GIF link or None). An address that is not on a provider's domain is dropped."""
    m = MARK.search(text or "")
    if not m:
        return text, None
    clean = text[: m.start()].rstrip()
    return clean, (m.group(1) if allowed(m.group(1)) else None)


def enabled() -> bool:
    s = get_settings()
    return bool(s.giphy_api_key or s.tenor_api_key)


def clean_query(raw: object) -> str:
    if not isinstance(raw, str):
        return ""
    q = " ".join(re.sub(r"[^\w\s'-]", " ", raw).split())[:50].strip()
    return q if len(q) >= 2 and not re.search(r"\d{4,}", q) else ""


async def search(query: object, http: httpx.AsyncClient | None = None) -> str | None:
    """A GIF link for a few plain words, or None (not set up, nothing found, the service failed)."""
    q = clean_query(query)
    s = get_settings()
    if not q or not enabled():
        return None
    own = http is None
    http = http or httpx.AsyncClient(timeout=10)
    try:
        if s.giphy_api_key:
            r = await http.get("https://api.giphy.com/v1/gifs/search", params={"api_key": s.giphy_api_key, "q": q, "limit": 8, "rating": "g", "lang": "id"})
            urls = [g["images"]["original"]["url"] for g in r.json().get("data", []) if g.get("images", {}).get("original", {}).get("url")] if r.status_code == 200 else []
        else:
            r = await http.get("https://tenor.googleapis.com/v2/search", params={"key": s.tenor_api_key, "q": q, "limit": 8, "contentfilter": "high", "media_filter": "gif"})
            urls = [g["media_formats"]["gif"]["url"] for g in r.json().get("results", []) if g.get("media_formats", {}).get("gif", {}).get("url")] if r.status_code == 200 else []
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        log.warning("gif_search_failed", error=type(exc).__name__)
        return None
    finally:
        if own:
            await http.aclose()
    urls = [u for u in urls if allowed(u)]
    return random.choice(urls[:5]) if urls else None


async def tool(redis: object, query: object) -> str:
    """The `gif` tool of the assistant: -> text for the model with the marker to put at the end of its reply."""
    if not enabled():
        return "GIFs are not set up on this server (GIPHY_API_KEY or TENOR_API_KEY in .env). Answer without one."
    day_key = None
    try:
        from datetime import datetime

        day_key = f"apa:gif:count:{datetime.now():%Y%m%d}"
        n = await redis.incr(day_key)  # type: ignore[attr-defined]
        await redis.expire(day_key, 2 * 86400)  # type: ignore[attr-defined]
        if n > 20:
            return "Enough GIFs for today: answer without one."
    except Exception:
        pass
    url = await search(query)
    if url is None:
        return "No suitable GIF was found: answer without one."
    return f"Found. Put exactly this at the very end of your reply, after your words: [[gif:{url}]]"
