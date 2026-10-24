"""The concurrency cap (LLMClient) and the 3-of-5 quorum (decide). No database needed."""

import asyncio
import json
import time

import httpx
import pytest

from apa.agents.debate import DEBATERS, required_quorum, run_debate
from apa.core.settings import get_settings
from apa.integrations.llm import LLMClient, LLMResult
from apa.pipeline.commit import DebateUnavailable, NoQuorum, decide
from tests.integration.helpers import TZ, FakeLLM
from tests.test_commit_logic import cand, gathered

REPLY = {"choices": [{"message": {"content": json.dumps(
    {"approve_score": 0.8, "confidence": 0.9, "category": "Food & Dining", "argument": "ok"})}}],
    "model": "m", "usage": {}}


class Provider:
    """A fake endpoint that records how many requests are in flight at once."""

    def __init__(self, delay=0.05, status=200, gate: asyncio.Event | None = None, reject_above=None):
        self.delay, self.status, self.gate, self.reject_above = delay, status, gate, reject_above
        self.inflight = self.peak = self.rejected = self.served = 0
        self.times: list[float] = []  # when each request reached the provider

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.times.append(time.monotonic())
        self.inflight += 1
        self.peak = max(self.peak, self.inflight)
        try:
            if self.reject_above is not None and self.inflight > self.reject_above:
                self.rejected += 1  # the provider's own burst limit: 429, answered at once
                return httpx.Response(429, json={"error": {"type": "rate_limit_exceeded"}})
            if self.gate is not None:
                await self.gate.wait()
            await asyncio.sleep(self.delay)
            self.served += 1
            return httpx.Response(self.status, json=REPLY)
        finally:
            self.inflight -= 1

    def client(self, **kw) -> LLMClient:
        kw.setdefault("max_rpm", 0)  # these tests are about concurrency; the rate limiter has its own below
        http = httpx.AsyncClient(transport=httpx.MockTransport(self), base_url="http://osiris.test")
        return LLMClient(http, **kw)


async def ask(llm):
    return await llm.complete("router-routine-classifier", "s", "u", json_mode=True)


# ---- concurrency cap -----------------------------------------------------------------------------


async def test_five_parallel_calls_never_exceed_three_in_flight_and_all_complete():
    p = Provider()
    llm = p.client(max_concurrency=3)  # one shared client, as the worker has
    await asyncio.gather(*(ask(llm) for _ in range(5)))
    assert p.peak == 3 and p.served == 5  # the cap is reached, never exceeded, and nothing is dropped


async def test_the_default_cap_comes_from_settings_and_is_three():
    assert get_settings().llm_max_concurrency == 3
    p = Provider()
    llm = p.client()
    await asyncio.gather(*(ask(llm) for _ in range(8)))
    assert p.peak == 3


@pytest.mark.parametrize(("cap", "expected_peak"), [(1, 1), (2, 2), (0, 1), (-5, 1)])
async def test_the_cap_is_configurable_and_never_below_one(cap, expected_peak):
    p = Provider()
    llm = p.client(max_concurrency=cap)
    await asyncio.gather(*(ask(llm) for _ in range(6)))
    assert p.peak == expected_peak


async def test_two_debates_at_once_share_one_cap():
    """The reason the cap is on the client and not on run_debate: ten transactions arriving
    together would otherwise send fifty calls at once."""
    p = Provider()
    llm = p.client(max_concurrency=3)
    a, b = await asyncio.gather(run_debate(llm, "ctx a"), run_debate(llm, "ctx b"))
    assert len(a) == len(b) == len(DEBATERS) and p.peak == 3 and p.served == 10


async def test_a_failing_provider_does_not_leak_slots():
    p = Provider(status=500)
    llm = p.client(max_concurrency=3)
    done = await asyncio.wait_for(
        asyncio.gather(*(ask(llm) for _ in range(12)), return_exceptions=True), timeout=5
    )
    assert len(done) == 12 and all(isinstance(r, httpx.HTTPStatusError) for r in done) and p.peak <= 3
    p.status = 200
    assert (await asyncio.wait_for(ask(llm), timeout=5)).model == "m"  # all three slots are free again


async def test_cancelled_calls_release_their_slots():
    gate = asyncio.Event()
    p = Provider(gate=gate)
    llm = p.client(max_concurrency=3)
    stuck = [asyncio.create_task(ask(llm)) for _ in range(3)]
    queued = [asyncio.create_task(ask(llm)) for _ in range(2)]  # waiting for a slot
    await asyncio.sleep(0.05)
    assert p.inflight == 3
    for t in stuck + queued:
        t.cancel()
    await asyncio.gather(*stuck, *queued, return_exceptions=True)
    gate.set()
    assert (await asyncio.wait_for(ask(llm), timeout=5)).model == "m"  # nothing is held by the dead ones


