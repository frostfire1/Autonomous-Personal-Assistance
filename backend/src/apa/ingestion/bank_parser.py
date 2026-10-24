"""Deterministic bank / e-wallet notification parser (Phase 2 regex library, started in Phase 1).

Goal: parse routine notifications with no LLM call. Anything that does not match returns None
and falls through to the LLM extraction fallback (never skips the debate: decisions.md D2).

The patterns are written from general knowledge of Indonesian notification wording, NOT from
real captured samples. Expect to add cases once real notifications are collected;
tests/test_bank_parser.py is where each new real sample should land.
"""

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from apa import currency

# Banking and wallet apps whose notifications are listened to; every other package is ignored. Package names are the
# best known ones, written from memory: a wrong or missing one is not a problem because the phone app can scan the
# installed apps and register the owner's own choices (see apa.ingestion.packages).
PACKAGE_WALLET = {
    "com.bca": "bank_bca",
    "com.bca.mybca.omni.android": "bank_bca",
    "com.bcadigital.blu": "bank_blu",
    "id.co.bankmandiri.livin": "bank_mandiri",
    "id.bmri.livin": "bank_mandiri",
    "id.co.bri.brimo": "bank_bri",
    "src.com.bni": "bank_bni",
    "id.bni.wondr": "bank_bni",
    "com.jago.digitalBanking": "bank_jago",
    "com.btpn.dc": "bank_jenius",
    "id.co.cimbniaga.mobile.android": "bank_cimb",
    "net.myinfosys.PermataMobileX": "bank_permata",
    "com.bsm.activity2": "bank_bsi",
    "com.bnc.finance": "bank_neo",
    "com.gojek.app": "gopay",
    "com.gojek.gopay": "gopay",
    "ovo.id": "ovo",
    "id.dana": "dana",
    "com.shopee.id": "shopeepay",
    "com.telkom.mwallet": "linkaja",
}

class _AmountPattern:
    """The amount pattern of the home currency: Rp 1.250.000,50 | Rp18.000 | IDR 18,000.00 for rupiah (the default), or the
    marks of the chosen currency ($12.50, EUR 1.250,50, RM 8.00 ...). Only home-currency amounts are read; one in another
    currency is not a ledger entry and falls through to review."""

    _cache: dict[str, re.Pattern] = {}

    def _re(self) -> re.Pattern:
        code = currency.active()
        if code not in self._cache:
            num = r"(?P<num>\d{1,3}(?:[.,]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)"
            self._cache[code] = re.compile(rf"(?P<cur>{currency.marks_regex(code)})\s?{num}", re.I)
        return self._cache[code]

    def search(self, text: str) -> re.Match | None:
        return self._re().search(text)

    def finditer(self, text: str):
        return self._re().finditer(text)


_AMOUNT = _AmountPattern()


def _value(m: re.Match) -> Decimal | None:
    """The number of an amount match, in the home currency's number format (rupiah keeps its Indonesian rules)."""
    code = currency.active()
    if code == "IDR":
        return parse_amount(m.group("num"), m.group("cur").rstrip("."))
    return currency.parse_number(m.group("num"), code)

_OUT_WORDS = re.compile(
    r"\b(transfer(?:\s+[\w-]+)?\s+ke|pembayaran|bayar|dibayar|pembelian|belanja|tarik tunai|top[\s-]?up|"
    r"debit|keluar|berhasil dikirim|kirim ke|payment|purchase|spent|paid|sent to|withdrawal|withdrew|charged|debited)\b",
    re.I,
)
_IN_WORDS = re.compile(
    r"\b(menerima|diterima|dana masuk|uang masuk|kredit|transfer masuk|masuk|terima dari|"
    r"received|deposit(?:ed)?|credited|refund(?:ed)?|top[\s-]?up berhasil ke saldo)\b",
    re.I,
)
_CARD = re.compile(r"kartu\s+kredit", re.I)
_FAIL_WORDS = re.compile(r"\b(gagal|dibatalkan|ditolak|failed|declined|expired|kedaluwarsa)\b", re.I)
_OTP_WORDS = re.compile(
    r"\b(otp|kode verifikasi|jangan berikan|password|pin anda|verification code|one[\s-]?time|do not share|passcode|security code)\b",
    re.I,
)

