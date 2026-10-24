"""Quorum and the concurrency cap against real Postgres, end to end through run_commit."""

import pytest

from apa.db.models.audit import AgentDebateTranscript, ExplainabilityLog
from apa.db.models.ledger import Transaction
from apa.db.models.ops import InboundEvent
from apa.pipeline.commit import DebateUnavailable, run_commit, run_release
from tests.integration.conftest import HAVE_INFRA
from tests.integration.helpers import (
    TZ,
    FakeLLM,
    FakeNotifier,
    commit,
    new_event,
    parsed,
    rows,
    tx_for,
)
from tests.test_debate_limits import Provider

pytestmark = pytest.mark.skipif(not HAVE_INFRA, reason="TEST_DATABASE_URL/TEST_REDIS_URL not set")


async def status_of(stack, eid):
    return (await rows(stack, InboundEvent, InboundEvent.id == eid))[0].status


async def test_two_of_five_votes_retry_and_a_healthy_retry_commits_one_clean_debate(stack):
    notifier = FakeNotifier()
    eid = await new_event(stack, parsed())
    flaky = FakeLLM(fail=("A-03", "A-04", "A-05"))  # only A-02 and A-07 answer
    with pytest.raises(DebateUnavailable, match="2/5 agents answered, need 3"):
        await commit(stack, eid, flaky, notifier, job_try=1)
    assert await status_of(stack, eid) == "processing" and notifier.sent == []
    assert await rows(stack, AgentDebateTranscript) == []  # a failed attempt leaves no partial record

    assert await commit(stack, eid, FakeLLM(), notifier, job_try=2) == "committed"
    # exactly one debate was recorded: the 5 from the retry, not 2 + 5
    assert len(await rows(stack, AgentDebateTranscript)) == 5
    assert len(await rows(stack, Transaction)) == 1 and len(notifier.sent) == 1


async def test_two_of_five_on_the_last_try_holds_the_transaction_with_the_count_in_the_audit(stack):
    notifier = FakeNotifier()
    eid = await new_event(stack, parsed())
    llm = FakeLLM(fail=("A-03", "A-04", "A-05"))
    assert await commit(stack, eid, llm, notifier, job_try=3, max_tries=3) == "quarantined"
    tx = await tx_for(stack, eid)
    assert (tx.status, tx.notes) == ("quarantined", "quarantined: no_quorum")
    (log,) = await rows(stack, ExplainabilityLog, ExplainabilityLog.transaction_id == tx.id)
    assert "2/5 agents answered, quorum is 3" in log.justification and "3 tries" in log.justification
    assert await rows(stack, AgentDebateTranscript) == []  # two votes are not a decision: nothing kept

    # once the LLM is healthy the owner's /release runs the full debate and commits the same row
    assert await run_release(stack, FakeLLM(), notifier, tx.id, tz=TZ) == "committed"
    done = (await rows(stack, Transaction))[0]
    assert (done.id, done.status) == (tx.id, "committed") and len(await rows(stack, AgentDebateTranscript)) == 5


async def test_exactly_three_votes_commit(stack):
    eid = await new_event(stack, parsed())
    assert await commit(stack, eid, FakeLLM(fail=("A-04", "A-07"))) == "committed"
    assert len(await rows(stack, AgentDebateTranscript)) == 3


async def test_invalid_json_does_not_count_toward_the_quorum(stack):
    eid = await new_event(stack, parsed())
    llm = FakeLLM(garbage=("A-02", "A-03", "A-04"))  # five answers, two usable
    with pytest.raises(DebateUnavailable):
        await commit(stack, eid, llm, job_try=1)
    assert len(llm.calls) == 5 and await status_of(stack, eid) == "processing"


async def test_a_held_no_quorum_event_is_not_debated_again(stack):
    eid = await new_event(stack, parsed())
    assert await commit(stack, eid, FakeLLM(fail=("*",)), job_try=3, max_tries=3) == "quarantined"
    again = FakeLLM()
    assert await commit(stack, eid, again, job_try=1) == "quarantined" and again.calls == []


# ---- the cap, end to end -----------------------------------------------------------------------------------------


async def test_with_the_cap_a_throttling_provider_never_rejects_a_debate_call(stack):
    """Osiris rejected 2 of 5 parallel calls in a quiet minute. Model that: the provider answers
    at most 3 requests at a time and returns 429 beyond that. With the cap the debate never trips it."""
    p = Provider(delay=0.05, reject_above=3)
    eid = await new_event(stack, parsed())
    assert await run_commit(stack, p.client(max_concurrency=3), FakeNotifier(), eid, tz=TZ) == "committed"
    assert p.rejected == 0 and p.peak == 3 and p.served == 5
    assert len(await rows(stack, AgentDebateTranscript)) == 5  # a full five-voice debate


async def test_without_the_cap_the_same_provider_rejects_calls_and_the_debate_shrinks(stack):
    """The control: proves the throttle model above is real, so the capped test is not vacuous."""
    p = Provider(delay=0.05, reject_above=3)
    eid = await new_event(stack, parsed())
    assert await run_commit(stack, p.client(max_concurrency=5), FakeNotifier(), eid, tz=TZ) == "committed"
    assert p.rejected == 2 and p.served == 3
    assert len(await rows(stack, AgentDebateTranscript)) == 3  # decided on the three that got through
