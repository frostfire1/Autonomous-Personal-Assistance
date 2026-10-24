"""The ledger's home currency. One ledger, one currency (no conversion): Indonesian rupiah by default (the blueprint), and the
owner can choose another in the dashboard. It decides how money is written (Rp 1.250.000, $1,250.50, 1 250,50 EUR ...),
which currency symbols a bank notification may carry, and the number format those use.

The choice is saved in the preferences (`prefs.Prefs.currency`); every process keeps the active code in memory and
refreshes it every minute, so a change applies without a restart."""

import asyncio
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from apa.core.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class Currency:
    code: str
    name: str
    symbol: str  # as written in front of (or after) the amount
    decimals: int
    thousands: str
    decimal: str
    after: bool = False  # symbol after the number ("1.250,50 €")
    marks: tuple[str, ...] = ()  # everything a notification may write for it (symbols and the code)


CURRENCIES: dict[str, Currency] = {c.code: c for c in (
    Currency("IDR", "Indonesian rupiah", "Rp ", 0, ".", ",", marks=("Rp", "Rp.", "IDR")),
    Currency("USD", "US dollar", "$", 2, ",", ".", marks=("US$", "USD", "$")),
    Currency("EUR", "Euro", "€", 2, ".", ",", marks=("EUR", "€")),
    Currency("GBP", "Pound sterling", "£", 2, ",", ".", marks=("GBP", "£")),
    Currency("JPY", "Japanese yen", "¥", 0, ",", ".", marks=("JPY", "¥", "円")),
    Currency("SGD", "Singapore dollar", "S$", 2, ",", ".", marks=("SGD", "S$")),
    Currency("MYR", "Malaysian ringgit", "RM ", 2, ",", ".", marks=("MYR", "RM")),
    Currency("AUD", "Australian dollar", "A$", 2, ",", ".", marks=("AUD", "A$")),
    Currency("INR", "Indian rupee", "₹", 2, ",", ".", marks=("INR", "₹", "Rs.", "Rs")),
    Currency("KRW", "South Korean won", "₩", 0, ",", ".", marks=("KRW", "₩", "원")),
    Currency("CNY", "Chinese yuan", "¥", 2, ",", ".", marks=("CNY", "RMB", "元")),
    Currency("THB", "Thai baht", "฿", 2, ",", ".", marks=("THB", "฿")),
    Currency("PHP", "Philippine peso", "₱", 2, ",", ".", marks=("PHP", "₱")),
    Currency("VND", "Vietnamese dong", "₫", 0, ".", ",", after=True, marks=("VND", "₫", "đ")),
)}
DEFAULT = "IDR"

_active = DEFAULT


def active() -> str:
    """The home currency code every process currently uses."""
    return _active


def set_active(code: str) -> None:
    global _active
    _active = code if code in CURRENCIES else DEFAULT


def fmt(amount: Decimal | int | float, code: str | None = None) -> str:
    """'Rp 1.250.000', '$1,250.50', '1.250,50€' ... in the given (default: the home) currency."""
    c = CURRENCIES.get(code or _active) or CURRENCIES[DEFAULT]
    q = Decimal(1).scaleb(-c.decimals)
    n = Decimal(str(amount)).quantize(q, rounding=ROUND_HALF_UP)
    whole, _, frac = f"{abs(n):,.{c.decimals}f}".partition(".")
    body = whole.replace(",", c.thousands) + (c.decimal + frac if c.decimals else "")
    sign = "-" if n < 0 else ""
    return f"{sign}{body}{c.symbol.strip()}" if c.after else f"{sign}{c.symbol}{body}"


def marks_regex(code: str | None = None) -> str:
    """A regex alternative matching every way a notification writes the currency (longest first)."""
    c = CURRENCIES.get(code or _active) or CURRENCIES[DEFAULT]
    return "|".join(re.escape(m) for m in sorted(c.marks, key=len, reverse=True))


def parse_number(raw: str, code: str | None = None) -> Decimal | None:
    """'1.250.000', '1,250.50', '1.250,50' -> Decimal, by the currency's own convention; when both separators appear the
    last one is the decimal point, and a lone separator followed by exactly three digits is a thousands separator."""
    s = raw.strip().strip(".,")
    if not s or not re.fullmatch(r"[\d.,]+", s):
        return None
    c = CURRENCIES.get(code or _active) or CURRENCIES[DEFAULT]
    last_dot, last_comma = s.rfind("."), s.rfind(",")
    if last_dot >= 0 and last_comma >= 0:
        dec = "." if last_dot > last_comma else ","
    else:
        sep = "." if last_dot >= 0 else ("," if last_comma >= 0 else "")
        if not sep:
            dec = ""
        elif s.count(sep) > 1 or len(s.split(sep)[1]) == 3 or c.decimals == 0:
            dec = ""  # repeated, or exactly three digits, or a currency without decimals: a thousands separator
        else:
            dec = sep
    if dec:
        whole, _, frac = s.rpartition(dec)
        s = re.sub(r"[.,]", "", whole) + "." + frac
    else:
        s = re.sub(r"[.,]", "", s)
    try:
        value = Decimal(s)
    except ArithmeticError:
        return None
    return value if value > 0 else None


async def refresh(sm: Any) -> str:
    """Read the saved choice into memory. A database hiccup keeps the last known one."""
    from apa import prefs

    try:
        set_active((await prefs.load(sm)).currency)
    except Exception as e:
        log.warning("currency_refresh_failed", error=type(e).__name__)
    return _active


def start_refresher(sm: Any, every_s: float = 60.0) -> asyncio.Task:
    """Keep this process's home currency fresh (call once at start, from a running event loop)."""

    async def loop() -> None:
        while True:
            await refresh(sm)
            await asyncio.sleep(every_s)

    return asyncio.get_running_loop().create_task(loop())
