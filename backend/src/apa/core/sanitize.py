"""A-01 input hygiene: PII stripping and prompt-injection screening (blueprint 4.5).

All raw text from notifications, merchants, RSS, manual input and CVE advisories is
untrusted. Run it through `sanitize_untrusted` before it is placed in an LLM payload.
"""

import re
from dataclasses import dataclass

_PII_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b\d{16}\b"), "[NIK]"),
    # before the card pattern: a 12-13 digit phone number would otherwise be taken for a card ("+62...")
    (re.compile(r"(?<![\w+])\+?(?:62|0)8\d{8,11}(?!\d)"), "[PHONE]"),
    (re.compile(r"\b(?:\d[ -]?){13,19}\b"), "[CARD]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[EMAIL]"),
    # account numbers after a keyword, e.g. "rek 1234567890", "no. rekening 1234567890"
    (re.compile(r"(?i)\b(rek(?:ening)?|acc(?:ount)?|no\.?)\s*[:#]?\s*\d{8,}"), r"\1 [ACCT]"),
]

_INJECTION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"ignore (all |any )?(previous|prior|above) (instructions|prompts?)",
        r"disregard (the )?(system|previous) (prompt|instructions?)",
        r"abaikan (semua )?(instruksi|perintah) (sebelumnya|di atas)",
        r"you are now\b",
        r"\bsystem prompt\b",
        r"reveal (your|the) (prompt|instructions|api key)",
        r"</?(system|assistant|tool)>",
        r"\bnew instructions?:",
    )
]


@dataclass(frozen=True)
class Sanitized:
    text: str
    pii_redactions: int
    injection_flags: list[str]

    @property
    def suspicious(self) -> bool:
        return bool(self.injection_flags)


def strip_pii(text: str) -> tuple[str, int]:
    count = 0
    for pattern, replacement in _PII_PATTERNS:
        text, n = pattern.subn(replacement, text)
        count += n
    return text, count


def sanitize_untrusted(text: str, max_len: int = 4000) -> Sanitized:
    """Strip PII, neutralise injection phrases and cap length. Never raises."""
    text = text[:max_len]
    text, redactions = strip_pii(text)
    flags: list[str] = []
    for pattern in _INJECTION_PATTERNS:
        if pattern.search(text):
            flags.append(pattern.pattern)
            text = pattern.sub("[removed]", text)
    return Sanitized(text=text, pii_redactions=redactions, injection_flags=flags)
