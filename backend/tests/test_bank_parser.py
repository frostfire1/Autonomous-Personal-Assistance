"""Samples are SYNTHETIC. Replace/extend with real captured notifications as they arrive."""

from decimal import Decimal

import pytest

from apa.ingestion.bank_parser import classify_skip, parse_amount, parse_notification


@pytest.mark.parametrize(
    ("raw", "cur", "expected"),
    [
        ("18.000", "Rp", "18000"),
        ("1.250.000", "Rp", "1250000"),
        ("1.250.000,50", "Rp", "1250000.50"),
        ("18000", "Rp", "18000"),
        ("18,000.00", "IDR", "18000.00"),
        ("0", "Rp", None),
    ],
)
def test_parse_amount(raw, cur, expected):
    assert parse_amount(raw, cur) == (Decimal(expected) if expected else None)


def test_qris_payment_out():
    p = parse_notification("com.bca", "Pembayaran QRIS Rp18.000 ke KOPI KENANGAN berhasil.")
    assert p and (p.wallet, p.direction, p.amount, p.channel) == ("bank_bca", "out", Decimal("18000"), "qris")
    assert p.merchant == "KOPI KENANGAN" and p.confidence == 1.0


def test_incoming_transfer():
    p = parse_notification("id.co.bri.brimo", "Anda menerima transfer Rp 50.000 dari ANDI SAPUTRA.")
    assert p and p.direction == "in" and p.amount == Decimal("50000") and p.merchant == "ANDI SAPUTRA"


def test_transfer_with_fee():
    p = parse_notification(
        "id.co.bankmandiri.livin", "Transfer BI-FAST ke BUDI Rp 200.000 berhasil. Biaya admin Rp 2.500"
    )
    assert p and p.channel == "bi_fast" and p.fee == Decimal("2500") and p.amount == Decimal("200000")


def test_cash_withdrawal_channel():
    p = parse_notification("src.com.bni", "Tarik tunai ATM Rp 500.000 berhasil")
    assert p and p.channel == "cash_withdrawal" and p.direction == "out"


def test_gopay_wallet_mapping():
    p = parse_notification("com.gojek.app", "Pembayaran Rp 25.000 di Gojek berhasil")
    assert p and p.wallet == "gopay"


@pytest.mark.parametrize(
    "text",
    [
        "Kode OTP Anda 123456. Jangan berikan kepada siapapun. Rp 10.000",
        "Transfer Rp 100.000 ke ANDI gagal",
        "Promo cashback hingga 50%!",
        "Saldo Anda saat ini",
    ],
)
def test_non_transactions_return_none(text):
    assert parse_notification("com.bca", text) is None
    assert classify_skip(text) is not None


def test_unknown_package_ignored():
    assert parse_notification("com.random.app", "Pembayaran Rp 18.000 ke TOKO berhasil") is None


def test_ambiguous_direction_defers_to_llm():
    # both outgoing and incoming wording: do not guess money direction
    assert parse_notification("com.bca", "Transfer ke ANDI Rp 10.000 dan menerima kredit") is None


# ---- promotions, reminders and the wider whitelist ------------------------------------------------------------------------


@pytest.mark.parametrize(("text", "reason"), [
    ("Cashback hingga Rp 50.000 untuk transaksi pertama! Buruan klaim", "promo"),
    ("Pinjaman hingga Rp 100 juta, ajukan sekarang", "promo"),
    ("Selamat! Anda berhak hadiah Rp 500.000", "promo"),
    ("Diskon 50% maks. Rp 25.000 di merchant favoritmu", "promo"),
    ("Transfer ke ANDI Rp 100.000 berhasil. Dapatkan cashback 10% min. transaksi Rp 50.000", "promo"),
    ("Tagihan kartu kredit Rp 1.200.000 jatuh tempo 25 Okt", "reminder"),
    ("Saldo Anda Rp 1.250.000", "reminder"),
    ("Gunakan kode OTP 123456 untuk transaksi Rp 50.000", "otp_or_credential"),
    ("Pembayaran Rp 20.000 gagal", "failed_or_cancelled"),
])
def test_promotions_reminders_and_non_movements_are_skipped_before_any_amount_is_read(text, reason):
    assert classify_skip(text) == reason
    assert parse_notification("com.bca", text) is None


