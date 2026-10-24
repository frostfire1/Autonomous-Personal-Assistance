"""A-03 as a scheduled watcher (blueprint 4.1c): keep today's intake estimate current, warn at 80% of a limit, notice
a bad night, and run the stress shield. Everything is deterministic; no model decides a health matter.

Messages say "perkiraan" (estimate) and give no medical advice; each goes out once per day per kind."""

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from apa.core.logging import get_logger
from apa.db.models.health import (
    BiomarkerMetric,
    HealthWarning,
    HuaweiHealthMetric,
    SleepSession,
    StressReading,
)
from apa.health.nutrition import day_intake
from apa.private_mode import recovery
from apa.reporting.digest import deliver
from apa.safety.policy import load_policy

log = get_logger(__name__)

LIMITS = {
    "caffeine_daily_mg": Decimal(400),
    "sugar_daily_g": Decimal(30),
    "sodium_daily_mg": Decimal(2000),
    "early_warning_pct": Decimal(80),
    "stress_shield_score": Decimal(75),
    "stress_shield_hours": Decimal(2),
}
SLEEP_DEFICIT_MINS = 360
STRESS_MIN_READINGS = 3
STRESS_BUFFER = Decimal(100_000)  # asked of the recovery step, which bounds it further


async def _rules(sm: async_sessionmaker) -> dict[str, Decimal]:
    async with sm() as s:
        _, _, v = await load_policy(s)
    return {k: Decimal(v.get(k, d)) for k, d in LIMITS.items()}


def _day(now: datetime, tz: ZoneInfo) -> tuple[datetime, datetime]:
    start = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


