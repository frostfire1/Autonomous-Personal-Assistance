"""Parsed event -> circuit breakers -> debate -> Arbiter -> ledger (decisions.md D2/D3).

Shape of `run_commit`:
  1. claim the event            (short txn: parsed -> processing)
  2. gather context             (read-only; no txn is held while the LLMs run)
  3. lockdown + breakers        -> quarantine path, no debate, no budget/wallet effects
  4. debate + Arbiter           (expenses only)
  5. ONE write txn              ledger, wallet, budget, merchant visit, transcripts,
                                hash-chained explainability row, Telegram row, event status
  6. send the Telegram message  (after commit, so the /undo token already resolves)

`run_release` is the owner's "commit it anyway" for a quarantined transaction: same steps 2, 4, 5
minus the breakers, updating the existing row instead of inserting a new one.

Exceptions to "always debate" (decisions.md D2 addendum): income and ATM withdrawals are not
spending, so they commit deterministically with no debate and say so in the audit log.
"""

import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apa.agents.arbiter import Consensus, resolve
from apa.agents.debate import DEBATERS, DebateTurn, required_quorum, run_debate
from apa.core.format import WALLET_LABEL, rupiah
from apa.core.logging import get_logger
from apa.core.settings import get_settings
from apa.db.models.audit import AgentDebateTranscript
from apa.db.models.budget import BudgetRule
from apa.db.models.health import BiomarkerMetric, SleepSession
from apa.db.models.ledger import CashWallet, Transaction
from apa.db.models.ops import InboundEvent, TelegramMessage
from apa.ingestion.merchants import (
    MIN_SAMPLES_FOR_DEVIATION,
    baseline_context,
    find_merchant,
    record_visit,
)
from apa.integrations.llm import LLMClient
from apa.integrations.notify import Notifier
from apa.pipeline.candidate import (
    CASH_WALLET,
    BudgetLine,
    Candidate,
    build_debate_context,
    candidate_from_event,
)
from apa.pipeline.categories import DEFAULT_CATEGORIES, canonicalize
from apa.pipeline.effects import apply_effects, month_start
from apa.pipeline.evidence import (
    attach_evidence,
    bank_transactions_for_receipt,
    receipt_transactions_for_bank,
    verification_for,
)
from apa.safety.audit_log import append_explainability
from apa.safety.breakers import BreakerPolicy, BreakerResult, evaluate
from apa.safety.policy import load_policy, lockdown_active, set_lockdown

log = get_logger(__name__)

# Bank balances are not tracked, so "unknown" must never trip the liquidity floor.
UNKNOWN_BALANCE = Decimal(10**18)


class DebateUnavailable(Exception):
    """Fewer than the quorum of debaters answered and retries remain. The worker turns this into
    a delayed retry; nothing is committed or announced meanwhile."""


class NoQuorum(Exception):
    """Fewer than the quorum answered on the LAST try. The transaction is held (quarantined)
    rather than committed on a handful of votes; /release re-runs the debate later."""

    def __init__(self, answered: int, asked: int, quorum: int):
        super().__init__(f"{answered}/{asked} agents answered, quorum is {quorum}")
        self.answered, self.asked, self.quorum = answered, asked, quorum


@dataclass
class Gathered:
    policy: BreakerPolicy
    policy_version: int
    undo_hours: int
    lockdown: bool
    recent_times: list[datetime]
    cash_balance: Decimal | None  # None: no cash_wallets row, so cash is not tracked
    merchant_avg: Decimal | None = None
    merchant_std: Decimal | None = None
    baseline: str | None = None
    default_category: str | None = None
    default_agent: str | None = None
    budgets: list[BudgetLine] = field(default_factory=list)
    categories: list[str] = field(default_factory=lambda: list(DEFAULT_CATEGORIES))
    fatigue: bool = False  # slept badly: scrutinise impulsive discretionary spending (blueprint 2d)
    merchant_visits: int = 0  # for the fast path (blueprint v2 4.2)
    merchant_verified_at: datetime | None = None
    merchant_avg_raw: Decimal | None = None  # the mean even when there are too few visits for a stddev
    health_today: tuple[Decimal, Decimal, Decimal] | None = None


