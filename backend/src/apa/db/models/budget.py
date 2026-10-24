"""Policy matrix, merchant cache, budgets, rebalance log, price basket, macro assumptions, goals."""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from apa.db.base import Base, created_at_col


class PolicyRule(Base):
    __tablename__ = "policy_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    rule_key: Mapped[str] = mapped_column(String(48))
    scope: Mapped[str] = mapped_column(String(12), server_default="global")
    scope_ref: Mapped[str] = mapped_column(String(48), server_default="")
    value_numeric: Mapped[Decimal | None]
    value_json: Mapped[dict | None] = mapped_column(JSONB)
    unit: Mapped[str | None] = mapped_column(String(16))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true")
    version: Mapped[int] = mapped_column(server_default="1")
    updated_by: Mapped[str] = mapped_column(String(32))
    updated_at: Mapped[created_at_col]

    __table_args__ = (
        UniqueConstraint("rule_key", "scope", "scope_ref"),
        CheckConstraint("scope IN ('global','category','wallet')", name="scope_valid"),
    )


class MerchantCache(Base):
    __tablename__ = "merchant_cache"

    id: Mapped[int] = mapped_column(primary_key=True)
    merchant_key: Mapped[str] = mapped_column(String(96), unique=True)
    display_name: Mapped[str] = mapped_column(String(128))
    match_patterns: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}")
    default_category: Mapped[str | None] = mapped_column(String(48))
    default_agent: Mapped[str | None] = mapped_column(String(5))
    visit_count: Mapped[int] = mapped_column(server_default="0")
    visits_per_week: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    avg_amount: Mapped[Decimal | None]
    stddev_amount: Mapped[Decimal | None]
    # Exact running sums (IDR has no sub-unit drift), so avg/stddev are derivable and an undo
    # can subtract a visit. avg_amount/stddev_amount above are denormalised from these.
    amount_sum: Mapped[Decimal] = mapped_column(Numeric(20, 2), server_default="0")
    amount_sumsq: Mapped[Decimal] = mapped_column(Numeric(30, 4), server_default="0")
    first_seen_at: Mapped[datetime | None]
    health_profile: Mapped[dict | None] = mapped_column(JSONB)
    source: Mapped[str] = mapped_column(String(8), server_default="learned")
    last_seen_at: Mapped[datetime | None]
    # When a full debate (or the owner) last confirmed the category; the fast path lapses 30 days later
    last_verified_at: Mapped[datetime | None]
    updated_at: Mapped[created_at_col]

    __table_args__ = (CheckConstraint("source IN ('learned','manual')", name="source_valid"),)


class BudgetRule(Base):
    __tablename__ = "budget_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    category: Mapped[str] = mapped_column(String(48))
    period_month: Mapped[date] = mapped_column(Date)
    base_limit: Mapped[Decimal]
    current_limit: Mapped[Decimal]
    spent_amount: Mapped[Decimal] = mapped_column(server_default="0")
    is_flexible: Mapped[bool] = mapped_column(Boolean, server_default="false")
    is_essential: Mapped[bool] = mapped_column(Boolean, server_default="false")
    elastic_band_pct: Mapped[Decimal] = mapped_column(Numeric(4, 2), server_default="15")
    is_sinking_fund: Mapped[bool] = mapped_column(Boolean, server_default="false")

    __table_args__ = (
        UniqueConstraint("category", "period_month"),
        CheckConstraint(
            "is_sinking_fund OR current_limit BETWEEN base_limit * (1 - elastic_band_pct / 100) "
            "AND base_limit * (1 + elastic_band_pct / 100)",
            name="within_elastic_band",
        ),
    )


class BudgetRebalanceLog(Base):
    __tablename__ = "budget_rebalance_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    trigger_type: Mapped[str] = mapped_column(String(24))
    from_category: Mapped[str] = mapped_column(String(48))
    to_category: Mapped[str] = mapped_column(String(48))
    period_month: Mapped[date] = mapped_column(Date)
    amount: Mapped[Decimal]
    from_limit_before: Mapped[Decimal]
    from_limit_after: Mapped[Decimal]
    to_limit_before: Mapped[Decimal]
    to_limit_after: Mapped[Decimal]
    trigger_ref: Mapped[dict | None] = mapped_column(JSONB)
    explainability_id: Mapped[int] = mapped_column(ForeignKey("explainability_log.id"))
    created_at: Mapped[created_at_col]

    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint(
            "trigger_type IN ('pihps_spike','health_tax','stress_shield','fatigue_defense','web_ui')",
            name="trigger_type_valid",
        ),
    )


class PriceBasket(Base):
    __tablename__ = "price_basket"

    id: Mapped[int] = mapped_column(primary_key=True)
    commodity_code: Mapped[str] = mapped_column(String(32), unique=True)
    commodity_name: Mapped[str] = mapped_column(String(64))
    monthly_qty: Mapped[Decimal] = mapped_column(Numeric(8, 2))
    unit: Mapped[str] = mapped_column(String(8))
    pihps_region: Mapped[str] = mapped_column(String(48))
    baseline_price: Mapped[Decimal]
    budget_category: Mapped[str] = mapped_column(String(48), server_default="Groceries")
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true")


class CommodityPrice(Base):
    __tablename__ = "commodity_prices"

    commodity_code: Mapped[str] = mapped_column(
        ForeignKey("price_basket.commodity_code"), primary_key=True
    )
    pihps_region: Mapped[str] = mapped_column(String(48), primary_key=True)
    price_date: Mapped[date] = mapped_column(Date, primary_key=True)
    price: Mapped[Decimal]


class MacroAssumption(Base):
    __tablename__ = "macro_assumptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    assumption_key: Mapped[str] = mapped_column(String(48))
    value: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    unit: Mapped[str] = mapped_column(String(16))
    effective_from: Mapped[date] = mapped_column(Date)
    source: Mapped[str] = mapped_column(String(64))
    projected_monthly_impact: Mapped[Decimal | None]
    explainability_id: Mapped[int | None] = mapped_column(ForeignKey("explainability_log.id"))
    created_at: Mapped[created_at_col]

    __table_args__ = (UniqueConstraint("assumption_key", "effective_from"),)


class SavingsGoal(Base):
    __tablename__ = "savings_goals"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(96))
    target_amount: Mapped[Decimal]
    current_amount: Mapped[Decimal] = mapped_column(server_default="0")
    deadline: Mapped[date | None] = mapped_column(Date)
    budget_category: Mapped[str | None] = mapped_column(String(48))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true")
    notes: Mapped[str | None] = mapped_column(Text)
