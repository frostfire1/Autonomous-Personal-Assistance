"""Blueprint v2 4.2 confidence-gated debate: a known, recently verified merchant charging a normal amount is
categorised from the merchant cache with no model call. Everything unusual keeps the full debate."""

from datetime import timedelta
from decimal import Decimal

import pytest

from apa.core.settings import get_settings
from apa.db.models.audit import AgentDebateTranscript, ExplainabilityLog
from apa.db.models.budget import BudgetRule, MerchantCache
from apa.db.models.ledger import Transaction
from apa.ingestion.merchants import record_visit, set_default_category
from apa.pipeline.commit import run_release
from apa.safety.audit_log import verify_log
from tests.integration.conftest import HAVE_INFRA
from tests.integration.helpers import T0, TZ, FakeLLM, FakeNotifier, commit, new_event, parsed, rows, tx_for

pytestmark = pytest.mark.skipif(not HAVE_INFRA, reason="TEST_DATABASE_URL/TEST_REDIS_URL not set")

MONTH = T0.date().replace(day=1)


async def know(stack, merchant="KOPI KENANGAN", amounts=(18000, 17000, 19000), *, verified_days_ago=1,
               category="Food & Dining", agent="A-02", verified=True):
    """The merchant as an earlier debate left it: visits, a default category, verified on some day."""
    async with stack() as s:
        for i, a in enumerate(amounts):
            await record_visit(
                s, merchant, Decimal(a), T0 - timedelta(days=verified_days_ago + len(amounts) - i),
                category=category, agent=agent, verified=False,
            )
        row = (await s.execute(MerchantCache.__table__.select())).first()
        if verified:
            from sqlalchemy import update

            await s.execute(update(MerchantCache).values(last_verified_at=T0 - timedelta(days=verified_days_ago)))
        await s.commit()
    return row


async def run(stack, p=None, *, llm=None):
    llm = llm or FakeLLM(cat="Shopping")  # a debate would say Shopping: the fast path must say otherwise
    eid = await new_event(stack, p or parsed(), at=T0)
    status = await commit(stack, eid, llm)
    return status, llm, eid


async def tx_and_log(stack, eid):
    tx = await tx_for(stack, eid)
    (log,) = await rows(stack, ExplainabilityLog, ExplainabilityLog.transaction_id == tx.id)
    return tx, log


async def test_a_known_merchant_is_categorised_with_no_model_call(stack):
    await know(stack)
    status, llm, eid = await run(stack)
    assert status == "committed" and llm.calls == []  # zero model calls
    tx, log = await tx_and_log(stack, eid)
    assert (tx.primary_category, tx.responsible_agent, tx.status) == ("Food & Dining", "A-02", "committed")
    assert log.consensus_formula == "fast_path_v1" and log.agents_invoked == [] and "no debate" in log.justification
    assert "3 visits" in log.justification and log.agent_scores["fast_path"]["visits"] == "3"
    assert await rows(stack, AgentDebateTranscript) == []
    async with stack() as s:
        assert await verify_log(s) is None


async def test_the_fast_path_still_tells_the_owner_and_still_learns_the_visit(stack):
    await know(stack)
    n = FakeNotifier()
    eid = await new_event(stack, parsed(), at=T0)
    await commit(stack, eid, FakeLLM(), n)
    assert len(n.sent) == 1 and "Dicatat" in n.sent[0] and "Food & Dining" in n.sent[0] and "/undo_" in n.sent[0]
    (m,) = await rows(stack, MerchantCache)
    assert m.visit_count == 4  # the visit counts toward the history
    assert m.last_verified_at == T0 - timedelta(days=1)  # but a fast-path commit does NOT re-verify the merchant


async def test_a_full_debate_verifies_the_merchant_and_starts_its_ttl(stack):
    status, llm, eid = await run(stack, llm=FakeLLM(cat="Food & Dining"))
    assert status == "committed" and len(llm.calls) == 5  # a new merchant: the full debate
    (m,) = await rows(stack, MerchantCache)
    assert m.last_verified_at == T0 and m.default_category == "Food & Dining"


async def test_the_third_visit_after_a_debate_is_the_first_to_take_the_fast_path(stack):
    counts = []
    for i in range(5):
        llm = FakeLLM(cat="Food & Dining")
        eid = await new_event(stack, parsed(amount=str(18000 + i * 500)), at=T0 + timedelta(hours=i * 3))
        assert await commit(stack, eid, llm) == "committed"
        counts.append(len(llm.calls))
    assert counts == [5, 5, 5, 0, 0]  # min_visits=3: debates until the cache is trusted, then none


