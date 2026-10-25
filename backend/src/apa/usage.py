"""App usage from the owner's devices (ActivityWatch on Android and desktop, or anything that pushes the same shape).

A device sends, once a day or more, the day's active time per app. We keep the app name and the seconds, nothing else: no
window titles, no URLs. `store` replaces the day of a device (a re-push is harmless); `category` groups apps for the profile."""

from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from apa.db.models.intel import AppUsage

KINDS = ("android", "desktop", "ios", "web", "other")
CATEGORIES = (
    ("finance", ("bank", "wondr", "gopay", "ovo", "dana", "jago", "bca", "mandiri", "bni", "bri", "livin", "blu", "flip", "shopeepay", "linkaja", "paypal", "wallet", "invest", "bibit", "ajaib", "stockbit")),
    ("messaging", ("whatsapp", "telegram", "signal", "line", "messenger", "discord", "slack", "teams", "mail", "gmail", "outlook")),
    ("social", ("instagram", "tiktok", "twitter", "facebook", "reddit", "threads", "linkedin", "snapchat", "x.com", "weibo")),
    ("video", ("youtube", "netflix", "vidio", "disney", "twitch", "prime video", "bilibili", "vlc", "mpv", "melolo", "dramabox", "reelshort", "shortmax", "viu", "wetv", "iqiyi", "tv")),
    ("music", ("spotify", "music", "joox", "soundcloud", "podcast", "audible")),
    ("games", ("game", "steam", "roblox", "minecraft", "genshin", "mobile legends", "pubg", "free fire", "epic", "riot", "valorant", "legends", "chess", "arena", "clash", "royale", "pokemon", "honkai", "wuthering", "sausage", "candy")),
    ("shopping", ("tokopedia", "shopee", "lazada", "bukalapak", "amazon", "gojek", "grab", "traveloka", "ebay")),
    ("work", ("code", "studio", "terminal", "powershell", "cmd", "git", "docs", "word", "excel", "powerpoint", "sheets", "notion",
              "obsidian", "figma", "jetbrains", "intellij", "pycharm", "android studio", "putty", "ssh", "zoom", "meet")),
    ("browser", ("chrome", "firefox", "edge", "safari", "brave", "opera", "browser", "vivaldi")),
)
LATE_FROM, LATE_TO = 23, 4  # "up late": the last activity of a day falls from 23:00 until 04:00
MAX_APPS = 300


def category(app: str) -> str:
    a = app.lower()
    for name, words in CATEGORIES:
        if any(w in a for w in words):
            return name
    return "other"


_PRETTY = {"msedge": "Microsoft Edge", "chrome": "Google Chrome", "code": "VS Code", "windowsterminal": "Windows Terminal", "explorer": "File Explorer",
           "powershell": "PowerShell", "cmd": "Command Prompt", "obs64": "OBS Studio", "applicationframehost": "Windows app", "searchhost": "Windows Search"}


NOISE = frozenset({"windows search", "windows app", "aw-qt", "activitywatch", "peluncur sistem", "keamanan", "searchhost", "system ui", "snippingtool", "taskmgr"})


def meaningful(app: str, seconds: int) -> bool:
    """An app worth naming as "what I used": not a system helper, and used for at least half a minute that day."""
    return app.lower() not in NOISE and seconds >= 30


def pretty(app: str) -> str:
    """A Windows process name read as an app name: 'msedge.exe' -> 'Microsoft Edge', 'discord.exe' -> 'Discord'."""
    name = " ".join(app.split())
    if name.lower().endswith(".exe"):
        base = name[:-4]
        return _PRETTY.get(base.lower(), base[:1].upper() + base[1:])
    return name


async def store(sm: async_sessionmaker, device: str, kind: str, day: date, apps: list[dict[str, Any]]) -> int:
    """Replace what one device reported for one day. -> number of apps kept. Apps are merged by name."""
    merged: dict[str, dict] = {}
    for a in apps[:MAX_APPS]:
        name = pretty(str(a["app"]))[:120]
        if not name or int(a["seconds"]) <= 0:
            continue
        m = merged.setdefault(name, {"seconds": 0, "first_at": None, "last_at": None})
        m["seconds"] = min(m["seconds"] + int(a["seconds"]), 86400)
        for k, pick in (("first_at", min), ("last_at", max)):
            if a.get(k):
                m[k] = a[k] if m[k] is None else pick(m[k], a[k])
    async with sm() as s:
        await s.execute(delete(AppUsage).where(AppUsage.day == day, AppUsage.device == device))
        for name, m in merged.items():
            await s.execute(insert(AppUsage).values(day=day, device=device, kind=kind if kind in KINDS else "other", app=name,
                                                    seconds=m["seconds"], first_at=m["first_at"], last_at=m["last_at"],
                                                    updated_at=func.now()))
        await s.commit()
    return len(merged)


async def recent(sm: async_sessionmaker, days: int, today: date | None = None) -> list[AppUsage]:
    since = (today or date.today()) - timedelta(days=days)
    async with sm() as s:
        return list((await s.execute(select(AppUsage).where(AppUsage.day >= since).order_by(AppUsage.day))).scalars().all())


def hours(seconds: float) -> str:
    return f"{seconds / 3600:.1f} h"


def summarise(rows: list[tuple[date, str, str, str, int, datetime | None]], tz: Any) -> dict[str, Any]:
    """Pure: [(day, device, kind, app, seconds, last_at)] -> per-day totals, top apps and categories, up-late days."""
    per_day: dict[date, int] = defaultdict(int)
    per_kind: dict[str, int] = defaultdict(int)
    per_app: dict[str, int] = defaultdict(int)
    per_cat: dict[str, int] = defaultdict(int)
    last: dict[tuple[date, str], datetime] = {}
    for day, device, kind, app, seconds, last_at in rows:
        per_day[day] += seconds
        per_kind[kind] += seconds
        per_app[app] += seconds
        per_cat[category(app)] += seconds
        if last_at is not None and ((day, device) not in last or last_at > last[(day, device)]):
            last[(day, device)] = last_at
    late_days = {d for (d, _), t in last.items() if (h := t.astimezone(tz).hour) >= LATE_FROM or h < LATE_TO}
    return {"days": len(per_day), "per_day": per_day, "per_kind": per_kind, "per_app": per_app, "per_cat": per_cat, "late_days": late_days}
