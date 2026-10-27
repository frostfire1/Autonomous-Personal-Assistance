"""Read-only lookups the assistant may call while it talks to the owner. Each one is plain SQL (or the existing news and CVE
readers): the model chooses WHICH question to ask of the data, code answers it, and the model only words the result. So a
figure in a reply always comes from the ledger, never from the model, and nothing here can change anything.

Results are short and bounded (row limits, character caps); merchant names and headlines inside them are untrusted text."""

import re
from datetime import datetime, time, timedelta
from decimal import Decimal
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from apa import assistant_memory
from apa.core.format import rupiah
from apa.core.logging import get_logger
from apa.db.models.budget import BudgetRule
from apa.db.models.health import SleepSession
from apa.db.models.ledger import CashWallet, Transaction
from apa.db.models.ops import InboundEvent, TelegramMessage

log = get_logger(__name__)

MAX_RESULT_CHARS = 4000


class _A(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TransactionsQ(_A):
    days: int = Field(default=7, ge=1, le=365)
    category: str | None = Field(default=None, max_length=48)
    merchant: str | None = Field(default=None, max_length=80)
    direction: Literal["out", "in"] | None = None
    min_amount: Decimal | None = Field(default=None, ge=0)
    max_amount: Decimal | None = Field(default=None, ge=0)
    limit: int = Field(default=15, ge=1, le=50)


class SpendingQ(_A):
    period: Literal["today", "week", "month"] = "month"
    by: Literal["category", "merchant", "day"] = "category"
    direction: Literal["out", "in"] = "out"


class DaysQ(_A):
    days: int = Field(default=7, ge=1, le=30)


class LimitQ(_A):
    limit: int = Field(default=5, ge=1, le=10)


class NewsQ(_A):
    topic: str = Field(default="nasional", max_length=24)
    limit: int = Field(default=5, ge=1, le=10)


class RecallQ(_A):
    query: str | None = Field(default=None, max_length=60)
    days: int = Field(default=30, ge=1, le=365)
    limit: int = Field(default=12, ge=1, le=20)


class ActivityQ(_A):
    hours: int = Field(default=24, ge=1, le=72)


class NoArgs(_A):
    pass

class SearchQ(_A):
    query: str = Field(min_length=1, max_length=120)
    limit: int = Field(default=5, ge=1, le=10)


class MapsQ(_A):
    origin: str | None = Field(default=None, max_length=100)
    destination: str | None = Field(default=None, max_length=100)
    query: str | None = Field(default=None, max_length=100)
    mode: Literal["transit", "motor", "driving", "walking"] = "transit"


READ_TOOLS: dict[str, type[_A]] = {
    "transactions": TransactionsQ,
    "spending": SpendingQ,
    "budgets": NoArgs,
    "balances": NoArgs,
    "sleep": DaysQ,
    "cve": LimitQ,
    "news": NewsQ,
    "recall": RecallQ,
    "profile": NoArgs,
    "activity": ActivityQ,
    "search": SearchQ,
    "maps": MapsQ,
}

# what the model is told about the read tools (the write tools are described by chat.actions.TOOL_SPEC)
READ_TOOL_DOC = """Look-up tools (read only; call one, you get the result, then answer or call another):
- transactions {"days": 1-365 (7), "category": string?, "merchant": string? (part of the name), "direction": "out"|"in"?, "min_amount": number?, "max_amount": number?, "limit": 1-25 (10)}
- spending {"period": "today"|"week"|"month" (month), "by": "category"|"merchant"|"day" (category), "direction": "out"|"in" (out)}
- budgets {}   this month's budgets with spent and limit
- balances {}  wallet balances
- sleep {"days": 1-30 (7)}
- cve {"limit": 1-10 (5)}  the newest high or critical vulnerabilities with the technology and versions
- news {"topic": "nasional"|"cyber"|a topic id (nasional), "limit": 1-10 (5)}
- recall {"query": string? (a word or phrase, or a time like "jam 9:20" or "tadi malam"), "days": 1-365 (30), "limit": 1-20 (12)}  search your diary, what you learned and the chat history of earlier days. Use this when the owner asks what you said or did at a specific time.
- activity {"hours": 1-72 (24)}  real-time ActivityWatch data: live active window/app on screen right now ("di jam sekarang", "aku lagi apa", "aku liat apa"), whether the owner is active/awake or away (AFK status), what YouTube video they are currently watching or last watched, and laptop/phone app usage. Always call this when asked what the owner is doing right now, what they are watching on YouTube, or if they are still awake.
- profile {}  the owner's patterns computed from their data: sleep, when they are active, what and when they eat, spending, steps, stress, interests
- search {"query": string, "limit": 1-10 (5)}  fast real-time search on the web and Wikipedia for facts, prices, definitions, or news. Use this when the owner asks to search, look something up, or asks about external facts.
- maps {"origin": string?, "destination": string?, "query": string?, "mode": "transit"|"motor"|"driving"|"walking" (transit)}  look up a place or get directions/routes, distance, travel duration, transit options (KRL, TransJakarta, Gojek/Grab), and clickable Google Maps links.
"""


def _clip(text: str) -> str:
    return text if len(text) <= MAX_RESULT_CHARS else text[: MAX_RESULT_CHARS - 20] + "\n…(shortened)"


def _clean(s: str | None, n: int = 40) -> str:
    return " ".join((s or "-").split())[:n]


async def _transactions(sm, tz, now, a: TransactionsQ) -> str:
    since = now - timedelta(days=a.days)
    q = select(Transaction).where(Transaction.status == "committed", Transaction.timestamp >= since)
    if a.category:
        q = q.where(func.lower(Transaction.primary_category) == a.category.lower())
    if a.merchant:
        q = q.where(Transaction.merchant.ilike(f"%{a.merchant}%"))
    if a.direction:
        q = q.where(Transaction.direction == a.direction)
    if a.min_amount is not None:
        q = q.where(Transaction.total_paid_amount >= a.min_amount)
    if a.max_amount is not None:
        q = q.where(Transaction.total_paid_amount <= a.max_amount)
    async with sm() as s:
        rows = (await s.execute(q.order_by(Transaction.timestamp.desc()).limit(a.limit))).scalars().all()
    if not rows:
        return "No transactions match."
    return "\n".join(
        f"#{t.id} {t.timestamp.astimezone(tz):%d/%m %H:%M} {_clean(t.merchant)} {rupiah(t.total_paid_amount)} "
        f"{t.direction} {_clean(t.primary_category, 24)}" + (f" «{_clean(t.notes, 40)}»" if t.notes else "")
        for t in rows
    )


async def _spending(sm, tz, now, a: SpendingQ) -> str:
    today = datetime.combine(now.date(), time.min, tzinfo=tz)
    start = {"today": today, "week": today - timedelta(days=6), "month": today.replace(day=1)}[a.period]
    key = {
        "category": Transaction.primary_category,
        "merchant": Transaction.merchant,
        "day": func.date(func.timezone(str(tz), Transaction.timestamp)),
    }[a.by]
    async with sm() as s:
        rows = (await s.execute(
            select(key, func.sum(Transaction.total_paid_amount), func.count())
            .where(Transaction.status == "committed", Transaction.direction == a.direction, Transaction.timestamp >= start)
            .group_by(key).order_by(func.sum(Transaction.total_paid_amount).desc()).limit(15)
        )).all()
    if not rows:
        return f"Nothing recorded for {a.period}."
    total = sum((r[1] for r in rows), Decimal(0))
    body = "\n".join(f"- {_clean(str(r[0]), 30)}: {rupiah(r[1])} ({r[2]}x)" for r in rows)
    return f"{a.direction} since {start:%d/%m}, total {rupiah(total)}\n{body}"


async def _budgets(sm, tz, now, a: NoArgs) -> str:
    async with sm() as s:
        rules = (await s.execute(
            select(BudgetRule).where(BudgetRule.period_month == now.date().replace(day=1)).order_by(BudgetRule.category)
        )).scalars().all()
    if not rules:
        return "No budgets are set for this month."
    return "\n".join(f"- {r.category}: {rupiah(r.spent_amount)} of {rupiah(r.current_limit)}" for r in rules)


async def _balances(sm, tz, now, a: NoArgs) -> str:
    async with sm() as s:
        wallets = (await s.execute(select(CashWallet).order_by(CashWallet.wallet_name))).scalars().all()
    return "\n".join(f"- {w.wallet_name}: {rupiah(w.current_balance)}" for w in wallets) or "No wallets yet."


async def _sleep(sm, tz, now, a: DaysQ) -> str:
    async with sm() as s:
        rows = (await s.execute(
            select(SleepSession).where(SleepSession.sleep_date >= now.date() - timedelta(days=a.days))
            .order_by(SleepSession.sleep_date.desc())
        )).scalars().all()
    if not rows:
        return "No sleep data in that period."
    return "\n".join(
        f"{r.sleep_date:%d/%m}: {divmod(r.total_sleep_mins or 0, 60)[0]} h {divmod(r.total_sleep_mins or 0, 60)[1]} min, "
        f"score {r.sleep_score or '-'}" for r in rows
    )


async def _cve(sm, tz, now, a: LimitQ) -> str:
    from apa.news import config as news_config
    from apa.news import cyber

    rule = (await news_config.current(sm)).cve
    rows = await cyber.severe_24h(sm, now, limit=a.limit, rule=rule)
    if not rows:
        return "No high or critical CVEs in the window."
    return "\n".join(
        f"{r['id']} CVSS {r['cvss'] if r['cvss'] is not None else '-'}{' EXPLOITED' if r['kev'] else ''}: "
        f"{_clean('; '.join(r['products'][:2]), 90)} — {_clean(r['summary'], 110)}" for r in rows
    )


async def _news(sm, tz, now, a: NewsQ) -> str:
    from apa.news import config as news_config
    from apa.news.ingest import recent_articles
    from apa.news.rank import rank

    cfg = await news_config.current(sm)
    if a.topic not in cfg.catalog():
        return f"Unknown topic {a.topic!r}. Topics: " + ", ".join(cfg.catalog())
    arts = await recent_articles(sm, now)
    ranked = [r for r in rank(arts, now, topic=a.topic, country=cfg.country) if r.score > 0][: a.limit]
    if not ranked:
        return "No recent stories for that topic."
    return "\n".join(f"- {_clean(r.cluster.headline, 120)} ({', '.join(r.cluster.sources[:3])})" for r in ranked)


async def _recall(sm, tz, now, a: RecallQ) -> str:
    since = now - timedelta(days=a.days)
    like = f"%{a.query}%" if a.query else None
    owner_q = select(InboundEvent).where(InboundEvent.source == "telegram_shorthand", InboundEvent.received_at >= since)
    bot_q = select(TelegramMessage).where(
        TelegramMessage.direction == "out", TelegramMessage.kind == "reply", TelegramMessage.created_at >= since)
    # Time-aware: if query looks like a time (HH:MM or 'jam N'), also search by timestamp window
    _time_re = re.compile(r'(?:jam\s*|pukul\s*)?(\d{1,2})[:.](\d{2})', re.I)
    _time_match = _time_re.search(a.query or '')
    _time_window_q_owner = None
    _time_window_q_bot = None
    if _time_match:
        _h, _m = int(_time_match.group(1)), int(_time_match.group(2))
        _window_lo = now.replace(hour=_h, minute=max(0, _m - 10), second=0, microsecond=0)
        _window_hi = now.replace(hour=_h, minute=min(59, _m + 10), second=59, microsecond=0)
        _time_window_q_owner = owner_q.where(InboundEvent.received_at.between(_window_lo, _window_hi))
        _time_window_q_bot = bot_q.where(TelegramMessage.created_at.between(_window_lo, _window_hi))
    if like:
        owner_q = owner_q.where(InboundEvent.payload["text"].astext.ilike(like))
        bot_q = bot_q.where(TelegramMessage.body.ilike(like))
    async with sm() as s:
        owner = (await s.execute(owner_q.order_by(InboundEvent.received_at.desc()).limit(a.limit))).scalars().all()
        bot = (await s.execute(bot_q.order_by(TelegramMessage.created_at.desc()).limit(a.limit))).scalars().all()
        if _time_match and _time_window_q_owner is not None:
            owner_t = (await s.execute(_time_window_q_owner.order_by(InboundEvent.received_at.desc()).limit(a.limit))).scalars().all()
            bot_t = (await s.execute(_time_window_q_bot.order_by(TelegramMessage.created_at.desc()).limit(a.limit))).scalars().all()
            seen_o = {x.id for x in owner}
            seen_b = {x.id for x in bot}
            owner = list(owner) + [x for x in owner_t if x.id not in seen_o]
            bot = list(bot) + [x for x in bot_t if x.id not in seen_b]
    chat = sorted(
        [(e.received_at, "OWNER", str(e.payload.get("text", ""))) for e in owner] + [(m.created_at, "APA", m.body or "") for m in bot],
        key=lambda x: x[0], reverse=True,
    )[: a.limit]
    diary = (await assistant_memory.search_diary(sm, a.query, a.days))[-5:]
    if a.query:  # full-text search over everything remembered (notes, learned items, diary), best match first
        known = [i for i in await assistant_memory.search_items(sm, a.query, 8) if i["kind"] not in ("diary", "note", "day", "fact")]
    else:
        known = (await assistant_memory.learned(sm))[-8:]
    day_hits = await assistant_memory.search_days(sm, a.query, max(a.days, 30), 4)
    parts = []
    if day_hits:
        parts.append("DAYS (what the owner was like each day):\n" + "\n".join(
            f"{d['date']}: {d['headline']} | {', '.join(d.get('personality', []))} | mood {d.get('mood')} | {', '.join(d.get('tags', []))} | {d.get('note', '')}"
            for d in day_hits))
    if known:
        parts.append("LEARNED:\n" + "\n".join(f"- [{i['kind']}] {i['text']} ({i.get('date', '?')})" for i in known))
    if diary:
        parts.append("DIARY:\n" + "\n".join(f"{e['date']}: {e['summary']}" for e in diary))
    if chat:
        parts.append("CHAT:\n" + "\n".join(f"{t.astimezone(tz):%d/%m %H:%M} {who}: {_clean(txt, 200)}" for t, who, txt in chat))
    return "\n".join(parts) or "Nothing found in the diary or the chat history."


async def _activity(sm, tz, now, a: ActivityQ) -> str:
    from apa import activitywatch, assistant_memory, usage

    parts = []

    # 1. Real-time ActivityWatch live status (current window, AFK status, live YouTube tab)
    live: dict = {}
    try:
        live = await activitywatch.sync_activitywatch_to_memory(sm, None, tz)
        if live.get("online"):
            hostname = live.get("hostname", "laptop")
            is_awake = live.get("is_awake", True)
            afk_since = live.get("afk_since", "")
            afk_desc = (
                f"AKTIF & MELEK (tidak AFK, input aktif di laptop sejak {afk_since} WIB)"
                if is_awake
                else f"AFK / Away sejak {afk_since} WIB"
            )
            act_win = live.get("active_window", {})
            cur_app = act_win.get("app", "")
            cur_title = act_win.get("title", "")
            clean_win = activitywatch.clean_youtube_title(cur_title) if cur_title else ""

            live_lines = [
                "🟢 STATUS AKTIF SAAT INI (REAL-TIME ACTIVITYWATCH):",
                f"- Perangkat: laptop {hostname}",
                f"- Status Pengguna: {afk_desc}",
            ]
            if cur_app:
                live_lines.append(f"- Aplikasi Aktif di Layar Sekarang: {cur_app} (pukul {act_win.get('time', '')} WIB)")
            if cur_title:
                live_lines.append(f"- Jendela / Tab Browser Terbuka: «{clean_win or cur_title[:100]}»")

            yt = live.get("latest_youtube")
            if yt:
                audible_tag = " (sedang memutar video/suara)" if yt.get("audible") else ""
                live_lines.append(
                    f"- YouTube Terakhir / Sedang Dibuka: «{yt['title']}» pada jam {yt['time']} WIB{audible_tag}"
                )

            parts.append("\n".join(live_lines))
    except Exception as exc:
        log.debug("live_aw_sync_failed: %s", exc)

    rows = []
    try:
        rows = await usage.recent(sm, 2, now.date())
    except Exception as exc:
        log.debug("usage_recent_failed: %s", exc)

    if not rows and not live.get("online"):
        return "No app usage has been reported yet (the phone app or the computer script must send it)."
    cutoff = now - timedelta(hours=a.hours)
    used = sorted((r for r in rows if r.last_at and r.last_at >= cutoff and usage.meaningful(r.app, r.seconds)), key=lambda r: r.last_at, reverse=True)

    # 1. Late-night active window check (22:00 - 05:00)
    late_rows = [
        r for r in rows
        if r.last_at and (r.last_at.astimezone(tz).hour >= 22 or r.last_at.astimezone(tz).hour < 5)
        and usage.meaningful(r.app, r.seconds)
    ]
    if late_rows:
        latest_late = max(late_rows, key=lambda r: r.last_at)
        late_top_apps = ", ".join(dict.fromkeys(r.app for r in sorted(late_rows, key=lambda r: r.seconds, reverse=True)[:3]))
        parts.append(
            f"🌙 AKTIVITAS TENGAH MALAM / BEGADANG (Pukul 22:00 - 05:00):\n"
            f"- Terakhir terdeteksi aktif pada pukul {latest_late.last_at.astimezone(tz):%H:%M} WIB di perangkat '{latest_late.device}'.\n"
            f"- Aplikasi utama yang dibuka larut malam: {late_top_apps}."
        )

    # 2. Apps used in the last N hours
    if used:
        parts.append(f"APPS USED IN THE LAST {a.hours} H, most recent first (local time):\n" + "\n".join(
            f"- {r.app} on {r.device}: used until {r.last_at.astimezone(tz):%d/%m %H:%M}"
            f"{', started ' + format(r.first_at.astimezone(tz), '%H:%M') if r.first_at else ''}, {round(r.seconds / 60)} min that day" for r in used[:10]))
    else:
        parts.append("No app has a recorded time in that window (older data has no timestamps, or nothing was used).")

    # 3. Category breakdown for today
    cat_durations: dict[str, int] = {}
    for r in rows:
        if r.day == now.date() and usage.meaningful(r.app, r.seconds):
            cat = usage.category(r.app)
            cat_durations[cat] = cat_durations.get(cat, 0) + r.seconds
    if cat_durations:
        summary_cats = ", ".join(f"{cat.title()}: {round(sec/60)}m" for cat, sec in sorted(cat_durations.items(), key=lambda x: x[1], reverse=True)[:4])
        parts.append(f"TOTAL SCREEN TIME HARI INI: {summary_cats}")

    # 4. Device report status
    seen: dict[str, datetime] = {}
    for r in rows:
        seen[r.device] = max(seen.get(r.device, r.updated_at), r.updated_at)
    parts.append("LAST REPORT FROM: " + ", ".join(f"{d} at {t.astimezone(tz):%d/%m %H:%M}" for d, t in seen.items())
                 + " (devices report about every 10-30 minutes)")

    # 5. Media & content
    kinds = ("watched", "listened", "liked", "saved", "uploaded", "searched")
    items = []
    try:
        items = sorted((w for w in await assistant_memory.watched(sm, 3, now.date()) if w["k"] in kinds), key=lambda w: (w["date"], w.get("at") or ""))[-8:]
    except Exception as exc:
        log.debug("watched_fetch_failed: %s", exc)
    if items:
        parts.append("LATEST CONTENT (YouTube, music; newest last):\n" + "\n".join(
            f"- [{w['k']}] {w.get('t', '')} - {w.get('c', '')} ({w['date']:%d/%m}{' ' + w['at'] if w.get('at') else ''})" for w in items))

    return "\n\n".join(parts)


async def _profile(sm, tz, now, a: NoArgs) -> str:
    from apa import profile

    rows = await profile.traits(sm)
    if not rows:
        return "No profile yet: it is computed each night from your data (and needs at least 5 days of each kind)."
    return "\n".join(f"- {r['summary']} ({r['evidence']} observations)" for r in rows)



async def _search(sm, tz, now, a: SearchQ) -> str:
    from apa import search

    return await search.run_search(a.query, limit=a.limit)


async def _maps(sm, tz, now, a: MapsQ) -> str:
    from apa import maps

    if a.origin and a.destination:
        return await maps.route_directions(a.origin, a.destination, mode=a.mode)
    if a.query:
        return await maps.search_place(a.query)
    if a.destination:
        return await maps.search_place(a.destination)
    if a.origin:
        return await maps.search_place(a.origin)
    return "Tolong sebutkan lokasi atau rute (titik asal dan tujuan) untuk dicari di Google Maps."


_RUN = {
    "recall": _recall,
    "activity": _activity,
    "profile": _profile,
    "transactions": _transactions, "spending": _spending, "budgets": _budgets, "balances": _balances,
    "sleep": _sleep, "cve": _cve, "news": _news,
    "search": _search,
    "maps": _maps,
}


async def run_read(sm: async_sessionmaker, tz: ZoneInfo, now: datetime, name: str, args: Any) -> str:
    """The result of one look-up as plain text. A bad argument comes back as a short message the model can act on."""
    cls = READ_TOOLS.get(name)
    if cls is None:
        return f"Unknown tool {name!r}."
    try:
        a = cls(**(args if isinstance(args, dict) else {}))
    except ValidationError as e:
        return "Bad arguments: " + "; ".join(f"{'.'.join(map(str, x['loc']))} {x['msg']}" for x in e.errors()[:3])[:200]
    except TypeError:
        return "Bad arguments."
    return _clip(await _RUN[name](sm, tz, now, a))
