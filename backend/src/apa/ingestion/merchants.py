"""merchant_cache learning (decisions.md D2): history is CONTEXT for agents and the deviation
breaker, never a reason to skip the debate.

Call `record_visit` when a transaction is committed (Phase 3) and `remove_visit` on /undo, so
quarantined or rolled-back entries never skew the statistics.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import any_, literal, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from apa.core.format import rupiah
from apa.db.models.budget import MerchantCache

# Below this many visits the stddev is too noisy to drive the >3-sigma deviation breaker.
MIN_SAMPLES_FOR_DEVIATION = 5

_NOISE = re.compile(r"[^a-z0-9 ]")
_LEGAL_PREFIX = re.compile(r"^(pt|cv|ud|toko)\s+")
_TRAILING_STORE_NO = re.compile(r"(\s+(?:no\s*)?\d{2,})+$")


def normalize_merchant_key(name: str) -> str:
    """'INDOMARET 1234' / 'Indomaret  Kemang' -> 'indomaret' / 'indomaret kemang'."""
    s = _NOISE.sub(" ", name.lower())
    s = re.sub(r"\s+", " ", s).strip()
    s = _LEGAL_PREFIX.sub("", s)
    s = _TRAILING_STORE_NO.sub("", s)
    return s.strip()


@dataclass(frozen=True)
class MerchantStats:
    visit_count: int
    avg_amount: Decimal | None
    stddev_amount: Decimal | None
    visits_per_week: Decimal | None

    @property
    def usable_for_deviation(self) -> bool:
        return self.visit_count >= MIN_SAMPLES_FOR_DEVIATION and bool(self.stddev_amount)


def derive_stats(n: int, total: Decimal, sumsq: Decimal) -> tuple[Decimal | None, Decimal | None]:
    """Mean and sample stddev from exact sums. stddev is None for n < 2."""
    if n <= 0:
        return None, None
    mean = total / n
    if n < 2:
        return mean, None
    variance = (sumsq - total * total / n) / (n - 1)
    return mean, (max(variance, Decimal(0))).sqrt()


def visits_per_week(n: int, first_seen: datetime | None, last_seen: datetime | None) -> Decimal | None:
    if n <= 0 or first_seen is None or last_seen is None:
        return None
    weeks = max(Decimal((last_seen - first_seen).total_seconds()) / Decimal(7 * 86400), Decimal(1))
    return Decimal(n) / weeks


def _apply(row: MerchantCache) -> None:
    mean, std = derive_stats(row.visit_count, row.amount_sum, row.amount_sumsq)
    row.avg_amount = mean.quantize(Decimal("0.01")) if mean is not None else None
    row.stddev_amount = std.quantize(Decimal("0.01")) if std is not None else None
    vpw = visits_per_week(row.visit_count, row.first_seen_at, row.last_seen_at)
    row.visits_per_week = vpw.quantize(Decimal("0.01")) if vpw is not None else None


async def _locked_row(session: AsyncSession, key: str) -> MerchantCache | None:
    return (
        await session.execute(
            select(MerchantCache).where(MerchantCache.merchant_key == key).with_for_update()
        )
    ).scalar_one_or_none()


async def record_visit(
    session: AsyncSession,
    merchant: str,
    amount: Decimal,
    when: datetime,
    *,
    category: str | None = None,
    agent: str | None = None,
    verified: bool = False,
) -> MerchantCache | None:
    """Upsert the merchant and fold one visit into its statistics. Returns None for blank names.
    `verified`: a full debate produced this category, which (re)starts the merchant's fast-path TTL."""
    key = normalize_merchant_key(merchant)
    if not key:
        return None
    # Concurrent first visits race on the unique key: insert-or-ignore, then lock and update.
    await session.execute(
        insert(MerchantCache)
        .values(merchant_key=key, display_name=merchant.strip()[:128], source="learned")
        .on_conflict_do_nothing(index_elements=["merchant_key"])
    )
    row = await _locked_row(session, key)
    assert row is not None
    row.visit_count += 1
    row.amount_sum += amount
    row.amount_sumsq += amount * amount
    row.first_seen_at = min(filter(None, [row.first_seen_at, when]))
    row.last_seen_at = max(filter(None, [row.last_seen_at, when]))
    # Learn a default only where nothing (including a manual rule) is set; never override.
    if category and not row.default_category:
        row.default_category = category
    if agent and not row.default_agent:
        row.default_agent = agent
    if verified:
        row.last_verified_at = max(filter(None, [row.last_verified_at, when]))
    _apply(row)
    return row


async def set_default_category(
    session: AsyncSession, merchant: str, category: str, when: datetime
) -> MerchantCache | None:
    """The owner corrected a category: it becomes the merchant's default and counts as verified now.
    Unlike record_visit this DOES override, because a human decision beats a learned guess."""
    row = await find_merchant(session, merchant)
    if row is None:
        return None
    row = await _locked_row(session, row.merchant_key)
    row.default_category, row.last_verified_at = category, when
    return row


async def remove_visit(session: AsyncSession, merchant: str, amount: Decimal) -> MerchantCache | None:
    """Inverse of record_visit, for /undo. No-op when the merchant is unknown or has no visits."""
    key = normalize_merchant_key(merchant)
    row = await _locked_row(session, key) if key else None
    if row is None or row.visit_count <= 0:
        return None
    row.visit_count -= 1
    row.amount_sum -= amount
    row.amount_sumsq -= amount * amount
    if row.visit_count == 0:
        row.amount_sum, row.amount_sumsq = Decimal(0), Decimal(0)
    _apply(row)
    return row


async def find_merchant(session: AsyncSession, merchant: str) -> MerchantCache | None:
    key = normalize_merchant_key(merchant)
    if not key:
        return None
    return (
        await session.execute(
            select(MerchantCache)
            .where(or_(MerchantCache.merchant_key == key, literal(key) == any_(MerchantCache.match_patterns)))
            .limit(1)
        )
    ).scalar_one_or_none()


async def get_stats(session: AsyncSession, merchant: str) -> MerchantStats | None:
    row = await find_merchant(session, merchant)
    if row is None:
        return None
    return MerchantStats(row.visit_count, row.avg_amount, row.stddev_amount, row.visits_per_week)


async def baseline_context(session: AsyncSession, merchant: str) -> str | None:
    """One line injected into agent prompts as historical baseline (blueprint 4.2-A)."""
    row = await find_merchant(session, merchant)
    if row is None or row.visit_count < 2 or row.avg_amount is None:
        return None
    parts = []
    if row.visits_per_week is not None:
        parts.append(f"rata-rata dikunjungi {row.visits_per_week:g}x/minggu")
    parts.append(f"dengan rata-rata nominal {rupiah(row.avg_amount)}")
    if row.default_category:
        parts.append(f"biasanya kategori {row.default_category}")
    return f"Merchant ini {' '.join(parts)} ({row.visit_count} kunjungan tercatat)."
