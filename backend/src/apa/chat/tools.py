"""Read-only lookups the assistant may call while it talks to the owner. Each one is plain SQL (or the existing news and CVE
readers): the model chooses WHICH question to ask of the data, code answers it, and the model only words the result. So a
figure in a reply always comes from the ledger, never from the model, and nothing here can change anything.

Results are short and bounded (row limits, character caps); merchant names and headlines inside them are untrusted text."""

from datetime import datetime, time, timedelta
from decimal import Decimal
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from apa.core.format import rupiah
from apa.db.models.budget import BudgetRule
from apa.db.models.health import SleepSession
from apa.db.models.ledger import CashWallet, Transaction

MAX_RESULT_CHARS = 2500


class _A(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TransactionsQ(_A):
    days: int = Field(default=7, ge=1, le=365)
    category: str | None = Field(default=None, max_length=48)
    merchant: str | None = Field(default=None, max_length=80)
    direction: Literal["out", "in"] | None = None
    min_amount: Decimal | None = Field(default=None, ge=0)
    max_amount: Decimal | None = Field(default=None, ge=0)
    limit: int = Field(default=10, ge=1, le=25)


class SpendingQ(_A):
    period: Literal["today", "week", "month"] = "month"
    by: Literal["category", "merchant", "day"] = "category"
    direction: Literal["out", "in"] = "out"


class DaysQ(_A):
    days: int = Field(default=7, ge=1, le=30)


class LimitQ(_A):
    limit: int = Field(default=5, ge=1, le=10)


class NewsQ(_A):
    topic: str = Field(default="nasional", max_length=24)
    limit: int = Field(default=5, ge=1, le=10)


class NoArgs(_A):
    pass


READ_TOOLS: dict[str, type[_A]] = {
    "transactions": TransactionsQ,
    "spending": SpendingQ,
    "budgets": NoArgs,
    "balances": NoArgs,
    "sleep": DaysQ,
    "cve": LimitQ,
    "news": NewsQ,
}

# what the model is told about the read tools (the write tools are described by chat.actions.TOOL_SPEC)
READ_TOOL_DOC = """Look-up tools (read only; call one, you get the result, then answer or call another):
- transactions {"days": 1-365 (7), "category": string?, "merchant": string? (part of the name), "direction": "out"|"in"?, "min_amount": number?, "max_amount": number?, "limit": 1-25 (10)}
- spending {"period": "today"|"week"|"month" (month), "by": "category"|"merchant"|"day" (category), "direction": "out"|"in" (out)}
- budgets {}   this month's budgets with spent and limit
- balances {}  wallet balances
- sleep {"days": 1-30 (7)}
- cve {"limit": 1-10 (5)}  the newest high or critical vulnerabilities with the technology and versions
- news {"topic": "nasional"|"cyber"|a topic id (nasional), "limit": 1-10 (5)}"""


def _clip(text: str) -> str:
    return text if len(text) <= MAX_RESULT_CHARS else text[: MAX_RESULT_CHARS - 20] + "\n…(shortened)"


def _clean(s: str | None, n: int = 40) -> str:
    return " ".join((s or "-").split())[:n]


async def _transactions(sm, tz, now, a: TransactionsQ) -> str:
    since = now - timedelta(days=a.days)
    q = select(Transaction).where(Transaction.status == "committed", Transaction.timestamp >= since)
    if a.category:
        q = q.where(func.lower(Transaction.primary_category) == a.category.lower())
    if a.merchant:
        q = q.where(Transaction.merchant.ilike(f"%{a.merchant}%"))
    if a.direction:
        q = q.where(Transaction.direction == a.direction)
    if a.min_amount is not None:
        q = q.where(Transaction.total_paid_amount >= a.min_amount)
    if a.max_amount is not None:
        q = q.where(Transaction.total_paid_amount <= a.max_amount)
    async with sm() as s:
        rows = (await s.execute(q.order_by(Transaction.timestamp.desc()).limit(a.limit))).scalars().all()
    if not rows:
        return "No transactions match."
    return "\n".join(
        f"#{t.id} {t.timestamp.astimezone(tz):%d/%m %H:%M} {_clean(t.merchant)} {rupiah(t.total_paid_amount)} "
        f"{t.direction} {_clean(t.primary_category, 24)}" + (f" «{_clean(t.notes, 40)}»" if t.notes else "")
        for t in rows
    )


async def _spending(sm, tz, now, a: SpendingQ) -> str:
    today = datetime.combine(now.date(), time.min, tzinfo=tz)
    start = {"today": today, "week": today - timedelta(days=6), "month": today.replace(day=1)}[a.period]
    key = {
        "category": Transaction.primary_category,
        "merchant": Transaction.merchant,
        "day": func.date(func.timezone(str(tz), Transaction.timestamp)),
    }[a.by]
    async with sm() as s:
        rows = (await s.execute(
            select(key, func.sum(Transaction.total_paid_amount), func.count())
            .where(Transaction.status == "committed", Transaction.direction == a.direction, Transaction.timestamp >= start)
            .group_by(key).order_by(func.sum(Transaction.total_paid_amount).desc()).limit(15)
        )).all()
    if not rows:
        return f"Nothing recorded for {a.period}."
    total = sum((r[1] for r in rows), Decimal(0))
    body = "\n".join(f"- {_clean(str(r[0]), 30)}: {rupiah(r[1])} ({r[2]}x)" for r in rows)
    return f"{a.direction} since {start:%d/%m}, total {rupiah(total)}\n{body}"


async def _budgets(sm, tz, now, a: NoArgs) -> str:
    async with sm() as s:
        rules = (await s.execute(
            select(BudgetRule).where(BudgetRule.period_month == now.date().replace(day=1)).order_by(BudgetRule.category)
        )).scalars().all()
    if not rules:
        return "No budgets are set for this month."
    return "\n".join(f"- {r.category}: {rupiah(r.spent_amount)} of {rupiah(r.current_limit)}" for r in rules)


async def _balances(sm, tz, now, a: NoArgs) -> str:
    async with sm() as s:
        wallets = (await s.execute(select(CashWallet).order_by(CashWallet.wallet_name))).scalars().all()
    return "\n".join(f"- {w.wallet_name}: {rupiah(w.current_balance)}" for w in wallets) or "No wallets yet."


async def _sleep(sm, tz, now, a: DaysQ) -> str:
    async with sm() as s:
        rows = (await s.execute(
            select(SleepSession).where(SleepSession.sleep_date >= now.date() - timedelta(days=a.days))
            .order_by(SleepSession.sleep_date.desc())
        )).scalars().all()
    if not rows:
        return "No sleep data in that period."
    return "\n".join(
        f"{r.sleep_date:%d/%m}: {divmod(r.total_sleep_mins or 0, 60)[0]} h {divmod(r.total_sleep_mins or 0, 60)[1]} min, "
        f"score {r.sleep_score or '-'}" for r in rows
    )


async def _cve(sm, tz, now, a: LimitQ) -> str:
    from apa.news import config as news_config
    from apa.news import cyber

    rule = (await news_config.current(sm)).cve
    rows = await cyber.severe_24h(sm, now, limit=a.limit, rule=rule)
    if not rows:
        return "No high or critical CVEs in the window."
    return "\n".join(
        f"{r['id']} CVSS {r['cvss'] if r['cvss'] is not None else '-'}{' EXPLOITED' if r['kev'] else ''}: "
        f"{_clean('; '.join(r['products'][:2]), 90)} — {_clean(r['summary'], 110)}" for r in rows
    )


async def _news(sm, tz, now, a: NewsQ) -> str:
    from apa.news import config as news_config
    from apa.news.ingest import recent_articles
    from apa.news.rank import rank

    cfg = await news_config.current(sm)
    if a.topic not in cfg.catalog():
        return f"Unknown topic {a.topic!r}. Topics: " + ", ".join(cfg.catalog())
    arts = await recent_articles(sm, now)
    ranked = [r for r in rank(arts, now, topic=a.topic, country=cfg.country) if r.score > 0][: a.limit]
    if not ranked:
        return "No recent stories for that topic."
    return "\n".join(f"- {_clean(r.cluster.headline, 120)} ({', '.join(r.cluster.sources[:3])})" for r in ranked)


_RUN = {
    "transactions": _transactions, "spending": _spending, "budgets": _budgets, "balances": _balances,
    "sleep": _sleep, "cve": _cve, "news": _news,
}


async def run_read(sm: async_sessionmaker, tz: ZoneInfo, now: datetime, name: str, args: Any) -> str:
    """The result of one look-up as plain text. A bad argument comes back as a short message the model can act on."""
    cls = READ_TOOLS.get(name)
    if cls is None:
        return f"Unknown tool {name!r}."
    try:
        a = cls(**(args if isinstance(args, dict) else {}))
    except ValidationError as e:
        return "Bad arguments: " + "; ".join(f"{'.'.join(map(str, x['loc']))} {x['msg']}" for x in e.errors()[:3])[:200]
    except TypeError:
        return "Bad arguments."
    return _clip(await _RUN[name](sm, tz, now, a))
