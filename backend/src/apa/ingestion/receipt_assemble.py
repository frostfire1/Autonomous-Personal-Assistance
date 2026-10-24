"""Turn the per-photo readings of an album into ONE receipt (pure: no model, no database).

The vision model reports what is printed on EACH photo and never merges or de-duplicates across
photos itself; this module does that deterministically so it can be tested and cannot hallucinate:

  * STITCHING (blueprint 4.1b case 2): consecutive pages of a long receipt overlap, because the last
    lines of one photo are repeated at the top of the next. We find the overlap by item equality. The
    overlap is ambiguous when a shopper genuinely bought the same item twice at a page boundary, so we
    try every plausible overlap and keep the first whose items add up to the grand total (the same
    arithmetic gate a single receipt faces). If none adds up, the album is not recorded.
  * DUAL VERIFICATION (case 1): a payment slip (EDC / QRIS / e-wallet / transfer) states what was
    actually charged. It must agree with the receipt; a tip or service charge that only the slip shows
    is split out as its own line instead of being rejected (blueprint: "selisih ... dipisahkan").
"""

import itertools
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from apa.ingestion.llm_extract import MAX_AMOUNT
from apa.ingestion.receipt_extract import TOLERANCE, ReceiptItem, ReceiptRead, check_arithmetic

MAX_RECEIPT_PAGES = 3
# A tip/service charge printed only on the slip: up to 25% of the receipt total or Rp 10.000.
TIP_MAX_FRACTION = Decimal("0.25")
TIP_MAX_FLOOR = Decimal(10_000)
TIP_LINE_NAME = "Tip / biaya layanan (selisih slip pembayaran)"

Kind = Literal["receipt", "slip", "other"]


class ImageRead(BaseModel):
    """What the model read on ONE photo."""

    index: int = Field(ge=0, le=20)
    kind: Kind = "other"
    items: list[ReceiptItem] = Field(default_factory=list, max_length=200)
    subtotal: Decimal | None = Field(default=None, ge=0, le=MAX_AMOUNT)
    discount: Decimal = Field(default=Decimal(0), ge=0, le=MAX_AMOUNT)
    tax: Decimal = Field(default=Decimal(0), ge=0, le=MAX_AMOUNT)
    service_charge: Decimal = Field(default=Decimal(0), ge=0, le=MAX_AMOUNT)
    total: Decimal | None = Field(default=None, gt=0, le=MAX_AMOUNT)  # grand total, if printed on this photo
    slip_total: Decimal | None = Field(default=None, gt=0, le=MAX_AMOUNT)
    slip_time: str | None = Field(default=None, max_length=40)
    card_last4: str | None = Field(default=None, max_length=8)
    batch: str | None = Field(default=None, max_length=24)
    slip_kind: str | None = Field(default=None, max_length=12)

    @field_validator("kind", mode="before")
    @classmethod
    def _kind(cls, v: Any) -> str:
        v = str(v or "other").strip().lower()
        return v if v in ("receipt", "slip", "other") else "other"


class MultiRead(BaseModel):
    is_receipt: bool
    merchant: str | None = Field(default=None, max_length=128)
    purchased_at: str | None = Field(default=None, max_length=40)
    payment_method: str = "unknown"
    images: list[ImageRead] = Field(min_length=1, max_length=10)
    confidence: float = Field(ge=0, le=1)


@dataclass(frozen=True)
class Assembled:
    read: ReceiptRead  # the merged receipt, exactly as printed (no tip line)
    items: list[dict[str, Any]]  # payload items: name, quantity, unit_price, total_price, page
    roles: list[str | None]  # per attachment index; None = not evidence (a stray photo)
    final_total: Decimal  # what was actually charged: the slip's total when there is one
    verification: str  # single_verified | stitched_multi_page | dual_verified | split_reconciled
    slip: dict[str, Any] | None
    tip: Decimal | None
    pages: int


def _norm(name: str) -> str:
    return " ".join(name.casefold().split())


def _same(a: ReceiptItem, b: ReceiptItem) -> bool:
    return _norm(a.name) == _norm(b.name) and a.quantity == b.quantity and a.total_price == b.total_price


def max_overlap(prev: list[ReceiptItem], nxt: list[ReceiptItem]) -> int:
    """The most trailing lines of `prev` that are repeated as the leading lines of `nxt`."""
    for k in range(min(len(prev), len(nxt)), 0, -1):
        if all(_same(prev[len(prev) - k + j], nxt[j]) for j in range(k)):
            return k
    return 0


