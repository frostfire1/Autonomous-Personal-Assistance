"""Price intelligence, the deterministic part (blueprint v2 4.4b): turn receipt lines into unit prices, decide whether a
commodity's price has genuinely jumped, and size what that costs the owner each month. No database, no model.

Anomaly rule (v2 C): the recent average against the longer baseline, and only valid when it is seen in TWO
consecutive observations, so one scraping error or one expensive shop cannot raise an alarm."""

import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

THRESHOLD = Decimal("0.10")  # a rise of more than 10% (blueprint 4.4b)
RECENT_DAYS = 7
BASELINE_DAYS = 30
MIN_BASELINE_POINTS = 2


@dataclass(frozen=True)
class Commodity:
    code: str
    name: str
    unit: str  # 'kg' | 'l'
    monthly_qty: Decimal  # a starting guess for a household of one, editable in price_basket
    baseline_price: Decimal  # typical IDR per unit, replaced by the owner's own history as it accumulates
    keywords: tuple[str, ...]


DEFAULT_BASKET: tuple[Commodity, ...] = (
    Commodity("beras", "Beras", "kg", Decimal(8), Decimal(14000), ("beras",)),
    Commodity("telur", "Telur ayam", "kg", Decimal(3), Decimal(30000), ("telur",)),
    Commodity("ayam", "Daging ayam", "kg", Decimal(3), Decimal(38000), ("ayam potong", "daging ayam", "dada ayam", "paha ayam", "ayam segar")),
    Commodity("minyak", "Minyak goreng", "l", Decimal(3), Decimal(20000), ("minyak goreng", "minyak sawit", "bimoli", "fortune")),
    Commodity("gula", "Gula pasir", "kg", Decimal(2), Decimal(18000), ("gula pasir", "gula putih")),
    Commodity("cabai", "Cabai", "kg", Decimal(1), Decimal(45000), ("cabai", "cabe", "lombok")),
    Commodity("bawang_merah", "Bawang merah", "kg", Decimal(1), Decimal(40000), ("bawang merah",)),
    Commodity("bawang_putih", "Bawang putih", "kg", Decimal(1), Decimal(38000), ("bawang putih",)),
)

_SIZE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(kg|kilo|kilogram|gr|gram|g|l|liter|lt|ml)\b", re.I)
_TO_BASE = {"kg": Decimal(1), "kilo": Decimal(1), "kilogram": Decimal(1), "gr": Decimal("0.001"), "gram": Decimal("0.001"),
            "g": Decimal("0.001"), "l": Decimal(1), "liter": Decimal(1), "lt": Decimal(1), "ml": Decimal("0.001")}
_BASE_OF = {"kg": "kg", "kilo": "kg", "kilogram": "kg", "gr": "kg", "gram": "kg", "g": "kg", "l": "l", "liter": "l",
            "lt": "l", "ml": "l"}


def classify(item_name: str) -> Commodity | None:
    """The commodity an item line is, by keyword (longest keyword first so 'daging ayam' beats a stray 'ayam')."""
    n = item_name.lower()
    best: tuple[int, Commodity] | None = None
    for c in DEFAULT_BASKET:
        for kw in c.keywords:
            if kw in n and (best is None or len(kw) > best[0]):
                best = (len(kw), c)
    return best[1] if best else None


def unit_price(item_name: str, quantity: Decimal, total_price: Decimal) -> tuple[str, Decimal] | None:
    """('beras', 13600) from 'Beras Premium 5kg' x1 for 68.000. None when the item is not a tracked staple, the size
    is not printed (a price per 'pack' means nothing) or the unit does not match the commodity's."""
    c = classify(item_name)
    if c is None or quantity <= 0 or total_price <= 0:
        return None
    m = _SIZE.search(item_name)
    if m is None:
        return None
    unit = m.group(2).lower()
    if _BASE_OF[unit] != c.unit:
        return None
    size = Decimal(m.group(1).replace(",", ".")) * _TO_BASE[unit] * quantity
    if size <= 0:
        return None
    price = (total_price / size).quantize(Decimal("1"))
    return (c.code, price) if price > 0 else None


@dataclass(frozen=True)
class Anomaly:
    code: str
    recent: Decimal  # mean of the last 7 days
    baseline: Decimal  # mean of the 30 days before that
    rise: Decimal  # recent / baseline - 1


def _mean(xs: list[Decimal]) -> Decimal:
    return sum(xs, Decimal(0)) / len(xs)


def detect(code: str, series: list[tuple[date, Decimal]], today: date) -> Anomaly | None:
    """`series` is (date, price) in any order. A valid rise needs history to compare with, the recent mean above the
    baseline by more than the threshold, and the last two observations both above the threshold line."""
    recent = sorted((d, p) for d, p in series if today - timedelta(days=RECENT_DAYS) < d <= today)
    base = [p for d, p in series if today - timedelta(days=RECENT_DAYS + BASELINE_DAYS) < d <= today - timedelta(days=RECENT_DAYS)]
    if len(recent) < 2 or len(base) < MIN_BASELINE_POINTS:
        return None
    baseline, now_ = _mean(base), _mean([p for _, p in recent])
    if baseline <= 0:
        return None
    line = baseline * (1 + THRESHOLD)
    if now_ <= line or recent[-1][1] <= line or recent[-2][1] <= line:
        return None
    return Anomaly(code, now_.quantize(Decimal("1")), baseline.quantize(Decimal("1")), (now_ / baseline - 1).quantize(Decimal("0.001")))


def monthly_impact(anomalies: list[Anomaly], qty: dict[str, Decimal]) -> Decimal:
    """Extra IDR per month: sum of (quantity per month x price rise per unit) over the risen commodities."""
    return sum(((a.recent - a.baseline) * qty.get(a.code, Decimal(0)) for a in anomalies), Decimal(0)).quantize(Decimal("1"))


def personal_index(prices: dict[str, Decimal], baseline: dict[str, Decimal], qty: dict[str, Decimal]) -> Decimal | None:
    """Cost of the owner's own monthly basket now against the baseline (1.00 = unchanged). Only commodities with both."""
    codes = [c for c in qty if c in prices and c in baseline and baseline[c] > 0]
    if not codes:
        return None
    now_cost = sum((prices[c] * qty[c] for c in codes), Decimal(0))
    base_cost = sum((baseline[c] * qty[c] for c in codes), Decimal(0))
    return (now_cost / base_cost).quantize(Decimal("0.001"))
