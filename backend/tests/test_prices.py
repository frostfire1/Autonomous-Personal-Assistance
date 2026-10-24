"""Price intelligence, the pure part: unit prices from receipt lines, the anomaly rule, the monthly cost."""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from apa.prices.engine import (
    DEFAULT_BASKET,
    Anomaly,
    classify,
    detect,
    monthly_impact,
    personal_index,
    unit_price,
)

D = Decimal
TODAY = date(2026, 10, 24)


@pytest.mark.parametrize(
    ("name", "code"),
    [("Beras Premium 5kg", "beras"), ("TELUR AYAM 1kg", "telur"), ("Daging Ayam Segar", "ayam"),
     ("Minyak Goreng Bimoli 2L", "minyak"), ("Gula Pasir 1kg", "gula"), ("Cabe Rawit", "cabai"),
     ("Bawang Merah", "bawang_merah"), ("Bawang Putih", "bawang_putih"), ("Kopi Susu", None), ("Ayam Geprek", None)],
)
def test_items_are_classified_by_keyword(name, code):
    c = classify(name)
    assert (c.code if c else None) == code


def test_the_basket_codes_are_unique_and_priced():
    assert len({c.code for c in DEFAULT_BASKET}) == len(DEFAULT_BASKET)
    assert all(c.baseline_price > 0 and c.monthly_qty > 0 for c in DEFAULT_BASKET)


@pytest.mark.parametrize(
    ("name", "qty", "total", "expected"),
    [("Beras Premium 5kg", 1, 68000, ("beras", 13600)),
     ("Beras 5 kg", 2, 136000, ("beras", 13600)),
     ("Telur Ayam 500gr", 1, 16000, ("telur", 32000)),
     ("Minyak Goreng 2L", 1, 40000, ("minyak", 20000)),
     ("Minyak Goreng 500ml", 1, 11000, ("minyak", 22000)),
     ("Beras Premium", 1, 68000, None),  # no size printed: a price per 'pack' means nothing
     ("Minyak Goreng 2kg", 1, 40000, None),  # a weight for a liquid: the unit does not match
     ("Kopi 250gr", 1, 20000, None),  # not a tracked staple
     ("Beras 5kg", 0, 68000, None), ("Beras 5kg", 1, 0, None)],
)
def test_unit_prices_need_a_printed_size_in_the_right_unit(name, qty, total, expected):
    got = unit_price(name, D(qty), D(total))
    assert got == ((expected[0], D(expected[1])) if expected else None)


def series(prices_by_days_ago):
    return [(TODAY - timedelta(days=d), D(p)) for d, p in prices_by_days_ago]


BASE = [(d, 10000) for d in (30, 25, 20, 15, 12, 10)]


def test_a_rise_over_ten_percent_seen_twice_is_an_anomaly():
    a = detect("beras", series(BASE + [(3, 12000), (1, 12200)]), TODAY)
    assert isinstance(a, Anomaly) and a.code == "beras" and a.baseline == D(10000) and a.recent == D(12100)
    assert a.rise == D("0.210")


def test_exactly_at_the_threshold_is_not_a_rise():
    assert detect("beras", series(BASE + [(3, 11000), (1, 11000)]), TODAY) is None


def test_one_expensive_observation_is_not_enough():
    assert detect("beras", series(BASE + [(1, 15000)]), TODAY) is None


def test_a_single_spike_followed_by_a_normal_price_is_not_an_alarm():
    assert detect("beras", series(BASE + [(3, 15000), (1, 10100)]), TODAY) is None
    assert detect("beras", series(BASE + [(3, 10100), (1, 15000)]), TODAY) is None  # the earlier of the two must be high too


def test_without_a_baseline_nothing_can_be_compared():
    assert detect("beras", series([(10, 10000), (3, 20000), (1, 20000)]), TODAY) is None


def test_old_and_future_points_are_not_recent():
    assert detect("beras", series(BASE + [(8, 12000), (9, 12000)]), TODAY) is None  # those fall in the baseline window
    assert detect("beras", series(BASE + [(-1, 12000), (-2, 12000)]), TODAY) is None


def test_a_fall_is_never_an_anomaly():
    assert detect("beras", series(BASE + [(3, 7000), (1, 7000)]), TODAY) is None


def test_input_order_does_not_matter():
    s = series(BASE + [(3, 12000), (1, 12200)])
    assert detect("beras", list(reversed(s)), TODAY) == detect("beras", s, TODAY)


def test_the_monthly_cost_is_quantity_times_the_rise_per_unit():
    a = [Anomaly("beras", D(12100), D(10000), D("0.21")), Anomaly("telur", D(33000), D(30000), D("0.1"))]
    assert monthly_impact(a, {"beras": D(8), "telur": D(3)}) == D(8 * 2100 + 3 * 3000)
    assert monthly_impact(a, {}) == D(0)  # a commodity the owner does not buy costs nothing


def test_the_personal_index_compares_the_owners_basket_with_its_baseline():
    qty = {"beras": D(8), "telur": D(2)}
    assert personal_index({"beras": D(15000), "telur": D(30000)}, {"beras": D(10000), "telur": D(30000)}, qty) == D("1.286")
    assert personal_index({}, {"beras": D(10000)}, qty) is None
    assert personal_index({"beras": D(10000)}, {"beras": D(10000)}, qty) == D("1.000")
