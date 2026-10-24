"""News, cyber advisories, homelab watchlist, research reports."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, CheckConstraint, Numeric, String, Text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from apa.db.base import Base, created_at_col


class NewsArticle(Base):
    __tablename__ = "news_articles"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    dedup_hash: Mapped[str] = mapped_column(String(64), unique=True)  # sha256(title|url)
    source: Mapped[str] = mapped_column(String(96))
    published_at: Mapped[datetime | None]
    body: Mapped[str | None] = mapped_column(Text)
    relevance: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    triage_status: Mapped[str] = mapped_column(String(12), server_default="new")
    fetched_at: Mapped[created_at_col]


class CyberAdvisory(Base):
    __tablename__ = "cyber_advisories"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    cve_id: Mapped[str] = mapped_column(String(24), unique=True)
    summary: Mapped[str] = mapped_column(Text)
    cvss: Mapped[Decimal | None] = mapped_column(Numeric(3, 1))
    epss: Mapped[Decimal | None] = mapped_column(Numeric(6, 5))
    in_cisa_kev: Mapped[bool] = mapped_column(Boolean, server_default="false")
    affected_products: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}")
    tier: Mapped[str] = mapped_column(String(8), server_default="archive")
    briefed_at: Mapped[datetime | None]
    source: Mapped[str] = mapped_column(String(32))
    fetched_at: Mapped[created_at_col]

    __table_args__ = (
        CheckConstraint("tier IN ('urgent','normal','archive')", name="tier_valid"),
    )


class HomelabWatch(Base):
    __tablename__ = "homelab_watchlist"

    id: Mapped[int] = mapped_column(primary_key=True)
    product: Mapped[str] = mapped_column(String(96), unique=True)  # Proxmox VE, Docker, pfSense...
    cpe_hint: Mapped[str | None] = mapped_column(String(128))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true")


class ResearchReport(Base):
    __tablename__ = "research_reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    query: Mapped[str] = mapped_column(Text)
    depth: Mapped[str] = mapped_column(String(8), server_default="normal")
    report_md: Mapped[str] = mapped_column(Text)
    sources: Mapped[list] = mapped_column(JSONB, server_default="[]")
    created_at: Mapped[created_at_col]


class Briefing(Base):
    """A composed voice briefing (blueprint v2 briefing_queue). `text` is what the Android app reads aloud."""

    __tablename__ = "briefings"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(String(8))  # short | full
    lang: Mapped[str] = mapped_column(String(2), server_default="id")  # id | en: the briefing language
    text: Mapped[str] = mapped_column(Text)
    sections: Mapped[list] = mapped_column(JSONB, server_default="[]")  # [{topic, text}]: skip a topic
    created_at: Mapped[created_at_col]
    played_at: Mapped[datetime | None]

    __table_args__ = (CheckConstraint("kind IN ('short','full')", name="kind_valid"),)
