"""What the /private companion can DO besides talk. The model only names a skill; this code runs it.

  breathing, grounding, journal   fixed guided exercises: no network, nothing to leak.
  search                          a web lookup, but only when the owner explicitly asked for one. The query is cleaned of
                                  personal details, shown to the owner, sent to Wikipedia (keyword only, no account, no
                                  cookies) and never logged. Results live in memory for that one answer.

Nothing here writes to the database, Redis or a log."""

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from urllib.parse import quote

import httpx

from apa.core.sanitize import sanitize_untrusted
from apa.news.fetch import FetchError, fetch_bytes

BREATHING = (
    "🌬️ Napas kotak, 4 putaran, pelan saja:\n"
    "1. Tarik napas lewat hidung, hitung 4\n"
    "2. Tahan, hitung 4\n"
    "3. Embuskan lewat mulut, hitung 4\n"
    "4. Tahan kosong, hitung 4\n"
    "Kalau pusing, berhenti dan napas biasa. Kabari aku ya setelah putaran terakhir, rasanya gimana."
)

GROUNDING = (
    "🌿 Latihan 5-4-3-2-1 untuk menarik pikiran kembali ke sini:\n"
    "• 5 hal yang bisa kamu lihat\n"
    "• 4 hal yang bisa kamu sentuh\n"
    "• 3 suara yang kamu dengar\n"
    "• 2 bau yang kamu cium\n"
    "• 1 hal yang kamu rasakan di mulut\n"
    "Tulis satu-satu di sini kalau mau, aku temani."
)

JOURNAL = (
    "📝 Kalau mau menulis, pilih satu saja:\n"
    "• Apa yang paling berat hari ini, dan seberapa besar bagian itu yang memang bisa kamu ubah?\n"
    "• Satu hal kecil yang sudah kamu lakukan dengan cukup baik walau capek.\n"
    "• Kalau sahabatmu ada di posisimu, apa yang akan kamu bilang ke dia?\n"
    "Tidak harus rapi. Tulis saja, nanti ceritakan kalau ingin."
)

EXERCISES = {"breathing": BREATHING, "grounding": GROUNDING, "journal": JOURNAL}

# The owner must ask: "cari", "carikan", "cariin", "search", "googling", "cek di internet" ...
_ASKED_TO_SEARCH = re.compile(
    r"\b(cari|carikan|carikanlah|cariin|mencari|search|googling|google|browsing|cek\s+di\s+internet|lihat\s+di\s+internet|"
    r"tolong\s+cari|bantu\s+cari)\b",
    re.I,
)

MAX_QUERY = 80
# ONE request per language: a search whose top articles come back with their introductions (generator=search + extracts),
# so a search costs one call, not two, and Wikimedia's per-address limits are met less often.
WIKI = (
    "https://{lang}.wikipedia.org/w/api.php?action=query&generator=search&gsrsearch={q}&gsrlimit=3&prop=extracts"
    "&exintro=1&explaintext=1&exchars=500&exlimit=3&format=json&utf8=1&redirects=1"
)

Search = Callable[[str], Awaitable[list[tuple[str, str]]]]

# Wikimedia answers 403 to the HTTP library's default Accept-Encoding/Accept headers (measured from the server: urllib and
# curl pass, httpx does not until these are set), and asks for a descriptive User-Agent.
WIKI_HEADERS = {
    "User-Agent": "AutonomousPersonalAssistant/1.0 (self-hosted personal project)",
    "Accept": "application/json",
    "Accept-Encoding": "identity",
}


def asked_to_search(text: str) -> bool:
    return bool(_ASKED_TO_SEARCH.search(text))


def clean_query(raw: object) -> str:
    """A short, generic keyword string, or '' when nothing safe is left. A query that contains anything personal (a
    phone number, a card or account number, an e-mail) or an injection attempt is dropped whole, not edited."""
    if not isinstance(raw, str):
        return ""
    s = sanitize_untrusted(raw, max_len=200)
    if s.pii_redactions or s.injection_flags:
        return ""
    q = re.sub(r"[^\w\s\-.,?']", " ", s.text)
    q = " ".join(q.split())[:MAX_QUERY].strip()
    return "" if re.search(r"\d{5,}", q) else q


async def brave(query: str, key: str) -> list[tuple[str, str]]:
    """Brave Search API (official, keyed): up to four (title, description). Raises on any failure."""
    async with httpx.AsyncClient(timeout=12) as http:
        r = await http.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": 4, "country": "ID", "search_lang": "id", "safesearch": "moderate"},
            headers={"X-Subscription-Token": key, "Accept": "application/json"},
        )
    if r.status_code != 200:
        raise FetchError(f"brave HTTP {r.status_code}")
    got = []
    for h in r.json().get("web", {}).get("results", [])[:4]:
        text = re.sub(r"<[^>]+>", "", h.get("description") or "").strip()
        if h.get("title") and text:
            got.append((re.sub(r"<[^>]+>", "", h["title"]).strip(), text))
    return got