@pytest.mark.parametrize(
    ("why", "setup", "p"),
    [
        ("never seen before", {"amounts": ()}, None),
        ("too few visits", {"amounts": (18000, 17000)}, None),
        ("never verified (e.g. learned before this feature)", {"verified": False}, None),
        ("verification older than the 30 day TTL", {"verified_days_ago": 40}, None),
        ("no learned category", {"category": None}, None),
        ("amount more than 2x the mean with few samples", {}, parsed(amount="60000")),
        ("a split / social tag", {}, {**parsed(), "mentions": ["@andi"]}),
        ("a receipt (its lines are audited)", {}, {**parsed(), "receipt": {"items": [], "total": "18000"}}),
    ],
)
async def test_anything_unusual_still_gets_the_full_debate(stack, why, setup, p):
    await know(stack, **setup)
    status, llm, _ = await run(stack, p)
    assert status == "committed" and len(llm.calls) == 5, why


async def test_an_amount_beyond_two_sigma_gets_the_debate_but_within_it_does_not(stack):
    # five visits give a usable sigma: mean 20000, sd ~1581 -> 2 sigma ~ 3162
    await know(stack, amounts=(18000, 19000, 20000, 21000, 22000))
    ok_status, ok_llm, _ = await run(stack, parsed(amount="22500"))
    assert ok_status == "committed" and ok_llm.calls == []
    odd_status, odd_llm, _ = await run(stack, parsed(amount="24500"))  # 2.8 sigma: odd, but under the 3-sigma breaker
    assert odd_status == "committed" and len(odd_llm.calls) == 5


async def test_a_spend_that_would_exceed_its_budget_gets_the_debate(stack):
    await know(stack)
    async with stack() as s:
        s.add(BudgetRule(category="Food & Dining", period_month=MONTH, base_limit=Decimal(100_000),
                         current_limit=Decimal(100_000), spent_amount=Decimal(90_000)))
        await s.commit()
    status, llm, _ = await run(stack)  # 90k + 18k > 100k
    assert status == "committed" and len(llm.calls) == 5


async def test_the_breakers_still_run_before_the_fast_path(stack):
    await know(stack, amounts=(2_400_000, 2_450_000, 2_500_000))
    status, llm, _ = await run(stack, parsed(amount="2600000"))  # over the single-transaction ceiling
    assert status == "quarantined" and llm.calls == []


async def test_a_release_always_gets_the_full_debate(stack):
    await know(stack, amounts=(2_400_000, 2_450_000, 2_500_000))
    eid = await new_event(stack, parsed(amount="2600000"), at=T0)
    assert await commit(stack, eid, FakeLLM()) == "quarantined"
    held = await tx_for(stack, eid)
    llm = FakeLLM(cat="Food & Dining")
    assert await run_release(stack, llm, FakeNotifier(), held.id, tz=TZ) == "committed"
    assert len(llm.calls) == 5  # the owner overriding a hold is not a routine entry


async def test_the_fast_path_can_be_switched_off(stack, monkeypatch):
    await know(stack)
    monkeypatch.setenv("FAST_PATH_ENABLED", "false")
    get_settings.cache_clear()
    status, llm, _ = await run(stack)
    assert status == "committed" and len(llm.calls) == 5


async def test_an_owner_correction_becomes_the_merchants_category_and_is_verified_now(stack):
    await know(stack, category="Shopping", verified_days_ago=60)  # stale and, as it turns out, wrong
    async with stack() as s:
        await set_default_category(s, "KOPI KENANGAN", "Food & Dining", T0)
        await s.commit()
    status, llm, eid = await run(stack)
    assert status == "committed" and llm.calls == []  # trusted again, with the owner's category
    assert (await tx_for(stack, eid)).primary_category == "Food & Dining"


async def test_the_fast_path_commit_is_undoable_like_any_other(stack):
    from apa.pipeline.undo import undo_transaction

    await know(stack)
    _, _, eid = await run(stack)
    tx = await tx_for(stack, eid)
    assert (await undo_transaction(stack, tx.id, tz=TZ, now=T0 + timedelta(hours=1))).status == "ok"
    assert (await rows(stack, Transaction, Transaction.id == tx.id))[0].status == "rolled_back"
    (m,) = await rows(stack, MerchantCache)
    assert m.visit_count == 3  # the visit was taken back out