async def test_debaters_answer_through_the_cap_with_valid_votes():
    p = Provider()
    turns = await run_debate(p.client(max_concurrency=3), "ctx")
    assert sorted(t.agent_id for t in turns) == sorted(a.id for a in DEBATERS)


# ---- quorum ---------------------------------------------------------------------------------------------


def settings_with(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, str(v))
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _fresh_settings():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def run_decide(llm, *, job_try=1, max_tries=3, **cand_kw):
    return await decide(llm, cand(**cand_kw), gathered(), TZ, job_try, max_tries)


@pytest.mark.parametrize("failing", [(), ("A-02",), ("A-02", "A-03")])
async def test_three_or_more_valid_votes_decide(failing):
    consensus, turns = await run_decide(FakeLLM(fail=failing))
    assert len(turns) == 5 - len(failing) >= 3 and consensus.category == "Food & Dining"


@pytest.mark.parametrize("failing", [("A-02", "A-03", "A-04"), ("A-02", "A-03", "A-04", "A-05"), ("*",)])
async def test_fewer_than_three_votes_ask_for_a_retry_while_tries_remain(failing):
    with pytest.raises(DebateUnavailable, match="need 3"):
        await run_decide(FakeLLM(fail=failing), job_try=1)
    with pytest.raises(DebateUnavailable):
        await run_decide(FakeLLM(fail=failing), job_try=2)


@pytest.mark.parametrize(("failing", "answered"), [(("A-02", "A-03", "A-04"), 2), (("*",), 0)])
async def test_fewer_than_three_votes_on_the_last_try_raise_no_quorum(failing, answered):
    with pytest.raises(NoQuorum) as exc:
        await run_decide(FakeLLM(fail=failing), job_try=3, max_tries=3)
    assert (exc.value.answered, exc.value.asked, exc.value.quorum) == (answered, 5, 3)
    assert str(exc.value) == f"{answered}/5 agents answered, quorum is 3"


async def test_an_agent_that_answers_with_invalid_json_does_not_count_as_a_vote():
    llm = FakeLLM(garbage=("A-02", "A-03", "A-04"))  # all five answered, but only two usably
    with pytest.raises(DebateUnavailable):
        await run_decide(llm)
    assert len(llm.calls) == 5


@pytest.mark.parametrize("kind_kw", [{"direction": "in"}, {"channel": "cash_withdrawal"}])
async def test_income_and_withdrawals_need_no_debate_and_so_no_quorum(kind_kw):
    llm = FakeLLM(fail=("*",))
    assert await run_decide(llm, **kind_kw) == (None, []) and llm.calls == []


@pytest.mark.parametrize(
    ("setting", "effective"), [(5, 5), (4, 4), (1, 1), (0, 1), (-3, 1), (99, 5)]
)
def test_quorum_setting_is_clamped_to_one_through_five(monkeypatch, setting, effective):
    settings_with(monkeypatch, DEBATE_QUORUM=setting)
    assert required_quorum() == effective


async def test_a_stricter_quorum_is_honoured(monkeypatch):
    settings_with(monkeypatch, DEBATE_QUORUM=5)
    with pytest.raises(DebateUnavailable, match="need 5"):
        await run_decide(FakeLLM(fail=("A-07",)))  # 4 of 5 is not enough when 5 are required
    assert len((await run_decide(FakeLLM()))[1]) == 5


# ---- images (vision routes) --------------------------------------------------------------------------------


