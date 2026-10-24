"""The owner's profile: patterns computed from their own data, so the assistant knows them and its advice rests on numbers.

When they sleep and for how long, when they are active, what they eat most and when, how they spend, how much they move, what
they talk about. Every figure is computed here from the ledger, the health data and the chat, by plain code; no model is
involved, so a trait is as trustworthy as the data under it. The assistant only words them (and says so when a trait rests on
too few observations). Recomputed every night into `profile_traits`, readable and refreshable in the dashboard.

Sleep TIMES are not recorded by the sleep tracker data we receive (only duration and quality), so "when you are active" is
inferred from the first and last thing the owner did each day (a payment or a message) and is labelled as inferred."""

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from statistics import mean, median, pstdev
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from apa.core.format import rupiah
from apa.db.models.health import BiomarkerMetric, HuaweiHealthMetric, SleepSession, StressReading
from apa.db.models.intel import ProfileTrait
from apa.db.models.ledger import Transaction, TransactionItem
from apa.db.models.ops import InboundEvent

MIN_DAYS = 5  # fewer observations than this and a trait is not claimed
FOOD_CATEGORIES = ("Food & Dining", "Groceries")
MEAL_SLOTS = (("breakfast", 5, 10), ("lunch", 11, 14), ("afternoon", 15, 16), ("dinner", 17, 21), ("late night", 22, 28))
STOP = frozenset(
    "yang dan atau tapi untuk dengan dari pada ini itu aku kamu saya dia kita mereka bisa tidak nggak gak sudah belum lagi juga "
    "mau akan karena kalau kalo jadi apa kenapa gimana bagaimana tadi nanti habis baru sama banget aja saja dong deh sih kok "
    "ya the and that this with have from your what when will would could about there their they them then than just like "
    "dari sudah masih lebih sedang punya buat bikin udah bukan ada apakah siapa mana kira kira".split()
)
_WORD = re.compile(r"[a-zA-Z]{4,}")


@dataclass
class Trait:
    key: str
    summary: str
    evidence: int
    data: dict = field(default_factory=dict)


def hm(minutes: float) -> str:
    m = int(round(minutes))
    return f"{m // 60}h{m % 60:02d}"


def clock(minutes: float) -> str:
    m = int(round(minutes)) % (24 * 60)
    return f"{m // 60:02d}:{m % 60:02d}"


# ---- pure computations (tested without a database) --------------------------------------------------------------------------


def sleep_trait(rows: list[tuple[date, int, int | None]]) -> Trait | None:
    """rows: (night date, minutes asleep, score or None)."""
    rows = [r for r in rows if r[1] and r[1] > 0]
    if len(rows) < MIN_DAYS:
        return None
    mins = [r[1] for r in rows]
    week = [r[1] for r in rows if r[0].weekday() < 5]
    end = [r[1] for r in rows if r[0].weekday() >= 5]
    scores = [r[2] for r in rows if r[2]]
    parts = [f"Sleep over {len(rows)} nights: average {hm(mean(mins))}, from {hm(min(mins))} to {hm(max(mins))}"]
    if len(week) >= 2 and len(end) >= 2:
        parts.append(f"weekdays {hm(mean(week))}, weekends {hm(mean(end))}")
    if scores:
        parts.append(f"average score {mean(scores):.0f}")
    short = sum(1 for m in mins if m < 360)
    if short:
        parts.append(f"{short} of {len(rows)} nights under 6 hours")
    spread = pstdev(mins) if len(mins) > 1 else 0
    return Trait("sleep.duration", "; ".join(parts) + ".", len(rows),
                 {"avg_min": round(mean(mins)), "min": min(mins), "max": max(mins), "under_6h": short, "spread_min": round(spread)})


