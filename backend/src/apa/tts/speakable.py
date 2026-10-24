"""Written Indonesian -> text a speech engine reads naturally. A voice reads "Rp 58.300" as digits and symbols, which is
the single biggest reason a good neural voice still sounds like a machine; this spells out money, numbers, percentages
and identifiers the way a person would say them, and drops emoji and markup. Deterministic and offline."""

import re

_UNITS = ["nol", "satu", "dua", "tiga", "empat", "lima", "enam", "tujuh", "delapan", "sembilan", "sepuluh", "sebelas"]
_SCALES = [(10**12, "triliun"), (10**9, "miliar"), (10**6, "juta"), (10**3, "ribu")]


def words(n: int) -> str:
    """12500 -> 'dua belas ribu lima ratus'. Indonesian forms: seratus, seribu, sebelas, belasan, puluhan."""
    if n < 0:
        return "minus " + words(-n)
    if n < 12:
        return _UNITS[n]
    if n < 20:
        return _UNITS[n - 10] + " belas"
    if n < 100:
        t, r = divmod(n, 10)
        return _UNITS[t] + " puluh" + (" " + _UNITS[r] if r else "")
    if n < 200:
        return "seratus" + (" " + words(n - 100) if n > 100 else "")
    if n < 1000:
        h, r = divmod(n, 100)
        return _UNITS[h] + " ratus" + (" " + words(r) if r else "")
    for size, name in _SCALES:
        if n >= size:
            q, r = divmod(n, size)
            head = "seribu" if (size == 1000 and q == 1) else f"{words(q)} {name}"
            return head + (" " + words(r) if r else "")
    return str(n)  # beyond a quadrillion: not a number a briefing speaks


def _digits(s: str) -> str:
    return " ".join(_UNITS[int(c)] for c in s if c.isdigit())


def _int(s: str) -> int:
    return int(s.replace(".", ""))


def _decimal(whole: str, frac: str) -> str:
    return f"{words(_int(whole))} koma {_digits(frac)}"


_MONEY = re.compile(r"Rp\.?\s*(\d{1,3}(?:\.\d{3})+|\d+)(?:,(\d{1,2}))?(\s*(?:juta|ribu|miliar|triliun))?")
_CVE = re.compile(r"\bCVE-(\d{4})-(\d{4,7})\b")
_NUM_DOTS = re.compile(r"\b\d{1,3}(?:\.\d{3})+\b")
_NUM_COMMA = re.compile(r"\b(\d+),(\d+)\b")
_NUM = re.compile(r"\b\d+\b")
_VERSION = re.compile(r"(?i)\b(versi|v)\s+(\d+(?:\.\d+)+)\b")
_CLOCK = re.compile(r"\b(\d{1,2})[:.](\d{2})\b")
_EMOJI = re.compile("[\U0001f300-\U0001faff☀-➿️‍]")


def _money(m: re.Match) -> str:
    whole, frac, scale = m.group(1), m.group(2), (m.group(3) or "").strip()
    amount = _decimal(whole, frac) if frac else words(_int(whole))
    return f"{amount} {scale} rupiah".replace("  ", " ")


def plain(text: str) -> str:
    """English text for a voice: emoji and markup removed, nothing spelled out (English voices read digits well)."""
    t = _EMOJI.sub(" ", text)
    t = re.sub(r"[*_`#>|~]+", " ", t)
    return " ".join(t.replace("&", " and ").replace("\u00b7", ",").split())


def speakable(text: str) -> str:
    t = _EMOJI.sub(" ", text)
    t = re.sub(r"[*_`#>|~]+", " ", t)
    t = _MONEY.sub(_money, t)
    t = _VERSION.sub(lambda m: f"{m.group(1)} " + " titik ".join(m.group(2).split(".")), t)
    t = _CVE.sub(lambda m: f"C V E {words(int(m.group(1)))}, {_digits(m.group(2))}", t)
    t = _CLOCK.sub(lambda m: f"{words(int(m.group(1)))} {words(int(m.group(2))) if int(m.group(2)) else 'tepat'}", t)
    t = _NUM_COMMA.sub(lambda m: _decimal(m.group(1), m.group(2)), t)
    t = _NUM_DOTS.sub(lambda m: words(_int(m.group(0))), t)
    t = _NUM.sub(lambda m: words(int(m.group(0))) if len(m.group(0)) <= 12 else _digits(m.group(0)), t)
    t = t.replace("%", " persen").replace("&", " dan ").replace("·", ",").replace(" - ", ", ")
    return " ".join(t.split())
