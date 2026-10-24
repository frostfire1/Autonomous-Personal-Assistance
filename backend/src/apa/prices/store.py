"""Price data in the database: the basket, daily prices from any source (the owner's receipts, an external feed, the
ingest API), and the series the anomaly rule reads."""

from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from apa.core.logging import get_logger
from apa.db.models.budget import CommodityPrice, PriceBasket
from apa.db.models.ledger import Transaction, TransactionItem
from apa.prices.engine import DEFAULT_BASKET, unit_price

log = get_logger(__name__)

REGION_PERSONAL = "personal"  # prices the owner actually paid, read from their receipts
DEFAULT_REGION = "Nasional"
MIN_PRICE = Decimal(100)
MAX_PRICE = Decimal(10_000_000)  # per kg or litre: anything beyond is a unit error, not a price


async def ensure_basket(sm: async_sessionmaker) -> int:
    """Seed the default basket once, when there is none. The quantities and baseline prices are editable guesses."""
    async with sm() as s:
        if (await s.execute(select(func.count()).select_from(PriceBasket))).scalar_one():
            return 0
        for c in DEFAULT_BASKET:
            s.add(PriceBasket(commodity_code=c.code, commodity_name=c.name, monthly_qty=c.monthly_qty, unit=c.unit,
                              pihps_region=DEFAULT_REGION, baseline_price=c.baseline_price, budget_category="Groceries"))
        await s.commit()
    return len(DEFAULT_BASKET)


async def basket(sm: async_sessionmaker) -> list[PriceBasket]:
    async with sm() as s:
        return list((await s.execute(select(PriceBasket).where(PriceBasket.is_active))).scalars())


async def record(sm: async_sessionmaker, code: str, region: str, day: date, price: Decimal) -> bool:
    """Store one observation (the latest for that commodity, region and day wins). False if it is not a plausible
    price or the commodity is not in the basket."""
    if not (MIN_PRICE <= price <= MAX_PRICE):
        return False
    async with sm() as s:
        if (await s.execute(select(PriceBasket.id).where(PriceBasket.commodity_code == code))).first() is None:
            return False
        stmt = insert(CommodityPrice).values(commodity_code=code, pihps_region=region, price_date=day, price=price)
        await s.execute(stmt.on_conflict_do_update(index_elements=["commodity_code", "pihps_region", "price_date"],
                                                   set_={"price": price}))
        await s.commit()
    return True


async def harvest_receipts(sm: async_sessionmaker, tz: ZoneInfo, now: datetime, *, days: int = 45) -> int:
    """Unit prices from the owner's committed receipts: 'Beras 5kg' for 68.000 is 13.600 per kg. Several lines of the
    same staple on one day are averaged. -> observations written."""
    async with sm() as s:
        rows = (
            await s.execute(
                select(TransactionItem.item_name, TransactionItem.quantity, TransactionItem.total_price, Transaction.timestamp)
                .join(Transaction, Transaction.id == TransactionItem.transaction_id)
                .where(Transaction.status == "committed", Transaction.timestamp >= now - timedelta(days=days))
            )
        ).all()
    per: dict[tuple[str, date], list[Decimal]] = defaultdict(list)
    for name, qty, total, ts in rows:
        if (u := unit_price(name, Decimal(qty or 1), total)) is not None:
            per[(u[0], ts.astimezone(tz).date())].append(u[1])
    written = 0
    for (code, day), prices in per.items():
        if await record(sm, code, REGION_PERSONAL, day, (sum(prices, Decimal(0)) / len(prices)).quantize(Decimal("1"))):
            written += 1
    return written


async def series(sm: async_sessionmaker, code: str, today: date, *, days: int = 45) -> list[tuple[date, Decimal]]:
    """One price per day (the mean across regions that reported that day)."""
    async with sm() as s:
        rows = (
            await s.execute(
                select(CommodityPrice.price_date, func.avg(CommodityPrice.price))
                .where(CommodityPrice.commodity_code == code, CommodityPrice.price_date > today - timedelta(days=days),
                       CommodityPrice.price_date <= today)
                .group_by(CommodityPrice.price_date)
            )
        ).all()
    return [(d, Decimal(p)) for d, p in rows]