async def gather(s: AsyncSession, c: Candidate, tz: ZoneInfo) -> Gathered:
    policy, version, rules = await load_policy(s)
    recent = (
        await s.execute(
            select(Transaction.timestamp, Transaction.batch_id).where(
                Transaction.timestamp >= c.timestamp - timedelta(minutes=policy.velocity_window_min),
                Transaction.timestamp <= c.timestamp,
                Transaction.direction == "out",
                Transaction.status != "rolled_back",
            )
        )
    ).all()
    # Entries the owner confirmed together from one chat draft are one event for the velocity breaker:
    # a batch of six breakfasts is not a runaway. Each of them still meets every other breaker on its own.
    batches: dict[UUID, datetime] = {}
    times = [ts for ts, b in recent if b is None]
    for ts, b in recent:
        if b is not None and b not in batches:
            batches[b] = ts
    times += batches.values()
    mine = UUID(c.batch_id) if c.batch_id else None
    counted = [] if mine is not None and mine in batches else [c.timestamp]
    g = Gathered(
        policy=policy,
        policy_version=version,
        undo_hours=int(rules.get("undo_window_hours", get_settings().undo_window_hours)),
        lockdown=await lockdown_active(s),
        recent_times=[*times, *counted],  # the candidate counts toward its own velocity (once per batch)
        cash_balance=None,
    )
    if c.wallet == CASH_WALLET:
        g.cash_balance = (
            await s.execute(select(CashWallet.current_balance).where(CashWallet.wallet_name == CASH_WALLET))
        ).scalar_one_or_none()
    if c.name:
        row = await find_merchant(s, c.name)
        if row is not None:
            g.default_category, g.default_agent = row.default_category, row.default_agent
            g.merchant_visits, g.merchant_verified_at = row.visit_count, row.last_verified_at
            g.merchant_avg_raw = row.avg_amount
            if row.visit_count >= MIN_SAMPLES_FOR_DEVIATION and row.stddev_amount:
                g.merchant_avg, g.merchant_std = row.avg_amount, row.stddev_amount
        g.baseline = await baseline_context(s, c.name)
    g.budgets = [
        BudgetLine(b.category, b.current_limit, b.spent_amount)
        for b in (
            await s.execute(
                select(BudgetRule)
                .where(BudgetRule.period_month == month_start(c.timestamp, tz))
                .order_by(BudgetRule.category)
            )
        ).scalars()
    ]
    if g.budgets:  # the user's own categories win over the built-in list
        g.categories = [b.category for b in g.budgets]
    bio = (
        await s.execute(
            select(BiomarkerMetric).where(BiomarkerMetric.log_date == c.timestamp.astimezone(tz).date())
        )
    ).scalar_one_or_none()
    if bio is not None:
        g.health_today = (bio.cumulative_caffeine_mg, bio.cumulative_sugar_g, bio.cumulative_sodium_mg)
    day = c.timestamp.astimezone(tz).date()
    g.fatigue = bool(
        (
            await s.execute(
                select(SleepSession.fatigue_defense_active).where(
                    SleepSession.sleep_date.in_([day, day - timedelta(days=1)]),
                    SleepSession.fatigue_defense_active.is_(True),
                ).limit(1)
            )
        ).scalar_one_or_none()
    )
    return g


