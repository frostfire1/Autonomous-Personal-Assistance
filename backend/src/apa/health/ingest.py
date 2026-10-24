"""Health data in: from the Android app (Health Connect) or from the Huawei Health Kit cloud sync. One validated
shape, idempotent writes (the app may resend), and the fatigue flag computed from the policy thresholds.

Nothing here is medical advice. It stores readings and derives the two flags the blueprint asks for:
fatigue defence (a poor night) and, in `jobs`, the stress shield."""

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from apa.db.models.health import HuaweiHealthMetric, SleepSession, StressReading
from apa.safety.policy import load_policy

DEFAULTS = {"fatigue_sleep_score": Decimal(70), "fatigue_deep_sleep_pct": Decimal(15)}


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MetricIn(_In):
    recorded_at: datetime
    resting_heart_rate: int | None = Field(default=None, ge=25, le=220)
    current_heart_rate: int | None = Field(default=None, ge=25, le=250)
    spo2_pct: Decimal | None = Field(default=None, ge=50, le=100)
    step_count: int | None = Field(default=None, ge=0, le=200_000)
    active_calories: int | None = Field(default=None, ge=0, le=20_000)
    daily_distance_m: Decimal | None = Field(default=None, ge=0, le=300_000)


class SleepIn(_In):
    sleep_date: date
    total_sleep_mins: int = Field(ge=0, le=1440)
    sleep_score: int | None = Field(default=None, ge=0, le=100)
    deep_sleep_pct: Decimal | None = Field(default=None, ge=0, le=100)
    light_sleep_pct: Decimal | None = Field(default=None, ge=0, le=100)
    rem_sleep_pct: Decimal | None = Field(default=None, ge=0, le=100)
    awake_mins: int | None = Field(default=None, ge=0, le=1440)
    breathing_score: int | None = Field(default=None, ge=0, le=100)


class StressIn(_In):
    timestamp: datetime
    stress_score: int = Field(ge=0, le=100)


class HealthIn(_In):
    device_id: str = Field(min_length=1, max_length=64)
    metrics: list[MetricIn] = Field(default_factory=list, max_length=500)
    sleep: list[SleepIn] = Field(default_factory=list, max_length=14)
    stress: list[StressIn] = Field(default_factory=list, max_length=500)


def stress_grade(score: int) -> str:
    return "low" if score < 40 else "normal" if score < 60 else "medium" if score < 75 else "high"


async def ingest(sm: async_sessionmaker, p: HealthIn, *, source: str) -> dict[str, int]:
    """-> counts of rows added/updated. Resending the same payload changes nothing."""
    async with sm() as s:
        _, _, rules = await load_policy(s)
    min_score = rules.get("fatigue_sleep_score", DEFAULTS["fatigue_sleep_score"])
    min_deep = rules.get("fatigue_deep_sleep_pct", DEFAULTS["fatigue_deep_sleep_pct"])
    added = {"metrics": 0, "sleep": 0, "stress": 0}
    async with sm() as s:
        if p.metrics:
            have = set(
                (
                    await s.execute(
                        select(HuaweiHealthMetric.recorded_at).where(
                            HuaweiHealthMetric.device_id == p.device_id,
                            HuaweiHealthMetric.recorded_at.in_([m.recorded_at for m in p.metrics]),
                        )
                    )
                ).scalars()
            )
            for m in p.metrics:
                if m.recorded_at not in have:
                    s.add(HuaweiHealthMetric(device_id=p.device_id, sync_source=source, **m.model_dump()))
                    added["metrics"] += 1
        for sl in p.sleep:
            row = (
                await s.execute(select(SleepSession).where(SleepSession.sleep_date == sl.sleep_date))
            ).scalar_one_or_none()
            tired = (sl.sleep_score is not None and sl.sleep_score < min_score) or (
                sl.deep_sleep_pct is not None and sl.deep_sleep_pct < min_deep
            )
            if row is None:
                s.add(SleepSession(fatigue_defense_active=tired, **sl.model_dump()))
                added["sleep"] += 1
            else:
                for k, v in sl.model_dump().items():
                    setattr(row, k, v)
                row.fatigue_defense_active = tired
        if p.stress:
            have_t = set(
                (
                    await s.execute(
                        select(StressReading.timestamp).where(
                            StressReading.timestamp.in_([x.timestamp for x in p.stress])
                        )
                    )
                ).scalars()
            )
            for x in p.stress:
                if x.timestamp not in have_t:
                    s.add(
                        StressReading(
                            timestamp=x.timestamp,
                            stress_score=x.stress_score,
                            stress_grade=stress_grade(x.stress_score),
                        )
                    )
                    added["stress"] += 1
        await s.commit()
    return added
