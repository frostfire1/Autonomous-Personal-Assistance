"""Several payments in one message: a numbered draft the owner reviews, labels ("1-5 breakfast") and
confirms once. A-01 style: everything that can be done with code is done with code, so the common cases cost
no model call at all. A model is only asked for messy lists and messy edits, and its output is validated
like any other proposal.

Nothing is recorded by this module. A confirmed draft becomes ordinary `parsed` events that go through the
normal breakers, debate and quorum (the velocity breaker counts the batch once, see pipeline/commit.py)."""

import json
import re
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from apa.core.format import rupiah
from apa.db.models.budget import BudgetRule
from apa.ingestion.shorthand import amounts_in, parse_shorthand
from apa.pipeline.categories import DEFAULT_CATEGORIES, canonicalize

DRAFT_KEY = "apa:draft"
DRAFT_TTL_S = 1800
MAX_ITEMS = 20
MAX_AMOUNT = Decimal(1_000_000_000)

# a label that names a category ("breakfast" is food) so the owner does not have to say it twice
LABEL_CATEGORY = {
    "Food & Dining": ("breakfast", "sarapan", "lunch", "makan siang", "dinner", "makan malam", "makan", "snack",
                      "jajan", "kopi", "minum", "food", "resto"),
    "Groceries": ("groceries", "belanja", "sembako", "grocery"),
    "Transport": ("transport", "ojek", "grab", "gojek", "bensin", "parkir", "tol", "bbm"),
    "Shopping": ("shopping", "baju", "belanja online"),
    "Bills & Utilities": ("tagihan", "listrik", "pulsa", "internet", "air", "bills"),
    "Entertainment": ("hiburan", "nonton", "game", "entertainment"),
    "Social": ("traktir", "sosial", "social", "hadiah", "kado"),
    "Health": ("kesehatan", "obat", "dokter", "health"),
}
_ALIAS = {w: c for c, ws in LABEL_CATEGORY.items() for w in ws}

_TIME = re.compile(r"(?<![\d.,:])(?:jam\s*)?(?P<h>[01]?\d|2[0-3])[:.](?P<m>[0-5]\d)(?![\d.,])", re.I)
# lines, ';', ', ' before a word (not the comma inside 1,5jt), or ' dan '
_SPLIT = re.compile(r"[\n;]+|,\s+(?=\D)|\s+dan\s+")