def decide_breakers(c: Candidate, g: Gathered) -> BreakerResult:
    """Income can never harm the ledger, so it skips every breaker. An active lockdown holds
    everything else; otherwise the policy breakers run."""
    if c.kind == "income":
        return BreakerResult.ok()
    if g.lockdown:
        return BreakerResult(True, "lockdown", "quarantine")
    cash_expense = c.kind == "expense" and c.wallet == CASH_WALLET
    return evaluate(
        amount=c.total,
        balance_after=(g.cash_balance - c.total)
        if (cash_expense and g.cash_balance is not None)
        else UNKNOWN_BALANCE,
        recent_tx_times=g.recent_times,
        now=c.timestamp,
        merchant_avg=g.merchant_avg,
        merchant_stddev=g.merchant_std,
        policy=g.policy,
        is_expense=cash_expense,
    )


FAST_PATH = "fast_path_v1"


def fast_path(c: Candidate, g: Gathered) -> Consensus | None:
    """Blueprint v2 4.2: a known, recently verified merchant charging a normal amount is categorised from the
    merchant cache, with no model call. Everything unusual keeps the full debate: a receipt (its lines are
    audited), a split or social tag, a merchant never verified or verified over `ttl` days ago, an amount
    beyond 2 sigma (or beyond 2x the mean while there are too few visits for a sigma), or a spend that would
    take its category over budget. The breakers have already run."""
    s = get_settings()
    if not s.fast_path_enabled or c.kind != "expense" or c.is_receipt or c.mentions:
        return None
    cat = canonicalize(g.default_category, [*DEFAULT_CATEGORIES, *g.categories])
    if cat is None or not g.default_agent or g.merchant_avg_raw is None or g.merchant_verified_at is None:
        return None
    if g.merchant_visits < s.fast_path_min_visits:
        return None
    if c.timestamp - g.merchant_verified_at > timedelta(days=s.fast_path_ttl_days):
        return None
    if g.merchant_std is not None:
        if abs(c.total - g.merchant_avg_raw) > Decimal(str(s.fast_path_max_sigma)) * g.merchant_std:
            return None
    elif c.total > 2 * g.merchant_avg_raw:
        return None
    line = next((b for b in g.budgets if b.category == cat), None)
    if line is not None and line.spent + c.total > line.limit:
        return None
    facts = {
        "visits": str(g.merchant_visits),
        "avg": str(g.merchant_avg_raw),
        "verified": g.merchant_verified_at.date().isoformat(),
    }
    return Consensus(FAST_PATH, Decimal("0.900"), cat, False, {"fast_path": facts}, g.default_agent)


def build_justification(consensus: Consensus, turns: list[DebateTurn], category: str) -> str:
    if consensus.formula == FAST_PATH:
        f = consensus.scores["fast_path"]
        return (
            f"{FAST_PATH}: known merchant ({f['visits']} visits, mean {f['avg']}, verified {f['verified']}), "
            f"amount within range and budget; category={category} lead={consensus.lead_agent}; no debate"
        )
    lines = [
        f"{consensus.formula}: final={consensus.final_score} category={category} "
        f"lead={consensus.lead_agent or '-'}"
    ]
    lines += [
        f"{t.agent_id} score={t.vote.approve_score} conf={t.vote.confidence}: {t.argument[:300]}"
        for t in turns
    ]
    if not turns:
        lines.append("no agent produced a valid vote; committed flagged with a default category")
    if consensus.flagged:
        lines.append("flagged: weighted approval is below the threshold")
    return "\n".join(lines)


def build_summary(c: Candidate, category: str, lead: str | None, token: str, flagged: bool) -> str:
    """The one Telegram line per transaction (blueprint 4.2c)."""
    title = c.name or "Transaksi"
    if c.kind == "income":
        body = f"Pemasukan: {title} · +{rupiah(c.total)} · Ke: {WALLET_LABEL.get(c.wallet, c.wallet)}"
    elif c.kind == "withdrawal":
        body = f"Tarik tunai · {rupiah(c.total)} · Dari: {WALLET_LABEL.get(c.wallet, c.wallet)}"
    else:
        body = (
            f"{title} · {rupiah(c.total)} · Pos: {category}"
            + (f" ({lead})" if lead else "")
            + f" · Kasir: {WALLET_LABEL.get(c.wallet, c.wallet)}"
        )
    return f"✅ Dicatat: {body}" + (" · ⚠ konsensus rendah" if flagged else "") + f" · Undo: /undo_{token}"