@pytest.mark.parametrize("text", [
    "Pembayaran QRIS Rp18.000 ke KOPI KENANGAN berhasil.",
    "Anda menerima transfer Rp 50.000 dari ANDI",
    "Tarik tunai ATM Rp 500.000 berhasil",
    "Pembayaran Rp 50.000 ke Tokopedia berhasil. Dapatkan cashback 10%",  # a real payment with a promo line appended
    "Transfer BI-FAST Rp 250.000 ke SITI berhasil",
])
def test_real_payments_still_pass_even_with_a_promotional_tail(text):
    assert classify_skip(text) is None


@pytest.mark.parametrize(("pkg", "wallet"), [
    ("id.bni.wondr", "bank_bni"), ("com.jago.digitalBanking", "bank_jago"), ("com.btpn.dc", "bank_jenius"),
    ("id.bmri.livin", "bank_mandiri"), ("com.bcadigital.blu", "bank_blu"), ("com.gojek.gopay", "gopay"),
])
def test_more_indonesian_banks_and_wallets_are_listened_to(pkg, wallet):
    p = parse_notification(pkg, "Pembayaran QRIS Rp18.000 ke KOPI KENANGAN berhasil.")
    assert p is not None and p.wallet == wallet


def test_an_explicit_wallet_map_replaces_the_built_in_one():
    text = "Pembayaran QRIS Rp18.000 ke KOPI KENANGAN berhasil."
    assert parse_notification("id.co.bankjatim.app", text) is None
    assert parse_notification("id.co.bankjatim.app", text, {"id.co.bankjatim.app": "bank_jatim"}).wallet == "bank_jatim"


@pytest.mark.parametrize(("label", "package", "wallet"), [
    ("Wondr by BNI", "x.y.z", "bank_bni"), ("JConnect Mobile Bank Jatim", "id.co.bj.app", "bank_jatim"),
    ("Jago", "com.jago.app", "bank_jago"), ("Bank Aceh Syariah", "id.co.acehsyariah.app", "bank_aceh_syariah"),
    ("", "id.co.mystery.mobile", "bank_mystery_mobile"), ("Bank Super Panjang Sekali Namanya", "a.b", "bank_super_panjang_sekal"),
])
def test_an_app_name_picks_its_wallet_and_an_unknown_bank_gets_a_safe_generated_one(label, package, wallet):
    from apa.ingestion.packages import wallet_for

    assert wallet_for(label, package) == wallet
    assert len(wallet_for(label, package)) <= 24


# ---- learned notification templates ----------------------------------------------------------------------------------------

NETFLIX = "Transaksi kartu kredit Rp 75.000 di NETFLIX.COM ref 8841 berhasil diproses"
NETFLIX_PARSED = {"amount": "75000", "direction": "out", "merchant": "NETFLIX.COM", "channel": "debit"}
TOKO = "Transaksi kartu kredit Rp 120.500 di Tokopedia ref 9932 berhasil diproses"


def tpl(text=NETFLIX, parsed=None, hits=2):
    from apa.ingestion.templates import make_template

    t = make_template(text, parsed or NETFLIX_PARSED)
    return dict(t, hits=hits) if t else None


def test_a_template_turns_amount_merchant_and_reference_into_slots():
    t = tpl()
    assert t["dir"] == "out" and t["ch"] == "debit" and t["m"] is True
    assert t["sk"].count("§") == 6 and "netflix" not in t["sk"] and "75" not in t["sk"] and "8841" not in t["sk"]


def test_another_notification_of_the_same_wording_is_read_with_its_own_amount_and_merchant():
    from apa.ingestion.templates import read

    p = read([tpl()], TOKO, "bank_bni")
    assert (p.wallet, p.direction, p.amount, p.channel, p.merchant, p.confidence) == ("bank_bni", "out", 120500, "debit", "Tokopedia", 0.9)


def test_the_amount_always_comes_from_the_text_never_from_the_template():
    from apa.ingestion.templates import read

    assert read([tpl()], "Transaksi kartu kredit Rp 1.500.000 di Apple ref 1 berhasil diproses", "w").amount == 1_500_000


def test_a_template_needs_two_agreeing_examples_before_it_reads_anything():
    from apa.ingestion.templates import read

    assert read([tpl(hits=1)], TOKO, "w") is None
    assert read([tpl(hits=2)], TOKO, "w") is not None


