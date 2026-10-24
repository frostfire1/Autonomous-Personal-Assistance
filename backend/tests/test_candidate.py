from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from apa.pipeline.candidate import BudgetLine, build_debate_context, candidate_from_event

TZ = ZoneInfo("Asia/Jakarta")
RECEIVED = datetime(2026, 10, 23, 5, 0, tzinfo=UTC)


def _android(**over):
    parsed = {"wallet": "gopay", "direction": "out", "amount": "18000", "channel": "qris",
              "merchant": "KOPI KENANGAN", "fee": None, "confidence": 1.0}
    parsed.update(over)
    return {"text": "x", "package": "com.gojek.app", "posted_at": "2026-10-23T12:00:00+07:00",
            "parsed": parsed}


def test_android_candidate_uses_posted_at_and_maps_channel():
    c = candidate_from_event("android_listener", _android(), RECEIVED)
    assert c.source_channel == "android_listener" and c.timestamp.hour == 12
    assert c.kind == "expense" and c.total == Decimal(18000) and c.name == "KOPI KENANGAN"


def test_shorthand_candidate_falls_back_to_received_at_and_description():
    payload = {"text": "x", "parsed": {"wallet": "cash_wallet", "direction": "out", "amount": "18000",
               "description": "kopi susu", "merchant": None, "tags": ["#a"], "mentions": ["@b"],
               "confidence": 0.9}}
    c = candidate_from_event("telegram_shorthand", payload, RECEIVED)
    assert c.source_channel == "manual_text" and c.timestamp == RECEIVED
    assert c.name == "kopi susu" and c.tags == ["#a"] and c.mentions == ["@b"]


def test_fee_is_part_of_the_total():
    c = candidate_from_event("android_listener", _android(amount="200000", fee="2500"), RECEIVED)
    assert c.fee == Decimal(2500) and c.total == Decimal(202500)


def test_kind_classification():
    assert candidate_from_event("android_listener", _android(direction="in"), RECEIVED).kind == "income"
    assert candidate_from_event(
        "android_listener", _android(channel="cash_withdrawal"), RECEIVED
    ).kind == "withdrawal"


def test_context_has_everything_and_fences_untrusted_text():
    c = candidate_from_event("android_listener", _android(), RECEIVED)
    ctx = build_debate_context(
        c, tz=TZ, baseline="Merchant ini rata-rata Rp 20.000.",
        budgets=[BudgetLine("Food & Dining", Decimal(1500000), Decimal(450000))],
        health_today=(Decimal(120), Decimal(22), Decimal(800)),
    )
    assert "Rp 18.000" in ctx and "GoPay" in ctx and "2026-10-23 12:00 (Friday)" in ctx
    assert "Rp 450.000 / Rp 1.500.000" in ctx and "caffeine 120 mg" in ctx
    assert "history: Merchant ini rata-rata" in ctx
    begin, end = ctx.index("untrusted text begins"), ctx.index("untrusted text ends")
    assert begin < ctx.index("KOPI KENANGAN") < end  # merchant sits inside the fence


def test_context_omits_optional_sections():
    c = candidate_from_event("android_listener", _android(), RECEIVED)
    ctx = build_debate_context(c, tz=TZ)
    assert "history:" not in ctx and "budgets" not in ctx and "intake so far" not in ctx