def build_alert(c: Candidate, reason: str, token: str | None = None) -> str:
    what = f"{c.name or 'Transaksi'} · {rupiah(c.total)}"
    if reason == "velocity":
        text = f"🚨 LOCKDOWN darurat: terlalu banyak transaksi dalam waktu singkat. Ditahan: {what}"
    elif reason == "lockdown":
        text = f"🚨 Mode lockdown aktif, transaksi ditahan: {what}"
    elif reason == "ambiguous_fusion":
        text = f"⚠️ Cocok dengan lebih dari satu catatan (struk atau notifikasi bank), ditahan: {what}"
    elif reason == "no_quorum":
        text = f"⚠️ Debat agen tidak mencapai kuorum (AI lambat atau tidak tersedia), ditahan: {what}"
    else:
        text = f"🚨 Transaksi ditahan ({reason}): {what}"
    if reason in ("velocity", "lockdown"):
        text += " · /unlock untuk mencabut lockdown"
    if token:
        text += f" · /release_{token} untuk melepas · /dismiss_{token} untuk membuang"
    return text


async def _claim(sm: async_sessionmaker, event_id: int, job_try: int):
    async with sm() as s:
        ev = (
            await s.execute(select(InboundEvent).where(InboundEvent.id == event_id).with_for_update())
        ).scalar_one_or_none()
        if ev is None:
            return "missing", None
        if not (ev.status == "parsed" or (ev.status == "processing" and job_try > 1)):
            return ev.status, None
        ev.status = "processing"
        claimed = (ev.source, dict(ev.payload), ev.received_at)
        await s.commit()
        return "claimed", claimed


async def _locked_processing_event(s: AsyncSession, event_id: int) -> InboundEvent | None:
    """Re-lock inside the write txn; if someone else already finished it, do nothing."""
    ev = (
        await s.execute(select(InboundEvent).where(InboundEvent.id == event_id).with_for_update())
    ).scalar_one()
    return ev if ev.status == "processing" else None


async def send_and_record(sm: async_sessionmaker, notifier: Notifier, tm_id: int, text: str) -> None:
    """Best effort: a Telegram outage must not fail or re-run a committed transaction."""
    try:
        message_id = await notifier.send(text)
    except Exception as e:
        log.warning("notify_failed", error=str(e))
        return
    if message_id is not None:
        async with sm() as s:
            await s.execute(
                update(TelegramMessage).where(TelegramMessage.id == tm_id).values(message_id=message_id)
            )
            await s.commit()


async def record_reply(
    sm: async_sessionmaker, notifier: Notifier, text: str, *, event_id: int | None = None
) -> None:
    """A direct answer to something the owner did (not a ledger announcement). Stored as
    telegram_messages.kind = 'reply', then sent best-effort."""
    async with sm() as s:
        tm = TelegramMessage(
            direction="out", chat_id=get_settings().telegram_owner_id, kind="reply", body=text,
            inbound_event_id=event_id,
        )
        s.add(tm)
        await s.flush()
        tm_id = tm.id
        await s.commit()
    await send_and_record(sm, notifier, tm_id, text)