_MERCHANT = re.compile(
    r"(?:\bke|\bdi|\bat|\bdari|\bfrom|\bmerchant:?)\s+(?P<m>[A-Z0-9][A-Za-z0-9 &'._\-/]{1,40}?)"
    r"(?=\s+(?:Rp|IDR|sebesar|pada|tgl|tanggal|ref|no\.?|berhasil|via|dengan|senilai|\d{1,2}[/-]\d{1,2})|[.,;\n]|$)"
)

_CHANNELS = [
    ("qris", re.compile(r"\bqris\b", re.I)),
    ("bi_fast", re.compile(r"\bbi[\s-]?fast\b", re.I)),
    ("cash_withdrawal", re.compile(r"\btarik tunai\b|\batm\b", re.I)),
    ("topup", re.compile(r"\btop[\s-]?up\b", re.I)),
    ("debit", re.compile(r"\bdebit\b|\bkartu\b", re.I)),
    ("transfer", re.compile(r"\btransfer\b", re.I)),
]

_FEE = re.compile(
    r"(?:biaya(?:\s+admin(?:istrasi)?)?|admin(?:\s+fee)?|fee)\s*:?\s*(?:Rp\.?|IDR)\s?(?P<f>[\d.,]+)", re.I
)


@dataclass(frozen=True)
class ParsedNotification:
    wallet: str
    direction: str  # 'out' | 'in'
    amount: Decimal
    channel: str | None
    merchant: str | None
    fee: Decimal | None
    confidence: float  # 1.0 only when amount, direction and merchant are all explicit


def parse_amount(raw: str, currency: str = "Rp") -> Decimal | None:
    """Indonesian format uses '.' for thousands and ',' for decimals; IDR-prefixed text is English."""
    english = currency.upper() == "IDR"
    s = raw.strip()
    thousands, decimal = (",", ".") if english else (".", ",")
    # A lone separator followed by exactly 3 digits is a thousands separator, not a decimal.
    if s.count(decimal) == 1 and thousands not in s and len(s.split(decimal)[1]) == 3:
        s = s.replace(decimal, "")
    s = s.replace(thousands, "").replace(decimal, ".")
    try:
        value = Decimal(s)
    except InvalidOperation:
        return None
    return value if value > 0 else None


def amounts_in(text: str) -> set[Decimal]:
    """Every 'Rp ...' / 'IDR ...' amount in `text`, parsed with the right number format."""
    values = (_value(m) for m in _AMOUNT.finditer(text))
    return {v for v in values if v is not None}


def _first_amount(text: str) -> tuple[Decimal, int] | None:
    m = _AMOUNT.search(text)
    if not m:
        return None
    value = _value(m)
    return (value, m.end()) if value is not None else None


def parse_notification(package: str, text: str, wallets: dict[str, str] | None = None) -> ParsedNotification | None:
    """Return a parse, or None when the text is not a confident money movement.

    None means: not a transaction (OTP, promo, failed) OR unrecognised wording. The caller
    distinguishes the two with `classify_skip` if it needs to.
    """
    wallet = (wallets if wallets is not None else PACKAGE_WALLET).get(package)
    if wallet is None or classify_skip(text) is not None:
        return None

    found = _first_amount(text)
    if found is None:
        return None
    amount, _ = found

    words = _CARD.sub("kartu_kredit", text)  # "kartu kredit" is a card being used (spending), not money credited
    is_out, is_in = bool(_OUT_WORDS.search(words)), bool(_IN_WORDS.search(words))
    if is_out == is_in:  # neither, or contradictory wording: let the LLM fallback decide
        return None
    direction = "out" if is_out else "in"

    channel = next((name for name, pat in _CHANNELS if pat.search(text)), None)
    merchant_m = _MERCHANT.search(text)
    merchant = merchant_m.group("m").strip(" .-") if merchant_m else None

    fee = None
    fm = _FEE.search(text)
    if fm:
        fee = parse_amount(fm.group("f").rstrip(".,"))

    confidence = 1.0 if merchant else 0.9
    return ParsedNotification(wallet, direction, amount, channel, merchant, fee, confidence)


