"""Health, nutrition and Huawei wearable telemetry tables."""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, Date, ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from apa.db.base import Base, created_at_col


class HuaweiHealthMetric(Base):
    __tablename__ = "huawei_health_metrics"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    recorded_at: Mapped[datetime] = mapped_column(index=True)
    device_id: Mapped[str] = mapped_column(String(64))
    resting_heart_rate: Mapped[int | None]
    current_heart_rate: Mapped[int | None]
    spo2_pct: Mapped[Decimal | None] = mapped_column(Numeric(4, 2))
    step_count: Mapped[int | None]
    active_calories: Mapped[int | None]
    daily_distance_m: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))
    sync_source: Mapped[str] = mapped_column(String(24))  # cloud_api|health_connect_bridge


class SleepSession(Base):
    __tablename__ = "sleep_sessions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    sleep_date: Mapped[date] = mapped_column(Date, unique=True)
    sleep_score: Mapped[int | None]
    total_sleep_mins: Mapped[int | None]
    deep_sleep_pct: Mapped[Decimal | None] = mapped_column(Numeric(4, 2))
    light_sleep_pct: Mapped[Decimal | None] = mapped_column(Numeric(4, 2))
    rem_sleep_pct: Mapped[Decimal | None] = mapped_column(Numeric(4, 2))
    awake_mins: Mapped[int | None]
    breathing_score: Mapped[int | None]
    fatigue_defense_active: Mapped[bool] = mapped_column(Boolean, server_default="false")


class StressReading(Base):
    __tablename__ = "stress_readings"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(index=True)
    stress_score: Mapped[int]
    stress_grade: Mapped[str] = mapped_column(String(10))
    high_stress_duration_mins: Mapped[int] = mapped_column(server_default="0")
    stress_shield_triggered: Mapped[bool] = mapped_column(Boolean, server_default="false")
    mental_buffer_allocated: Mapped[Decimal | None]


class WorkoutActivity(Base):
    __tablename__ = "workout_activities"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    workout_type: Mapped[str] = mapped_column(String(24))
    start_time: Mapped[datetime] = mapped_column(index=True)
    duration_mins: Mapped[int]
    calories_burned: Mapped[int | None]
    avg_hr: Mapped[int | None]
    max_hr: Mapped[int | None]
    recovery_hours: Mapped[int | None]
    post_workout_supplements_taken: Mapped[bool] = mapped_column(Boolean, server_default="false")


class BodyCompositionLog(Base):
    __tablename__ = "body_composition_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    measured_at: Mapped[datetime] = mapped_column(index=True)
    weight_kg: Mapped[Decimal] = mapped_column(Numeric(5, 2))
    body_fat_pct: Mapped[Decimal | None] = mapped_column(Numeric(4, 2))
    muscle_mass_kg: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    visceral_fat: Mapped[int | None]
    bmr_kcal: Mapped[int | None]


class MealLog(Base):
    __tablename__ = "meal_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(index=True)
    meal_type: Mapped[str] = mapped_column(String(10))
    photo_storage_uri: Mapped[str | None] = mapped_column(Text)
    food_items_payload: Mapped[dict | list | None] = mapped_column(JSONB)
    total_calories: Mapped[int | None]
    total_protein_g: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    total_carbs_g: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    total_fat_g: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    sugar_g: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    caffeine_mg: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    nutri_score: Mapped[str | None] = mapped_column(String(2))
    transaction_ref: Mapped[int | None] = mapped_column(ForeignKey("transactions.id"))


class BiomarkerMetric(Base):
    __tablename__ = "biomarker_metrics"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    log_date: Mapped[date] = mapped_column(Date, unique=True)
    cumulative_caffeine_mg: Mapped[Decimal] = mapped_column(Numeric(6, 2), server_default="0")
    cumulative_sugar_g: Mapped[Decimal] = mapped_column(Numeric(6, 2), server_default="0")
    cumulative_sodium_mg: Mapped[Decimal] = mapped_column(Numeric(6, 2), server_default="0")
    sleep_duration_hours: Mapped[Decimal | None] = mapped_column(Numeric(4, 2))
    deep_sleep_hours: Mapped[Decimal | None] = mapped_column(Numeric(4, 2))
    resting_heart_rate: Mapped[int | None]
    hrv_score: Mapped[int | None]
    caffeine_limit_breached: Mapped[bool] = mapped_column(Boolean, server_default="false")
    sugar_limit_breached: Mapped[bool] = mapped_column(Boolean, server_default="false")
    health_penalty_applied: Mapped[Decimal] = mapped_column(server_default="0")


class SupplementRegimen(Base):
    __tablename__ = "supplement_regimen"

    id: Mapped[int] = mapped_column(primary_key=True)
    supplement_name: Mapped[str] = mapped_column(String(96))
    dosage: Mapped[str] = mapped_column(String(48))
    schedule_timing: Mapped[str] = mapped_column(String(24))
    # morning_with_fats|post_workout|evening_before_sleep
    current_stock: Mapped[int]
    daily_pills: Mapped[int] = mapped_column(server_default="1")
    low_stock_warning: Mapped[bool] = mapped_column(Boolean, server_default="false")
    last_taken_at: Mapped[datetime | None]


class HealthWarning(Base):
    __tablename__ = "health_warnings"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    timestamp: Mapped[created_at_col]
    warning_type: Mapped[str] = mapped_column(String(24))
    # caffeine_spike|sugar_overload|sleep_deficit|junk_food_penalty
    message: Mapped[str] = mapped_column(Text)
    action_taken: Mapped[str | None] = mapped_column(Text)
    acknowledged: Mapped[bool] = mapped_column(Boolean, server_default="false")