class Item(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    amount: Decimal = Field(gt=0, le=MAX_AMOUNT)
    merchant: str = Field(min_length=1, max_length=80)
    time: datetime
    wallet: str = Field(default="unknown", pattern=r"^[a-z_]{2,24}$")
    direction: Literal["out", "in"] = "out"
    label: str | None = Field(default=None, max_length=30, pattern=r"^[\w .&-]+$")
    category: str | None = Field(default=None, max_length=48)


class Draft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rev: str
    items: list[Item] = Field(max_length=MAX_ITEMS)
    nonce: str | None = None  # the pending confirmation that matches this revision


# ---- the draft store ----------------------------------------------------------------------------------


async def load_draft(redis: Any | None) -> Draft | None:
    if redis is None:
        return None
    try:
        raw = await redis.get(DRAFT_KEY)
        return Draft.model_validate_json(raw) if raw else None
    except Exception:  # a draft is a convenience: if it cannot be read there is simply no draft
        return None


async def save_draft(redis: Any, draft: Draft) -> None:
    await redis.set(DRAFT_KEY, draft.model_dump_json(), ex=DRAFT_TTL_S)


async def clear_draft(redis: Any) -> None:
    await redis.delete(DRAFT_KEY)


def new_draft(items: list[Item]) -> Draft:
    return Draft(rev=uuid.uuid4().hex[:12], items=items)


# ---- reading a list from text (no model) ---------------------------------------------------------------


def _resolve_time(h: int, m: int, sent_at: datetime, tz: ZoneInfo) -> datetime:
    local = sent_at.astimezone(tz)
    t = local.replace(hour=h, minute=m, second=0, microsecond=0)
    return t - timedelta(days=1) if t > local + timedelta(minutes=5) else t  # "19:00" said at 06:00 was yesterday


def parse_batch(text: str, sent_at: datetime, tz: ZoneInfo) -> list[Item] | None:
    """'kopi 18k, roti 12k' / one payment per line / '07:10 kopi 18k'. None unless there are 2+ payments and
    EVERY segment is a clean single payment: a half-understood list is not guessed at."""
    parts = [p.strip() for p in _SPLIT.split(text) if p.strip()]
    if not 2 <= len(parts) <= MAX_ITEMS:
        return None
    items: list[Item] = []
    for part in parts:
        tm = _TIME.search(part)
        body = (part[: tm.start()] + " " + part[tm.end():]).strip() if tm else part
        p = parse_shorthand(body)
        name = (p.merchant or p.description) if p else None
        if p is None or not name:
            return None
        when = _resolve_time(int(tm["h"]), int(tm["m"]), sent_at, tz) if tm else sent_at.astimezone(tz)
        try:
            items.append(Item(amount=p.amount, merchant=name[:80], time=when,
                              wallet="unknown" if p.wallet_defaulted else p.wallet, direction=p.direction))
        except ValidationError:
            return None
    return items


def ground(items: list[dict], text: str, sent_at: datetime, tz: ZoneInfo) -> list[Item]:
    """Model-extracted items, kept only if valid and every amount really is written in the message."""
    written = amounts_in(text)
    out: list[Item] = []
    for raw in items[:MAX_ITEMS]:
        try:
            amount = Decimal(str(raw.get("amount")))
            if amount not in written:
                continue
            hhmm = str(raw.get("time") or "")
            tm = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", hhmm)
            when = _resolve_time(int(tm[1]), int(tm[2]), sent_at, tz) if tm else sent_at.astimezone(tz)
            wallet = raw.get("wallet") or "unknown"
            out.append(Item(amount=amount, merchant=str(raw.get("merchant") or "")[:80], time=when,
                            wallet=wallet if re.fullmatch(r"[a-z_]{2,24}", str(wallet)) else "unknown",
                            direction="in" if raw.get("direction") == "in" else "out"))
        except (ValidationError, ArithmeticError, ValueError, TypeError):
            continue
    return out


# ---- editing the draft ---------------------------------------------------------------------------------

_SEL = r"(?P<sel>(?:\d+(?:\s*[-–]\s*\d+)?)(?:\s*(?:,|dan|&|and)\s*\d+(?:\s*[-–]\s*\d+)?)*|semua|all)"
_LABEL_CMD = re.compile(rf"^\s*{_SEL}\s*(?:adalah|=|:|jadi|itu)?\s+(?P<label>[^\d\n]{{1,30}}?)\s*$", re.I)
_DROP_CMD = re.compile(rf"^\s*(?:hapus|buang|drop|delete|remove|skip)\s+{_SEL}\s*$", re.I)
_YES = {"ok", "oke", "ya", "yes", "y", "catat", "simpan", "lanjut", "gas", "confirm", "betul", "benar"}
_NO = {"batal", "cancel", "tidak", "no", "gak", "ga", "nggak", "jangan"}


def pick(sel: str, n: int) -> list[int] | None:
    """'1-5', '1,3', '2 dan 4', 'semua' -> sorted 1-based numbers, all within 1..n; None if any is out of range."""
    if sel.strip().lower() in ("semua", "all"):
        return list(range(1, n + 1))
    picked: set[int] = set()
    for chunk in re.split(r"\s*(?:,|dan|&|and)\s*", sel.strip(), flags=re.I):
        m = re.fullmatch(r"(\d+)(?:\s*[-–]\s*(\d+))?", chunk)
        if not m:
            return None
        a, b = int(m[1]), int(m[2] or m[1])
        if a > b:
            a, b = b, a
        picked.update(range(a, b + 1))
    return sorted(picked) if picked and all(1 <= i <= n for i in picked) else None


def label_to_category(label: str, categories: list[str]) -> str | None:
    key = label.strip().lower()
    return canonicalize(label, categories) or (
        c if (c := _ALIAS.get(key)) and canonicalize(c, categories) else None
    )


class Edit(BaseModel):
    changed: bool = False
    ask: list[str] = []
    reply: str | None = None  # a plain answer instead of a re-rendered draft (e.g. out-of-range)


def apply_label(draft: Draft, nums: list[int], label: str, categories: list[str]) -> Edit:
    cat = label_to_category(label, categories)
    tag = re.sub(r"\s+", " ", label.strip())[:30]
    for i in nums:
        draft.items[i - 1].label = tag
        if cat:
            draft.items[i - 1].category = cat
    ask = [] if cat else [
        f"«{tag}» belum cocok dengan kategori mana pun, jadi saya hanya menandainya. "
        f"Mau masuk kategori apa? ({', '.join(categories[:8])})"
    ]
    return Edit(changed=True, ask=ask)


def apply_drop(draft: Draft, nums: list[int]) -> Edit:
    draft.items = [it for i, it in enumerate(draft.items, 1) if i not in set(nums)]
    return Edit(changed=True)


def set_category(draft: Draft, nums: list[int], category: str, categories: list[str]) -> Edit:
    cat = canonicalize(category, categories)
    if cat is None:
        return Edit(reply=f"Kategori «{category[:30]}» tidak dikenal. Pilihan: {', '.join(categories[:10])}")
    for i in nums:
        draft.items[i - 1].category = cat
    return Edit(changed=True)


def text_edit(draft: Draft, text: str, categories: list[str]) -> tuple[str, Edit | None]:
    """Deterministic reading of the owner's reply to a draft. -> ('confirm'|'cancel'|'edit'|'none', edit)."""
    word = text.strip().lower().rstrip(".!")
    if word in _YES:
        return "confirm", None
    if word in _NO:
        return "cancel", None
    n = len(draft.items)
    if m := _DROP_CMD.match(text):
        nums = pick(m["sel"], n)
        if nums is None:
            return "edit", Edit(reply=f"Nomor di luar daftar (1-{n}).")
        return "edit", apply_drop(draft, nums)
    if (m := _LABEL_CMD.match(text)) and not _TIME.search(text):
        nums = pick(m["sel"], n)
        if nums is None:
            return "edit", Edit(reply=f"Nomor di luar daftar (1-{n}).")
        return "edit", apply_label(draft, nums, m["label"], categories)
    return "none", None


# ---- the card ---------------------------------------------------------------------------------------------


async def finance_note(sm: async_sessionmaker, draft: Draft, tz: ZoneInfo, categories: list[str]) -> str:
    """What the finance agent (A-02) adds: group totals, the budget they land in, and what is still unlabelled.
    Computed in code from the ledger; no model involved."""
    lines: list[str] = []
    month = draft.items[0].time.astimezone(tz).date().replace(day=1) if draft.items else None
    async with sm() as s:
        budgets = {
            b.category: b
            for b in (await s.execute(select(BudgetRule).where(BudgetRule.period_month == month))).scalars()
        } if month else {}
        # spent so far is in the budget row; for a category with no budget there is nothing to compare
    groups: dict[tuple[str | None, str | None], list[Item]] = {}
    for it in draft.items:
        if it.direction == "out" and (it.label or it.category):
            groups.setdefault((it.label, it.category), []).append(it)
    for (label, cat), its in groups.items():
        total = sum((i.amount for i in its), Decimal(0))
        head = f"• {'#' + label if label else cat}: {len(its)} item, {rupiah(total)}"
        b = budgets.get(cat or "")
        if b and b.current_limit:
            before = int(b.spent_amount / b.current_limit * 100)
            after = int((b.spent_amount + total) / b.current_limit * 100)
            head += f" → {cat}: {before}% dari anggaran {rupiah(b.current_limit)}, jadi {after}% jika dicatat"
        elif cat:
            head += f" → {cat} (belum ada anggaran)"
        lines.append(head)
    todo = [str(i) for i, it in enumerate(draft.items, 1) if it.direction == "out" and not (it.label or it.category)]
    if todo and groups:
        lines.append(f"• Belum diberi label/kategori: {', '.join(todo)} (saya tentukan lewat debat agen bila dibiarkan)")
    return "🧮 A-02 Finance:\n" + "\n".join(lines) if lines else ""


def render(draft: Draft, tz: ZoneInfo, *, ask: list[str] | None = None, note: str = "") -> str:
    total = sum((i.amount for i in draft.items if i.direction == "out"), Decimal(0))
    lines = [f"🧾 Draft {len(draft.items)} pembayaran · keluar {rupiah(total)} (belum dicatat)"]
    for n, it in enumerate(draft.items, 1):
        tags = " ".join(x for x in ((f"#{it.label}" if it.label else ""), (f"[{it.category}]" if it.category else "")) if x)
        sign = "+" if it.direction == "in" else ""
        lines.append(f"{n}. {it.time.astimezone(tz):%H:%M} {it.merchant} {sign}{rupiah(it.amount)} {tags}".rstrip())
    if note:
        lines.append(note)
    lines += [f"❓ {q}" for q in (ask or [])]
    lines.append("Balas mis. «1-3 breakfast», «4 transport», «hapus 2», atau «ok» untuk mencatat semua.")
    return "\n".join(lines)


def categories_for(budget_categories: list[str]) -> list[str]:
    return list(dict.fromkeys([*DEFAULT_CATEGORIES, *budget_categories]))


def dump_items(draft: Draft) -> list[dict]:
    return json.loads(draft.model_dump_json())["items"]

