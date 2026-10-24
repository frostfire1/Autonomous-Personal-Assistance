"""Read-only view of the conversation: what the owner sent, what the system answered, and why.

One "exchange" = an inbound event, the replies it produced (summary, answer, error note, draft card) and the
transaction it became. `detail` adds the audit trail behind a transaction: which agents were invoked, their
scores and arguments, the Arbiter's reasoning.

Nothing here writes. /private sessions never appear because they are never stored."""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from apa.core.format import rupiah
from apa.db.models.audit import AgentDebateTranscript, ExplainabilityLog
from apa.db.models.ledger import Transaction
from apa.db.models.ops import InboundEvent, TelegramMessage

TEXT_LIMIT = 120  # a row of the list
ARGUMENT_LIMIT = 220  # one agent's argument in the detail view
TELEGRAM_LIMIT = 3800  # a Telegram message holds 4096; leave room for the footer


@dataclass
class Reply:
    kind: str
    body: str
    at: datetime


@dataclass
class TxInfo:
    id: int
    status: str
    merchant: str | None
    amount: Decimal
    category: str | None
    agent: str | None


@dataclass
class Exchange:
    event_id: int
    source: str
    status: str
    text: str
    at: datetime
    route: str
    reason: str | None = None
    latency_s: float | None = None
    replies: list[Reply] = field(default_factory=list)
    tx: TxInfo | None = None


@dataclass
class Turn:
    agent: str
    score: str | None
    confidence: str | None
    argument: str


@dataclass
class Detail:
    exchange: Exchange
    audit: list[str] = field(default_factory=list)  # one line per explainability row
    turns: list[Turn] = field(default_factory=list)
    tokens: int | None = None


def _text_of(ev: InboundEvent) -> str:
    p = ev.payload or {}
    if ev.source == "telegram_photo":
        n = len(p.get("attachments") or [])
        cap = p.get("caption")
        return f"📷 {n or 1} foto" + (f" · «{cap}»" if cap else "")
    if ev.source == "android_listener":
        return f"🔔 {p.get('package', '')}: {p.get('text', '')}"
    return p.get("text", "")


def _route(ev: InboundEvent, formula: str | None) -> str:
    """How the system handled it, in a few words."""
    p = ev.payload or {}
    parts: list[str] = []
    if ev.source == "telegram_photo":
        parts.append("vision")
    elif p.get("skip_reason") == "batch_draft":
        parts.append("daftar (kode)")
    elif p.get("chat_done"):
        parts.append(f"chat:{p.get('chat_role', '?')}")
    elif p.get("master_done"):
        parts.append("master→ekstraktor")
    elif (p.get("parsed") or {}).get("extraction", {}).get("method") == "llm":
        parts.append("ekstraktor LLM")
    elif p.get("parsed"):
        parts.append("regex")
    if formula == "fast_path_v1":
        parts.append("jalur cepat")
    elif formula:
        parts.append("debat")
    return " → ".join(parts) or ev.status


def _reason(ev: InboundEvent) -> str | None:
    p = ev.payload or {}
    return p.get("review_reason") or p.get("skip_reason") or ev.error


async def timeline(
    sm: async_sessionmaker, tz: ZoneInfo, *, limit: int = 10, before_id: int | None = None
) -> list[Exchange]:
    """Newest first. `before_id` pages backwards by event id."""
    async with sm() as s:
        q = select(InboundEvent).order_by(InboundEvent.id.desc()).limit(max(1, min(limit, 50)))
        if before_id is not None:
            q = q.where(InboundEvent.id < before_id)
        events = (await s.execute(q)).scalars().all()
        ids = [e.id for e in events]
        txs = {
            t.inbound_event_id: t
            for t in (await s.execute(select(Transaction).where(Transaction.inbound_event_id.in_(ids)))).scalars()
        }
        direct = (
            await s.execute(
                select(TelegramMessage).where(TelegramMessage.inbound_event_id.in_(ids)).order_by(TelegramMessage.id)
            )
        ).scalars().all()
        by_tx = (
            (await s.execute(select(TelegramMessage).where(TelegramMessage.transaction_id.in_([t.id for t in txs.values()]))))
            .scalars()
            .all()
            if txs
            else []
        )
        formulas = {
            r.transaction_id: r.consensus_formula
            for r in (
                await s.execute(
                    select(ExplainabilityLog).where(ExplainabilityLog.transaction_id.in_([t.id for t in txs.values()]))
                )
            ).scalars()
            if txs
        }
    tx_event = {t.id: eid for eid, t in txs.items()}
    replies: dict[int, dict[int, Reply]] = {}
    for m in [*direct, *by_tx]:
        eid = m.inbound_event_id or tx_event.get(m.transaction_id)
        if eid is not None and m.body:
            replies.setdefault(eid, {})[m.id] = Reply(m.kind, m.body, m.created_at)
    out = []
    for e in events:
        tx = txs.get(e.id)
        rs = sorted(replies.get(e.id, {}).values(), key=lambda r: r.at)
        out.append(
            Exchange(
                event_id=e.id, source=e.source, status=e.status, text=_text_of(e), at=e.received_at,
                route=_route(e, formulas.get(tx.id) if tx else None), reason=_reason(e),
                latency_s=(rs[0].at - e.received_at).total_seconds() if rs else None,
                replies=rs,
                tx=TxInfo(tx.id, tx.status, tx.merchant, tx.total_paid_amount, tx.primary_category,
                          tx.responsible_agent) if tx else None,
            )
        )
    return out