# Promotions, offers and marketing. These often carry a "Rp" amount ("cashback hingga Rp 50.000"), which is exactly why
# they must be dropped before anything reads an amount.
_PROMO = re.compile(
    r"\b(promo|promosi|diskon|cashback|voucher|kupon|undian|hadiah|gratis|bonus|reward|poin|penawaran|tawaran|"
    r"flash\s*sale|sale|dapatkan|ajukan|pinjaman|kta|kredit\s+tanpa|cicilan\s+0|bunga\s+\d|"
    r"s\s*&\s*k|syarat\s+(?:dan|&)\s+ketentuan|buruan|jangan\s+lewatkan|klaim|menang|selamat|berhak|"
    r"khusus\s+(?:untuk\s+)?anda|referral|ajak\s+teman|investasi|reksa\s*dana|asuransi|upgrade|naikkan\s+limit|"
    r"pre-?approved|limited|kuota\s+terbatas|offer|discount|\d+\s*%\s*off|congratulations|apply\s+now|"
    r"limited[\s-]time|earn|rewards|win\s+a)\b",
    re.I,
)
# "hingga Rp 50 juta", "mulai dari Rp 10.000", "up to Rp ..": a price or a ceiling in an offer, never a payment made
_OFFER_AMOUNT = re.compile(
    r"\b(hingga|sampai\s+dengan|s\.?d\.?|mulai\s+dari|up\s+to|senilai\s+hingga|maks(?:imal|\.)?|min(?:imal|\.)?\s+"
    r"(?:transaksi|belanja|pembelian|top[\s-]?up))\s*(?:Rp\.?|IDR|USD|US\$|\$|S\$|SGD|RM|MYR|EUR|€|GBP|£|JPY|¥|A\$|AUD|INR|₹|KRW|₩|CNY|THB|฿|PHP|₱|VND)",
    re.I,
)
# Bills and reminders: money is mentioned but none moved.
_REMINDER = re.compile(
    r"\b(tagihan|jatuh\s+tempo|pengingat|reminder|minimum\s+payment|segera\s+(?:lunasi|bayar)|belum\s+dibayar|"
    r"batas\s+waktu\s+pembayaran|cek\s+saldo|info\s+saldo|saldo\s+(?:anda|kamu)|payment\s+due|due\s+date|bill\s+is\s+ready|"
    r"statement\s+is\s+ready|low\s+balance|available\s+balance|your\s+balance\s+is)\b",
    re.I,
)
# A confirmation that money actually moved: the only thing allowed to outweigh a promotional word.
_CONFIRMED = re.compile(
    r"\b(?:(?:berhasil|sukses)\b.{0,40}\b(?:transfer|pembayaran|pembelian|top[\s-]?up|tarik\s+tunai|kirim|dikirim|dibayar|"
    r"debit)|(?:transfer|pembayaran|pembelian|top[\s-]?up|tarik\s+tunai|debit|bayar)\b.{0,60}\b(?:berhasil|sukses)|"
    r"(?:menerima|diterima|dana\s+masuk|uang\s+masuk|transfer\s+masuk)\b|"
    r"(?:payment|purchase|transfer|withdrawal|deposit)\b.{0,60}\b(?:successful|completed|approved)|"
    r"\byou\s+(?:paid|spent|sent|received)\b|\b(?:debited|credited)\b)",
    re.I | re.S,
)


# Jago and similar banks: money moved between the owner's OWN pockets is neither spending nor income.
_OWN_MOVE = re.compile(
    r"\b(?:moved|dipindahkan|pindah)\b.{0,80}\b(?:pockets?|kantong|saku)\b", re.I | re.S
)


def classify_skip(text: str) -> str | None:
    """Reason a notification must not become a transaction, else None."""
    if _OTP_WORDS.search(text):
        return "otp_or_credential"
    if _FAIL_WORDS.search(text):
        return "failed_or_cancelled"
    if not _AMOUNT.search(text):
        return "no_amount"
    if _OWN_MOVE.search(text):
        return "own_pocket_move"
    confirmed = bool(_CONFIRMED.search(text))
    if _OFFER_AMOUNT.search(text):
        return "promo"  # a ceiling or a starting price is an offer even beside a confirmed payment
    if _REMINDER.search(text) and not confirmed:
        return "reminder"
    if _PROMO.search(text) and not confirmed:
        return "promo"
    return None  # unusual wording is not a reason to drop a possible payment: the model decides, within its hourly cap
