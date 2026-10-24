"""Pydantic models for the blueprint's section 5A structs.

These are the single source of truth; web/src/lib/api-schema.d.ts is generated from the
FastAPI OpenAPI document (`npm run gen:api` in web/).
"""

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

ReconciliationStatus = Literal[
    "dual_verified", "stitched_multi_page", "single_verified", "split_reconciled"
]
AttachmentRole = Literal[
    "receipt_primary",
    "receipt_continuation",
    "payment_proof_slip",
    "product_nutrition_label",
    "asset_warranty_card",
    "tax_invoice_pdf",
]
Wallet = Literal["cash_wallet", "bank_bca", "bank_mandiri", "gopay", "ovo"]


class LocationContext(BaseModel):
    latitude: float
    longitude: float
    venue_name: str | None = None


class EvidenceAttachmentItem(BaseModel):
    attachment_id: UUID
    sequence_index: int
    attachment_role: AttachmentRole
    storage_uri: str
    sha256_checksum: str = Field(min_length=64, max_length=64)
    ocr_extracted_data: dict = Field(default_factory=dict)


class CompositeEvidenceBundle(BaseModel):
    bundle_id: UUID
    transaction_ref_id: int | None = None
    user_caption_text: str | None = None
    voice_transcript_text: str | None = None
    location_context: LocationContext | None = None
    attachments: list[EvidenceAttachmentItem] = Field(min_length=1)
    reconciliation_status: ReconciliationStatus
    created_at: datetime


class ManualTextInput(BaseModel):
    input_id: UUID
    raw_input_text: str
    parsed_amount: Decimal
    parsed_merchant: str
    parsed_category: str
    source_wallet: Wallet
    input_channel: Literal["telegram_shorthand", "telegram_nlp", "voice_transcribe"]
    tags: list[str] = Field(default_factory=list)
    timestamp: datetime
    confidence_score: float = Field(ge=0, le=1)  # 1.0 regex, >= 0.85 NLP


class SplitReceivable(BaseModel):
    debtor_name: str
    share_amount: Decimal
    settlement_status: Literal["pending", "settled", "waived"] = "pending"
    settled_at: datetime | None = None


class SplitBill(BaseModel):
    split_id: UUID
    transaction_id: int
    total_bill_amount: Decimal
    user_personal_share: Decimal
    receivables: list[SplitReceivable]


class SleepSessionTelemetry(BaseModel):
    sleep_date: str
    sleep_score: int = Field(ge=0, le=100)
    total_sleep_minutes: int
    deep_sleep_percentage: float
    light_sleep_percentage: float
    rem_sleep_percentage: float
    awake_minutes: int
    breathing_quality_score: int
    nap_sessions_count: int = 0
    fatigue_defense_triggered: bool = False


class StressTelemetry(BaseModel):
    timestamp: datetime
    stress_score: int = Field(ge=1, le=100)
    stress_grade: Literal["RELAXED", "NORMAL", "MEDIUM", "HIGH"]
    consecutive_high_stress_hours: float = 0
    stress_shield_active: bool = False


class VitalSigns(BaseModel):
    resting_heart_rate_bpm: int
    current_heart_rate_bpm: int
    spo2_percentage: float
    skin_temp_celsius: float


class DailyActivity(BaseModel):
    step_count: int
    active_calories_kcal: int
    total_distance_meters: float
    moderate_high_intensity_minutes: int


class BodyCompositionTelemetry(BaseModel):
    measured_at: datetime
    weight_kg: float
    body_fat_percentage: float
    muscle_mass_kg: float
    visceral_fat_level: int
    bmr_kcal: int


class WorkoutSessionTelemetry(BaseModel):
    workout_id: str
    workout_type: Literal["RUNNING", "STRENGTH_TRAINING", "CYCLING", "SWIMMING", "HIIT"]
    start_time: datetime
    duration_minutes: int
    calories_burned_kcal: int
    avg_heart_rate_bpm: int
    max_heart_rate_bpm: int
    recovery_hours_needed: int


class HuaweiHealthSyncPayload(BaseModel):
    sync_timestamp: datetime
    device_model: str
    battery_level: int
    sleep_telemetry: SleepSessionTelemetry | None = None
    stress_telemetry: StressTelemetry | None = None
    vital_signs: VitalSigns
    daily_activity: DailyActivity
    body_composition: BodyCompositionTelemetry | None = None


class DetectedFood(BaseModel):
    food_name: str
    portion_grams: float
    calories_kcal: float
    protein_g: float
    carbs_g: float
    fat_g: float
    sugar_g: float
    sodium_mg: float
    glycemic_index: Literal["LOW", "MEDIUM", "HIGH"]


class MealAnalysis(BaseModel):
    meal_id: UUID
    timestamp: datetime
    meal_photo_uri: str | None = None
    detected_foods: list[DetectedFood]
    total_calories: float
    nutri_score: Literal["A", "B", "C", "D", "E"]
    caffeine_mg: float
    sugar_total_g: float
    audit_verdict: str


class SupplementItem(BaseModel):
    supplement_id: UUID
    name: str
    dosage_per_serving: str
    intake_timing: Literal["morning_with_fats", "post_workout", "evening_before_sleep"]
    current_stock_capsules: int
    daily_consumption: int
    reorder_threshold_days: int = 7
    estimated_stock_depletion_date: str


class NotificationIn(BaseModel):
    """Payload posted by the Android NotificationListenerService (already PII-filtered on device)."""

    idempotency_key: str = Field(min_length=8, max_length=128)
    package_name: str
    title: str | None = None
    text: str
    posted_at: datetime
