"""News, cyber advisories, homelab watchlist, research reports."""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
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


class MemoryItem(Base):
    """What the assistant remembers about the owner, one row each: a note the owner asked it to keep, something it learned
    from the chats (person, preference, goal, routine, plan, health), a day's diary summary, or an entry of the owner's own
    private journal. `text` is Fernet ciphertext when `enc` is true; `shared` says whether the assistant may read the entry."""

    __tablename__ = "memory_items"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(String(12))  # note|diary|journal|person|preference|goal|routine|plan|health|other
    text: Mapped[str] = mapped_column(Text)
    day: Mapped[date | None] = mapped_column(Date)
    source: Mapped[str] = mapped_column(String(8), server_default="owner")  # owner | auto
    enc: Mapped[bool] = mapped_column(Boolean, server_default="false")
    shared: Mapped[bool] = mapped_column(Boolean, server_default="true")
    created_at: Mapped[created_at_col]

    __table_args__ = (Index("ix_memory_items_kind_day", "kind", "day"),)


class ProfileTrait(Base):
    """A pattern computed from the owner's own data (sleep, meals, spending, activity, interests): code computes it, the
    assistant only words it. Recomputed every night."""

    __tablename__ = "profile_traits"

    key: Mapped[str] = mapped_column(String(48), primary_key=True)
    summary: Mapped[str] = mapped_column(Text)
    data: Mapped[dict] = mapped_column(JSONB, server_default="{}")
    evidence: Mapped[int] = mapped_column(Integer, server_default="0")  # how many observations it rests on
    computed_at: Mapped[datetime] = mapped_column(server_default=func.now())


class AppUsage(Base):
    """Active seconds per app, per day and device (ActivityWatch or anything that pushes the same shape). App names only."""

    __tablename__ = "app_usage"
    __table_args__ = (UniqueConstraint("day", "device", "app", name="uq_app_usage"), Index("ix_app_usage_day", "day"))

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    day: Mapped[date] = mapped_column(Date)
    device: Mapped[str] = mapped_column(String(40))
    kind: Mapped[str] = mapped_column(String(12), server_default="other")
    app: Mapped[str] = mapped_column(String(120))
    seconds: Mapped[int] = mapped_column(Integer)
    first_at: Mapped[datetime | None]
    last_at: Mapped[datetime | None]
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())
