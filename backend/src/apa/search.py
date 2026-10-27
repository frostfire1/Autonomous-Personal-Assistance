"""Fast real-time web and knowledge search for the conversational assistant.

Supports:
1. Brave Search API (if BRAVE_API_KEY is configured)
2. Firecrawl Search API (if FIRECRAWL_API_KEY is configured)
3. Direct organic web search with snippet & link extraction
4. Indonesian and English Wikipedia API for factual definitions and articles

Runs synchronously within the assistant conversation loop so the owner receives
immediate answers with links, rather than waiting for a background research job.
"""

import base64
import re
from urllib.parse import quote

import httpx

from apa.core.logging import get_logger
from apa.core.sanitize import sanitize_untrusted
from apa.core.settings import get_settings

log = get_logger(__name__)

TIMEOUT_S = 8.0
MAX_QUERY_LEN = 120

WIKI_HEADERS = {
    "User-Agent": "AutonomousPersonalAssistant/1.0 (self-hosted personal assistant)",
    "Accept": "application/json",
    "Accept-Encoding": "identity",
}

WEB_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "id,en;q=0.9",
}


def _clean_query(raw: str) -> str:
    """Sanitize and trim query string."""
    s = sanitize_untrusted(raw, max_len=MAX_QUERY_LEN)
    q = re.sub(r"[^\w\s\-.,?']", " ", s.text)
    return " ".join(q.split())[:MAX_QUERY_LEN].strip()


def _decode_bing_url(href: str) -> str:
    """Decode direct destination URL from Bing redirect (u=a1<base64>)."""
    m = re.search(r"[?&]u=a1([a-zA-Z0-9_-]+)", href)
    if not m:
        return href
    encoded = m.group(1).replace("-", "+").replace("_", "/")
    # pad base64
    padded = encoded + "=" * ((4 - len(encoded) % 4) % 4)
    try:
        decoded = base64.b64decode(padded).decode("utf-8", errors="ignore")
        if decoded.startswith("http"):
            return decoded
    except Exception:
        pass
    return href


async def _search_brave(query: str, key: str, count: int = 4) -> list[dict[str, str]]:
    """Query Brave Search API."""
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as http:
        r = await http.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={
                "q": query,
                "count": count,
                "country": "ID",
                "search_lang": "id",
                "safesearch": "moderate",
            },
            headers={"X-Subscription-Token": key, "Accept": "application/json"},
        )
    if r.status_code != 200:
        return []
    results = []
    for h in r.json().get("web", {}).get("results", [])[:count]:
        title = re.sub(r"<[^>]+>", "", h.get("title") or "").strip()
        desc = re.sub(r"<[^>]+>", "", h.get("description") or "").strip()
        url = h.get("url") or ""
        if title and desc:
            results.append({"title": title, "snippet": desc, "url": url})
    return results


