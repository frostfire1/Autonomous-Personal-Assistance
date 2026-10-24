"""The morning briefing as text for Telegram, in Indonesian or English: everything the voice says plus the detail a voice
cannot carry (links, CVE numbers, versions, EPSS, category and merchant breakdowns, the day's budget per category).
Facts come from SQL and the stored articles; nothing here is written by a model, so every figure can be checked."""

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from apa import prefs as prefs_mod
from apa.core.format import rupiah
from apa.db.models.budget import BudgetRule
from apa.db.models.health import SleepSession
from apa.db.models.ledger import Transaction
from apa.news import config as news_config
from apa.news import cyber
from apa.news.briefing import weather_line
from apa.news.daily import daily_budget
from apa.news.i18n import DAYS, MONTHS, norm, table
from apa.news.ingest import recent_articles
from apa.news.rank import Ranked, rank
from apa.news.translate import translate_all
from apa.prices.check import price_line

MAX_MESSAGE = 3800  # Telegram's limit is 4096
NEWS_ITEMS = 8
CYBER_NEWS_ITEMS = 5
SEVERE_ITEMS = 12

TG = {
    "id": {
        "title": "🌅 RINGKASAN PAGI · {day}, {d} {month} {year}",
        "finance": "💰 KEUANGAN",
        "yesterday": "Kemarin: {amount} dalam {n} transaksi.",
        "yesterday_none": "Kemarin: tidak ada pengeluaran tercatat.",
        "top_cats": "Kategori terbesar: ",
        "top_shops": "Paling banyak di: ",
        "uncategorised": "Tanpa kategori",
        "unnamed": "tanpa nama",
        "month_so_far": "Bulan ini sampai sekarang: {amount}.",
        "tight": "Anggaran yang mulai tipis:",
        "tight_line": "• {cat}: {pct}% terpakai, sisa {left}",
        "today_budget": "Anggaran hari ini: {per_day} (sisa {remaining} untuk {days} hari), sudah terpakai {spent}.",
        "today_budget_cats": "Per kategori, per hari:",
        "weather": "☁️ CUACA",
        "health": "😴 KESEHATAN",
        "slept": "Tidur {h} jam {m} menit",
        "score": ", skor {score}",
        "stages": "fase: ",
        "deep": "dalam", "light": "ringan", "rem": "REM",
        "awake": "terjaga {n} menit",
        "fatigue": "\n⚠️ Tidurmu kurang; pertahanan kelelahan aktif (pengeluaran impulsif diawasi lebih ketat).",
        "price": "📈 HARGA",
        "news": "📰 BERITA NASIONAL",
        "no_news": "Tidak ada berita nasional besar saat ini.",
        "security": "🔐 KEAMANAN",
        "cve_counts": "24 jam terakhir: {total} CVE baru dicatat, {crit} kritis, {high} tinggi, {kev} sudah dieksploitasi aktif (CISA KEV), {unscored} belum diberi skor.",
        "no_cve": "Tidak ada CVE tinggi/kritis baru.",
        "cvss": "CVSS {score}",
        "unscored": "belum diberi skor",
        "exploited": " · 🔥 DIEKSPLOITASI",
        "tech": "Teknologi/versi: ",
        "unknown_tech": "teknologi belum teridentifikasi",
        "cyber_news": "🛡️ BERITA KEAMANAN",
    },
    "en": {
        "title": "🌅 MORNING SUMMARY · {day}, {month} {d}, {year}",
        "finance": "💰 FINANCE",
        "yesterday": "Yesterday: {amount} in {n} transactions.",
        "yesterday_none": "Yesterday: no spending recorded.",
        "top_cats": "Biggest categories: ",
        "top_shops": "Most spent at: ",
        "uncategorised": "Uncategorised",
        "unnamed": "unnamed",
        "month_so_far": "This month so far: {amount}.",
        "tight": "Budgets getting tight:",
        "tight_line": "• {cat}: {pct}% used, {left} left",
        "today_budget": "Today's budget: {per_day} ({remaining} left for {days} days), already spent {spent}.",
        "today_budget_cats": "By category, per day:",
        "weather": "☁️ WEATHER",
        "health": "😴 HEALTH",
        "slept": "Slept {h} h {m} min",
        "score": ", score {score}",
        "stages": "stages: ",
        "deep": "deep", "light": "light", "rem": "REM",
        "awake": "awake {n} min",
        "fatigue": "\n⚠️ You slept too little; fatigue defence is on (impulse spending is watched more closely).",
        "price": "📈 PRICES",
        "news": "📰 NATIONAL NEWS",
        "no_news": "No major national news right now.",
        "security": "🔐 SECURITY",
        "cve_counts": "Last 24 h: {total} new CVEs recorded, {crit} critical, {high} high, {kev} already exploited (CISA KEV), {unscored} not scored yet.",
        "no_cve": "No new high or critical CVEs.",
        "cvss": "CVSS {score}",
        "unscored": "not scored yet",
        "exploited": " · 🔥 EXPLOITED",
        "tech": "Technology/versions: ",
        "unknown_tech": "technology not identified yet",
        "cyber_news": "🛡️ SECURITY NEWS",
    },
}


