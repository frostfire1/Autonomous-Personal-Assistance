"""The data steward's eyes (GET). Finds data that fell through: messages that never finished, photos that
produced no transaction, expenses left without a category, transactions held only because the AI was down.

Read-only. Whether a ticket is still worth working on is decided from the attempts recorded on the event
(`payload['steward']`) or in the audit log, so a persistent problem is tried a few times and then reported,
never retried forever."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from apa.db.models.audit import ExplainabilityLog
from apa.db.models.ledger import Transaction
from apa.db.models.ops import InboundEvent

STUCK_AFTER = timedelta(minutes=15)  # a normal job finishes in seconds; a debate in well under a minute
IMAGE_WAIT = timedelta(minutes=2)  # let the normal retries finish first
IMAGE_WINDOW = timedelta(days=3)
HELD_WAIT = timedelta(minutes=10)
MAX_STUCK_ATTEMPTS = 3
MAX_IMAGE_ATTEMPTS = 2
MAX_RELEASE_ATTEMPTS = 3
MAX_CATEGORY_ATTEMPTS = 1
PER_KIND = 25

STUCK_STATUSES = ("received", "parsed", "processing", "extracting", "reading", "needs_llm", "reconciling")
# failures that a second look can plausibly fix (a misread, a blurry frame, a transient outage)
RETRYABLE_IMAGE = {
    "total_mismatch",
    "item_line_mismatch",
    "subtotal_mismatch",
    "no_items",
    "low_confidence",
    "invalid_output",
    "llm_unavailable",
    "image_unavailable",
    "no_total",
}

Kind = Literal["stuck", "failed_commit", "image", "uncategorized", "held"]


@dataclass(frozen=True)
class Ticket:
    kind: Kind
    event_id: int | None = None
    tx_id: int | None = None
    source: str | None = None
    status: str | None = None
    reason: str | None = None
    attempts: int = 0
    gave_up: bool = False  # the steward already tried everything for this one
    reported: bool = False  # and the owner was already told


def _steward(ev: InboundEvent) -> dict:
    return (ev.payload or {}).get("steward") or {}


async def _repairs(s, tx_ids: list[int], prefix: str) -> dict[int, int]:
    """How many times the steward already acted on each transaction for this kind of problem."""
    if not tx_ids:
        return {}
    rows = (
        await s.execute(
            select(ExplainabilityLog.transaction_id, func.count())
            .where(
                ExplainabilityLog.decision_type == "repair",
                ExplainabilityLog.transaction_id.in_(tx_ids),
                ExplainabilityLog.verdict.like(f"{prefix}%"),
            )
            .group_by(ExplainabilityLog.transaction_id)
        )
    ).all()
    return dict(rows)


async def find(sm: async_sessionmaker, now: datetime) -> list[Ticket]:
    out: list[Ticket] = []
    async with sm() as s:
        stuck = (
            (
                await s.execute(
                    select(InboundEvent)
                    .where(
                        InboundEvent.status.in_(STUCK_STATUSES), InboundEvent.received_at < now - STUCK_AFTER
                    )
                    .order_by(InboundEvent.id)
                    .limit(PER_KIND)
                )
            )
            .scalars()
            .all()
        )
        for e in stuck:
            st = _steward(e)
            if st.get("reported") or st.get("gave_up"):
                continue
            if e.received_at < now - timedelta(hours=12):
                continue
            n = (st.get("attempts") or {}).get("stuck", 0)
            if n < MAX_STUCK_ATTEMPTS:
                out.append(Ticket("stuck", event_id=e.id, source=e.source, status=e.status, attempts=n))

        failed = (
            (
                await s.execute(
                    select(InboundEvent)
                    .where(InboundEvent.status == "failed", InboundEvent.received_at < now - IMAGE_WAIT)
                    .order_by(InboundEvent.id)
                    .limit(PER_KIND)
                )
            )
            .scalars()
            .all()
        )
        for e in failed:
            n = (_steward(e).get("attempts") or {}).get("failed_commit", 0)
            if (e.payload or {}).get("parsed") and n < 2:
                out.append(
                    Ticket("failed_commit", event_id=e.id, source=e.source, status=e.status, attempts=n)
                )

        photos = (
            (
                await s.execute(
                    select(InboundEvent)
                    .where(
                        InboundEvent.source == "telegram_photo",
                        InboundEvent.status.in_(("needs_review", "failed")),
                        InboundEvent.received_at < now - IMAGE_WAIT,
                        InboundEvent.received_at > now - IMAGE_WINDOW,
                        ~exists().where(Transaction.inbound_event_id == InboundEvent.id),
                    )
                    .order_by(InboundEvent.id)
                    .limit(PER_KIND)
                )
            )
            .scalars()
            .all()
        )
        for e in photos:
            st = _steward(e)
            if st.get("reported") or st.get("gave_up"):
                continue
            p = e.payload or {}
            reason = p.get("review_reason") or p.get("skip_reason") or e.error
            if reason not in RETRYABLE_IMAGE and (
                st.get("reported") or st.get("gave_up") or e.received_at < now - timedelta(hours=12)
            ):
                continue
            out.append(
                Ticket(
                    "image",
                    event_id=e.id,
                    source=e.source,
                    status=e.status,
                    reason=reason,
                    attempts=(st.get("attempts") or {}).get("image", 0),
                    gave_up=bool(st.get("gave_up")),
                    reported=bool(st.get("reported")),
                )
            )

        uncat = (
            (
                await s.execute(
                    select(Transaction)
                    .where(
                        Transaction.status == "committed",
                        Transaction.direction == "out",
                        (Transaction.primary_category.is_(None))
                        | (Transaction.primary_category == "Uncategorized"),
                        Transaction.timestamp < now - timedelta(minutes=5),
                        Transaction.timestamp > now - timedelta(days=7),
                    )
                    .order_by(Transaction.id.desc())
                    .limit(PER_KIND)
                )
            )
            .scalars()
            .all()
        )
        if uncat:
            cat_repairs = await _repairs(s, [t.id for t in uncat], "repair:category")
            for tx in uncat:
                if cat_repairs.get(tx.id, 0) < MAX_CATEGORY_ATTEMPTS:
                    out.append(
                        Ticket(
                            "uncategorized",
                            tx_id=tx.id,
                            attempts=cat_repairs.get(tx.id, 0),
                        )
                    )

        held = (
            (
                await s.execute(
                    select(Transaction)
                    .where(
                        Transaction.status == "quarantined",
                        Transaction.notes == "quarantined: no_quorum",
                        Transaction.timestamp < now - HELD_WAIT,
                        Transaction.timestamp > now - timedelta(days=2),
                    )
                    .order_by(Transaction.id)
                    .limit(PER_KIND)
                )
            )
            .scalars()
            .all()
        )
        if held:
            rel_repairs = await _repairs(s, [t.id for t in held], "repair:release_requeued")
            for tx in held:
                if rel_repairs.get(tx.id, 0) < MAX_RELEASE_ATTEMPTS:
                    out.append(Ticket("held", tx_id=tx.id, attempts=rel_repairs.get(tx.id, 0)))

    return out