async def _write_quarantine(
    sm, notifier, event_id: int, c: Candidate, g: Gathered, r: BreakerResult, detail: str | None = None
):
    async with sm() as s:
        ev = await _locked_processing_event(s, event_id)
        if ev is None:
            return "already_handled"
        tx = Transaction(
            timestamp=c.timestamp,
            source_channel=c.source_channel,
            payment_wallet=c.wallet,
            merchant=c.name,
            direction=c.direction,
            total_paid_amount=c.total,
            # held amount: releasing later is a status flip. Reports must filter status='committed'.
            net_user_expense=c.total if c.kind == "expense" else Decimal(0),
            primary_category=None,
            responsible_agent="A-01",
            execution_mode="circuit_override",
            circuit_breaker_cleared=False,
            status="quarantined",
            notes=f"quarantined: {r.reason}",
            inbound_event_id=event_id,
            batch_id=UUID(c.batch_id) if c.batch_id else None,
        )
        s.add(tx)
        await s.flush()
        await append_explainability(
            s,
            decision_type="quarantine",
            transaction_id=tx.id,
            verdict=f"quarantine:{r.reason}",
            justification=(
                f"{detail}; held (action={r.action}), no wallet/budget effect"
                if detail
                else f"circuit breaker '{r.reason}' tripped (action={r.action}); "
                f"debate deferred, no wallet/budget effect"
            ),
            policy_version=g.policy_version,
        )
        if r.reason == "velocity":
            await set_lockdown(s, True, by="circuit_breaker")
            await append_explainability(
                s,
                decision_type="config_change",
                transaction_id=tx.id,
                verdict="lockdown_active=1",
                justification="velocity breaker tripped: emergency lockdown on until cleared manually",
                policy_version=g.policy_version,
            )
        token = secrets.token_hex(3)  # /release_<token>, /dismiss_<token>
        tm = TelegramMessage(
            direction="out",
            chat_id=get_settings().telegram_owner_id,
            kind="alert",
            transaction_id=tx.id,
            undo_token=token,
            body=build_alert(c, r.reason or "unknown", token),
        )
        s.add(tm)
        ev.status, ev.processed_at = "quarantined", datetime.now(UTC)
        await s.flush()
        tm_id, text = tm.id, tm.body
        await s.commit()
    await send_and_record(sm, notifier, tm_id, text)
    return "quarantined"