def _pct(x: Decimal | None) -> str:
    return f"{int((x or 0) * 100)}%"


async def finance_block(sm: async_sessionmaker, tz: ZoneInfo, now: datetime, lang: str = "id") -> str | None:
    s_ = TG[table(lang)]
    today = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    y0 = today - timedelta(days=1)
    month = today.date().replace(day=1)
    out = (Transaction.status == "committed", Transaction.direction == "out")
    async with sm() as s:
        total, n = (
            await s.execute(
                select(func.coalesce(func.sum(Transaction.total_paid_amount), 0), func.count()).where(
                    *out, Transaction.timestamp >= y0, Transaction.timestamp < today
                )
            )
        ).one()
        cats = (
            await s.execute(
                select(Transaction.primary_category, func.sum(Transaction.total_paid_amount).label("t"))
                .where(*out, Transaction.timestamp >= y0, Transaction.timestamp < today)
                .group_by(Transaction.primary_category).order_by(func.sum(Transaction.total_paid_amount).desc()).limit(3)
            )
        ).all()
        shops = (
            await s.execute(
                select(Transaction.merchant, func.sum(Transaction.total_paid_amount).label("t"))
                .where(*out, Transaction.timestamp >= y0, Transaction.timestamp < today)
                .group_by(Transaction.merchant).order_by(func.sum(Transaction.total_paid_amount).desc()).limit(3)
            )
        ).all()
        mtd = (
            await s.execute(
                select(func.coalesce(func.sum(Transaction.total_paid_amount), 0)).where(
                    *out, Transaction.timestamp >= datetime.combine(month, datetime.min.time(), tzinfo=tz)
                )
            )
        ).scalar_one()
        rules = (await s.execute(select(BudgetRule).where(BudgetRule.period_month == month))).scalars().all()
    lines = [s_["finance"]]
    lines.append(s_["yesterday"].format(amount=rupiah(total), n=n) if n else s_["yesterday_none"])
    if cats:
        lines.append(s_["top_cats"] + ", ".join(f"{c or s_['uncategorised']} {rupiah(t)}" for c, t in cats))
    if shops:
        lines.append(s_["top_shops"] + ", ".join(f"{m or s_['unnamed']} {rupiah(t)}" for m, t in shops))
    lines.append(s_["month_so_far"].format(amount=rupiah(mtd)))
    day = await daily_budget(sm, tz, now)
    if day is not None:
        lines.append(s_["today_budget"].format(per_day=rupiah(day.per_day), remaining=rupiah(day.remaining), days=day.days_left,
                                               spent=rupiah(day.spent_today)))
        if day.categories:
            lines.append(s_["today_budget_cats"])
            lines += [f"• {c}: {rupiah(per_day.quantize(Decimal(1)))}" for c, per_day, _ in day.categories[:6]]
    used = sorted(((r, r.spent_amount / r.current_limit) for r in rules if r.current_limit), key=lambda x: -x[1])
    tight = [(r, u) for r, u in used if u >= Decimal("0.7")]
    if tight:
        lines.append(s_["tight"])
        lines += [s_["tight_line"].format(cat=r.category, pct=int(u * 100), left=rupiah(max(r.current_limit - r.spent_amount, 0))) for r, u in tight]
    return "\n".join(lines)