async def _search_firecrawl(query: str, key: str, base_url: str, count: int = 4) -> list[dict[str, str]]:
    """Query Firecrawl search."""
    base = base_url.rstrip("/")
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as http:
        r = await http.post(
            f"{base}/v1/search",
            json={"query": query, "limit": count, "lang": "id"},
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
    if r.status_code != 200:
        return []
    results = []
    for item in r.json().get("data", [])[:count]:
        title = item.get("title") or ""
        desc = item.get("description") or item.get("snippet") or ""
        url = item.get("url") or ""
        if title and desc:
            results.append({"title": title, "snippet": desc, "url": url})
    return results


async def _search_web_organic(query: str, count: int = 4) -> list[dict[str, str]]:
    """Direct organic web search with snippet extraction and redirect unwrapping."""
    url = f"https://www.bing.com/search?q={quote(query)}"
    try:
        async with httpx.AsyncClient(headers=WEB_HEADERS, timeout=TIMEOUT_S, follow_redirects=True) as http:
            r = await http.get(url)
            html = r.text
        blocks = re.findall(r'<li class="b_algo"[^>]*>(.*?)</li>', html, re.DOTALL)
        results = []
        for b in blocks:
            m_link = re.search(r'<h2>\s*<a\s+[^>]*href="([^"]+)"[^>]*>(.*?)</a>', b, re.DOTALL)
            if not m_link:
                continue
            href, raw_title = m_link.groups()
            title = re.sub(r"<[^>]+>", "", raw_title).strip()
            m_snip = re.search(r'<p[^>]*>(.*?)</p>', b, re.DOTALL)
            snip = re.sub(r"<[^>]+>", "", m_snip.group(1)).strip() if m_snip else ""
            direct_url = _decode_bing_url(href)
            if title and "chatgpt.com" not in direct_url:
                results.append({"title": title, "snippet": snip, "url": direct_url})
            if len(results) >= count:
                break
        return results
    except Exception as exc:
        log.warning("organic_search_failed", error=str(exc))
        return []


async def _search_wikipedia(query: str, count: int = 2) -> list[dict[str, str]]:
    """Wikipedia search (Indonesian first, then English)."""
    results = []
    try:
        async with httpx.AsyncClient(headers=WIKI_HEADERS, timeout=TIMEOUT_S) as http:
            for lang in ("id", "en"):
                url = (
                    f"https://{lang}.wikipedia.org/w/api.php?action=query&generator=search&"
                    f"gsrsearch={quote(query)}&gsrlimit={count}&prop=extracts&exintro=1&explaintext=1&"
                    f"exchars=400&format=json&utf8=1"
                )
                r = await http.get(url)
                if r.status_code != 200:
                    continue
                pages = r.json().get("query", {}).get("pages", {}).values()
                for p in sorted(pages, key=lambda x: x.get("index", 99)):
                    title = p.get("title", "")
                    extract = (p.get("extract") or "").strip()
                    if title and extract:
                        url_p = f"https://{lang}.wikipedia.org/wiki/{quote(title.replace(' ', '_'))}"
                        results.append({
                            "title": f"Wikipedia ({lang.upper()}): {title}",
                            "snippet": extract,
                            "url": url_p,
                        })
                if results:
                    break
    except Exception as exc:
        log.warning("wikipedia_search_failed", error=str(exc))
    return results[:count]


async def search_web(query: str, limit: int = 5) -> list[dict[str, str]]:
    """Multi-tiered web search that cascades gracefully."""
    q = _clean_query(query)
    if not q:
        return []

    s = get_settings()
    results: list[dict[str, str]] = []

    # 1. Official API if configured
    if s.brave_api_key:
        try:
            results = await _search_brave(q, s.brave_api_key, count=limit)
        except Exception:
            results = []

    if not results and s.firecrawl_api_key:
        try:
            results = await _search_firecrawl(q, s.firecrawl_api_key, s.firecrawl_url, count=limit)
        except Exception:
            results = []

    # 2. Organic web search
    if not results:
        results = await _search_web_organic(q, count=limit)

    # 3. Augment or fallback with Wikipedia
    if len(results) < limit:
        wiki_hits = await _search_wikipedia(q, count=limit - len(results))
        results.extend(wiki_hits)

    return results[:limit]


async def run_search(query: str, limit: int = 5) -> str:
    """Look-up tool string result for the conversational agent."""
    q = _clean_query(query)
    if not q:
        return "Tolong masukkan kata kunci pencarian yang lebih spesifik."

    hits = await search_web(q, limit=limit)
    if not hits:
        return f"Tidak ditemukan hasil pencarian web terbaru untuk '{q}'."

    lines = [f"HASIL PENCARIAN WEB untuk '{q}':"]
    for i, h in enumerate(hits, 1):
        lines.append(f"{i}. [{h['title']}]({h['url']})")
        if h.get("snippet"):
            lines.append(f"   {h['snippet']}")
    return "\n".join(lines)
