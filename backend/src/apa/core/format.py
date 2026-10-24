from decimal import Decimal

WALLET_LABEL = {
    "cash_wallet": "Tunai",
    "bank_bca": "BCA",
    "bank_mandiri": "Mandiri",
    "bank_bri": "BRI",
    "bank_bni": "BNI",
    "gopay": "GoPay",
    "ovo": "OVO",
    "dana": "DANA",
    "shopeepay": "ShopeePay",
    "unknown": "Belum diketahui",  # a card/QRIS receipt that no bank notification has completed yet
}


def rupiah(x: Decimal | int | float) -> str:
    """Money in the ledger's home currency: 'Rp 1.250.000' by default (the name is historical; the owner can choose another
    currency in the dashboard, see apa.currency)."""
    from apa import currency

    return currency.fmt(x)