async def health_block(sm: async_sessionmaker, now: datetime, lang: str = "id") -> str | None:
    s_ = TG[table(lang)]
    async with sm() as s:
        row = (
            await s.execute(
                select(SleepSession).where(SleepSession.sleep_date >= now.date() - timedelta(days=1))
                .order_by(SleepSession.sleep_date.desc()).limit(1)
            )
        ).scalar_one_or_none()
    if row is None or not row.total_sleep_mins:
        return None
    h, m = divmod(row.total_sleep_mins, 60)
    parts = [s_["slept"].format(h=h, m=m) + (s_["score"].format(score=row.sleep_score) if row.sleep_score else "")]
    stages = [(s_[k], v) for k, v in (("deep", row.deep_sleep_pct), ("light", row.light_sleep_pct), ("rem", row.rem_sleep_pct)) if v]
    if stages:
        parts.append(s_["stages"] + ", ".join(f"{n} {v.normalize():f}%" for n, v in stages))
    if row.awake_mins:
        parts.append(s_["awake"].format(n=row.awake_mins))
    note = s_["fatigue"] if row.fatigue_defense_active else ""
    return s_["health"] + "\n" + "; ".join(parts) + "." + note


def _link_items(ranked: list[Ranked], n: int) -> list[str]:
    out = []
    for r in ranked[:n]:
        a = r.cluster.articles[0]
        out.append(f"• {r.cluster.headline}\n  {', '.join(r.cluster.sources)}\n  {a.url}")
    return out


def cve_block(severe: list[dict], digest: dict, lang: str = "id") -> str:
    s_ = TG[table(lang)]
    sev = digest["by_severity"]
    lines = [s_["security"]]
    lines.append(s_["cve_counts"].format(total=digest["total"], crit=sev.get("critical", 0), high=sev.get("high", 0),
                                         kev=digest["kev"], unscored=sev.get("unscored", 0)))
    if not severe:
        lines.append(s_["no_cve"])
    for r in severe:
        score = s_["cvss"].format(score=r["cvss"]) if r["cvss"] is not None else s_["unscored"]
        flags = (f" · EPSS {_pct(r['epss'])}" if r["epss"] is not None else "") + (s_["exploited"] if r["kev"] else "")
        mark = "👁" if r.get("watch") else ("🔴" if (r["cvss"] or 0) >= 9 or r["kev"] else "🟠")
        tech = "; ".join(r["products"][:4]) or s_["unknown_tech"]
        lines.append(f"{mark} {r['id']} · {score}{flags}\n   {s_['tech']}{tech}\n   {r['summary'][:200]}")
    return "\n".join(lines)