@pytest.mark.parametrize("text", [
    "Promo kartu kredit Rp 75.000 di NETFLIX.COM ref 8841 berhasil diproses",  # different wording
    TOKO + " dan segera klaim hadiah",  # extra words: the whole text must fit, not just the start
    "Transaksi kartu kredit di Tokopedia ref 9932 berhasil diproses",  # no amount at all
    "",
])
def test_text_that_does_not_fit_the_wording_is_not_read(text):
    from apa.ingestion.templates import read

    assert read([tpl()], text, "w") is None


def test_a_template_without_a_merchant_slot_is_learned_when_the_models_merchant_is_not_in_the_text():
    from apa.ingestion.templates import make_template, read

    t = dict(make_template("Dana masuk Rp 50.000 ke rekening Anda dari transfer", {**NETFLIX_PARSED, "amount": "50000", "direction": "in", "merchant": "ANDI"}), hits=2)
    assert t["m"] is False
    p = read([t], "Dana masuk Rp 99.000 ke rekening Anda dari transfer", "w")
    assert p.direction == "in" and p.merchant is None and p.amount == 99000


@pytest.mark.parametrize(("text", "parsed"), [
    ("Rp 5.000", NETFLIX_PARSED),  # too little wording to be a template
    ("Transfer Rp 5.000 dan Rp 7.000 ke ANDI berhasil dikirim", {**NETFLIX_PARSED, "amount": "5000", "merchant": "ANDI"}),  # two amounts
    (NETFLIX, {**NETFLIX_PARSED, "amount": "99999"}),  # the model's amount is not the text's
    (NETFLIX, {**NETFLIX_PARSED, "direction": None}),
    (NETFLIX, {**NETFLIX_PARSED, "amount": "abc"}),
    (NETFLIX * 20, NETFLIX_PARSED),  # far too long
])
def test_nothing_is_learned_from_text_that_is_not_clearly_one_payment(text, parsed):
    from apa.ingestion.templates import make_template

    assert make_template(text, parsed) is None


def test_punctuation_that_means_something_to_a_regex_is_just_text():
    from apa.ingestion.templates import make_template, read

    text = "Pembayaran (QRIS) Rp 18.000 ke [KOPI*] berhasil.+"
    t = dict(make_template(text, {"amount": "18000", "direction": "out", "merchant": "[KOPI*]", "channel": "qris"}), hits=2)
    assert read([t], "Pembayaran (QRIS) Rp 25.000 ke [WARUNG?] berhasil.+", "w").merchant == "[WARUNG?]"
    assert read([t], "Pembayaran QRIS Rp 25.000 ke WARUNG berhasil", "w") is None


def test_agreeing_repeats_add_hits_a_contradiction_retires_the_template_and_the_list_is_capped():
    from apa.ingestion.templates import MAX_PER_PACKAGE, make_template, merge, read

    a = make_template(NETFLIX, NETFLIX_PARSED)
    entries = merge(merge([], a), make_template(TOKO, {**NETFLIX_PARSED, "amount": "120500", "merchant": "Tokopedia"}))
    assert len(entries) == 1 and entries[0]["hits"] == 2 and read(entries, TOKO, "w") is not None
    entries = merge(entries, make_template(TOKO, {**NETFLIX_PARSED, "amount": "120500", "merchant": "Tokopedia", "direction": "in"}))
    assert entries[0]["bad"] is True and read(entries, TOKO, "w") is None  # the model disagreed with itself: not trusted
    many: list = []
    for i in range(MAX_PER_PACKAGE + 20):
        many = merge(many, {"sk": f"kata unik nomor {i} §a§ selesai", "dir": "out", "ch": None, "m": False, "hits": 1})
    assert len(many) == MAX_PER_PACKAGE


def test_a_credit_card_payment_is_never_read_as_money_coming_in():
    # "kredit" alone means credited money, but "kartu kredit" is a card being used: spending
    assert parse_notification("com.bca", "Transaksi kartu kredit Rp 75.000 di NETFLIX.COM ref 8841 berhasil diproses") is None
    p = parse_notification("com.bca", "Pembayaran kartu kredit Rp 75.000 ke NETFLIX berhasil")
    assert p is not None and p.direction == "out"
    q = parse_notification("com.bca", "Kredit masuk Rp 50.000 ke rekening Anda")
    assert q is not None and q.direction == "in"



# ---- a home currency other than rupiah ---------------------------------------------------------------------------------------


