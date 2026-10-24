

def test_an_international_phone_number_is_a_phone_not_a_card():
    from apa.core.sanitize import sanitize_untrusted

    for raw in ("hubungi +6281234567890 segera", "hubungi 6281234567890 segera", "hubungi 081234567890 segera"):
        out = sanitize_untrusted(raw).text
        assert "[PHONE]" in out and "[CARD]" not in out and "+[" not in out, out
    assert "[CARD]" in sanitize_untrusted("kartu 4111 1111 1111 1111").text  # real card numbers still caught
    assert sanitize_untrusted("bayar Rp 1.250.000 ke toko").text == "bayar Rp 1.250.000 ke toko"