async def build_telegram(sm: async_sessionmaker, redis, tz: ZoneInfo, now: datetime, lang: str | None = None, llm=None) -> list[str]:
    """The briefing as a list of messages, each under Telegram's size limit, split on block boundaries."""
    prefs = await prefs_mod.load(sm)
    target = norm(lang or prefs.language)
    lang = table(target)  # a language without written frames is translated block by block at the end
    s_ = TG[lang]
    local = now.astimezone(tz)
    ncfg = await news_config.current(sm)
    catalog = ncfg.catalog()
    topics = [x for x in prefs.topics if x in catalog]
    kws = tuple(prefs.keywords)
    blocks = [s_["title"].format(day=DAYS[lang][local.weekday()], d=local.day, month=MONTHS[lang][local.month - 1], year=local.year)]
    if weather := await weather_line(prefs, tz, redis, lang):
        blocks.append(f"{s_['weather']}\n{weather}")
    for block in (await finance_block(sm, tz, local, lang), await health_block(sm, local, lang)):
        if block:
            blocks.append(block)
    if price := await price_line(sm, local, lang):
        blocks.append(f"{s_['price']}\n{price}")
    articles = await recent_articles(sm, now)
    for topic in topics:
        if topic == "nasional":
            news = _link_items([r for r in rank(articles, now, topic="nasional", keywords=kws, country=ncfg.country) if r.score > 0], NEWS_ITEMS)
            blocks.append(s_["news"] + "\n" + ("\n".join(news) if news else s_["no_news"]))
        elif topic == "cyber":
            digest = await cyber.digest_24h(sm, now, ncfg.cve)
            blocks.append(cve_block(await cyber.severe_24h(sm, now, limit=SEVERE_ITEMS, rule=ncfg.cve), digest, lang))
            cy_news = _link_items([r for r in rank(articles, now, topic="cyber", keywords=kws) if r.score > 0], CYBER_NEWS_ITEMS)
            if cy_news:
                blocks.append(s_["cyber_news"] + "\n" + "\n".join(cy_news))
        else:
            items = _link_items([r for r in rank(articles, now, topic=topic, keywords=kws) if r.score > 0], NEWS_ITEMS)
            if items:
                title = catalog[topic][0 if lang == "id" else 1].upper()
                blocks.append(f"📌 {title}\n" + "\n".join(items))
    if kws:  # any headline, from any source, with a word the owner follows
        pool = [replace(a, topic="minat") for a in articles if any(k.lower() in a.title.lower() for k in kws)]
        items = _link_items([r for r in rank(pool, now, topic="minat", keywords=kws) if r.score > 0], NEWS_ITEMS)
        if items:
            blocks.append("⭐ " + ("YANG KAMU IKUTI" if lang == "id" else "WHAT YOU FOLLOW") + "\n" + "\n".join(items))
    if target != lang and llm is not None:
        blocks = await translate_all(llm, redis, blocks, target)
    messages, cur = [], ""
    for b in blocks:
        chunks = [b] if len(b) <= MAX_MESSAGE else _split_block(b)
        for c in chunks:
            if cur and len(cur) + len(c) + 2 > MAX_MESSAGE:
                messages.append(cur)
                cur = ""
            cur = f"{cur}\n\n{c}" if cur else c
    return [*messages, cur] if cur else messages


TELEGRAM_CACHE_KEY = "apa:briefing_tg:"
TELEGRAM_CACHE_TTL_S = 36 * 3600


async def daily_telegram(sm, redis, tz: ZoneInfo, now: datetime, *, rebuild: bool = False, lang: str | None = None, llm=None) -> list[str]:
    """The day's Telegram briefing, made once per language: the first request builds it and later ones (the app's "play
    now" button, a rerun) get the saved text. `rebuild` replaces the saved text (the morning job starting fresh)."""
    p = await prefs_mod.load(sm)
    lang = norm(lang or p.language)
    mix = hashlib.sha256(json.dumps([p.topics, p.keywords]).encode()).hexdigest()[:8]
    key = f"{TELEGRAM_CACHE_KEY}{now.astimezone(tz):%Y-%m-%d}:{lang}:{mix}"
    if redis is not None and not rebuild:
        try:
            if hit := await redis.get(key):
                return json.loads(hit)
        except Exception:
            pass
    messages = await build_telegram(sm, redis, tz, now, lang, llm)
    if redis is not None:
        try:
            await redis.set(key, json.dumps(messages), ex=TELEGRAM_CACHE_TTL_S)
        except Exception:
            pass
    return messages


def _split_block(block: str) -> list[str]:
    """A block longer than one message is cut between its entries, never mid-line."""
    out, cur = [], ""
    for line in block.split("\n"):
        if cur and len(cur) + len(line) + 1 > MAX_MESSAGE:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n{line}" if cur else line
    return [*out, cur] if cur else out