async def _write_commit(
    sm,
    notifier,
    c: Candidate,
    g: Gathered,
    tz: ZoneInfo,
    consensus: Consensus | None,
    turns: list[DebateTurn],
    *,
    event_id: int | None = None,
    release_tx_id: int | None = None,
    via: str | None = None,
):
    """Commit a new transaction (event_id) or promote a quarantined one (release_tx_id)."""
    expense = c.kind == "expense"
    now = datetime.now(UTC)
    async with sm() as s:
        tx: Transaction | None = None
        old_reason = ""
        if release_tx_id is None:
            ev = await _locked_processing_event(s, event_id)
            if ev is None:
                return "already_handled"
        else:
            tx = (
                await s.execute(select(Transaction).where(Transaction.id == release_tx_id).with_for_update())
            ).scalar_one()
            if tx.status != "quarantined":
                return "already_handled"
            old_reason = (tx.notes or "").removeprefix("quarantined: ")
            ev = (
                await s.execute(
                    select(InboundEvent).where(InboundEvent.id == tx.inbound_event_id).with_for_update()
                )
            ).scalar_one()

        owner_category = canonicalize(c.category, [*DEFAULT_CATEGORIES, *g.categories]) if expense else None
        category = (
            (owner_category or (consensus.category if consensus else None) or "Uncategorized")
            if expense
            else {"income": "Income", "withdrawal": "Cash Withdrawal"}[c.kind]
        )
        lead = consensus.lead_agent if consensus else None
        debate_id: UUID | None = uuid4() if expense else None
        flagged = bool(consensus and consensus.flagged)
        # A withdrawal's bank fee is the only real expense in it; the rest is a transfer to cash.
        net = c.total if expense else (c.fee if c.kind == "withdrawal" else Decimal(0))
        notes = "; ".join(
            n
            for n in (
                f"released via {via} (was {old_reason})" if release_tx_id else None,
                "flagged: low consensus" if flagged else None,
                f"fee {rupiah(c.fee)} included in total" if c.fee else None,
                f"batch label: {', '.join(c.tags)}" if c.batch_id and c.tags else None,
                "receipt lines unreadable: total only" if (c.receipt or {}).get("total_only") else None,
            )
            if n
        )
        fields = dict(
            timestamp=c.timestamp,
            source_channel=c.source_channel,
            payment_wallet=c.wallet,
            merchant=c.name,
            direction=c.direction,
            total_paid_amount=c.total,
            net_user_expense=net,
            primary_category=category,
            responsible_agent=(lead or "A-10") if expense else "A-01",
            execution_mode="consensus_auto" if expense else "deterministic_transfer",
            confidence_score=(
                consensus.final_score if consensus else Decimal(str(round(c.parse_confidence, 3)))
            ),
            circuit_breaker_cleared=True,
            status="committed",
            debate_id=debate_id,
            reversibility_deadline=now + timedelta(hours=g.undo_hours),
            inbound_event_id=ev.id,
            notes=notes or None,
            batch_id=UUID(c.batch_id) if c.batch_id else None,
        )
        if tx is None:
            tx = Transaction(**fields)
            s.add(tx)
        else:
            for k, v in fields.items():
                setattr(tx, k, v)
        await s.flush()

        await apply_effects(s, tx, sign=+1, tz=tz, now=now)
        if c.is_receipt:  # the items and the image that backs this transaction
            await attach_evidence(s, tx, c, verification=verification_for(c, fused_with_bank=False))
        if expense:
            if c.name:
                # never teach a merchant that its category is the placeholder: it would block learning
                # a real one later (a learned default is only ever set once)
                learned = None if category == "Uncategorized" else category
                verified = consensus is not None and consensus.formula != FAST_PATH
                await record_visit(
                    s, c.name, c.total, c.timestamp, category=learned, agent=lead, verified=verified
                )
            for t in turns:
                s.add(
                    AgentDebateTranscript(
                        debate_id=debate_id,
                        subject_type="transaction",
                        subject_ref=tx.id,
                        round_no=1,
                        agent_id=t.agent_id,
                        model=t.model[:48],
                        stance="approve" if t.vote.approve_score >= Decimal("0.5") else "object",
                        score=t.vote.approve_score,
                        confidence=t.vote.confidence,
                        argument=t.argument,
                        structured_output=t.raw,
                        prompt_tokens=t.prompt_tokens,
                        completion_tokens=t.completion_tokens,
                        cached_tokens=t.cached_tokens,
                        latency_ms=t.latency_ms,
                    )
                )

        tokens = sum((t.prompt_tokens or 0) + (t.completion_tokens or 0) for t in turns) or None
        justification = (
            build_justification(consensus, turns, category)
            if consensus
            else f"{c.kind}: not spending, committed deterministically without a debate"
        )
        if release_tx_id:
            justification = f"released from quarantine ({old_reason}) by owner via {via}\n{justification}"
        await append_explainability(
            s,
            decision_type="release" if release_tx_id else "ledger_commit",
            transaction_id=tx.id,
            debate_id=debate_id,
            agents_invoked=[t.agent_id for t in turns],
            agent_scores=consensus.scores if consensus else {},
            consensus_formula=consensus.formula if consensus else None,
            final_score=consensus.final_score if consensus else None,
            verdict=f"{'release' if release_tx_id else 'commit'}:{category}",
            justification=justification,
            policy_version=g.policy_version,
            total_tokens=tokens,
        )

        token = secrets.token_hex(3)
        summary = build_summary(c, category, lead, token, flagged)
        tm = TelegramMessage(
            direction="out",
            chat_id=get_settings().telegram_owner_id,
            kind="summary",
            transaction_id=tx.id,
            undo_token=token,
            body=summary,
        )
        s.add(tm)
        ev.status, ev.processed_at = "committed", now
        await s.flush()
        tm_id = tm.id
        await s.commit()
    await send_and_record(sm, notifier, tm_id, summary)
    return "committed"


