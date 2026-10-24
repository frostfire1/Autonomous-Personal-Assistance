"""Live policy (policy_rules) and the persistent emergency-lockdown flag."""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from apa.db.models.budget import PolicyRule
from apa.safety.breakers import BreakerPolicy

LOCKDOWN_KEY = "lockdown_active"


async def _global_rules(session: AsyncSession) -> list[PolicyRule]:
    return list(
        (
            await session.execute(
                select(PolicyRule).where(
                    PolicyRule.scope == "global", PolicyRule.scope_ref == "", PolicyRule.is_active
                )
            )
        ).scalars()
    )


async def load_policy(session: AsyncSession) -> tuple[BreakerPolicy, int, dict[str, Decimal]]:
    """Breaker thresholds from policy_rules (blueprint defaults for anything missing), the highest
    rule version (recorded in explainability_log so a decision can be tied to its rules), and the
    raw numeric rules for non-breaker settings such as undo_window_hours."""
    rules = await _global_rules(session)
    v = {r.rule_key: r.value_numeric for r in rules if r.value_numeric is not None}
    d = BreakerPolicy()
    policy = BreakerPolicy(
        velocity_max_tx=int(v.get("velocity_max_tx", d.velocity_max_tx)),
        velocity_window_min=int(v.get("velocity_window_min", d.velocity_window_min)),
        single_tx_ceiling=Decimal(v.get("single_tx_ceiling", d.single_tx_ceiling)),
        liquidity_floor=Decimal(v.get("liquidity_floor", d.liquidity_floor)),
        deviation_sigma=Decimal(v.get("deviation_sigma", d.deviation_sigma)),
    )
    return policy, max((r.version for r in rules), default=0), v


async def lockdown_active(session: AsyncSession) -> bool:
    value = (
        await session.execute(
            select(PolicyRule.value_numeric).where(
                PolicyRule.rule_key == LOCKDOWN_KEY, PolicyRule.scope == "global"
            )
        )
    ).scalar_one_or_none()
    return bool(value)


async def set_lockdown(session: AsyncSession, active: bool, *, by: str) -> None:
    """Upsert the flag and bump its version. The caller writes the config_change audit row."""
    stmt = insert(PolicyRule).values(
        rule_key=LOCKDOWN_KEY, scope="global", scope_ref="", value_numeric=int(active),
        unit="flag", updated_by=by,
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=["rule_key", "scope", "scope_ref"],
            set_={
                "value_numeric": int(active),
                "updated_by": by,
                "version": PolicyRule.version + 1,
                "updated_at": stmt.excluded.updated_at,
            },
        )
    )
