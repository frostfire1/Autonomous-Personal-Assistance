"""telegram_statement event -> read -> match against the ledger in three tiers -> write -> one report.

Same claim / work-outside-the-transaction / re-lock-and-write shape as the other jobs. Statuses on the event:
reconciling (in flight), reconciled, skipped (already done), needs_review (could not be trusted)."""

import asyncio
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from apa.core.logging import get_logger
from apa.core.settings import get_settings
from apa.db.models.ops import InboundEvent
from apa.ingestion.llm_extract import LLMUnavailable
from apa.integrations.evidence_store import EvidenceError, EvidenceStore
from apa.integrations.llm import LLMClient
from apa.integrations.notify import Notifier
from apa.pipeline.commit import record_reply
from apa.reconcile import apply as writer
from apa.reconcile.match import plan as make_plan
from apa.reconcile.statement import Statement, StatementRefused, read_statement, render_pdf

log = get_logger(__name__)


async def _locked(s, event_id: int) -> InboundEvent | None:
    return (
        await s.execute(select(InboundEvent).where(InboundEvent.id == event_id).with_for_update())
    ).scalar_one_or_none()


async def _finish(sm, event_id: int, status: str, extra: dict) -> None:
    async with sm() as s:
        ev = await _locked(s, event_id)
        ev.status, ev.processed_at = status, datetime.now(UTC)
        ev.payload = {**ev.payload, **extra}
        await s.commit()


async def run_reconcile(
    sm: async_sessionmaker, llm: LLMClient, notifier: Notifier, store: EvidenceStore, event_id: int,
    *, tz: ZoneInfo | None = None, job_try: int = 1, max_tries: int = 3,
) -> str:
    """Returns the event's resulting status. Raises LLMUnavailable while retries remain."""
    tz = tz or ZoneInfo(get_settings().timezone)
    async with sm() as s:
        ev = await _locked(s, event_id)
        if ev is None:
            return "missing"
        if not (ev.status == "received" or (ev.status == "reconciling" and job_try > 1)):
            return ev.status
        ev.status = "reconciling"
        payload = dict(ev.payload)
        await s.commit()

    att = payload["attachments"][0]
    try:
        data = await store.get(att["uri"])
        pages = await asyncio.to_thread(render_pdf, data) if att["mime"] == "application/pdf" else [(data, att["mime"])]
        stmt: Statement = await read_statement(llm, pages, payload.get("caption") or "")
    except (EvidenceError, KeyError):
        return await _refuse(sm, notifier, event_id, "file mutasi tidak bisa dibuka lagi")
    except StatementRefused as e:
        return await _refuse(sm, notifier, event_id, str(e))
    except LLMUnavailable:
        if job_try < max_tries:
            raise
        return await _refuse(sm, notifier, event_id, "layanan AI sedang tidak tersedia")

    if await writer.already_reconciled(sm, att["sha256"], stmt.wallet):
        await _finish(sm, event_id, "skipped", {"skip_reason": "already_reconciled"})
        await record_reply(sm, notifier, "Mutasi ini sudah pernah direkonsiliasi, jadi tidak diproses lagi.", event_id=event_id)
        return "skipped"

    start, end = min(ln.on for ln in stmt.lines), max(ln.on for ln in stmt.lines)
    ledger = writer.ledger_entries(await writer.ledger_window(sm, stmt.wallet, start, end, tz), tz)
    plan = make_plan(stmt.lines, ledger, allow_inject=bool(payload.get("import_all")))
    counts = await writer.apply_plan(sm, plan, wallet=stmt.wallet, uri=att["uri"], sha256=att["sha256"], tz=tz)
    await _finish(sm, event_id, "reconciled", {
        "reconcile": {"wallet": stmt.wallet, "lines": len(stmt.lines), "suspicious": plan.suspicious,
                      **{k: str(v) for k, v in counts.items()}}})
    log.info("reconciled", event_id=event_id, wallet=stmt.wallet, lines=len(stmt.lines), catchup=counts["catchup"])
    await record_reply(sm, notifier, writer.report(stmt.bank, len(stmt.lines), plan, counts, start, end), event_id=event_id)
    return "reconciled"


async def _refuse(sm, notifier, event_id: int, why: str) -> str:
    """Nothing was written. The owner is told why, once."""
    await _finish(sm, event_id, "needs_review", {"review_reason": why})
    await record_reply(sm, notifier, f"❓ Mutasi belum bisa direkonsiliasi: {why}. Tidak ada yang diubah.", event_id=event_id)
    return "needs_review"

