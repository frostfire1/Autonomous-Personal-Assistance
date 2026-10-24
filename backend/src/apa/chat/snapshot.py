"""Read-only facts the chat agents may answer from. Computed in code, never by the model: the model only
phrases what is here, so a question about money is answered from the ledger and not from a guess.

Merchant names inside are untrusted text: the caller wraps this block as data."""

from datetime import datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from apa.core.format import rupiah
from apa.db.models.budget import BudgetRule
from apa.db.models.ledger import CashWallet, Transaction

RECENT = 10
TOP_CATEGORIES = 8


def _clean(s: str | None, n: int = 40) -> str:
    return " ".join((s or "-").split())[:n]


async def health_snapshot(sm: async_sessionmaker, tz: ZoneInfo, now: datetime | None = None) -> str:
    """Facts for the health agent: last nights of sleep, resting heart rate, steps, and today's intake estimate."""
    from apa.db.models.health import BiomarkerMetric, HuaweiHealthMetric, SleepSession

    now = (now or datetime.now(tz)).astimezone(tz)
    async with sm() as s:
        sleeps = (
            (await s.execute(select(SleepSession).order_by(SleepSession.sleep_date.desc()).limit(5)))
            .scalars()
            .all()
        )
        today = (
            await s.execute(select(BiomarkerMetric).where(BiomarkerMetric.log_date == now.date()))
        ).scalar_one_or_none()
        steps = (
            await s.execute(
                select(HuaweiHealthMetric)
                .where(HuaweiHealthMetric.step_count.is_not(None))
                .order_by(HuaweiHealthMetric.recorded_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    lines = [f"Tanggal sekarang: {now:%d/%m/%Y %H:%M} ({tz})"]
    if today:
        lines.append(
            f"ASUPAN HARI INI (perkiraan dari belanjaan): kafein {today.cumulative_caffeine_mg:.0f} mg, "
            f"gula {today.cumulative_sugar_g:.0f} g, natrium {today.cumulative_sodium_mg:.0f} mg"
        )
    for r in sleeps:
        h, m = divmod(r.total_sleep_mins or 0, 60)
        lines.append(
            f"TIDUR {r.sleep_date:%d/%m}: {h} jam {m} menit, skor {r.sleep_score or '-'}, "
            f"tidur dalam {r.deep_sleep_pct or '-'}%"
            + (" (mode kelelahan aktif)" if r.fatigue_defense_active else "")
        )
    if steps:
        lines.append(f"LANGKAH terakhir: {steps.step_count} ({steps.recorded_at:%d/%m})")
    if len(lines) == 1:
        lines.append("Belum ada data kesehatan yang masuk.")
    return "\n".join(lines)


async def finance_snapshot(sm: async_sessionmaker, tz: ZoneInfo, now: datetime | None = None) -> str:
    now = (now or datetime.now(tz)).astimezone(tz)
    today = datetime.combine(now.date(), time.min, tzinfo=tz)
    month_start = today.replace(day=1)
    week_start = today - timedelta(days=6)
    committed = Transaction.status == "committed"

    async with sm() as s:
        month = (
            await s.execute(
                select(
                    Transaction.direction,
                    Transaction.primary_category,
                    func.count(),
                    func.sum(Transaction.total_paid_amount),
                )
                .where(committed, Transaction.timestamp >= month_start)
                .group_by(Transaction.direction, Transaction.primary_category)
            )
        ).all()
        day = func.date(func.timezone(str(tz), Transaction.timestamp))  # one object: one bind parameter
        days = (
            await s.execute(
                select(day, func.sum(Transaction.total_paid_amount))
                .where(committed, Transaction.direction == "out", Transaction.timestamp >= week_start)
                .group_by(day)
                .order_by(day)
            )
        ).all()
        recent = (
            (
                await s.execute(
                    select(Transaction).where(committed).order_by(Transaction.timestamp.desc()).limit(RECENT)
                )
            )
            .scalars()
            .all()
        )
        budgets = {
            b.category: b
            for b in (
                await s.execute(select(BudgetRule).where(BudgetRule.period_month == month_start.date()))
            ).scalars()
        }
        wallets = (await s.execute(select(CashWallet).order_by(CashWallet.wallet_name))).scalars().all()
        held = (
            await s.execute(
                select(func.count()).select_from(Transaction).where(Transaction.status == "quarantined")
            )
        ).scalar_one()

    out = [r for r in month if r[0] == "out"]
    spent = sum((r[3] for r in out), Decimal(0))
    earned = sum((r[3] for r in month if r[0] == "in"), Decimal(0))
    lines = [
        f"Tanggal sekarang: {now:%d/%m/%Y %H:%M} ({tz})",
        f"BULAN INI ({month_start:%m/%Y}): pengeluaran {rupiah(spent)} dalam {sum(r[2] for r in out)} transaksi; "
        f"pemasukan {rupiah(earned)}",
    ]
    for r in sorted(out, key=lambda r: r[3], reverse=True)[:TOP_CATEGORIES]:
        b = budgets.get(r[1] or "")
        cap = (
            f" dari anggaran {rupiah(b.current_limit)} ({int(r[3] / b.current_limit * 100)}%)"
            if b and b.current_limit
            else ""
        )
        lines.append(f"  - {_clean(r[1], 30) if r[1] else 'Tanpa kategori'}: {rupiah(r[3])}{cap}")
    unspent = [b for c, b in budgets.items() if c not in {r[1] for r in out} and b.current_limit]
    lines += [f"  - {b.category}: Rp 0 dari anggaran {rupiah(b.current_limit)} (0%)" for b in unspent[:5]]
    if days:
        lines.append(
            "7 HARI TERAKHIR (pengeluaran per hari): " + ", ".join(f"{d:%d/%m} {rupiah(a)}" for d, a in days)
        )
    if recent:
        lines.append(f"{len(recent)} TRANSAKSI TERAKHIR:")
        lines += [
            f"  - #{t.id} {t.timestamp.astimezone(tz):%d/%m %H:%M} {_clean(t.merchant)} "
            f"{'+' if t.direction == 'in' else '-'}{rupiah(t.total_paid_amount)} [{_clean(t.primary_category, 30)}]"
            for t in recent
        ]
    else:
        lines.append("Belum ada transaksi tercatat.")
    if wallets:
        lines.append(
            "DOMPET TUNAI: " + ", ".join(f"{w.wallet_name} {rupiah(w.current_balance)}" for w in wallets)
        )
    if held:
        lines.append(f"DITAHAN menunggu keputusan: {held} transaksi")
    return "\n".join(lines)
