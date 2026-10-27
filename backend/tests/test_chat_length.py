"""Tests for chat length settings, multi-part Telegram message splitting, and anti-truncation."""

from apa import llm_config
from apa.integrations import notify


def test_split_telegram_text_short():
    """Short text below limit should return single item unchanged."""
    text = "Halo Joe! Apa kabar hari ini?"
    chunks = notify.split_telegram_text(text, max_chars=100)
    assert chunks == [text]


def test_split_telegram_text_paragraphs():
    """Multi-paragraph long text should split cleanly between paragraphs without cutting mid-sentence."""
    p1 = "Paragraf 1: " + ("penjelasan penting " * 10)
    p2 = "Paragraf 2: " + ("analisis mendalam " * 10)
    p3 = "Paragraf 3: " + ("rekomendasi final " * 10)
    full = f"{p1}\n\n{p2}\n\n{p3}"

    chunks = notify.split_telegram_text(full, max_chars=len(p1) + 20)
    assert len(chunks) == 3
    for chunk in chunks:
        assert len(chunk) <= len(p1) + 20
    assert "Paragraf 1" in chunks[0]
    assert "Paragraf 2" in chunks[1]
    assert "Paragraf 3" in chunks[2]


def test_split_telegram_text_single_huge_line():
    """A huge unbroken line should split cleanly on whitespace words."""
    huge_line = " ".join(["kata"] * 200)
    chunks = notify.split_telegram_text(huge_line, max_chars=100)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 100


def test_llm_config_maximum_preset():
    """Test that the new 'maximum' (Tanpa Potong / Full) preset is available and configured."""
    assert "maximum" in llm_config.PRESETS
    mult, turns, ctx = llm_config.PRESETS["maximum"]
    assert mult >= 3.0
    assert turns >= 32
    assert "Tanpa Potong" in llm_config.PRESET_LABELS["maximum"]


def test_llm_config_chat_settings_defaults_and_custom():
    """Test chat_max_tokens and chat_max_reply_chars settings."""
    cfg = llm_config.LLMConfig()
    assert cfg.chat_max_tokens == 2500
    assert cfg.chat_max_reply_chars == 15000
    assert cfg.chat_split_long_messages is True

    custom_cfg = llm_config.LLMConfig(
        chat_max_tokens=5000,
        chat_max_reply_chars=30000,
        chat_split_long_messages=False,
    )
    assert custom_cfg.chat_max_tokens == 5000
    assert custom_cfg.chat_max_reply_chars == 30000
    assert custom_cfg.chat_split_long_messages is False
