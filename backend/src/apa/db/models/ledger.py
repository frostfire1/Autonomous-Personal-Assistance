"""Master ledger, cash wallets, line items, split bills, warranties."""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from apa.db.base import Base, created_at_col


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    timestamp: Mapped[datetime]
    source_channel: Mapped[str] = mapped_column(String(24))
    # android_listener|multi_image_receipt|manual_text|reconciliation
    payment_wallet: Mapped[str] = mapped_column(String(24))
    merchant: Mapped[str | None] = mapped_column(String(128))
    direction: Mapped[str] = mapped_column(String(3), server_default="out")  # out | in
    total_paid_amount: Mapped[Decimal]
    net_user_expense: Mapped[Decimal]  # excludes other people's split-bill share
    primary_category: Mapped[str | None] = mapped_column(String(48))
    responsible_agent: Mapped[str | None] = mapped_column(String(5))
    execution_mode: Mapped[str] = mapped_column(String(24))
    # consensus_auto|circuit_override|reconciliation_catchup (decisions.md D2)
    evidence_bundle_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "transaction_evidence_bundles.id",
            use_alter=True,
            name="fk_transactions_evidence_bundle_id",
        ),
    )
    confidence_score: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    circuit_breaker_cleared: Mapped[bool] = mapped_column(Boolean, server_default="false")
    auto_reconciled: Mapped[bool] = mapped_column(Boolean, server_default="false")
    reversibility_deadline: Mapped[datetime | None]
    notes: Mapped[str | None] = mapped_column(Text)

    # Added by decisions.md D3 (soft reversal)
    status: Mapped[str] = mapped_column(String(16), server_default="committed")
    debate_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    rolled_back_at: Mapped[datetime | None]
    rolled_back_via: Mapped[str | None] = mapped_column(String(16))
    inbound_event_id: Mapped[int | None] = mapped_column(ForeignKey("inbound_events.id"))
    # Payments the owner confirmed together from one chat draft. The velocity breaker counts a batch once.
    batch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('committed','quarantined','rolled_back')", name="status_valid"
        ),
        CheckConstraint("rolled_back_via IN ('telegram','web_ui')", name="rolled_back_via_valid"),
        CheckConstraint("direction IN ('out','in')", name="direction_valid"),
        Index("ix_transactions_timestamp", "timestamp"),
        Index("ix_transactions_status", "status"),
        Index("ix_transactions_debate_id", "debate_id"),
    )


class CashWallet(Base):
    __tablename__ = "cash_wallets"

    id: Mapped[int] = mapped_column(primary_key=True)
    wallet_name: Mapped[str] = mapped_column(String(48), unique=True)
    current_balance: Mapped[Decimal]
    currency: Mapped[str] = mapped_column(String(3), server_default="IDR")
    last_transaction_ref: Mapped[int | None] = mapped_column(ForeignKey("transactions.id"))
    updated_at: Mapped[created_at_col]


class TransactionItem(Base):
    __tablename__ = "transaction_items"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    transaction_id: Mapped[int] = mapped_column(
        ForeignKey("transactions.id", ondelete="CASCADE"), index=True
    )
    evidence_file_ref: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("transaction_evidence_files.id")
    )
    item_name: Mapped[str] = mapped_column(String(160))
    normalized_item_name: Mapped[str | None] = mapped_column(String(160))
    quantity: Mapped[Decimal] = mapped_column(Numeric(8, 2))
    unit_price: Mapped[Decimal]
    total_price: Mapped[Decimal]
    category: Mapped[str | None] = mapped_column(String(48))
    nutrition_grade: Mapped[str | None] = mapped_column(String(2))
    sugar_grams: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    caffeine_mg: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    health_audit_note: Mapped[str | None] = mapped_column(Text)


class TransactionSplit(Base):
    __tablename__ = "transaction_splits"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    transaction_id: Mapped[int] = mapped_column(
        ForeignKey("transactions.id", ondelete="CASCADE"), index=True
    )
    debtor_name: Mapped[str] = mapped_column(String(64))
    share_amount: Mapped[Decimal]
    settlement_status: Mapped[str] = mapped_column(String(8), server_default="pending")
    settled_transaction_ref: Mapped[int | None] = mapped_column(ForeignKey("transactions.id"))
    settled_at: Mapped[datetime | None]

    __table_args__ = (
        CheckConstraint(
            "settlement_status IN ('pending','settled','waived')", name="settlement_status_valid"
        ),
    )


class AssetWarranty(Base):
    __tablename__ = "asset_warranties"

    id: Mapped[int] = mapped_column(primary_key=True)
    transaction_id: Mapped[int] = mapped_column(ForeignKey("transactions.id"), index=True)
    item_name: Mapped[str] = mapped_column(String(160))
    serial_number: Mapped[str | None] = mapped_column(String(96))
    vendor_distributor: Mapped[str | None] = mapped_column(String(96))
    warranty_duration_months: Mapped[int | None]
    purchase_date: Mapped[date | None] = mapped_column(Date)
    warranty_expiry_date: Mapped[date | None] = mapped_column(Date, index=True)
    evidence_file_ref: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("transaction_evidence_files.id")
    )
    created_at: Mapped[created_at_col]
