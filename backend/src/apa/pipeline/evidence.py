"""Attach receipt evidence to a transaction: the bundle, the image file record and the line items."""

from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apa.db.models.evidence import EvidenceBundle, EvidenceFile
from apa.db.models.ledger import Transaction, TransactionItem
from apa.pipeline.candidate import Candidate

# A receipt and a bank notification for the same purchase arrive within hours of each other.
FUSION_WINDOW = timedelta(hours=12)


def _ocr_for(role: str, receipt: dict) -> dict | None:
    """What each image's record carries: the whole reading lives on the first page, the slip carries
    what it printed, continuation pages carry nothing of their own (their lines link to them)."""
    if role == "receipt_primary":
        return receipt
    return receipt.get("slip") if role == "payment_proof_slip" else None


# Verification states a payment slip established on its own; a bank notification cannot improve them.
SLIP_VERIFIED = ("dual_verified", "split_reconciled")


def verification_for(c: Candidate, *, fused_with_bank: bool) -> str:
    """The bundle's cross_verification_status. The reading already decided how well it stands on its own
    (single page, stitched pages, a matching payment slip, a slip with a tip split off). Being fused with
    a bank notification of the same total corroborates anything the slip had not already verified."""
    own = (c.receipt or {}).get("verification") or "single_verified"
    if own in SLIP_VERIFIED:
        return own
    return "dual_verified" if fused_with_bank else own


async def attach_evidence(
    s: AsyncSession, tx: Transaction, c: Candidate, *, verification: str
) -> EvidenceBundle:
    """Create the bundle, the file record(s) and the transaction_items for `c`'s receipt on `tx`.

    Each image gets the role the reading assigned it (`receipt['image_roles']`, aligned with the
    attachments: receipt page, continuation page, payment slip, or None for a stray photo that is not
    evidence). Each item links to the image it was read from. Needs tx.id (flush first). An image can
    back only one transaction: a unique index on its checksum makes a second attempt fail loudly
    instead of double counting.
    """
    receipt = c.receipt or {}
    roles = receipt.get("image_roles") or [
        "receipt_primary" if i == 0 else "receipt_continuation" for i in range(len(c.attachments))
    ]
    bundle = EvidenceBundle(
        transaction_id=tx.id,
        bundle_caption=c.caption,
        total_attachments=sum(1 for r in roles if r),
        cross_verification_status=verification,
    )
    s.add(bundle)
    await s.flush()

    files: dict[int, EvidenceFile] = {}
    for i, a in enumerate(c.attachments):
        role = roles[i] if i < len(roles) else None
        if role is None:
            continue
        files[i] = EvidenceFile(
            bundle_id=bundle.id,
            sequence_index=i,  # the order the photos were sent in
            file_role=role,
            file_storage_uri=a["uri"],
            sha256_checksum=a["sha256"],
            ocr_payload=_ocr_for(role, receipt),
        )
        s.add(files[i])
    await s.flush()
    first = next(iter(files.values()), None)

    for it in receipt.get("items", []):
        qty, total = Decimal(it["quantity"]), Decimal(it["total_price"])
        unit = (
            Decimal(it["unit_price"])
            if it.get("unit_price")
            else (total / qty).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        )
        source = files.get(it.get("page"), first)
        s.add(
            TransactionItem(
                transaction_id=tx.id,
                evidence_file_ref=source.id if source else None,
                item_name=it["name"],
                normalized_item_name=" ".join(it["name"].lower().split()),
                quantity=qty,
                unit_price=unit,
                total_price=total,
            )
        )
    tx.evidence_bundle_id = bundle.id
    await s.flush()
    return bundle


def _wallet_compatible(tx_wallet: str, receipt_wallet: str) -> bool:
    """An unknown-wallet receipt fits any bank transaction; a named one only its own wallet."""
    return receipt_wallet == "unknown" or tx_wallet == receipt_wallet


async def bank_transactions_for_receipt(s: AsyncSession, c: Candidate) -> list[Transaction]:
    """Committed bank/e-wallet transactions (from notifications) this receipt may belong to: same
    total, within the window, not yet backed by evidence. Row-locked so two receipts cannot take
    the same transaction. A cash receipt matches none: cash never produces a notification."""
    if c.wallet == "cash_wallet":
        return []
    rows = (
        await s.execute(
            select(Transaction)
            .where(
                Transaction.status == "committed",
                Transaction.direction == "out",
                Transaction.source_channel == "android_listener",
                Transaction.evidence_bundle_id.is_(None),
                Transaction.total_paid_amount == c.total,
                Transaction.timestamp.between(c.timestamp - FUSION_WINDOW, c.timestamp + FUSION_WINDOW),
            )
            .with_for_update()
        )
    ).scalars()
    return [t for t in rows if _wallet_compatible(t.payment_wallet, c.wallet)]


async def receipt_transactions_for_bank(s: AsyncSession, c: Candidate) -> list[Transaction]:
    """Committed receipt-created transactions whose wallet is still unknown that this bank
    notification completes: same total, within the window."""
    return list(
        (
            await s.execute(
                select(Transaction)
                .where(
                    Transaction.status == "committed",
                    Transaction.direction == "out",
                    Transaction.source_channel == "multi_image_receipt",
                    Transaction.payment_wallet == "unknown",
                    Transaction.total_paid_amount == c.total,
                    Transaction.timestamp.between(c.timestamp - FUSION_WINDOW, c.timestamp + FUSION_WINDOW),
                )
                .with_for_update()
            )
        ).scalars()
    )
