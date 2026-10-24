"""Normalise a parsed InboundEvent into one `Candidate`, and build the debate prompt context."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from apa.core.format import WALLET_LABEL, rupiah

CASH_WALLET = "cash_wallet"
SOURCE_CHANNEL = {
    "android_listener": "android_listener",
    "telegram_photo": "multi_image_receipt",
    "telegram_shorthand": "manual_text",
}
MAX_ITEMS_IN_PROMPT = 15


@dataclass(frozen=True)
class Candidate:
    source_channel: str  # android_listener | manual_text
    wallet: str
    direction: str  # out | in
    amount: Decimal
    fee: Decimal
    merchant: str | None
    description: str | None
    channel: str | None  # qris | bi_fast | cash_withdrawal | ...
    timestamp: datetime
    parse_confidence: float
    tags: list[str] = field(default_factory=list)
    mentions: list[str] = field(default_factory=list)
    receipt: dict | None = None  # parsed receipt: items, tax, ... (vision extraction)
    attachments: list[dict] = field(default_factory=list)  # [{'uri','sha256','mime','size'}]
    caption: str | None = None
    category: str | None = None  # chosen by the owner (a confirmed chat batch); beats the debate's pick
    batch_id: str | None = None  # entries confirmed together count once toward the velocity breaker

    @property
    def total(self) -> Decimal:
        return self.amount + self.fee

    @property
    def name(self) -> str | None:
        """What to call this counterparty: merchant if known, else the free-text description."""
        return (self.merchant or self.description or "").strip()[:128] or None

    @property
    def is_receipt(self) -> bool:
        return self.receipt is not None

    @property
    def kind(self) -> str:
        """'income' | 'withdrawal' | 'expense'. Only expenses are debated (decisions.md D2 addendum)."""
        if self.direction == "in":
            return "income"
        if self.channel == "cash_withdrawal":
            return "withdrawal"
        return "expense"


def candidate_from_event(source: str, payload: dict, received_at: datetime) -> Candidate:
    """`payload['parsed']` is what process_inbound_event stored (Decimals as strings)."""
    p = payload["parsed"]
    # a vision parse carries its own resolved purchase time; otherwise when the message was sent
    ts_raw = p.get("timestamp") or payload.get("posted_at") or payload.get("sent_at")
    return Candidate(
        source_channel=SOURCE_CHANNEL.get(source, "manual_text"),
        wallet=p["wallet"],
        direction=p["direction"],
        amount=Decimal(p["amount"]),
        fee=Decimal(p["fee"]) if p.get("fee") else Decimal(0),
        merchant=p.get("merchant"),
        description=p.get("description"),
        channel=p.get("channel"),
        timestamp=datetime.fromisoformat(ts_raw) if ts_raw else received_at,
        parse_confidence=float(p.get("confidence", 0.9)),
        tags=list(p.get("tags") or []),
        mentions=list(p.get("mentions") or []),
        receipt=p.get("receipt"),
        attachments=list(payload.get("attachments") or []),
        caption=payload.get("caption"),
        category=p.get("category"),
        batch_id=p.get("batch_id"),
    )


@dataclass(frozen=True)
class BudgetLine:
    category: str
    limit: Decimal
    spent: Decimal


def _item_lines(c: Candidate) -> list[str]:
    """Receipt lines for the debaters (A-03 audits what was bought). OCR'd text: it sits inside the
    untrusted fence, and a long receipt is cut so it cannot crowd out the rest of the prompt."""
    if not c.receipt:
        return []
    items = c.receipt.get("items") or []
    lines = [f"receipt items ({len(items)}), paid by {c.receipt.get('payment_method', 'unknown')}:"]
    for it in items[:MAX_ITEMS_IN_PROMPT]:
        lines.append(f"  {it['name']} x{Decimal(it['quantity']):g} = {rupiah(Decimal(it['total_price']))}")
    if len(items) > MAX_ITEMS_IN_PROMPT:
        lines.append(f"  (+{len(items) - MAX_ITEMS_IN_PROMPT} more lines)")
    return lines


def build_debate_context(
    c: Candidate,
    *,
    tz: ZoneInfo,
    baseline: str | None = None,
    budgets: Sequence[BudgetLine] = (),
    health_today: tuple[Decimal, Decimal, Decimal] | None = None,
    categories: Sequence[str] = (),
    fatigue: bool = False,
) -> str:
    """Prompt context for the five debaters. Free text is already sanitized at intake; it is
    fenced and labelled untrusted so the agents' system prompt can refuse embedded instructions.
    `health_today` is (caffeine_mg, sugar_g, sodium_mg) accumulated so far today."""
    local = c.timestamp.astimezone(tz)
    lines = [
        "Evaluate this new expense. Judge it from your own role's perspective.",
        f"amount: {rupiah(c.total)}" + (f" (includes fee {rupiah(c.fee)})" if c.fee else ""),
        f"wallet: {WALLET_LABEL.get(c.wallet, c.wallet)}",
        f"channel: {c.channel or 'unknown'}",
        f"time: {local:%Y-%m-%d %H:%M} ({local:%A}), {tz.key}",
        "--- untrusted text begins ---",
        f"merchant: {c.merchant or '-'}",
        f"description: {c.description or '-'}",
        f"tags: {' '.join(c.tags) or '-'}",
        f"mentions: {' '.join(c.mentions) or '-'}",
        *_item_lines(c),
        "--- untrusted text ends ---",
    ]
    if categories:
        lines.append(f"categories (copy exactly one for \"category\"): {' | '.join(categories)}")
    if baseline:
        lines.append(f"history: {baseline}")
    if budgets:
        lines.append("budgets this month (spent / limit):")
        lines += [f"  {b.category}: {rupiah(b.spent)} / {rupiah(b.limit)}" for b in budgets]
    if fatigue:
        lines.append(
            "fatigue defense is ACTIVE: the owner slept badly. Be stricter with impulsive, discretionary or "
            "late-night spending, and lenient with essentials."
        )
    if health_today is not None:
        caffeine, sugar, sodium = health_today
        lines.append(
            f"intake so far today: caffeine {caffeine:g} mg, sugar {sugar:g} g, sodium {sodium:g} mg"
        )
    return "\n".join(lines)
