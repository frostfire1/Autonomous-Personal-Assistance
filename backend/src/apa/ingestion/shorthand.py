"""Deterministic shorthand parser for manual Telegram entries (blueprint 4.1b-D).

    kopi susu 18k tunai          -> 18000, cash_wallet, "kopi susu"
    bensin pertamax 50rb cash    -> 50000, cash_wallet
    makan siang 35.000 gopay     -> 35000, gopay
    parkir 2000 di mall kota     -> 2000, merchant "mall kota"
    gaji 5jt bca                 -> 5000000, direction in

Anything ambiguous (no amount, several amounts, a bare number that could be a quantity)
returns None so the message goes to the NL/LLM fallback. This parser never classifies a
category: that is the debate's job.
"""

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

WALLET_WORDS = {
    "tunai": "cash_wallet",
    "cash": "cash_wallet",
    "bca": "bank_bca",
    "mandiri": "bank_mandiri",
    "bri": "bank_bri",
    "bni": "bank_bni",
    "gopay": "gopay",
    "ovo": "ovo",
    "dana": "dana",
    "shopeepay": "shopeepay",
}
_SUFFIX = {"k": 1_000, "rb": 1_000, "ribu": 1_000, "jt": 1_000_000, "juta": 1_000_000}

# Bare numbers below this are treated as quantities ("2 gelas", "4 orang"), not money.
MIN_BARE_AMOUNT = Decimal("500")

_AMOUNT = re.compile(
    r"(?<![\w.,#@])(?:rp\.?\s?)?(?P<num>\d{1,3}(?:[.,]\d{3})+|\d+(?:[.,]\d+)?)"
    r"(?:\s?(?P<suf>k|rb|ribu|jt|juta)\b)?(?![\w])",
    re.I,
)
_THOUSANDS_FORM = re.compile(r"^\d{1,3}(?:[.,]\d{3})+$")
_TAG = re.compile(r"[#@]\w+")
_IN_PREFIX = re.compile(r"^(terima|dapat|gaji|masuk|bonus|dibayar)\b", re.I)
_MERCHANT = re.compile(r"\bdi\s+(?P<m>.+)$", re.I)


@dataclass(frozen=True)
class ParsedShorthand:
    amount: Decimal
    wallet: str
    wallet_defaulted: bool  # no wallet word: assumed cash
    direction: str  # 'out' | 'in'
    description: str
    merchant: str | None
    tags: list[str]
    mentions: list[str]  # @names, for split-bill / social tagging
    confidence: float  # 1.0 when the wallet was explicit, else 0.9


def _to_amount(num: str, suffix: str | None) -> Decimal | None:
    try:
        if suffix:
            # "1,5jt" / "2.5k": one separator is a decimal point; "1.500k" style is thousands
            one_separator = num.count(".") + num.count(",") == 1
            digits = num.replace(",", ".") if one_separator else num.replace(".", "").replace(",", "")
            return Decimal(digits) * _SUFFIX[suffix.lower()]
        if _THOUSANDS_FORM.match(num):
            return Decimal(num.replace(".", "").replace(",", ""))
        if num.isdigit():
            value = Decimal(num)
            return value if value >= MIN_BARE_AMOUNT else None
    except InvalidOperation:
        return None
    return None  # a bare decimal like "1.5" is not money


def extract_tags(text: str) -> tuple[list[str], list[str]]:
    """(#tags, @mentions). Always done by regex, never by an LLM."""
    found = _TAG.findall(text)
    return [t for t in found if t.startswith("#")], [t for t in found if t.startswith("@")]


def amounts_in(text: str) -> set[Decimal]:
    """Every money-looking number in `text` ('18k', '35.000', '1,5jt', 'Rp18000')."""
    values = (_to_amount(m.group("num"), m.group("suf")) for m in _AMOUNT.finditer(text))
    return {v for v in values if v is not None and v > 0}


def parse_shorthand(text: str) -> ParsedShorthand | None:
    text = text.strip()
    if not text or text.startswith("/"):
        return None

    amounts: list[tuple[Decimal, re.Match[str]]] = []
    for m in _AMOUNT.finditer(text):
        value = _to_amount(m.group("num"), m.group("suf"))
        if value is not None and value > 0:
            amounts.append((value, m))
    if len(amounts) != 1:  # none, or several: ambiguous
        return None
    amount, amount_match = amounts[0]

    tags, mentions = extract_tags(text)

    rest = text[: amount_match.start()] + " " + text[amount_match.end():]
    rest = _TAG.sub(" ", rest)

    wallet, defaulted = "cash_wallet", True
    words = rest.split()
    kept: list[str] = []
    for w in words:
        key = w.lower().strip(".,;:")
        if key in WALLET_WORDS and defaulted:
            wallet, defaulted = WALLET_WORDS[key], False
        elif key in WALLET_WORDS:  # a second wallet word: ambiguous
            return None
        else:
            kept.append(w)
    description = re.sub(r"\s+", " ", " ".join(kept)).strip(" ,.;:-")
    if not description:
        return None

    merchant = None
    mm = _MERCHANT.search(description)
    if mm:
        merchant = mm.group("m").strip(" ,.;:-") or None

    direction = "in" if _IN_PREFIX.match(description) else "out"
    return ParsedShorthand(
        amount=amount,
        wallet=wallet,
        wallet_defaulted=defaulted,
        direction=direction,
        description=description,
        merchant=merchant,
        tags=tags,
        mentions=mentions,
        confidence=0.9 if defaulted else 1.0,
    )


def wallet_in(text: str) -> str | None:
    """The wallet a free-text caption names ('pake gopay', 'tunai'), or None if it names none or
    several different ones (ambiguous: do not guess)."""
    found = {WALLET_WORDS[w] for w in re.findall(r"[a-z]+", text.lower()) if w in WALLET_WORDS}
    return found.pop() if len(found) == 1 else None
