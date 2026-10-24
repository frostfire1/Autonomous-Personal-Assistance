"""Operational tables: raw inbound events, Telegram message map, OAuth tokens."""

from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from apa.db.base import Base, created_at_col


class InboundEvent(Base):
    """Sanitized raw event store. Phase 1 gate: notification -> row in < 200 ms.

    `idempotency_key` makes replays from the Android offline buffer safe.
    """

    __tablename__ = "inbound_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    source: Mapped[str] = mapped_column(String(24))  # android_listener|telegram_*|huawei|reconciliation
    payload: Mapped[dict] = mapped_column(JSONB)  # already PII-stripped
    injection_flags: Mapped[list] = mapped_column(JSONB, server_default="[]")
    status: Mapped[str] = mapped_column(String(16), server_default="received")
    received_at: Mapped[created_at_col]
    processed_at: Mapped[datetime | None]
    error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_inbound_events_status", "status"),)


class TelegramMessage(Base):
    """Maps outbound summaries to transactions so `/undo_8f9a2` short tokens resolve."""

    __tablename__ = "telegram_messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    direction: Mapped[str] = mapped_column(String(3))  # in | out
    chat_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    transaction_id: Mapped[int | None] = mapped_column(ForeignKey("transactions.id"))
    # The inbound message this answers, so the conversation can be read back (apa.inspector)
    inbound_event_id: Mapped[int | None] = mapped_column(ForeignKey("inbound_events.id"), index=True)
    undo_token: Mapped[str | None] = mapped_column(String(16), unique=True)
    kind: Mapped[str] = mapped_column(String(24))  # summary|alert|digest|health_warning|briefing
    # Body is stored only for non-/private traffic
    body: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[created_at_col]


class OAuthToken(Base):
    """Huawei Health Kit tokens. `*_enc` columns hold app-level encrypted values."""

    __tablename__ = "oauth_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(24), unique=True)
    access_token_enc: Mapped[str] = mapped_column(Text)
    refresh_token_enc: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime]
    scope: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[created_at_col]
