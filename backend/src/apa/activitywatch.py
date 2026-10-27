"""ActivityWatch (AW) live integration: real-time active window, AFK/awake status, and YouTube history.

Connects to the local or remote ActivityWatch server (default: http://localhost:5600/api/0)
to fetch real-time events, performs smart deduplication, syncs new media into assistant_memory,
and provides live context so the assistant knows what the user is watching and doing right now.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from apa import assistant_memory
from apa.core.settings import get_settings

log = logging.getLogger(__name__)

# Cache for deduplication in memory (sha256 hash -> timestamp)
_SEEN_EVENT_HASHES: set[str] = set()
MAX_SEEN_HASHES = 5000

PRETTY_APP_NAMES = {
    "msedge.exe": "Microsoft Edge",
    "chrome.exe": "Google Chrome",
    "firefox.exe": "Mozilla Firefox",
    "brave.exe": "Brave Browser",
    "code.exe": "VS Code",
    "antigravity.exe": "Antigravity",
    "windowsterminal.exe": "Windows Terminal",
    "powershell.exe": "PowerShell",
    "cmd.exe": "Command Prompt",
    "explorer.exe": "File Explorer",
    "telegram.exe": "Telegram",
    "discord.exe": "Discord",
    "slack.exe": "Slack",
    "teams.exe": "Microsoft Teams",
    "spotify.exe": "Spotify",
}


def pretty_app(raw_app: str) -> str:
    """Format executable name into a clean, human-readable app name."""
    if not raw_app:
        return ""
    clean = raw_app.strip().lower()
    if clean in PRETTY_APP_NAMES:
        return PRETTY_APP_NAMES[clean]
    base = clean.removesuffix(".exe")
    return base.capitalize() if len(base) > 1 else raw_app


def clean_youtube_title(raw_title: str) -> str:
    """Clean browser tab / window title into clean video title.

    Strips unread notification badges like '(39) ', browser page suffixes,
    browser app suffixes ('- Personal - Microsoft Edge', '- Google Chrome'),
    and trailing ' - YouTube' or ' - YouTube Music'.
    """
    if not raw_title:
        return ""
    # Strip (39) notification count at the beginning
    t = re.sub(r"^\(\d+\)\s*", "", raw_title).strip()
    # Strip browser page suffix like ' and 9 more pages - Personal - Microsoft Edge'
    t = re.sub(r"\s+and\s+\d+\s+more\s+pages.*$", "", t, flags=re.IGNORECASE).strip()
    # Strip browser window suffixes
    t = re.sub(
        r"\s*-\s*(?:Personal|Work|Profile\s*\d+)?\s*-\s*Microsoft[\u200b\s]*Edge.*$",
        "",
        t,
        flags=re.IGNORECASE,
    ).strip()
    t = re.sub(r"\s*-\s*Microsoft[\u200b\s]*Edge.*$", "", t, flags=re.IGNORECASE).strip()
    t = re.sub(r"\s*-\s*Google\s+Chrome.*$", "", t, flags=re.IGNORECASE).strip()
    t = re.sub(r"\s*-\s*(?:Mozilla\s+)?Firefox.*$", "", t, flags=re.IGNORECASE).strip()
    t = re.sub(r"\s*-\s*Brave.*$", "", t, flags=re.IGNORECASE).strip()
    # Strip '- YouTube' or '- YouTube Music'
    t = re.sub(r"\s*-\s*YouTube(?:\s*Music)?.*$", "", t, flags=re.IGNORECASE).strip()
    return " ".join(t.split())


def _hash_event(bucket: str, ts: str, title: str, url: str) -> str:
    msg = f"{bucket}:{ts}:{title}:{url}".encode()
    return hashlib.sha256(msg).hexdigest()


async def fetch_aw_data(
    aw_url: str = "http://localhost:5600",
    timeout_s: float = 3.0,
) -> dict[str, Any] | None:
    """Query ActivityWatch REST API directly for available buckets and recent events."""
    base = aw_url.rstrip("/")
    api_url = f"{base}/api/0" if not base.endswith("/api/0") else base

    try:
        async with httpx.AsyncClient(timeout=timeout_s, follow_redirects=True) as client:
            info_r = await client.get(f"{api_url}/info")
            if info_r.status_code != 200:
                return None
            info = info_r.json()
            hostname = info.get("hostname", "desktop")

            buckets_r = await client.get(f"{api_url}/buckets/")
            if buckets_r.status_code != 200:
                return None
            buckets = buckets_r.json()

            # 1. AFK bucket
            afk_bucket = next((k for k in buckets if "afk" in k), None)
            afk_events = []
            if afk_bucket:
                afk_r = await client.get(f"{api_url}/buckets/{afk_bucket}/events?limit=5")
                if afk_r.status_code == 200:
                    afk_events = afk_r.json()

            # 2. Window bucket
            win_bucket = next((k for k in buckets if "window" in k), None)
            win_events = []
            if win_bucket:
                win_r = await client.get(f"{api_url}/buckets/{win_bucket}/events?limit=15")
                if win_r.status_code == 200:
                    win_events = win_r.json()

            # 3. Web buckets (Chrome/Edge/Firefox)
            web_events = []
            for b in [k for k in buckets if "web" in k]:
                web_r = await client.get(f"{api_url}/buckets/{b}/events?limit=25")
                if web_r.status_code == 200:
                    for ev in web_r.json():
                        ev["_bucket"] = b
                        web_events.append(ev)

            # Sort all events chronologically (newest first)
            web_events.sort(key=lambda ev: ev.get("timestamp", ""), reverse=True)
            win_events.sort(key=lambda ev: ev.get("timestamp", ""), reverse=True)

            return {
                "hostname": hostname,
                "afk_events": afk_events,
                "win_events": win_events,
                "web_events": web_events,
            }
    except Exception as exc:
        log.debug("aw_fetch_failed: %s", exc)
        return None


async def process_aw_payload(
    sm: Any,
    redis: Any | None,
    tz: ZoneInfo,
    raw: dict[str, Any],
) -> dict[str, Any]:
    """Process raw AW events, perform deduplication, ingest YouTube into assistant_memory,

    and cache live activity in Redis.
    """
    hostname = raw.get("hostname", "desktop")

    # 1. Evaluate AFK status
    afk_events = raw.get("afk_events", [])
    afk_status = "unknown"
    afk_since_str = ""
    is_awake = True
    if afk_events:
        latest_afk = afk_events[0]
        afk_status = latest_afk.get("data", {}).get("status", "unknown")
        is_awake = afk_status == "not-afk"
        try:
            afk_dt = datetime.fromisoformat(latest_afk["timestamp"]).astimezone(tz)
            afk_since_str = f"{afk_dt:%H:%M}"
        except Exception:
            pass

    # 2. Evaluate current active window & recent apps
    win_events = raw.get("win_events", [])
    active_window = {}
    recent_apps = []
    seen_apps = set()
    for ev in win_events:
        d = ev.get("data", {})
        raw_app = d.get("app", "")
        raw_title = d.get("title", "")
        app_name = pretty_app(raw_app)
        if not app_name:
            continue
        try:
            ev_dt = datetime.fromisoformat(ev["timestamp"]).astimezone(tz)
            time_str = f"{ev_dt:%H:%M}"
        except Exception:
            time_str = ""

        if not active_window:
            active_window = {
                "app": app_name,
                "title": raw_title,
                "time": time_str,
                "raw_app": raw_app,
            }
        if app_name not in seen_apps:
            seen_apps.add(app_name)
            recent_apps.append({"app": app_name, "title": raw_title[:80], "time": time_str})

    # 3. Process web & YouTube events with deduplication
    web_events = raw.get("web_events", [])
    youtube_items_to_store = []
    latest_youtube: dict[str, Any] | None = None
    recent_youtube: list[dict[str, Any]] = []
    seen_yt_titles = set()

    for ev in web_events:
        d = ev.get("data", {})
        url_str = str(d.get("url", ""))
        title_str = str(d.get("title", ""))
        bucket = ev.get("_bucket", "web")
        ts = ev.get("timestamp", "")
        audible = bool(d.get("audible", False))

        is_youtube = "youtube.com" in url_str.lower() or "youtube" in title_str.lower()
        if not is_youtube:
            continue

        clean_t = clean_youtube_title(title_str)
        if not clean_t or len(clean_t) < 3 or clean_t in seen_yt_titles:
            continue

        seen_yt_titles.add(clean_t)
        try:
            ev_dt = datetime.fromisoformat(ts).astimezone(tz)
            time_str = f"{ev_dt:%H:%M}"
        except Exception:
            ev_dt = datetime.now(tz)
            time_str = f"{ev_dt:%H:%M}"

        yt_entry = {
            "title": clean_t,
            "url": url_str,
            "audible": audible,
            "time": time_str,
            "dt": ev_dt,
        }
        if latest_youtube is None:
            latest_youtube = yt_entry
        recent_youtube.append(yt_entry)

        # Deduplication check
        ev_hash = _hash_event(bucket, ts, clean_t, url_str)
        if ev_hash not in _SEEN_EVENT_HASHES:
            _SEEN_EVENT_HASHES.add(ev_hash)
            if len(_SEEN_EVENT_HASHES) > MAX_SEEN_HASHES:
                _SEEN_EVENT_HASHES.clear()

            youtube_items_to_store.append({
                "app": "YouTube",
                "title": clean_t,
                "artist": "",
                "at": ev_dt,
            })

    # 4. Ingest new YouTube items into assistant_memory
    new_media_count = 0
    if youtube_items_to_store and sm is not None:
        try:
            new_media_count = await assistant_memory.add_playing(sm, youtube_items_to_store, tz)
        except Exception as exc:
            log.warning("aw_add_playing_failed: %s", exc)

    # 5. Build live status snapshot
    live_status = {
        "online": True,
        "hostname": hostname,
        "afk_status": afk_status,
        "is_awake": is_awake,
        "afk_since": afk_since_str,
        "active_window": active_window,
        "recent_apps": recent_apps[:5],
        "latest_youtube": latest_youtube,
        "recent_youtube": recent_youtube[:5],
        "new_media_synced": new_media_count,
        "updated_at": f"{datetime.now(tz):%H:%M:%S}",
    }

    # Cache in Redis
    if redis is not None:
        try:
            clean_cache = {
                **live_status,
                "latest_youtube": {
                    k: v for k, v in (latest_youtube or {}).items() if k != "dt"
                } if latest_youtube else None,
                "recent_youtube": [
                    {k: v for k, v in item.items() if k != "dt"}
                    for item in recent_youtube[:5]
                ],
            }
            await redis.set("apa:aw:live_state", json.dumps(clean_cache), ex=1800)
        except Exception:
            pass

    return live_status


async def sync_activitywatch_to_memory(
    sm: Any,
    redis: Any | None,
    tz: ZoneInfo,
    aw_url: str | None = None,
) -> dict[str, Any]:
    """Fetch newest AW events, perform deduplication, ingest YouTube into assistant_memory,

    and cache live activity in Redis.
    """
    settings = get_settings()
    url = aw_url or getattr(settings, "activitywatch_url", "http://localhost:5600")

    raw = await fetch_aw_data(url)
    if not raw:
        if redis is not None:
            try:
                cached = await redis.get("apa:aw:live_state")
                if cached:
                    return json.loads(cached)
            except Exception:
                pass
        return {"online": False}

    return await process_aw_payload(sm, redis, tz, raw)
