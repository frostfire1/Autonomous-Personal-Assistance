"""Caffeine, sugar and sodium from what was bought (blueprint 4.1c, A-03). These are ESTIMATES: a keyword table of
typical servings, not a measurement. They are good enough to warn "you are at 80% of today's limit", and every
message says "perkiraan". The longest keyword wins ('es teh manis' before 'teh'). Quantities from a receipt multiply
the estimate (capped); a purchase with no line items is estimated from its merchant or description."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from apa.db.models.ledger import Transaction, TransactionItem


@dataclass(frozen=True)
class Nutrition:
    caffeine_mg: Decimal = Decimal(0)
    sugar_g: Decimal = Decimal(0)
    sodium_mg: Decimal = Decimal(0)

    def __add__(self, o: "Nutrition") -> "Nutrition":
        return Nutrition(
            self.caffeine_mg + o.caffeine_mg, self.sugar_g + o.sugar_g, self.sodium_mg + o.sodium_mg
        )

    def scaled(self, n: Decimal) -> "Nutrition":
        return Nutrition(self.caffeine_mg * n, self.sugar_g * n, self.sodium_mg * n)


def _n(c=0, s=0, na=0) -> Nutrition:
    return Nutrition(Decimal(c), Decimal(s), Decimal(na))


# (keyword, typical serving). Caffeine mg / sugar g / sodium mg.
TABLE: dict[str, Nutrition] = {
    "espresso": _n(65),
    "americano": _n(95),
    "cappuccino": _n(95, 8),
    "latte": _n(95, 12),
    "kopi susu": _n(95, 18),
    "kopi gula aren": _n(95, 22),
    "es kopi": _n(95, 18),
    "kopi": _n(80, 4),
    "coffee": _n(95, 4),
    "matcha": _n(70, 20),
    "es teh manis": _n(25, 20),
    "teh manis": _n(25, 20),
    "es teh": _n(25, 20),
    "teh": _n(25, 2),
    "boba": _n(30, 38),
    "bubble tea": _n(30, 38),
    "thai tea": _n(40, 30),
    "coca cola": _n(32, 35),
    "cola": _n(32, 35),
    "pepsi": _n(38, 41),
    "soda": _n(0, 35),
    "energy drink": _n(80, 27),
    "extra joss": _n(50, 12),
    "kratingdaeng": _n(50, 27),
    "red bull": _n(80, 27),
    "susu coklat": _n(5, 22),
    "coklat": _n(10, 20),
    "es krim": _n(0, 18),
    "donat": _n(0, 15),
    "kue": _n(0, 12),
    "es jeruk": _n(0, 22),
    "jus": _n(0, 22),
    "sirup": _n(0, 25),
    "es campur": _n(0, 35),
    "es cendol": _n(0, 30),
    "mie instan": _n(0, 2, 1500),
    "indomie": _n(0, 2, 1500),
    "mie ayam": _n(0, 3, 1200),
    "bakso": _n(0, 2, 1000),
    "soto": _n(0, 2, 900),
    "nasi goreng": _n(0, 3, 800),
    "gorengan": _n(0, 1, 300),
    "sate": _n(0, 6, 600),
    "burger": _n(0, 7, 1000),
    "kfc": _n(0, 4, 1200),
    "mcd": _n(0, 8, 1200),
    "mcdonald": _n(0, 8, 1200),
    "pizza": _n(0, 6, 1100),
    "keripik": _n(0, 1, 400),
    "snack": _n(0, 8, 250),
}
_BY_LENGTH = sorted(TABLE, key=len, reverse=True)
MAX_QUANTITY = Decimal(10)


def estimate(name: str | None) -> Nutrition | None:
    if not name:
        return None
    n = name.lower()
    for kw in _BY_LENGTH:
        if kw in n:
            return TABLE[kw]
    return None


async def day_intake(sm: async_sessionmaker, start: datetime, end: datetime) -> Nutrition:
    """Estimated intake of the committed purchases in [start, end)."""
    total = Nutrition()
    window = (Transaction.timestamp >= start, Transaction.timestamp < end, Transaction.status == "committed")
    async with sm() as s:
        items = (
            await s.execute(
                select(TransactionItem.item_name, TransactionItem.quantity)
                .join(Transaction, Transaction.id == TransactionItem.transaction_id)
                .where(*window)
            )
        ).all()
        loose = (
            (
                await s.execute(
                    select(Transaction.merchant).where(
                        *window,
                        Transaction.direction == "out",
                        Transaction.merchant.is_not(None),
                        ~exists().where(TransactionItem.transaction_id == Transaction.id),
                    )
                )
            )
            .scalars()
            .all()
        )
    for name, qty in items:
        if (e := estimate(name)) is not None:
            total += e.scaled(min(max(Decimal(qty or 1), Decimal(1)), MAX_QUANTITY))
    for merchant in loose:
        if (e := estimate(merchant)) is not None:
            total += e
    return total
