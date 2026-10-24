"""Category constraint end to end: the live run charged `food_beverage`, which no budget has."""

from datetime import timedelta
from decimal import Decimal

import pytest

from apa.db.models.audit import AgentDebateTranscript
from apa.db.models.budget import BudgetRule, MerchantCache
from apa.pipeline.categories import DEFAULT_CATEGORIES
from tests.integration.conftest import HAVE_INFRA
from tests.integration.helpers import T0, FakeLLM, FakeNotifier, commit, new_event, parsed, rows, tx_for

pytestmark = pytest.mark.skipif(not HAVE_INFRA, reason="TEST_DATABASE_URL/TEST_REDIS_URL not set")

MONTH = T0.date().replace(day=1)


async def seed_budgets(stack, *cats):
    async with stack() as s:
        for c in cats:
            s.add(BudgetRule(category=c, period_month=MONTH, base_limit=Decimal(1_000_000),
                             current_limit=Decimal(1_000_000)))
        await s.commit()


async def spent(stack, category):
    return next(b.spent_amount for b in await rows(stack, BudgetRule) if b.category == category)


async def test_a_vote_that_differs_only_in_case_still_charges_the_budget(stack):
    await seed_budgets(stack, "Food & Dining", "Transport")
    eid = await new_event(stack, parsed())
    assert await commit(stack, eid, FakeLLM(cat="food & DINING")) == "committed"
    assert (await tx_for(stack, eid)).primary_category == "Food & Dining"  # the user's spelling
    assert await spent(stack, "Food & Dining") == Decimal(18_000)
    assert await spent(stack, "Transport") == 0


async def test_a_made_up_category_is_not_committed_and_charges_no_budget(stack):
    """The exact live failure: the model said `food_beverage`."""
    await seed_budgets(stack, "Food & Dining")
    eid = await new_event(stack, parsed())
    assert await commit(stack, eid, FakeLLM(cat="food_beverage")) == "committed"
    tx = await tx_for(stack, eid)
    assert tx.primary_category == "Uncategorized"  # not 'food_beverage'
    assert await spent(stack, "Food & Dining") == 0
    # what the agents said is still on record, so it can be reviewed
    assert {t.structured_output["category"] for t in await rows(stack, AgentDebateTranscript)} == {"food_beverage"}
    # and the placeholder is never learned as the merchant's category
    assert (await rows(stack, MerchantCache))[0].default_category is None


async def test_unknown_votes_do_not_outvote_a_real_category(stack):
    await seed_budgets(stack, "Food & Dining")
    llm = FakeLLM(cat="coffee", cats={"A-02": "Food & Dining", "A-07": "food and dining"})  # 2 real, 3 junk
    eid = await new_event(stack, parsed())
    assert await commit(stack, eid, llm) == "committed"
    assert (await tx_for(stack, eid)).primary_category == "Food & Dining"
    assert await spent(stack, "Food & Dining") == Decimal(18_000)


async def test_the_prompt_offers_the_users_own_categories_when_budgets_exist(stack):
    await seed_budgets(stack, "Makan & Minum", "Transport")
    llm = FakeLLM()
    await commit(stack, await new_event(stack, parsed()), llm)
    prompt = llm.calls[0][1]
    assert "Makan & Minum | Transport" in prompt and "Groceries" not in prompt  # not the built-in list


async def test_the_prompt_offers_the_built_in_list_when_there_are_no_budgets(stack):
    llm = FakeLLM()
    eid = await new_event(stack, parsed())
    assert await commit(stack, eid, llm) == "committed"
    assert " | ".join(DEFAULT_CATEGORIES) in llm.calls[0][1]
    assert (await tx_for(stack, eid)).primary_category == "Food & Dining"  # in the built-in list


async def test_a_stale_merchant_default_that_is_not_on_the_list_is_ignored(stack):
    """History learned before categories were constrained must not leak into new decisions."""
    await seed_budgets(stack, "Food & Dining")
    async with stack() as s:
        s.add(MerchantCache(merchant_key="kopi kenangan", display_name="KOPI KENANGAN",
                            default_category="food_beverage", source="learned"))
        await s.commit()
    eid = await new_event(stack, parsed())
    assert await commit(stack, eid, FakeLLM(cat="coffee")) == "committed"  # nobody names a real category
    assert (await tx_for(stack, eid)).primary_category == "Uncategorized"  # not the stale 'food_beverage'


async def test_a_valid_merchant_default_is_used_when_no_vote_names_a_category(stack):
    await seed_budgets(stack, "Food & Dining")
    async with stack() as s:
        s.add(MerchantCache(merchant_key="kopi kenangan", display_name="KOPI KENANGAN",
                            default_category="Food & Dining", source="manual"))
        await s.commit()
    eid = await new_event(stack, parsed(), at=T0 + timedelta(days=1))
    assert await commit(stack, eid, FakeLLM(cat="coffee"), FakeNotifier()) == "committed"
    tx = await tx_for(stack, eid)
    assert tx.primary_category == "Food & Dining" and await spent(stack, "Food & Dining") == Decimal(18_000)