async def firecrawl(query: str, key: str, base: str) -> list[tuple[str, str]]:
    """Firecrawl search (POST {base}/v2/search with a bearer key): up to four (title, description). Raises on any
    failure. The answer is read in both shapes the API has used: data as a list, or data.web as a list."""
    async with httpx.AsyncClient(timeout=20) as http:
        r = await http.post(
            f"{base.rstrip('/')}/v2/search",
            json={"query": query, "limit": 4},
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
    if r.status_code != 200:
        raise FetchError(f"firecrawl HTTP {r.status_code}")
    data = r.json().get("data") or []
    items = data.get("web", []) if isinstance(data, dict) else data
    got = []
    for h in items[:4]:
        if not isinstance(h, dict):
            continue
        text = re.sub(r"<[^>]+>", "", h.get("description") or h.get("snippet") or (h.get("markdown") or "")[:400]).strip()
        title = re.sub(r"<[^>]+>", "", h.get("title") or "").strip()
        if title and text:
            got.append((title, text))
    return got


async def web_search(query: str) -> list[tuple[str, str]]:
    """ONE lookup of ONE query: Firecrawl if FIRECRAWL_API_KEY is set, else Brave if BRAVE_API_KEY is set, else Wikipedia;
    a service that fails or finds nothing falls through to the next.
    [] when nothing was found. It never retries with other wording: choosing the wording is the research agent's job."""
    from apa.core.settings import get_settings

    s = get_settings()
    if s.firecrawl_api_key:
        try:
            if found := await firecrawl(query, s.firecrawl_api_key, s.firecrawl_url):
                return found
        except (FetchError, httpx.HTTPError, ValueError):
            pass
    if s.brave_api_key:
        try:
            if found := await brave(query, s.brave_api_key):
                return found
        except (FetchError, httpx.HTTPError, ValueError):
            pass
    return await wikipedia(query)


PLAN_SYSTEM = (
    "You are a research agent. You get ONE topic and nothing else. Plan the searches that would answer it: at most "
    "{n} queries that cover different angles (for example the basics, practical steps, what to avoid), in the topic's "
    "language, with no names, numbers, places or personal details. {style} Reply with JSON only: "
    '{{"queries": [string, ...]}}.'
)
# The planner is told which engine it writes for, because each searches differently and a query is never retried.
STYLE_WEB = "The engine is a web search: short natural keyword queries of 3 to 6 words."
STYLE_ENCYCLOPEDIA = (
    "The engine is an encyclopedia (Wikipedia): each query must be the title of an article that probably exists, "
    "1 to 3 words, for example 'Insomnia', 'Higiene tidur', 'Kafein', never a question or a sentence."
)
MAX_QUERIES = 3
MAX_RESULTS = 6


async def research(llm: object, topic: str, search: Search) -> tuple[list[str], list[tuple[str, str]]]:
    """A short research session: the agent plans queries from the topic alone (it never sees the conversation), each
    query is searched exactly once and in parallel, and the results are merged. -> (queries used, results).
    Raises the search error only if every search failed; an empty answer is [] and is not retried."""
    from apa.agents.personas import RESEARCH
    from apa.core.settings import get_settings

    cfg = get_settings()
    style = STYLE_WEB if (cfg.firecrawl_api_key or cfg.brave_api_key) else STYLE_ENCYCLOPEDIA
    queries: list[str] = []
    try:
        res = await llm.complete(  # type: ignore[attr-defined]
            RESEARCH, PLAN_SYSTEM.format(n=MAX_QUERIES, style=style), f"TOPIC: {topic}", json_mode=True, max_tokens=200, temperature=0.3
        )
        planned = res.json().get("queries", [])
        for raw in planned[: MAX_QUERIES * 2] if isinstance(planned, list) else []:
            q = clean_query(raw)
            if q and q.lower() not in {x.lower() for x in queries}:
                queries.append(q)
    except Exception:  # an outage or a garbled plan: fall back to the topic itself
        queries = []
    queries = queries[:MAX_QUERIES] or [topic]
    answers = await asyncio.gather(*(search(q) for q in queries), return_exceptions=True)
    results: list[tuple[str, str]] = []
    seen: set[str] = set()
    for ans in answers:
        if isinstance(ans, BaseException):
            continue
        for title, text in ans:
            if title.lower() not in seen:
                seen.add(title.lower())
                results.append((title, text))
    if not results and all(isinstance(a, BaseException) for a in answers):
        raise next(a for a in answers if isinstance(a, BaseException))
    return queries, results[:MAX_RESULTS]


async def wikipedia(query: str) -> list[tuple[str, str]]:
    """Up to three (title, introduction) from Indonesian Wikipedia, else English, best match first. [] means Wikipedia
    answered and had nothing; FetchError means it could not be asked at all (blocked, rate limited, down)."""
    answered = False
    async with httpx.AsyncClient(timeout=12) as http:
        for lang in ("id", "en"):
            try:
                data = json.loads(await fetch_bytes(http, WIKI.format(lang=lang, q=quote(query)), max_bytes=300_000, headers=WIKI_HEADERS))
                answered = True
                pages = sorted(data.get("query", {}).get("pages", {}).values(), key=lambda p: p.get("index", 99))
                got = [(p["title"], (p.get("extract") or "").strip()) for p in pages if p.get("title")]
                got = [(title, text) for title, text in got if text]
                if got:
                    return got[:3]
            except (FetchError, ValueError, KeyError):
                continue
    if not answered:
        raise FetchError("wikipedia unavailable")
    return []
