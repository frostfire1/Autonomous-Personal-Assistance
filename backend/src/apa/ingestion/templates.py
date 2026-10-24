"""Learned notification templates. A bank notification the fixed rules cannot read goes to the model once; when the model
reads one correctly, its wording is remembered as a template (amount, merchant and reference numbers become slots). After
TWO agreeing examples the template reads later notifications of that wording with no model call at all.

Safety, because a template reads money:
  * the amount is always taken from the text itself by the same amount reader as everything else, never from the template;
  * a template is only learned from text that already passed the promo/OTP/failure filter, and only when the text holds one
    amount and the model's direction is out or in; it is never used on text that filter rejects;
  * two examples must agree on direction and channel before it is used; a contradiction retires the template for good;
  * everything it parses still goes through the full commit pipeline (breakers, debate or fast path, undo)."""

import re
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from apa.db.models.budget import PolicyRule
from apa.ingestion.bank_parser import ParsedNotification, _first_amount, amounts_in

RULE_KEY = "notification_templates"
MIN_HITS = 2
MAX_PER_PACKAGE = 100
MAX_TEXT = 400
MIN_WORDS = 3  # a skeleton of "Rp <a>" alone would match anything
MAX_MERCHANT = 60

_AMT = re.compile(r"(?:Rp\.?|IDR)\s?(?:\d{1,3}(?:[.,]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)", re.I)
_NUM = re.compile(r"\d+(?:[.,:/\-]\d+)*")
_A, _N, _M = "§a§", "§n§", "§m§"  # slot markers: not letters, digits or punctuation a bank writes


def _norm(text: str) -> str:
    return " ".join(text.split())


def skeleton(text: str, merchant: str | None = None) -> str | None:
    """The wording with its variable parts (merchant, amounts, other numbers) turned into slots. None if the merchant the
    model named is not literally in the text."""
    t = _norm(text)
    if merchant:
        found = re.search(re.escape(merchant.strip()), t, re.I)
        if not found:
            return None
        t = t[: found.start()] + _M + t[found.end():]
    return _NUM.sub(_N, _AMT.sub(_A, t)).lower()


def make_template(text: str, parsed: dict) -> dict | None:
    """A template to remember from a notification the model read, or None when it is not safe to learn from."""
    if len(text) > MAX_TEXT or parsed.get("direction") not in ("out", "in"):
        return None
    try:
        amount = Decimal(str(parsed.get("amount")))
    except Exception:
        return None
    if amounts_in(text) != {amount}:  # exactly one amount, and the one the model gave
        return None
    merchant = (parsed.get("merchant") or "").strip() or None
    sk = skeleton(text, merchant)
    if sk is None and merchant:  # the model's merchant is not verbatim in the text: learn without a merchant slot
        merchant, sk = None, skeleton(text)
    if sk is None or _A not in sk or len(re.findall(r"[a-zÀ-ɏ]{2,}", sk)) < MIN_WORDS:
        return None
    return {"sk": sk, "dir": parsed["direction"], "ch": parsed.get("channel"), "m": merchant is not None, "hits": 1}


def _regex(sk: str) -> re.Pattern:
    out = []
    for part in re.split(f"({_A}|{_N}|{_M})", sk):
        if part == _A:
            out.append(_AMT.pattern)
        elif part == _N:
            out.append(r"\d+(?:[.,:/\-]\d+)*")
        elif part == _M:
            out.append(rf"(?P<m>.{{1,{MAX_MERCHANT}}}?)")
        else:
            out.append(re.escape(part))
    return re.compile("".join(out), re.I)


def match(entry: dict, text: str) -> tuple[str | None] | None:
    """(merchant,) when the template reads this text, else None."""
    m = _regex(entry["sk"]).fullmatch(_norm(text))
    if m is None:
        return None
    return ((m.group("m").strip(" .-") or None) if entry.get("m") else None,)


def read(entries: list[dict], text: str, wallet: str) -> ParsedNotification | None:
    """The first trusted template that reads `text`, as a parse. The amount comes from the text."""
    found = _first_amount(text)
    if found is None:
        return None
    for e in entries:
        if e.get("bad") or e.get("hits", 0) < MIN_HITS:
            continue
        hit = match(e, text)
        if hit is not None:
            return ParsedNotification(wallet, e["dir"], found[0], e.get("ch"), hit[0], None, 0.9)
    return None


def merge(entries: list[dict], new: dict) -> list[dict]:
    """Add one more observation: a repeat of the same wording agreeing on direction and channel earns a hit; a
    disagreement retires the template."""
    for e in entries:
        if e["sk"] == new["sk"]:
            if e["dir"] == new["dir"] and e.get("ch") == new.get("ch"):
                e["hits"] = e.get("hits", 1) + 1
            else:
                e["bad"] = True
            return entries
    return [*entries, new][-MAX_PER_PACKAGE:]


async def _row(s: AsyncSession, lock: bool = False) -> dict:
    q = select(PolicyRule).where(PolicyRule.rule_key == RULE_KEY)
    row = (await s.execute(q.with_for_update() if lock else q)).scalar_one_or_none()
    return dict(row.value_json or {}) if row else {}


async def apply(s: AsyncSession, package: str, text: str, wallets: dict[str, str]) -> ParsedNotification | None:
    wallet = wallets.get(package)
    if wallet is None:
        return None
    entries = (await _row(s)).get(package, [])
    return read(entries, text, wallet) if entries else None


async def learn(s: AsyncSession, package: str, text: str, parsed: dict) -> bool:
    """Remember what the model just read. False when the text is not safe to learn from."""
    new = make_template(text, parsed)
    if new is None:
        return False
    data = await _row(s, lock=True)
    data[package] = merge(data.get(package, []), new)
    stmt = insert(PolicyRule).values(
        rule_key=RULE_KEY, scope="global", scope_ref="", value_json=data, updated_by="template_learner", is_active=True)
    await s.execute(stmt.on_conflict_do_update(
        index_elements=["rule_key", "scope", "scope_ref"], set_={"value_json": data, "updated_by": "template_learner"}))
    return True
