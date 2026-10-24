"""Read-only data for the web dashboard. Same key as /ingest. Nothing here writes, and nothing here can contain a /private
session: those exist only in the bot's memory and are never stored."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from apa import prefs as prefs_mod
from apa.core.db import SessionLocal
from apa.core.settings import get_settings
from apa.db.models.audit import ExplainabilityLog
from apa.db.models.budget import BudgetRule
from apa.db.models.health import SleepSession
from apa.db.models.ledger import Transaction
from apa.db.models.ops import InboundEvent
from apa.gateway.routers.ingest import require_ingest_key
from apa.news import config as news_config
from apa.news import cyber
from apa.news.briefing import weather_line
from apa.news.daily import daily_budget
from apa.prices.check import price_line

router = APIRouter(prefix="/dashboard", tags=["dashboard"], dependencies=[Depends(require_ingest_key)])


def _num(d: Decimal | None) -> int | float | None:
    if d is None:
        return None
    return int(d) if d == d.to_integral_value() else float(d)


def _tx(t: Transaction, tz: ZoneInfo) -> dict:
    return {
        "id": t.id, "time": t.timestamp.astimezone(tz).isoformat(), "merchant": t.merchant, "direction": t.direction,
        "amount": _num(t.total_paid_amount), "category": t.primary_category, "wallet": t.payment_wallet,
        "status": t.status, "source": t.source_channel, "mode": t.execution_mode,
    }


@router.get("/overview")
async def overview() -> dict:
    tz = ZoneInfo(get_settings().timezone)
    now = datetime.now(tz)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month0 = today.replace(day=1)
    out = (Transaction.status == "committed", Transaction.direction == "out")
    async with SessionLocal() as s:
        spent_today = (await s.execute(select(func.coalesce(func.sum(Transaction.total_paid_amount), 0)).where(
            *out, Transaction.timestamp >= today))).scalar_one()
        spent_month = (await s.execute(select(func.coalesce(func.sum(Transaction.total_paid_amount), 0)).where(
            *out, Transaction.timestamp >= month0))).scalar_one()
        income_month = (await s.execute(select(func.coalesce(func.sum(Transaction.total_paid_amount), 0)).where(
            Transaction.status == "committed", Transaction.direction == "in", Transaction.timestamp >= month0))).scalar_one()
        budgets = (await s.execute(select(BudgetRule).where(BudgetRule.period_month == month0.date())
                                   .order_by(BudgetRule.category))).scalars().all()
        recent = (await s.execute(select(Transaction).order_by(Transaction.id.desc()).limit(8))).scalars().all()
        waiting = dict((await s.execute(select(InboundEvent.status, func.count()).where(
            InboundEvent.status.in_(("needs_llm", "needs_review", "extracting", "reading", "reconciling")))
            .group_by(InboundEvent.status))).all())
        quarantined = (await s.execute(select(func.count()).select_from(Transaction).where(
            Transaction.status == "quarantined"))).scalar_one()
        sleep = (await s.execute(select(SleepSession).order_by(SleepSession.sleep_date.desc()).limit(1))).scalar_one_or_none()
    dg = await cyber.digest_24h(SessionLocal, datetime.now(UTC))
    choice = await prefs_mod.load(SessionLocal)
    day = await daily_budget(SessionLocal, tz, now)
    return {
        "daily": None if day is None else {
            "per_day": _num(day.per_day), "remaining": _num(day.remaining), "days_left": day.days_left,
            "spent_today": _num(day.spent_today),
            "categories": [{"category": c, "per_day": _num(p.quantize(Decimal(1))), "left": _num(r)} for c, p, r in day.categories[:6]],
        },
        "weather": await weather_line(choice, tz, None, choice.language),
        "today": today.date().isoformat(),
        "spent_today": _num(spent_today), "spent_month": _num(spent_month), "income_month": _num(income_month),
        "budgets": [{"category": b.category, "limit": _num(b.current_limit), "base": _num(b.base_limit), "spent": _num(b.spent_amount),
                     "percent": round(float(b.spent_amount / b.current_limit * 100), 1) if b.current_limit else None,
                     "essential": b.is_essential} for b in budgets],
        "recent": [_tx(t, tz) for t in recent],
        "waiting": waiting, "quarantined": quarantined,
        "sleep": None if sleep is None else {"date": sleep.sleep_date.isoformat(), "minutes": sleep.total_sleep_mins, "score": sleep.sleep_score},
        "cve": {"total": dg["total"], "critical": dg["by_severity"].get("critical", 0), "high": dg["by_severity"].get("high", 0), "kev": dg["kev"]},
        "price": await price_line(SessionLocal, now),
    }


@router.get("/transactions")
async def transactions(limit: int = Query(50, ge=1, le=200), before_id: int | None = Query(None, ge=1),
                       status: str | None = Query(None, pattern="^(committed|quarantined|rolled_back)$")) -> list[dict]:
    tz = ZoneInfo(get_settings().timezone)
    q = select(Transaction).order_by(Transaction.id.desc()).limit(limit)
    if before_id:
        q = q.where(Transaction.id < before_id)
    if status:
        q = q.where(Transaction.status == status)
    async with SessionLocal() as s:
        return [_tx(t, tz) for t in (await s.execute(q)).scalars()]


@router.get("/decisions")
async def decisions(limit: int = Query(40, ge=1, le=200)) -> list[dict]:
    """The audit trail: what was decided, by whom (agent scores), and why."""
    tz = ZoneInfo(get_settings().timezone)
    async with SessionLocal() as s:
        rows = (await s.execute(select(ExplainabilityLog).order_by(ExplainabilityLog.id.desc()).limit(limit))).scalars().all()
    return [{"id": r.id, "time": r.created_at.astimezone(tz).isoformat(), "type": r.decision_type, "verdict": r.verdict,
             "justification": r.justification, "transaction_id": r.transaction_id, "agents": list(r.agents_invoked or []),
             "scores": r.agent_scores or {}, "final_score": _num(r.final_score)} for r in rows]


@router.get("/health")
async def health(days: int = Query(14, ge=1, le=90)) -> list[dict]:
    async with SessionLocal() as s:
        rows = (await s.execute(select(SleepSession).where(
            SleepSession.sleep_date >= datetime.now(UTC).date() - timedelta(days=days)).order_by(SleepSession.sleep_date.desc()))).scalars().all()
    return [{"date": r.sleep_date.isoformat(), "minutes": r.total_sleep_mins, "score": r.sleep_score, "deep_pct": _num(r.deep_sleep_pct),
             "rem_pct": _num(r.rem_sleep_pct), "awake_mins": r.awake_mins, "fatigue_defense": r.fatigue_defense_active} for r in rows]


@router.get("/security")
async def security(limit: int = Query(20, ge=1, le=50)) -> dict:
    now = datetime.now(UTC)
    rule = (await news_config.current()).cve
    dg = await cyber.digest_24h(SessionLocal, now, rule)
    severe = await cyber.severe_24h(SessionLocal, now, limit=limit, rule=rule)
    return {"total": dg["total"], "by_severity": dg["by_severity"], "kev": dg["kev"],
            "severe": [{**r, "cvss": _num(r["cvss"]), "epss": _num(r["epss"])} for r in severe]}