async def refresh_biomarkers(sm: async_sessionmaker, tz: ZoneInfo, now: datetime) -> BiomarkerMetric:
    """Today's row in biomarker_metrics: intake so far, last night's sleep, resting heart rate, and the breach flags."""
    start, end = _day(now, tz)
    rules, intake = await _rules(sm), await day_intake(sm, start, end)
    async with sm() as s:
        sleep = (
            await s.execute(
                select(SleepSession)
                .where(SleepSession.sleep_date >= start.date() - timedelta(days=1))
                .order_by(SleepSession.sleep_date.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        rhr = (
            await s.execute(
                select(HuaweiHealthMetric.resting_heart_rate)
                .where(
                    HuaweiHealthMetric.recorded_at >= start - timedelta(days=1),
                    HuaweiHealthMetric.resting_heart_rate.is_not(None),
                )
                .order_by(HuaweiHealthMetric.recorded_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        values = dict(
            cumulative_caffeine_mg=intake.caffeine_mg,
            cumulative_sugar_g=intake.sugar_g,
            cumulative_sodium_mg=intake.sodium_mg,
            sleep_duration_hours=(Decimal(sleep.total_sleep_mins) / 60).quantize(Decimal("0.01"))
            if sleep and sleep.total_sleep_mins is not None
            else None,
            resting_heart_rate=rhr,
            caffeine_limit_breached=intake.caffeine_mg > rules["caffeine_daily_mg"],
            sugar_limit_breached=intake.sugar_g > rules["sugar_daily_g"],
        )
        stmt = insert(BiomarkerMetric).values(log_date=start.date(), **values)
        await s.execute(stmt.on_conflict_do_update(index_elements=["log_date"], set_=values))
        await s.commit()
        return (
            await s.execute(select(BiomarkerMetric).where(BiomarkerMetric.log_date == start.date()))
        ).scalar_one()


async def today_intake_tuple(sm: async_sessionmaker, tz: ZoneInfo, when: datetime):
    """(caffeine_mg, sugar_g, sodium_mg) so far on `when`'s day, as the debate prompt wants it; None if unknown."""
    async with sm() as s:
        row = (
            await s.execute(
                select(BiomarkerMetric).where(BiomarkerMetric.log_date == when.astimezone(tz).date())
            )
        ).scalar_one_or_none()
    return (row.cumulative_caffeine_mg, row.cumulative_sugar_g, row.cumulative_sodium_mg) if row else None


async def _warn(sm, notifier, redis, kind: str, day: str, text: str) -> bool:
    sent = await deliver(sm, notifier, redis, f"health:{kind}:{day}", text)
    if sent:
        async with sm() as s:
            s.add(HealthWarning(warning_type=kind, message=text, action_taken="telegram"))
            await s.commit()
    return sent


async def check_intake(sm, notifier: Any, redis: Any | None, tz: ZoneInfo, now: datetime) -> list[str]:
    """Early warning at `early_warning_pct` of a daily limit (once a day per kind). -> kinds sent."""
    row = await refresh_biomarkers(sm, tz, now)
    rules = await _rules(sm)
    pct = rules["early_warning_pct"] / 100
    day = now.astimezone(tz).strftime("%Y-%m-%d")
    sent = []
    cafe, sugar = row.cumulative_caffeine_mg, row.cumulative_sugar_g
    if cafe >= rules["caffeine_daily_mg"] * pct:
        late = now.astimezone(tz).hour >= 14
        text = (
            f"☕ Kafein hari ini sekitar {cafe:.0f} mg (perkiraan), {int(cafe / rules['caffeine_daily_mg'] * 100)}% "
            f"dari batas {rules['caffeine_daily_mg']:.0f} mg."
            + (" Sudah lewat jam 14:00, sebaiknya berhenti agar tidur malam tidak terganggu." if late else "")
        )
        if await _warn(sm, notifier, redis, "caffeine_spike", day, text):
            sent.append("caffeine_spike")
    if sugar >= rules["sugar_daily_g"] * pct:
        text = (
            f"🍬 Gula hari ini sekitar {sugar:.0f} g (perkiraan), {int(sugar / rules['sugar_daily_g'] * 100)}% "
            f"dari batas {rules['sugar_daily_g']:.0f} g."
        )
        if await _warn(sm, notifier, redis, "sugar_overload", day, text):
            sent.append("sugar_overload")
    return sent


async def check_sleep(sm, notifier: Any, redis: Any | None, tz: ZoneInfo, now: datetime) -> bool:
    """A short night is said once, in the morning."""
    local = now.astimezone(tz)
    if local.hour < 6:
        return False
    async with sm() as s:
        row = (
            await s.execute(
                select(SleepSession)
                .where(SleepSession.sleep_date >= local.date() - timedelta(days=1))
                .order_by(SleepSession.sleep_date.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    if row is None or row.total_sleep_mins is None or row.total_sleep_mins >= SLEEP_DEFICIT_MINS:
        return False
    h, m = divmod(row.total_sleep_mins, 60)
    return await _warn(
        sm,
        notifier,
        redis,
        "sleep_deficit",
        local.strftime("%Y-%m-%d"),
        f"😴 Tidurmu semalam hanya {h} jam {m} menit. Hari ini keputusan belanja impulsif akan "
        "diperiksa lebih ketat.",
    )


async def check_stress_shield(
    sm, notifier: Any, redis: Any | None, tz: ZoneInfo, now: datetime
) -> Decimal | None:
    """Stress at or above the threshold across the last `stress_shield_hours` (at least three readings, none below
    it) moves a small, bounded amount to Mental Care. No narrative is involved: the reading is only a number."""
    rules = await _rules(sm)
    window = now - timedelta(hours=float(rules["stress_shield_hours"]))
    async with sm() as s:
        readings = (
            (
                await s.execute(
                    select(StressReading)
                    .where(StressReading.timestamp >= window, StressReading.timestamp <= now)
                    .order_by(StressReading.timestamp)
                )
            )
            .scalars()
            .all()
        )
    if len(readings) < STRESS_MIN_READINGS or any(
        r.stress_score < rules["stress_shield_score"] for r in readings
    ):
        return None
    if readings[-1].timestamp - readings[0].timestamp < timedelta(hours=float(rules["stress_shield_hours"]) * 0.5):
        return None  # a burst, not a sustained stretch
    day = now.astimezone(tz).strftime("%Y-%m-%d")
    if redis is not None and await redis.exists(f"apa:digest:health:stress:{day}"):
        return None
    moved = await recovery.reallocate(sm, STRESS_BUFFER, tz, now)
    if moved is None:
        return None
    amount, source = moved
    async with sm() as s:
        for r in readings:
            row = await s.get(StressReading, r.id)
            row.stress_shield_triggered = True
        (await s.get(StressReading, readings[-1].id)).mental_buffer_allocated = amount
        await s.commit()
    from apa.core.format import rupiah

    await _warn(
        sm,
        notifier,
        redis,
        "stress",
        day,
        f"🧘 Stres tinggi terdeteksi beberapa jam ini. {rupiah(amount)} "
        f"dipindah dari {source} ke {recovery.RECOVERY_CATEGORY} sebagai penyangga.",
    )
    return amount


async def run_health_tick(sm, notifier: Any, redis: Any | None, tz: ZoneInfo, now: datetime) -> dict:
    out = {"intake": await check_intake(sm, notifier, redis, tz, now)}
    out["sleep"] = await check_sleep(sm, notifier, redis, tz, now)
    out["stress"] = await check_stress_shield(sm, notifier, redis, tz, now)
    return out