async def _try_fuse(sm, notifier, event_id: int, c: Candidate, g: Gathered):
    """Receipt <-> bank notification fusion: the same purchase must not be counted twice.

    A receipt photo that matches an existing bank transaction (same total, within 12 h, not yet
    backed by evidence) is ATTACHED to it. A bank notification that matches a receipt-created
    transaction whose wallet is still unknown COMPLETES it. Exactly one match fuses; none falls
    through to a normal new transaction; more than one is held (quarantined) for the owner, because
    guessing would either double count or lose a purchase. Returns None to continue, else a status.
    """
    if c.kind != "expense":
        return None
    if c.is_receipt:
        finder, direction = bank_transactions_for_receipt, "receipt->bank"
    elif c.source_channel == "android_listener":
        finder, direction = receipt_transactions_for_bank, "bank->receipt"
    else:
        return None

    ambiguous = 0
    reply_id, reply = None, None
    async with sm() as s:
        ev = await _locked_processing_event(s, event_id)
        if ev is None:
            return "already_handled"
        matches = await finder(s, c)
        if not matches:
            return None
        if len(matches) > 1:
            ambiguous = len(matches)  # leave the transaction to the quarantine path below
        else:
            (tx,) = matches
            now = datetime.now(UTC)
            if direction == "receipt->bank":
                await attach_evidence(s, tx, c, verification=verification_for(c, fused_with_bank=True))
                n = len((c.receipt or {}).get("items", []))
                justification = (
                    f"receipt total {rupiah(c.total)} matches the bank notification of transaction "
                    f"#{tx.id} ({tx.payment_wallet}) within 12 h; attached {n} items and the image, "
                    f"no new transaction"
                )
                reply = f"🧾 Struk dilampirkan ke {tx.merchant or 'transaksi'} · {rupiah(c.total)} ({n} item)"
            else:
                tx.payment_wallet = c.wallet
                confirmed = f"wallet {WALLET_LABEL.get(c.wallet, c.wallet)} confirmed by bank"
                tx.notes = "; ".join(part for part in (tx.notes, confirmed) if part)
                justification = (
                    f"bank notification {rupiah(c.total)} via {c.wallet} completes receipt transaction "
                    f"#{tx.id} (wallet was unknown) within 12 h, no new transaction"
                )
            await append_explainability(
                s,
                decision_type="fusion",
                transaction_id=tx.id,
                verdict=f"fuse:{direction}",
                justification=justification,
                policy_version=g.policy_version,
            )
            ev.status, ev.processed_at = "committed", now
            ev.payload = {**ev.payload, "fused_into": tx.id}
            if reply:
                tm = TelegramMessage(
                    direction="out", chat_id=get_settings().telegram_owner_id, kind="reply", body=reply
                )
                s.add(tm)
                await s.flush()
                reply_id = tm.id
            await s.commit()
    if ambiguous:
        return await _write_quarantine(
            sm,
            notifier,
            event_id,
            c,
            g,
            BreakerResult(True, "ambiguous_fusion", "quarantine"),
            detail=f"{rupiah(c.total)} matches {ambiguous} existing transactions: fusion is ambiguous",
        )
    if reply_id is not None:
        await send_and_record(sm, notifier, reply_id, reply)
    return "committed"


async def decide(
    llm: LLMClient,
    c: Candidate,
    g: Gathered,
    tz: ZoneInfo,
    job_try: int,
    max_tries: int,
    *,
    allow_fast: bool = True,
):
    """Debate + Arbiter for an expense; (None, []) for income and withdrawals.

    Needs a quorum of votes. Short of it: DebateUnavailable while retries remain (the retry asks
    all five again), NoQuorum on the last try. It never decides on fewer votes than the quorum."""
    if c.kind != "expense":
        return None, []
    if allow_fast and (fast := fast_path(c, g)) is not None:
        return fast, []
    turns = await run_debate(
        llm,
        build_debate_context(
            c,
            tz=tz,
            baseline=g.baseline,
            budgets=g.budgets,
            health_today=g.health_today,
            categories=g.categories,
            fatigue=g.fatigue,
        ),
        g.categories,
    )
    need = required_quorum()
    if len(turns) < need:
        if job_try < max_tries:
            raise DebateUnavailable(f"{len(turns)}/{len(DEBATERS)} agents answered, need {need}")
        raise NoQuorum(len(turns), len(DEBATERS), need)
    # a merchant default learned before categories were constrained may not be on the list
    return resolve([t.vote for t in turns], canonicalize(g.default_category, g.categories)), turns


