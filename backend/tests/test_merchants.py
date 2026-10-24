import statistics
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from apa.ingestion.merchants import (
    MIN_SAMPLES_FOR_DEVIATION,
    MerchantStats,
    derive_stats,
    normalize_merchant_key,
    visits_per_week,
)


@pytest.mark.parametrize(
    ("raw", "key"),
    [
        ("INDOMARET 1234", "indomaret"),
        ("Indomaret  Kemang", "indomaret kemang"),
        ("PT Sumber Alfaria", "sumber alfaria"),
        ("Warkop Pak Ujang!", "warkop pak ujang"),
        ("Alfamart No 0451", "alfamart"),
        ("   ", ""),
    ],
)
def test_normalize_merchant_key(raw, key):
    assert normalize_merchant_key(raw) == key


def test_derive_stats_matches_statistics_module():
    xs = [Decimal(x) for x in (18000, 22000, 20000, 25000, 15000, 19000)]
    mean, std = derive_stats(len(xs), sum(xs), sum(x * x for x in xs))
    assert mean == sum(xs) / len(xs)
    assert abs(float(std) - statistics.stdev(float(x) for x in xs)) < 1e-6


def test_derive_stats_edge_cases():
    assert derive_stats(0, Decimal(0), Decimal(0)) == (None, None)
    assert derive_stats(1, Decimal(100), Decimal(10000)) == (Decimal(100), None)
    assert derive_stats(3, Decimal(300), Decimal(30000))[1] == 0  # identical amounts


def test_visits_per_week_uses_at_least_a_one_week_window():
    t0 = datetime(2026, 10, 1, tzinfo=UTC)
    assert visits_per_week(3, t0, t0 + timedelta(days=1)) == 3  # not inflated to 21/week
    assert visits_per_week(8, t0, t0 + timedelta(weeks=4)) == 2
    assert visits_per_week(0, t0, t0) is None


def test_deviation_needs_enough_samples():
    few = MerchantStats(MIN_SAMPLES_FOR_DEVIATION - 1, Decimal(100), Decimal(10), Decimal(1))
    enough = MerchantStats(MIN_SAMPLES_FOR_DEVIATION, Decimal(100), Decimal(10), Decimal(1))
    flat = MerchantStats(10, Decimal(100), Decimal(0), Decimal(1))
    assert not few.usable_for_deviation and enough.usable_for_deviation
    assert not flat.usable_for_deviation  # zero variance cannot define a sigma
