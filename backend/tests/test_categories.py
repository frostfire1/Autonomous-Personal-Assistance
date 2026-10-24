import json
from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from apa.agents.arbiter import AgentVote, resolve
from apa.agents.debate import SYSTEM_TMPL, run_debate
from apa.integrations.llm import LLMResult
from apa.pipeline.candidate import Candidate, build_debate_context
from apa.pipeline.categories import DEFAULT_CATEGORIES, canonicalize

ALLOWED = ["Food & Dining", "Groceries", "Transport", "Bills & Utilities"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Food & Dining", "Food & Dining"),
        ("food & dining", "Food & Dining"),
        ("  FOOD   &   DINING  ", "Food & Dining"),
        ("food and dining", "Food & Dining"),
        ("food_dining", "Food & Dining"),
        ("Food-Dining", "Food & Dining"),
        ("bills & utilities", "Bills & Utilities"),
        ("bills_and_utilities", "Bills & Utilities"),
        ("GROCERIES", "Groceries"),
    ],
)
def test_formatting_differences_map_to_the_allowed_spelling(raw, expected):
    assert canonicalize(raw, ALLOWED) == expected


@pytest.mark.parametrize(
    "raw",
    ["food_beverage", "Food & Beverage", "coffee", "Food", "Dining", "Transportation", "other", "", "   ", None, "&", "and"],
)
def test_anything_not_on_the_list_is_none_never_a_guess(raw):
    # the live model said food_beverage / coffee / Food & Beverage for one coffee: a similarity
    # guess would charge the wrong budget, so an unknown category simply does not vote
    assert canonicalize(raw, ALLOWED) is None


def test_returns_the_users_own_spelling_not_the_models():
    assert canonicalize("makan & minum", ["Makan & Minum"]) == "Makan & Minum"
    assert canonicalize("transport", ["TRANSPORT"]) == "TRANSPORT"


def test_default_categories_are_unique_after_normalisation():
    from apa.pipeline.categories import _key

    keys = [_key(c) for c in DEFAULT_CATEGORIES]
    assert len(set(keys)) == len(keys) and "other" in keys


# ---- the prompt offers the list --------------------------------------------------------------------------


def _cand():
    return Candidate(
        source_channel="android_listener", wallet="gopay", direction="out", amount=Decimal(18000),
        fee=Decimal(0), merchant="KOPI", description=None, channel="qris",
        timestamp=datetime(2026, 10, 23, 5, tzinfo=UTC), parse_confidence=1.0,
    )


def test_context_offers_the_allowed_categories_and_the_system_prompt_demands_one_of_them():
    ctx = build_debate_context(_cand(), tz=ZoneInfo("Asia/Jakarta"), categories=ALLOWED)
    assert "Food & Dining | Groceries | Transport | Bills & Utilities" in ctx
    assert "copy exactly one" in ctx
    assert "copy exactly one category from the list" in SYSTEM_TMPL


def test_context_without_categories_has_no_empty_list_line():
    assert "categories" not in build_debate_context(_cand(), tz=ZoneInfo("Asia/Jakarta"))


# ---- votes are normalised -----------------------------------------------------------------------------------


class SayCategory:
    def __init__(self, category):
        self.category = category

    async def complete(self, route, system, user, **kw):
        body = {"approve_score": 0.8, "confidence": 0.9, "category": self.category, "argument": "ok"}
        return LLMResult(json.dumps(body), "m", 10, 5, 0)


async def test_a_vote_for_an_allowed_category_is_canonicalised_but_the_raw_answer_is_kept():
    turns = await run_debate(SayCategory("food & dining "), "ctx", ALLOWED)
    assert {t.vote.category for t in turns} == {"Food & Dining"}
    assert {t.raw["category"] for t in turns} == {"food & dining "}  # audit trail keeps what was said


async def test_a_vote_for_an_unknown_category_still_counts_but_names_no_category():
    turns = await run_debate(SayCategory("food_beverage"), "ctx", ALLOWED)
    assert len(turns) == 5 and {t.vote.category for t in turns} == {None}
    assert {t.raw["category"] for t in turns} == {"food_beverage"}


async def test_without_an_allowed_list_categories_pass_through_unchecked():
    turns = await run_debate(SayCategory("whatever"), "ctx")  # categories=None
    assert {t.vote.category for t in turns} == {"whatever"}


def test_when_no_vote_names_a_category_the_arbiter_uses_the_proposed_one():
    votes = [AgentVote(a, Decimal("0.8"), Decimal("0.9"), None) for a in ("A-02", "A-03", "A-05")]
    assert resolve(votes, "Groceries").category == "Groceries"
    assert resolve(votes, None).category is None  # the pipeline turns this into 'Uncategorized'
