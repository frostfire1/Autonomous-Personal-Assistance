"""Multi-image evidence bundles (receipts, EDC slips, nutrition labels, warranty cards)."""

import uuid
from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, Index, Numeric, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from apa.db.base import Base, created_at_col

FILE_ROLES = (
    "receipt_primary",
    "receipt_continuation",
    "payment_proof_slip",
    "product_nutrition_label",
    "asset_warranty_card",
    "tax_invoice_pdf",
)


class EvidenceBundle(Base):
    __tablename__ = "transaction_evidence_bundles"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    transaction_id: Mapped[int | None] = mapped_column(ForeignKey("transactions.id"), index=True)
    bundle_caption: Mapped[str | None] = mapped_column(Text)
    voice_transcript: Mapped[str | None] = mapped_column(Text)
    venue_name: Mapped[str | None] = mapped_column(String(128))
    latitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    longitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    total_attachments: Mapped[int] = mapped_column(server_default="0")
    cross_verification_status: Mapped[str | None] = mapped_column(String(24))
    # dual_verified|stitched_multi_page|single_verified|split_reconciled
    created_at: Mapped[created_at_col]


class EvidenceFile(Base):
    __tablename__ = "transaction_evidence_files"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    bundle_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("transaction_evidence_bundles.id", ondelete="CASCADE"), index=True
    )
    sequence_index: Mapped[int]
    file_role: Mapped[str] = mapped_column(String(32))
    file_storage_uri: Mapped[str] = mapped_column(Text)
    sha256_checksum: Mapped[str] = mapped_column(String(64), index=True)
    ocr_payload: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[created_at_col]

    __table_args__ = (
        UniqueConstraint("bundle_id", "sequence_index"),
        CheckConstraint(
            "file_role IN (" + ",".join(f"'{r}'" for r in FILE_ROLES) + ")", name="file_role_valid"
        ),
        # An image must never back two transactions: the DB-level half of "zero double-counting"
        # (intake also checks, but two photos can race past a check). It covers every receipt page and
        # payment slip, not just the first page: a slip reused would "verify" a second purchase.
        Index(
            "uq_evidence_image_sha",
            "sha256_checksum",
            unique=True,
            postgresql_where=text(
                "file_role IN ('receipt_primary', 'receipt_continuation', 'payment_proof_slip')"
            ),
        ),
    )