@pytest.fixture
def home_currency():
    from apa import currency

    def use(code):
        currency.set_active(code)

    yield use
    currency.set_active("IDR")


def test_money_is_written_in_the_home_currency(home_currency):
    from decimal import Decimal

    from apa import currency
    from apa.core.format import rupiah

    assert rupiah(1250000) == "Rp 1.250.000"  # the default, unchanged
    for code, amount, text in (("USD", Decimal("1250.5"), "$1,250.50"), ("EUR", Decimal("1250.5"), "€1.250,50"),
                               ("JPY", 1250, "¥1,250"), ("MYR", Decimal("8"), "RM 8.00"), ("VND", 50000, "50.000₫")):
        home_currency(code)
        assert rupiah(amount) == text == currency.fmt(amount)
    assert currency.fmt(Decimal("-3.5"), "USD") == "-$3.50"


def test_numbers_follow_the_currencys_own_convention():
    from decimal import Decimal

    from apa import currency

    p = currency.parse_number
    assert p("1,250.50", "USD") == Decimal("1250.50") and p("1.250,50", "EUR") == Decimal("1250.50")
    assert p("1,250", "USD") == Decimal("1250") and p("12.5", "USD") == Decimal("12.5")  # a lone separator and 3 digits = thousands
    assert p("1.250.000", "IDR") == Decimal("1250000") and p("1,250", "JPY") == Decimal("1250")
    assert p("abc", "USD") is None and p("0", "USD") is None


def test_the_parser_reads_amounts_in_the_home_currency_only(home_currency):
    from decimal import Decimal

    from apa.ingestion.bank_parser import parse_notification

    pkg = "com.example.bank"  # an app of any country: the owner ticked it, so it is an approved wallet
    wallets = {pkg: "bank_example"}
    rupiah_text = "Payment to Starbucks Rp 58.300 successful"
    assert parse_notification(pkg, rupiah_text, wallets).amount == Decimal("58300")  # rupiah ledger: as before
    home_currency("USD")
    got = parse_notification(pkg, "You spent $12.50 at Starbucks", wallets)
    assert got is not None and got.amount == Decimal("12.50") and got.direction == "out"
    assert parse_notification(pkg, "You received $1,250.00 from ACME Payroll", wallets).amount == Decimal("1250.00")
    assert parse_notification(pkg, rupiah_text, wallets) is None  # a rupiah amount is not a dollar ledger entry
    home_currency("EUR")
    assert parse_notification(pkg, "Card payment at Lidl EUR 23,40 charged", wallets).amount == Decimal("23.40")


def test_prefs_accept_every_listed_currency_and_refuse_others():
    from apa.currency import CURRENCIES
    from apa.prefs import Prefs

    assert Prefs().currency == "IDR"  # the blueprint default
    for code in CURRENCIES:
        assert Prefs(currency=code.lower()).currency == code
    with pytest.raises(ValueError):
        Prefs(currency="XXX")


def test_english_promotions_codes_and_reminders_are_skipped_but_real_payments_are_not(home_currency):
    from apa.ingestion.bank_parser import classify_skip

    home_currency("USD")
    for text, why in (
        ("Your verification code is 123456. Do not share it. Pay $5", "otp_or_credential"),
        ("Get 20% off up to $50 cashback this weekend", "promo"),
        ("Your statement is ready. Minimum payment $25.00 due date Friday", "reminder"),
        ("Transfer of $20.00 failed", "failed_or_cancelled"),
    ):
        assert classify_skip(text) == why, text
    assert classify_skip("You paid $12.50 at Starbucks. Earn rewards points!") is None  # a confirmed payment outweighs the promo word
    assert classify_skip("Payment of $12.50 to ACME was successful") is None


def test_money_moved_between_your_own_pockets_is_recognised_and_a_real_receipt_is_not():
    from apa.ingestion.bank_parser import classify_skip

    assert classify_skip("Rp5.000 has been moved from your Main Pocket Pocket to your visa joe Pocket.") == "own_pocket_move"
    assert classify_skip("Rp50.000 dipindahkan dari kantong Utama ke kantong Liburan") == "own_pocket_move"
    assert classify_skip("You've received Rp200.000 from GoPay. Need help? Contact Tanya Jago at 1500 746.") is None
    assert classify_skip("Transfer ke ANDI Rp 50.000 berhasil") is None  # a real transfer out is still a transaction
