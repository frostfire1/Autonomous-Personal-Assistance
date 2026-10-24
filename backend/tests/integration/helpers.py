"""Shared fakes and builders for the pipeline integration tests."""

import asyncio
import json
import re
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Jakarta")
T0 = datetime(2026, 10, 23, 12, 0, tzinfo=TZ)
DEBATERS = ("A-02", "A-03", "A-04", "A-05", "A-07")


class FakeLLM:
    """Stands in for the LiteLLM proxy. The agent is read from the system prompt."""

    def __init__(self, score=0.8, conf=0.9, cat="Food & Dining", fail=(), garbage=(), cats=None):
        """`fail`: agents whose call raises. `garbage`: agents that answer, but not with valid JSON.
        `cats`: per-agent category overrides, e.g. {"A-03": "coffee"}."""
        self.cats = dict(cats or {})
        self.score, self.conf, self.cat, self.fail = score, conf, cat, set(fail)
        self.garbage = set(garbage)
        self.calls: list[tuple[str, str]] = []

    async def complete(self, route, system, user, *, json_mode=False, **kw):
        from apa.integrations.llm import LLMResult

        agent = re.search(r"\((A-\d\d)\)", system).group(1)
        self.calls.append((agent, user))
        if agent in self.fail or "*" in self.fail:
            raise RuntimeError("llm down")
        if agent in self.garbage:
            return LLMResult("Sure! Here is my verdict: looks fine to me.", "fake-model", 100, 20, 0)
        body = {"approve_score": self.score, "confidence": self.conf, "category": self.cats.get(agent, self.cat),
                "argument": f"{agent}: looks fine"}
        return LLMResult(json.dumps(body), "fake-model", 100, 20, 0)


class FakeNotifier:
    def __init__(self):
        self.sent: list[str] = []

    async def send(self, text):
        self.sent.append(text)
        return 100 + len(self.sent)


def parsed(amount="18000", wallet="gopay", merchant="KOPI KENANGAN", channel="qris",
           direction="out", fee=None, description=None):
    return {"wallet": wallet, "direction": direction, "amount": amount, "channel": channel,
            "merchant": merchant, "description": description, "fee": fee, "confidence": 1.0,
            "tags": [], "mentions": []}


async def new_event(stack, p, *, at=T0, source="android_listener"):
    from apa.db.models.ops import InboundEvent

    payload = {"text": "x", "package": "com.gojek.app", "posted_at": at.isoformat(), "parsed": p}
    async with stack() as s:
        ev = InboundEvent(idempotency_key=uuid.uuid4().hex, source=source, payload=payload,
                          status="parsed")
        s.add(ev)
        await s.commit()
        return ev.id


async def commit(stack, event_id, llm=None, notifier=None, **kw):
    from apa.pipeline.commit import run_commit

    return await run_commit(stack, llm or FakeLLM(), notifier or FakeNotifier(), event_id, tz=TZ, **kw)


async def rows(stack, model, *where):
    from sqlalchemy import select

    q = select(model).where(*where)
    if (key := getattr(model, "id", None)) is not None:  # Postgres promises no row order; tests index by position
        q = q.order_by(key)
    async with stack() as s:
        return list((await s.execute(q)).scalars())


async def tx_for(stack, event_id):
    from apa.db.models.ledger import Transaction

    (tx,) = await rows(stack, Transaction, Transaction.inbound_event_id == event_id)
    return tx


class FlowLLM:
    """One fake for the whole pipeline: extraction calls get `extraction` (a dict, or an exception
    to raise), everything else goes to the debate fake."""

    def __init__(self, extraction=None, debate=None, delay=0.0, receipt=None):
        self.extraction = extraction
        self.receipt = receipt  # a dict (or an exception to raise) answered to calls that carry images
        self.vision_calls: list[int] = []  # number of images per vision call
        self.vision_images: list[list[tuple[bytes, str]]] = []  # the images of each call, in order sent
        self.delay = delay  # simulated model latency, so a second worker can arrive mid-call
        self.debate = debate or FakeLLM()
        self.extract_calls: list[str] = []

    @property
    def calls(self):  # debate calls only, so tests can assert "no debate happened"
        return self.debate.calls

    async def complete(self, route, system, user, **kw):
        from apa.integrations.llm import LLMResult

        if kw.get("images"):
            self.vision_calls.append(len(kw["images"]))
            self.vision_images.append(list(kw["images"]))
            if self.delay:
                await asyncio.sleep(self.delay)
            if isinstance(self.receipt, Exception):
                raise self.receipt
            return LLMResult(json.dumps(self.receipt), "fake-vision", 900, 220, 0)
        if "extract ONE financial transaction" not in system:
            return await self.debate.complete(route, system, user, **kw)
        self.extract_calls.append(user)
        if self.delay:
            await asyncio.sleep(self.delay)
        if isinstance(self.extraction, Exception):
            raise self.extraction
        return LLMResult(json.dumps(self.extraction), "fake-flash", 120, 40, 0)


def extraction(**over):
    base = {"is_transaction": True, "direction": "out", "amount": 30000, "merchant": "warteg",
            "description": None, "wallet": None, "channel": None, "fee": None, "confidence": 0.92}
    return {**base, **over}


async def raw_event(stack, text, *, source="telegram_shorthand", package="com.bca", status="received"):
    """An event as intake leaves it: sanitized text, nothing parsed yet."""
    from apa.db.models.ops import InboundEvent

    payload = {"text": text}
    if source == "android_listener":
        payload.update(package=package, posted_at=T0.isoformat())
    else:
        payload["sent_at"] = T0.isoformat()
    async with stack() as s:
        ev = InboundEvent(idempotency_key=uuid.uuid4().hex, source=source, payload=payload, status=status)
        s.add(ev)
        await s.commit()
        return ev.id