async def run_commit(
    sm: async_sessionmaker,
    llm: LLMClient,
    notifier: Notifier,
    event_id: int,
    *,
    job_try: int = 1,
    max_tries: int = 3,
    tz: ZoneInfo | None = None,
) -> str:
    """Returns the final event status. Safe to call again for the same event."""
    tz = tz or ZoneInfo(get_settings().timezone)
    state, claimed = await _claim(sm, event_id, job_try)
    if claimed is None:
        return state
    source, payload, received_at = claimed
    try:
        c = candidate_from_event(source, payload, received_at)
        async with sm() as s:
            g = await gather(s, c, tz)

        fused = await _try_fuse(sm, notifier, event_id, c, g)
        if fused is not None:
            return fused

        result = decide_breakers(c, g)
        if result.tripped:
            return await _write_quarantine(sm, notifier, event_id, c, g, result)

        try:
            consensus, turns = await decide(llm, c, g, tz, job_try, max_tries)
        except NoQuorum as nq:  # held, not committed: the owner can /release once the LLM is healthy
            log.warning("debate_no_quorum", event_id=event_id, answered=nq.answered, job_try=job_try)
            return await _write_quarantine(
                sm,
                notifier,
                event_id,
                c,
                g,
                BreakerResult(True, "no_quorum", "quarantine"),
                detail=f"debate quorum not reached after {job_try} tries: {nq}",
            )
        return await _write_commit(sm, notifier, c, g, tz, consensus, turns, event_id=event_id)
    except DebateUnavailable:
        raise
    except Exception as e:
        async with sm() as s:  # make the failure visible instead of leaving it 'processing'
            await s.execute(
                update(InboundEvent)
                .where(InboundEvent.id == event_id)
                .values(status="failed", error=repr(e)[:500])
            )
            await s.commit()
        raise


async def run_release(
    sm: async_sessionmaker,
    llm: LLMClient,
    notifier: Notifier,
    tx_id: int,
    *,
    via: str = "telegram",
    job_try: int = 1,
    max_tries: int = 3,
    tz: ZoneInfo | None = None,
) -> str:
    """Owner override for a quarantined transaction: debate (if an expense) and commit it now,
    skipping the breakers. Returns 'committed', or the transaction's current status if it is
    not quarantined (so a double tap is harmless), or 'not_found'."""
    tz = tz or ZoneInfo(get_settings().timezone)
    async with sm() as s:
        tx = await s.get(Transaction, tx_id)
        if tx is None:
            return "not_found"
        if tx.status != "quarantined":
            return tx.status
        ev = await s.get(InboundEvent, tx.inbound_event_id)
        c = candidate_from_event(ev.source, dict(ev.payload), ev.received_at)
        g = await gather(s, c, tz)
    try:
        # a release is the owner overriding a held transaction: it always gets the full debate
        consensus, turns = await decide(llm, c, g, tz, job_try, max_tries, allow_fast=False)
    except NoQuorum as nq:  # it stays held; say so, because /release was the owner's own action
        await record_reply(
            sm,
            notifier,
            f"⚠️ /release belum bisa: debat tidak mencapai kuorum ({nq.answered} dari {nq.asked} agen "
            f"menjawab). Transaksi tetap ditahan, coba lagi nanti: {tx.merchant or 'Transaksi'} · "
            f"{rupiah(tx.total_paid_amount)}",
        )
        return "quarantined"
    # 'already_handled' if a concurrent release won the race while we were debating
    return await _write_commit(sm, notifier, c, g, tz, consensus, turns, release_tx_id=tx_id, via=via)
