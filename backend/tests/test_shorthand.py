from decimal import Decimal

import pytest

from apa.ingestion.shorthand import parse_shorthand


@pytest.mark.parametrize(
    ("text", "amount", "wallet", "desc"),
    [
        ("kopi susu 18k tunai", "18000", "cash_wallet", "kopi susu"),
        ("bensin pertamax 50rb cash", "50000", "cash_wallet", "bensin pertamax"),
        ("makan siang 35.000 gopay", "35000", "gopay", "makan siang"),
        ("parkir 2000", "2000", "cash_wallet", "parkir"),
        ("gopay nasi goreng 25rb", "25000", "gopay", "nasi goreng"),
        ("laptop bekas 1,5jt bca", "1500000", "bank_bca", "laptop bekas"),
        ("es teh 5 ribu", "5000", "cash_wallet", "es teh"),
        ("sate 2 porsi 40k", "40000", "cash_wallet", "sate 2 porsi"),  # '2' is a quantity
        ("Kopi Susu Rp18.000 tunai", "18000", "cash_wallet", "Kopi Susu"),
    ],
)
def test_parses(text, amount, wallet, desc):
    p = parse_shorthand(text)
    assert p is not None
    assert (p.amount, p.wallet, p.description) == (Decimal(amount), wallet, desc)


def test_wallet_defaulted_lowers_confidence():
    assert parse_shorthand("parkir 2000").confidence == 0.9
    assert parse_shorthand("parkir 2000 tunai").confidence == 1.0


def test_merchant_after_di():
    p = parse_shorthand("kopi 18k tunai di warkop pak ujang")
    assert p.merchant == "warkop pak ujang"


def test_tags_and_mentions_extracted_and_removed_from_description():
    p = parse_shorthand("traktir makan malam 150rb @andi #hangout")
    assert p.mentions == ["@andi"] and p.tags == ["#hangout"]
    assert p.description == "traktir makan malam"


def test_incoming_direction():
    assert parse_shorthand("gaji 5jt bca").direction == "in"
    assert parse_shorthand("kopi 18k").direction == "out"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "/undo abc123",
        "kopi susu",  # no amount
        "makan 2 orang",  # only a quantity
        "kopi 18k dan roti 12k",  # two amounts: ambiguous
        "18k tunai",  # nothing to describe
        "kopi 18k tunai gopay",  # two wallets: ambiguous
        "suhu 1.5 derajat",  # bare decimal is not money
    ],
)
def test_ambiguous_or_invalid_defers_to_llm(text):
    assert parse_shorthand(text) is None