def overlap_candidates(pages: list[list[ReceiptItem]]) -> Iterator[list[int]]:
    """Plausible overlaps (lines to drop from the top of each later page), most de-duplication first.

    Two pages: every k from the maximal overlap down to 0. Three: each boundary either fully
    de-duplicated or not at all, so the search stays tiny.
    """
    kmax = [max_overlap(pages[i], pages[i + 1]) for i in range(len(pages) - 1)]
    if not kmax:
        yield []
    elif len(kmax) == 1:
        yield from ([k] for k in range(kmax[0], -1, -1))
    else:
        options = [sorted({k, 0}, reverse=True) for k in kmax]
        yield from sorted((list(c) for c in itertools.product(*options)), key=sum, reverse=True)


def stitch(pages: list[list[ReceiptItem]], overlaps: list[int]) -> tuple[list[ReceiptItem], list[int]]:
    """Merge the pages, dropping `overlaps[i]` leading lines of page i+1. Returns the merged items and,
    for each, the index (into `pages`) of the page it was read from."""
    merged, from_page = list(pages[0]), [0] * len(pages[0])
    for i, nxt in enumerate(pages[1:], start=1):
        kept = nxt[overlaps[i - 1]:]
        merged += kept
        from_page += [i] * len(kept)
    return merged, from_page


def assemble(m: MultiRead) -> Assembled | str:
    """One receipt from an album, or the reason it cannot be trusted (a `needs_review` reason)."""
    images = sorted(m.images, key=lambda im: im.index)
    receipt_pages = [im for im in images if im.kind == "receipt"]
    slips = [im for im in images if im.kind == "slip"]
    if not receipt_pages:
        return "no_receipt_page"  # a payment slip alone says what was paid, not what was bought
    if len(slips) > 1:
        return "too_many_slips"
    if len(receipt_pages) > MAX_RECEIPT_PAGES:
        return "too_many_pages"
    final_page = next((p for p in reversed(receipt_pages) if p.total is not None), None)
    if final_page is None:
        return "no_total"  # a long receipt prints its grand total only at the bottom of the last page

    best_reason: str | None = None
    chosen: tuple[ReceiptRead, list[int]] | None = None
    for overlaps in overlap_candidates([p.items for p in receipt_pages]):
        merged, from_page = stitch([p.items for p in receipt_pages], overlaps)
        read = ReceiptRead.model_validate(
            {
                "is_receipt": True, "merchant": m.merchant, "purchased_at": m.purchased_at,
                "items": [it.model_dump() for it in merged],
                "subtotal": final_page.subtotal, "discount": final_page.discount, "tax": final_page.tax,
                "service_charge": final_page.service_charge, "total": final_page.total,
                "payment_method": m.payment_method, "confidence": m.confidence,
            }
        )
        reason = check_arithmetic(read)
        if reason is None:
            chosen = (read, [receipt_pages[i].index for i in from_page])
            break
        best_reason = best_reason or reason  # report the failure of the most-de-duplicated reading
    if chosen is None:
        return best_reason or "total_mismatch"
    read, item_attachment = chosen

    roles: list[str | None] = [None] * (images[-1].index + 1)
    for n, p in enumerate(receipt_pages):
        roles[p.index] = "receipt_primary" if n == 0 else "receipt_continuation"

    items = [
        {"name": it.name, "quantity": str(it.quantity),
         "unit_price": str(it.unit_price) if it.unit_price is not None else None,
         "total_price": str(it.total_price), "page": item_attachment[i]}
        for i, it in enumerate(read.items)
    ]
    verification = "stitched_multi_page" if len(receipt_pages) > 1 else "single_verified"
    final_total, slip_info, tip = read.total, None, None

    if slips:
        slip = slips[0]
        if slip.slip_total is None:
            return "slip_unreadable"
        roles[slip.index] = "payment_proof_slip"
        diff = slip.slip_total - read.total
        if abs(diff) <= TOLERANCE:
            verification = "dual_verified"
        elif TOLERANCE < diff <= max(read.total * TIP_MAX_FRACTION, TIP_MAX_FLOOR):
            tip, verification = diff, "split_reconciled"
            items.append({"name": TIP_LINE_NAME, "quantity": "1", "unit_price": str(diff),
                          "total_price": str(diff), "page": slip.index})
        else:
            return "slip_total_mismatch"  # charged less than the receipt, or far more than a tip explains
        final_total = slip.slip_total  # what actually left the account
        slip_info = {"total": str(slip.slip_total), "time": slip.slip_time, "card_last4": slip.card_last4,
                     "batch": slip.batch, "kind": slip.slip_kind}

    return Assembled(read=read, items=items, roles=roles, final_total=final_total,
                     verification=verification, slip=slip_info, tip=tip, pages=len(receipt_pages))
