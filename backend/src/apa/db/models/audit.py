"""Explainability log (append-only, hash-chained), debate transcripts, reconciliation log."""

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Numeric,
    SmallInteger,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import ARRAY, CHAR, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from apa.db.base import Base, created_at_col

DECISION_TYPES = (
    "ledger_commit",
    "quarantine",
    "undo",
    "release",
    "dismiss",
    "fusion",
    "budget_rebalance",
    "health_interlock",
    "reconciliation",
    "config_change",
    "edit",  # an owner-confirmed transaction edit made through chat
    "budget_set",  # an owner-confirmed budget change made through chat
    "repair",  # an automatic data repair by the steward
)


class ExplainabilityLog(Base):
    """Append-only. UPDATE/DELETE are blocked by a trigger and revoked from the app role
    (see the initial migration). `row_hash` chains rows for tamper evidence."""

    __tablename__ = "explainability_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    debate_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), unique=True)
    transaction_id: Mapped[int | None] = mapped_column(ForeignKey("transactions.id"), index=True)
    decision_type: Mapped[str] = mapped_column(String(32))
    agents_invoked: Mapped[list[str]] = mapped_column(ARRAY(String(5)), server_default="{}")
    agent_scores: Mapped[dict] = mapped_column(JSONB, server_default="{}")
    consensus_formula: Mapped[str | None] = mapped_column(String(32))
    final_score: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    verdict: Mapped[str] = mapped_column(String(64))
    justification: Mapped[str] = mapped_column(Text)
    policy_version: Mapped[int | None]
    total_tokens: Mapped[int | None]
    cost_idr: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    created_at: Mapped[created_at_col]
    prev_hash: Mapped[str | None] = mapped_column(CHAR(64))
    row_hash: Mapped[str] = mapped_column(CHAR(64))

    __table_args__ = (
        CheckConstraint(
            "decision_type IN (" + ",".join(f"'{d}'" for d in DECISION_TYPES) + ")",
            name="decision_type_valid",
        ),
    )


class AgentDebateTranscript(Base):
    __tablename__ = "agent_debate_transcripts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    debate_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    subject_type: Mapped[str] = mapped_column(String(16))
    subject_ref: Mapped[int | None] = mapped_column(BigInteger)
    round_no: Mapped[int] = mapped_column(SmallInteger, server_default="1")
    agent_id: Mapped[str] = mapped_column(String(5))
    model: Mapped[str] = mapped_column(String(48))
    stance: Mapped[str | None] = mapped_column(String(8))
    score: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    argument: Mapped[str] = mapped_column(Text)
    structured_output: Mapped[dict | None] = mapped_column(JSONB)
    prompt_tokens: Mapped[int | None]
    completion_tokens: Mapped[int | None]
    cached_tokens: Mapped[int | None]
    latency_ms: Mapped[int | None]
    created_at: Mapped[created_at_col]

    __table_args__ = (
        Index("ix_agent_debate_transcripts_debate_round", "debate_id", "round_no"),
        CheckConstraint("subject_type IN ('transaction','meal','health_event')", name="subject_valid"),
        CheckConstraint("stance IN ('approve','object','abstain')", name="stance_valid"),
    )


class ReconciliationLog(Base):
    __tablename__ = "reconciliation_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    wallet: Mapped[str] = mapped_column(String(32))
    statement_storage_uri: Mapped[str] = mapped_column(Text)
    statement_sha256: Mapped[str] = mapped_column(CHAR(64))
    statement_line: Mapped[dict] = mapped_column(JSONB)
    statement_date: Mapped[date] = mapped_column(Date)
    statement_amount: Mapped[Decimal]
    match_tier: Mapped[int | None] = mapped_column(SmallInteger)
    match_type: Mapped[str] = mapped_column(String(20))
    matched_transaction_id: Mapped[int | None] = mapped_column(ForeignKey("transactions.id"))
    fee_amount: Mapped[Decimal | None]
    created_transaction_id: Mapped[int | None] = mapped_column(ForeignKey("transactions.id"))
    date_diff_days: Mapped[int | None] = mapped_column(SmallInteger)
    created_at: Mapped[created_at_col]

    __table_args__ = (
        CheckConstraint("match_tier IN (1,2,3)", name="match_tier_valid"),
        CheckConstraint(
            "match_type IN ('exact','fuzzy_fee','split','synthetic_catchup','unmatched')",
            name="match_type_valid",
        ),
    )