async def detail(sm: async_sessionmaker, tz: ZoneInfo, event_id: int) -> Detail | None:
    rows = await timeline(sm, tz, limit=1, before_id=event_id + 1)
    if not rows or rows[0].event_id != event_id:
        return None
    d = Detail(rows[0])
    if d.exchange.tx is None:
        return d
    async with sm() as s:
        tx = await s.get(Transaction, d.exchange.tx.id)
        logs = (
            await s.execute(
                select(ExplainabilityLog).where(ExplainabilityLog.transaction_id == tx.id).order_by(ExplainabilityLog.id)
            )
        ).scalars().all()
        turns = (
            (
                await s.execute(
                    select(AgentDebateTranscript)
                    .where(AgentDebateTranscript.debate_id == tx.debate_id)
                    .order_by(AgentDebateTranscript.id)
                )
            ).scalars().all()
            if tx.debate_id
            else []
        )
    for r in logs:
        d.audit.append(f"{r.decision_type}: {r.verdict} · {r.consensus_formula or '-'} · skor {r.final_score or '-'}")
        d.tokens = (d.tokens or 0) + (r.total_tokens or 0) or None
        d.audit.append(r.justification.splitlines()[0][:300] if r.justification else "")
    d.turns = [
        Turn(t.agent_id, str(t.score) if t.score is not None else None,
             str(t.confidence) if t.confidence is not None else None, (t.argument or "")[:ARGUMENT_LIMIT])
        for t in turns
    ]
    return d


# ---- text for Telegram ----------------------------------------------------------------------------------------


def _short(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def render_timeline(rows: list[Exchange], tz: ZoneInfo) -> str:
    if not rows:
        return "🗂️ Belum ada pesan."
    lines = ["🗂️ Riwayat terakhir (↩ pesanmu, → jawaban):"]
    for r in rows:
        lines.append(f"\n#{r.event_id} {r.at.astimezone(tz):%d/%m %H:%M} ← {_short(r.text, TEXT_LIMIT)}")
        lines.append(f"   ⚙ {r.route} · {r.status}" + (f" ({r.reason})" if r.reason and r.status != "committed" else ""))
        for rep in r.replies[:2]:
            lines.append(f"   → {_short(rep.body, TEXT_LIMIT * 2)}")
        if not r.replies:
            lines.append("   → (tidak ada balasan)")
    return "\n".join(lines)[:TELEGRAM_LIMIT]


def render_detail(d: Detail, tz: ZoneInfo) -> str:
    x = d.exchange
    lines = [
        f"🔍 #{x.event_id} · {x.at.astimezone(tz):%d/%m/%Y %H:%M:%S}",
        f"Masuk ({x.source}): {_short(x.text, 400)}",
        f"Jalur: {x.route} · status {x.status}" + (f" · alasan {x.reason}" if x.reason else ""),
    ]
    if x.latency_s is not None:
        lines.append(f"Waktu sampai balasan pertama: {x.latency_s:.1f} dtk")
    for rep in x.replies:
        lines.append(f"\n→ [{rep.kind}] {rep.body}")
    if x.tx:
        t = x.tx
        lines.append(
            f"\nTransaksi #{t.id}: {t.merchant or '-'} · {rupiah(t.amount)} · {t.category or '-'} · {t.status}"
            + (f" · agen {t.agent}" if t.agent else "")
        )
    if d.audit:
        lines += ["\nAudit:", *[f"  {a}" for a in d.audit if a]]
    if d.turns:
        lines.append("\nDebat agen:")
        lines += [f"  {t.agent} skor {t.score} yakin {t.confidence}: {t.argument}" for t in d.turns]
    if d.tokens:
        lines.append(f"\nToken: {d.tokens}")
    return "\n".join(lines)[:TELEGRAM_LIMIT]
