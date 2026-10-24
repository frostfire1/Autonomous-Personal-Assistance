"""What the owner watches and searches on YouTube, from their own Google Takeout export.

YouTube's API does not give out watch history (Google closed that in 2016), so the owner exports it themselves: Google Takeout >
YouTube and YouTube Music > History, format JSON, and uploads `watch-history.json` on the Diary page (or posts it to
`POST /import/youtube`). We keep, per day, the channel and title of what was watched and what was searched (encrypted like
the rest of the memory), for the last 90 days; the profile then names the channels and topics the owner returns to."""

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

MAX_ITEMS = 5000
WINDOW_DAYS = 90
_PREFIXES = ("watched ", "telah menonton ", "menonton ", "searched for ", "telah mencari ", "mencari ")
_SEARCH = ("searched for ", "telah mencari ", "mencari ")


def parse(items: list[Any], tz: ZoneInfo, now: datetime, days: int = WINDOW_DAYS) -> dict[date, list[dict]]:
    """{day: [{"k": "watched"|"searched", "c": channel, "t": title}]} for the last `days` days; anything else in the export
    (ads, community posts, items without a title) is skipped."""
    since = now - timedelta(days=days)
    out: dict[date, list[dict]] = {}
    n = 0
    for it in items:
        if not isinstance(it, dict) or not isinstance(it.get("title"), str) or not isinstance(it.get("time"), str):
            continue
        low = it["title"].lower()
        prefix = next((p for p in _PREFIXES if low.startswith(p)), None)
        if prefix is None:
            continue
        try:
            at = datetime.fromisoformat(it["time"].replace("Z", "+00:00")).astimezone(tz)
        except ValueError:
            continue
        if at < since or at > now + timedelta(days=1):
            continue
        title = " ".join(it["title"][len(prefix):].split())[:140]
        subs = it.get("subtitles")
        channel = " ".join(str(subs[0].get("name", "")).split())[:60] if isinstance(subs, list) and subs and isinstance(subs[0], dict) else ""
        if len(title) < 2 or (title.startswith("http") and not channel):
            continue  # a removed video: nothing to learn from it
        music = str(it.get("header", "")).lower() == "youtube music" or "youtube music" in [str(p).lower() for p in it.get("products") or []]
        kind = "searched" if prefix in _SEARCH else "listened" if music else "watched"
        out.setdefault(at.date(), []).append({"k": kind, "c": channel.removesuffix(" - Topic"), "t": title})
        n += 1
        if n >= MAX_ITEMS:
            break
    return out

