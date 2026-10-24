"""Append to / verify explainability_log (hash-chained, append-only).

Appends are serialised with a transaction-scoped advisory lock so two concurrent commits can
never read the same `prev_hash` and fork the chain. The lock is taken last in a transaction,
after the row locks for wallets/budgets, so it cannot participate in a lock-order deadlock.
"""

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from apa.db.models.audit import ExplainabilityLog
from apa.safety.audit import compute_row_hash, verify_chain

# stable 63-bit key derived from a name, not a magic number
CHAIN_LOCK_KEY = int.from_bytes(hashlib.sha256(b"apa.explainability_log").digest()[:8], "big") >> 1


def log_content(
    *,
    decision_type: str,
    debate_id: UUID | str | None,
    transaction_id: int | None,
    agents_invoked: Sequence[str],
    agent_scores: dict[str, Any],
    consensus_formula: str | None,
    final_score: Decimal | None,
    verdict: str,
    justification: str,
    policy_version: int | None,
    total_tokens: int | None,
    cost_idr: Decimal | None,
    created_at: datetime,
) -> dict[str, Any]:
    """The exact fields covered by row_hash, normalised so a value read back from Postgres
    hashes identically to the value that was written."""
    return {
        "decision_type": decision_type,
        "debate_id": str(debate_id) if debate_id else None,
        "transaction_id": transaction_id,
        "agents_invoked": list(agents_invoked),
        "agent_scores": agent_scores,
        "consensus_formula": consensus_formula,
        "final_score": f"{final_score:.3f}" if final_score is not None else None,
        "verdict": verdict,
        "justification": justification,
        "policy_version": policy_version,
        "total_tokens": total_tokens,
        "cost_idr": f"{cost_idr:.4f}" if cost_idr is not None else None,
        "created_at": created_at.astimezone(UTC).isoformat(),
    }


def _content_from_row(r: ExplainabilityLog) -> dict[str, Any]:
    return log_content(
        decision_type=r.decision_type,
        debate_id=r.debate_id,
        transaction_id=r.transaction_id,
        agents_invoked=r.agents_invoked or [],
        agent_scores=r.agent_scores or {},
        consensus_formula=r.consensus_formula,
        final_score=r.final_score,
        verdict=r.verdict,
        justification=r.justification,
        policy_version=r.policy_version,
        total_tokens=r.total_tokens,
        cost_idr=r.cost_idr,
        created_at=r.created_at,
    )


async def append_explainability(
    session: AsyncSession,
    *,
    decision_type: str,
    verdict: str,
    justification: str,
    debate_id: UUID | None = None,
    transaction_id: int | None = None,
    agents_invoked: Sequence[str] = (),
    agent_scores: dict[str, Any] | None = None,
    consensus_formula: str | None = None,
    final_score: Decimal | None = None,
    policy_version: int | None = None,
    total_tokens: int | None = None,
    cost_idr: Decimal | None = None,
) -> ExplainabilityLog:
    await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": CHAIN_LOCK_KEY})
    prev = (
        await session.execute(
            select(ExplainabilityLog.row_hash).order_by(ExplainabilityLog.id.desc()).limit(1)
        )
    ).scalar_one_or_none()
    created_at = datetime.now(UTC)
    fields = dict(
        decision_type=decision_type,
        debate_id=debate_id,
        transaction_id=transaction_id,
        agents_invoked=list(agents_invoked),
        agent_scores=agent_scores or {},
        consensus_formula=consensus_formula,
        final_score=final_score,
        verdict=verdict,
        justification=justification,
        policy_version=policy_version,
        total_tokens=total_tokens,
        cost_idr=cost_idr,
    )
    row = ExplainabilityLog(
        **fields,
        created_at=created_at,
        prev_hash=prev,
        row_hash=compute_row_hash(prev, log_content(**fields, created_at=created_at)),
    )
    session.add(row)
    await session.flush()
    return row


async def verify_log(session: AsyncSession) -> int | None:
    """Re-walk the whole chain. Returns the id of the first row that fails, or None if intact."""
    rows = list(
        (await session.execute(select(ExplainabilityLog).order_by(ExplainabilityLog.id))).scalars()
    )
    bad = verify_chain([(r.prev_hash, _content_from_row(r), r.row_hash) for r in rows])
    return rows[bad].id if bad is not None else None