def activity_trait(stamps: list[datetime], tz: ZoneInfo) -> Trait | None:
    """When the owner is usually up, from the first and last thing they did each local day (payments and messages)."""
    days: dict[date, list[float]] = {}
    for t in stamps:
        local = t.astimezone(tz)
        days.setdefault(local.date(), []).append(local.hour * 60 + local.minute)
    full = {d: v for d, v in days.items() if len(v) >= 2}  # a day with one event says nothing about its start and end
    if len(full) < MIN_DAYS:
        return None
    firsts = [min(v) for v in full.values()]
    lasts = [max(v) for v in full.values()]
    hours = Counter(int(m // 60) for v in days.values() for m in v)
    busiest = hours.most_common(1)[0][0]
    return Trait(
        "activity.hours",
        f"Over {len(full)} days the first thing you do is around {clock(median(firsts))} and the last around {clock(median(lasts))} "
        f"(inferred from your payments and messages, not from a sleep tracker); busiest hour {busiest:02d}:00.",
        len(full), {"first_median_min": round(median(firsts)), "last_median_min": round(median(lasts)), "busiest_hour": busiest},
    )


def food_trait(meals: list[tuple[datetime, str | None, Any]], items: list[str], tz: ZoneInfo,
               caffeine: list[float], sugar: list[float]) -> Trait | None:
    """meals: (time, merchant, amount) of food and grocery payments; items: purchased item names; caffeine/sugar: daily mg/g."""
    if len(meals) < MIN_DAYS:
        return None
    slots: Counter[str] = Counter()
    for t, _, _ in meals:
        h = t.astimezone(tz).hour
        h = h + 24 if h < 5 else h
        for name, lo, hi in MEAL_SLOTS:
            if lo <= h <= hi:
                slots[name] += 1
                break
    shops = Counter(m.strip() for _, m, _ in meals if m and m.strip())
    foods = Counter(i.strip().lower() for i in items if i and i.strip())
    spends = [float(a) for _, _, a in meals]
    parts = [f"Food and groceries: {len(meals)} payments, average {rupiah(mean(spends))}"]
    if slots:
        parts.append("usually at " + ", ".join(f"{n} ({c})" for n, c in slots.most_common(3)))
    if shops:
        parts.append("most often at " + ", ".join(f"{n} ({c}x)" for n, c in shops.most_common(4)))
    if foods:
        parts.append("most bought: " + ", ".join(f"{n} ({c}x)" for n, c in foods.most_common(5)))
    if caffeine:
        parts.append(f"caffeine about {mean(caffeine):.0f} mg a day")
    if sugar:
        parts.append(f"sugar about {mean(sugar):.0f} g a day")
    return Trait("food.habits", "; ".join(parts) + ".", len(meals),
                 {"slots": dict(slots), "shops": shops.most_common(5), "items": foods.most_common(8),
                  "avg_spend": round(mean(spends)), "caffeine_mg": round(mean(caffeine)) if caffeine else None,
                  "sugar_g": round(mean(sugar)) if sugar else None})


def spending_trait(spends: list[tuple[datetime, str | None, Any]], tz: ZoneInfo, days: int) -> Trait | None:
    """spends: (time, category, amount) of the last `days` days of payments out."""
    if len(spends) < MIN_DAYS:
        return None
    total = sum(float(a) for _, _, a in spends)
    cats: Counter[str] = Counter()
    for _, c, a in spends:
        cats[c or "Other"] += float(a)
    weekdays: Counter[int] = Counter()
    for t, _, a in spends:
        weekdays[t.astimezone(tz).weekday()] += float(a)
    names = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
    top = cats.most_common(3)
    biggest = max(spends, key=lambda s: float(s[2]))
    return Trait(
        "spending.pattern",
        f"Spending over {days} days: {rupiah(total)} in {len(spends)} payments (about {rupiah(total / days)} a day); top: "
        + ", ".join(f"{c} {v / total:.0%}" for c, v in top) + f"; spends most on {names[weekdays.most_common(1)[0][0]]}s; "
        f"largest payment {rupiah(float(biggest[2]))} ({biggest[1] or 'Other'}).",
        len(spends), {"total": round(total), "per_day": round(total / days), "top": [(c, round(v)) for c, v in top]},
    )


def steps_trait(daily_steps: list[int], resting: list[int]) -> Trait | None:
    steps = [s for s in daily_steps if s and s > 0]
    if len(steps) < MIN_DAYS:
        return None
    parts = [f"Movement over {len(steps)} days: about {mean(steps):,.0f} steps a day (best {max(steps):,}, lowest {min(steps):,})"]
    if resting:
        parts.append(f"resting heart rate about {mean(resting):.0f}")
    return Trait("activity.steps", "; ".join(parts) + ".", len(steps), {"avg": round(mean(steps)), "max": max(steps), "min": min(steps)})


def stress_trait(readings: list[tuple[datetime, int]], tz: ZoneInfo) -> Trait | None:
    if len(readings) < 10:
        return None
    by_hour: dict[int, list[int]] = {}
    for t, s in readings:
        by_hour.setdefault(t.astimezone(tz).hour, []).append(s)
    worst = max(((h, mean(v)) for h, v in by_hour.items() if len(v) >= 2), key=lambda x: x[1], default=None)
    parts = [f"Stress: average {mean(s for _, s in readings):.0f} over {len(readings)} readings"]
    if worst:
        parts.append(f"highest around {worst[0]:02d}:00 (about {worst[1]:.0f})")
    return Trait("health.stress", "; ".join(parts) + ".", len(readings), {"avg": round(mean(s for _, s in readings))})


def usage_trait(rows: list[tuple[date, str, str, str, int, datetime | None]], tz: ZoneInfo) -> Trait | None:
    """Screen time and app habits from ActivityWatch: rows are (day, device, kind, app, seconds, last_at)."""
    from apa import usage

    u = usage.summarise(rows, tz)
    if u["days"] < MIN_DAYS:
        return None
    avg = mean(u["per_day"].values())
    total = sum(u["per_app"].values())
    top = sorted(u["per_app"].items(), key=lambda x: -x[1])[:5]
    cats = [(c, s) for c, s in sorted(u["per_cat"].items(), key=lambda x: -x[1]) if c != "other"][:3]
    kinds = ", ".join(f"{k} {usage.hours(s / u['days'])}" for k, s in sorted(u["per_kind"].items(), key=lambda x: -x[1]))
    late = len(u["late_days"])
    days_sorted = sorted(u["per_day"].items())
    recent, before = [s for _, s in days_sorted[-7:]], [s for _, s in days_sorted[-14:-7]]
    trend = (f" The last {len(recent)} days average {usage.hours(mean(recent))}, "
             f"{'up' if mean(recent) >= mean(before) else 'down'} {abs(round(100 * (mean(recent) / mean(before) - 1)))}% on the week before."
             if len(before) >= 3 and len(recent) >= 3 else "")
    text = (f"Active on devices about {usage.hours(avg)} a day over {u['days']} days ({kinds}). Most used: "
            + ", ".join(f"{a} ({usage.hours(s / u['days'])}/day)" for a, s in top) + ".")
    if cats and total:
        text += " By kind: " + ", ".join(f"{c} {round(100 * s / total)}%" for c, s in cats) + "."
    text += f" Still on a device after 23:00 on {late} of {u['days']} days." if late else ""
    text += trend
    return Trait("screen_time", text, u["days"], {"avg_seconds": round(avg), "daily": [(d.isoformat(), s) for d, s in days_sorted[-14:]], "top_apps": top, "categories": cats, "late_days": late})


def watch_trait(items: list[tuple[str, str, str]]) -> Trait | None:
    """What the owner watches and searches on YouTube: (kind, channel, title) from their Takeout export."""
    watched = [(c, t) for k, c, t in items if k == "watched"]
    liked = [(c, t) for k, c, t in items if k == "liked"]
    subs = [c for k, c, _ in items if k == "subscribed"]
    if len(watched) < 10 and len(liked) < 5 and len(subs) < 5:
        return None
    channels = Counter(c for c, _ in watched if c).most_common(5)
    words = Counter(w for _, t in watched + liked for w in _WORD.findall(t.lower()) if w not in STOP)
    topics = [w for w, n in words.most_common(8) if n >= 3][:6]
    searches = Counter(t.lower() for k, _, t in items if k == "searched").most_common(4)
    text = f"On YouTube ({len(watched)} videos): mostly " + ", ".join(f"{c} ({n})" for c, n in channels) + "." if len(watched) >= 10 and channels else ""
    if subs:
        text += f" Follows {len(subs)} channels, among them " + ", ".join(subs[:6]) + "."
    if len(liked) >= 5:
        fav = Counter(c for c, _ in liked if c).most_common(4)
        text += " Liked videos come mostly from " + ", ".join(f"{c} ({n})" for c, n in fav) + "."
    if topics and len(watched) >= 10:
        text += " Topics that keep coming up: " + ", ".join(topics) + "."
    if searches:
        text += " Searched for: " + ", ".join(t for t, _ in searches) + "."
    return Trait("youtube", text.strip(), len(watched) + len(liked) + len(subs), {"channels": channels, "topics": topics, "searches": searches, "subscriptions": len(subs)})


def habits_trait(obs: list[tuple[str, str]]) -> Trait | None:
    """Anything the owner keeps mentioning under a kind the code does not know (reading, skincare, caffeine...): the most
    repeated things per kind. Kinds with their own trait are left to it."""
    own = {"meal", "drink", "mood", "like", "dislike", "other"}
    by_kind: dict[str, Counter] = {}
    for k, t in obs:
        if k not in own:
            by_kind.setdefault(k, Counter())[t.lower()] += 1
    parts = [f"{k}: " + ", ".join(f"{t} (x{c})" for t, c in cnt.most_common(3)) for k, cnt in sorted(by_kind.items(), key=lambda x: -sum(x[1].values())) if sum(cnt.values()) >= 3][:4]
    n = sum(sum(c.values()) for c in by_kind.values())
    return Trait("habits", "Recurring in what the owner shared: " + "; ".join(parts) + ".", n, {"by_kind": {k: c.most_common(5) for k, c in by_kind.items()}}) if parts else None


def mood_trait(moods: list[tuple[date, int]], sleeps: list[tuple[date, int]]) -> Trait | None:
    """The average mood the owner reported, and (with enough days) how it differs after short and long sleep."""
    by_day: dict[date, list[int]] = {}
    for d, m in moods:
        by_day.setdefault(d, []).append(m)
    daily = {d: mean(v) for d, v in by_day.items()}
    if len(daily) < MIN_DAYS:
        return None
    text = f"Mood the owner reported averages {mean(daily.values()):.1f} of 5 over {len(daily)} days."
    slept = {d: m for d, m in sleeps}
    short = [v for d, v in daily.items() if d in slept and slept[d] < 390]
    long_ = [v for d, v in daily.items() if d in slept and slept[d] >= 390]
    if len(short) >= 3 and len(long_) >= 3:
        text += f" After under 6.5 h of sleep {mean(short):.1f}, after more {mean(long_):.1f}."
    return Trait("mood", text, len(daily), {"avg": round(mean(daily.values()), 2), "after_short_sleep": round(mean(short), 2) if short else None,
                                            "after_long_sleep": round(mean(long_), 2) if long_ else None})


def likes_trait(obs: list[tuple[str, str]]) -> Trait | None:
    """What the owner said they like and dislike (from the nightly diary's observations): (type, text) pairs."""
    def top(kind: str) -> list[tuple[str, int]]:
        c = Counter(t.lower() for k, t in obs if k == kind)
        return c.most_common(5)

    likes, dislikes = top("like"), top("dislike")
    n = sum(c for _, c in likes) + sum(c for _, c in dislikes)
    if n < 3:
        return None
    parts = []
    if likes:
        parts.append("Likes: " + ", ".join(t for t, _ in likes))
    if dislikes:
        parts.append("Dislikes: " + ", ".join(t for t, _ in dislikes))
    return Trait("likes", ". ".join(parts) + ".", n, {"likes": likes, "dislikes": dislikes})


def interest_trait(messages: list[str], keywords: list[str], learned_texts: list[str], categories: list[tuple[str, float]]) -> Trait | None:
    """What the owner talks about and follows: frequent words in their messages, the words they chose to follow, what the
    assistant learned about their preferences and goals, and the non-essential categories they spend on."""
    words: Counter[str] = Counter()
    for m in messages:
        words.update(w for w in (x.lower() for x in _WORD.findall(m)) if w not in STOP)
    talk = [(w, c) for w, c in words.most_common(30) if c >= 3][:8]
    parts = []
    if talk:
        parts.append("talks about " + ", ".join(f"{w} ({c}x)" for w, c in talk))
    if keywords:
        parts.append("follows " + ", ".join(keywords[:8]))
    if learned_texts:
        parts.append("learned: " + "; ".join(t[:80] for t in learned_texts[:4]))
    fun = [c for c, _ in categories if c in ("Entertainment", "Shopping", "Hobbies", "Travel", "Education")][:3]
    if fun:
        parts.append("spends on " + ", ".join(fun))
    if not parts:
        return None
    return Trait("interests.topics", "Interests: " + "; ".join(parts) + ".", len(messages) + len(learned_texts),
                 {"words": talk, "keywords": keywords, "spend_categories": fun})


# ---- reading the data and storing the traits --------------------------------------------------------------------------------


async def compute(sm: async_sessionmaker, tz: ZoneInfo, now: datetime) -> list[Trait]:
    from apa import assistant_memory, prefs

    since30, since60 = now - timedelta(days=30), now - timedelta(days=60)
    committed = Transaction.status == "committed"
    async with sm() as s:
        sleeps = (await s.execute(select(SleepSession.sleep_date, SleepSession.total_sleep_mins, SleepSession.sleep_score)
                                  .where(SleepSession.sleep_date >= (now - timedelta(days=30)).date()))).all()
        tx_times = (await s.execute(select(Transaction.timestamp).where(committed, Transaction.timestamp >= since30))).scalars().all()
        msg_rows = (await s.execute(select(InboundEvent.received_at, InboundEvent.payload).where(
            InboundEvent.source == "telegram_shorthand", InboundEvent.received_at >= since60))).all()
        meals = (await s.execute(select(Transaction.timestamp, Transaction.merchant, Transaction.total_paid_amount).where(
            committed, Transaction.direction == "out", Transaction.primary_category.in_(FOOD_CATEGORIES),
            Transaction.timestamp >= since60))).all()
        item_names = (await s.execute(
            select(func.coalesce(TransactionItem.normalized_item_name, TransactionItem.item_name))
            .join(Transaction, Transaction.id == TransactionItem.transaction_id)
            .where(committed, Transaction.timestamp >= since60, Transaction.primary_category.in_(FOOD_CATEGORIES)))).scalars().all()
        bio = (await s.execute(select(BiomarkerMetric.cumulative_caffeine_mg, BiomarkerMetric.cumulative_sugar_g).where(
            BiomarkerMetric.log_date >= (now - timedelta(days=30)).date()))).all()
        spends = (await s.execute(select(Transaction.timestamp, Transaction.primary_category, Transaction.total_paid_amount).where(
            committed, Transaction.direction == "out", Transaction.timestamp >= since30))).all()
        steps = (await s.execute(select(func.date(HuaweiHealthMetric.recorded_at), func.max(HuaweiHealthMetric.step_count),
                                        func.avg(HuaweiHealthMetric.resting_heart_rate)).where(
            HuaweiHealthMetric.recorded_at >= now - timedelta(days=14), HuaweiHealthMetric.step_count.is_not(None))
            .group_by(func.date(HuaweiHealthMetric.recorded_at)))).all()
        stress = (await s.execute(select(StressReading.timestamp, StressReading.stress_score).where(
            StressReading.timestamp >= since30))).all()
    from apa import usage as usage_mod

    usage_rows = await usage_mod.recent(sm, 30, now.date())
    obs = await assistant_memory.observations(sm, 60, now.date())
    watch = await assistant_memory.watched(sm, 90, now.date())
    messages = [str(p.get("text", "")) for _, p in msg_rows]
    p = await prefs.load(sm)
    learned_texts = [i["text"] for i in await assistant_memory.learned(sm) if i["kind"] in ("preference", "goal", "person")]
    cat_totals: Counter[str] = Counter()
    for _, c, a in spends:
        cat_totals[c or "Other"] += float(a)
    traits = [
        sleep_trait([(d, m or 0, sc) for d, m, sc in sleeps]),
        activity_trait(list(tx_times) + [t for t, _ in msg_rows] + [r.last_at for r in usage_rows if r.last_at]
                       + [r.first_at for r in usage_rows if r.first_at], tz),
        food_trait([(t, m, a) for t, m, a in meals], list(item_names) + [o["text"] for o in obs if o["type"] in ("meal", "drink")], tz,
                   [float(c) for c, _ in bio if c], [float(g) for _, g in bio if g]),
        spending_trait([(t, c, a) for t, c, a in spends], tz, 30),
        steps_trait([int(m) for _, m, _ in steps if m], [int(r) for _, _, r in steps if r]),
        stress_trait([(t, int(sc)) for t, sc in stress], tz),
        interest_trait(messages, list(p.keywords), learned_texts, cat_totals.most_common()),
        usage_trait([(r.day, r.device, r.kind, r.app, r.seconds, r.last_at) for r in usage_rows], tz),
        mood_trait([(o["date"], o["mood"]) for o in obs if o.get("mood")], [(d, m or 0) for d, m, _ in sleeps]),
        likes_trait([(o["type"], o["text"]) for o in obs]),
        habits_trait([(o["type"], o["text"]) for o in obs]),
        watch_trait([(w["k"], w.get("c", ""), w.get("t", "")) for w in watch]),
    ]
    return [t for t in traits if t is not None]


async def refresh(sm: async_sessionmaker, tz: ZoneInfo, now: datetime | None = None) -> list[Trait]:
    """Recompute every trait and store them; a trait that no longer has enough data is removed. -> the traits."""
    traits = await compute(sm, tz, now or datetime.now(tz))
    async with sm() as s:
        for t in traits:
            stmt = insert(ProfileTrait).values(key=t.key, summary=t.summary, data=t.data, evidence=t.evidence, computed_at=func.now())
            await s.execute(stmt.on_conflict_do_update(
                index_elements=["key"], set_={"summary": t.summary, "data": t.data, "evidence": t.evidence, "computed_at": func.now()}))
        await s.execute(delete(ProfileTrait).where(ProfileTrait.key.not_in([t.key for t in traits] or [""])))
        await s.commit()
    return traits


async def traits(sm: async_sessionmaker) -> list[dict]:
    async with sm() as s:
        rows = (await s.execute(select(ProfileTrait).order_by(ProfileTrait.key))).scalars().all()
    return [{"key": r.key, "summary": r.summary, "evidence": r.evidence, "data": r.data, "computed_at": r.computed_at.isoformat()}
            for r in rows]


async def block(sm: async_sessionmaker, limit: int = 1400) -> str:
    """The profile as lines for the assistant's context."""
    return "\n".join(f"- {t['summary']}" for t in await traits(sm))[:limit]