async def test_images_are_sent_as_base64_data_urls_after_the_text_and_text_only_is_unchanged():
    import base64

    seen = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=REPLY)

    llm = LLMClient(httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://x"))
    await llm.complete("router-vision-ocr", "sys", "read this", images=[(b"\x89PNG-bytes", "image/png"),
                                                                       (b"jpeg-bytes", "image/jpeg")])
    await llm.complete("router-routine-classifier", "sys", "plain text")
    vision, plain = seen
    parts = vision["messages"][1]["content"]
    assert parts[0] == {"type": "text", "text": "read this"} and len(parts) == 3
    assert parts[1]["image_url"]["url"] == "data:image/png;base64," + base64.b64encode(b"\x89PNG-bytes").decode()
    assert parts[2]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert plain["messages"][1]["content"] == "plain text"  # a string, exactly as before


async def test_image_calls_share_the_same_concurrency_cap():
    p = Provider()
    llm = p.client(max_concurrency=3)
    await asyncio.gather(*(llm.complete("r", "s", "u", images=[(b"x" * 100, "image/png")]) for _ in range(8)))
    assert p.peak == 3


# ---- code-fenced JSON (the live debate lost 7 valid votes in 6 debates to this) ---------------------------


@pytest.mark.parametrize(
    "content",
    [
        '{"a": 1}',
        '```json\n{"a": 1}\n```',
        '```json{"a": 1}```',
        '```\n{"a": 1}\n```',
        '  ```JSON\n{"a": 1}\n```  \n',
        '```json\n{\n  "a": 1\n}\n```',
    ],
)
def test_llm_result_json_accepts_a_fenced_or_plain_object(content):
    assert LLMResult(content, "m", None, None, None).json() == {"a": 1}


@pytest.mark.parametrize("content", ["", "Sure! Here you go: {\"a\": 1}", "```json\nnot json\n```", "```json\n{\"a\": 1}", '{"a": 1'])
def test_llm_result_json_still_rejects_everything_else(content):
    with pytest.raises(ValueError):
        LLMResult(content, "m", None, None, None).json()


async def test_a_debater_whose_valid_vote_is_wrapped_in_a_fence_still_counts():
    class Fenced:
        async def complete(self, route, system, user, **kw):
            body = {"approve_score": 0.8, "confidence": 0.9, "category": "Food & Dining", "argument": "ok"}
            return LLMResult("```json\n" + json.dumps(body) + "\n```", "m", 10, 5, 0)

    turns = await run_debate(Fenced(), "ctx", ["Food & Dining"])
    assert len(turns) == 5 and {t.vote.category for t in turns} == {"Food & Dining"}


# ---- timeouts ----------------------------------------------------------------------------------------------------


async def test_the_client_default_timeout_and_the_per_request_override_reach_httpx():
    from apa.integrations.llm import DEFAULT_TIMEOUT_S, VISION_TIMEOUT_S

    seen = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions["timeout"]["read"])
        return httpx.Response(200, json=REPLY)

    llm = LLMClient(httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://x",
                                      timeout=DEFAULT_TIMEOUT_S))
    await llm.complete("r", "s", "u")
    await llm.complete("r", "s", "u", timeout=VISION_TIMEOUT_S)
    await llm.complete("r", "s", "u", timeout=5)
    assert seen == [DEFAULT_TIMEOUT_S, VISION_TIMEOUT_S, 5]


def test_the_real_default_client_uses_the_default_timeout():
    from apa.integrations.llm import DEFAULT_TIMEOUT_S

    assert LLMClient()._client.timeout.read == DEFAULT_TIMEOUT_S


# ---- requests-per-minute limiter ---------------------------------------------------------------------------------


def worst_burst(times: list[float], window: float) -> int:
    """The most requests that ever reached the provider inside any single window."""
    times = sorted(times)
    return max(sum(1 for u in times if t <= u < t + window) for t in times)


async def test_the_rate_limiter_queues_calls_instead_of_exceeding_the_limit():
    p = Provider(delay=0.0)
    llm = p.client(max_rpm=3, rate_window_s=0.3, max_concurrency=10)
    t0 = time.monotonic()
    results = await asyncio.gather(*(ask(llm) for _ in range(8)))
    elapsed = time.monotonic() - t0
    assert len(results) == 8 and p.served == 8  # nothing is dropped or refused: it waits
    assert worst_burst(p.times, 0.3) <= 3  # never more than 3 in any 0.3 s window
    assert elapsed >= 0.55  # 8 calls at 3 per 0.3 s need at least two full windows


async def test_calls_within_the_limit_are_not_delayed_and_zero_disables_the_limiter():
    p = Provider(delay=0.0)
    t0 = time.monotonic()
    await asyncio.gather(*(ask(p.client(max_rpm=3, rate_window_s=5, max_concurrency=10)) for _ in range(3)))
    assert time.monotonic() - t0 < 0.2  # three calls under a limit of three: no waiting

    off = p.client(max_rpm=0, rate_window_s=5, max_concurrency=10)
    t0 = time.monotonic()
    await asyncio.gather(*(ask(off) for _ in range(20)))
    assert time.monotonic() - t0 < 0.5  # disabled: 20 calls straight through


async def test_failed_requests_count_toward_the_rate_because_they_reached_the_provider():
    p = Provider(delay=0.0, status=500)
    llm = p.client(max_rpm=2, rate_window_s=0.3, max_concurrency=10)
    out = await asyncio.gather(*(ask(llm) for _ in range(5)), return_exceptions=True)
    assert all(isinstance(r, httpx.HTTPStatusError) for r in out) and len(p.times) == 5
    assert worst_burst(p.times, 0.3) <= 2  # a failing call is still a request the provider counted


async def test_waiting_for_the_rate_does_not_hold_a_concurrency_slot():
    """With one slot and one request per window, a call stuck waiting for the rate must not keep the
    slot from a call that is already allowed to go."""
    p = Provider(delay=0.0)
    llm = p.client(max_rpm=2, rate_window_s=0.4, max_concurrency=1)
    t0 = time.monotonic()
    await asyncio.gather(*(ask(llm) for _ in range(4)))
    assert time.monotonic() - t0 < 1.5 and p.served == 4  # finishes in about two windows, no deadlock


def test_the_default_rate_limit_leaves_headroom_under_kunciai_60_per_minute():
    assert 0 < LLMClient()._rpm <= 54
