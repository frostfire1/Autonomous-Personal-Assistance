"""What the assistant has been asked to remember about the owner ("I am vegetarian", "my wife is Sari", "I pay rent on the 5th").
A short list of the owner's own words, kept on the server (`policy_rules`, key `assistant_memory`) and shown to the assistant
with every message. The owner sees and deletes it in the dashboard. It never holds secrets by design: the assistant is told
not to store passwords, keys or card numbers, and the text is scrubbed of long digit runs before it is saved."""

import re

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from apa.db.models.budget import PolicyRule

RULE_KEY = "assistant_memory"
MAX_FACTS = 30
MAX_CHARS = 200
_LONG_DIGITS = re.compile(r"\d[\d\s-]{8,}\d")  # account, card and phone numbers


def clean(fact: str) -> str:
    return _LONG_DIGITS.sub("[number]", " ".join(fact.split()))[:MAX_CHARS]


async def load(sm: async_sessionmaker) -> list[str]:
    async with sm() as s:
        row = (await s.execute(select(PolicyRule).where(PolicyRule.rule_key == RULE_KEY))).scalar_one_or_none()
    facts = (row.value_json or {}).get("facts", []) if row else []
    return [f for f in facts if isinstance(f, str)][:MAX_FACTS]


async def _save(sm: async_sessionmaker, facts: list[str]) -> None:
    data = {"facts": facts[:MAX_FACTS]}
    async with sm() as s:
        stmt = insert(PolicyRule).values(rule_key=RULE_KEY, scope="global", scope_ref="", value_json=data, updated_by="assistant",
                                         is_active=True)
        await s.execute(stmt.on_conflict_do_update(index_elements=["rule_key", "scope", "scope_ref"],
                                                   set_={"value_json": data, "updated_by": "assistant"}))
        await s.commit()


async def remember(sm: async_sessionmaker, fact: str) -> str:
    """Add one fact (the oldest is dropped when full). -> a short sentence for the assistant to relay."""
    fact = clean(fact)
    if len(fact) < 3:
        return "Nothing to remember."
    facts = await load(sm)
    if fact.lower() in (f.lower() for f in facts):
        return "Already remembered."
    await _save(sm, [*facts, fact][-MAX_FACTS:])
    return f"Remembered: {fact}"


async def forget(sm: async_sessionmaker, index: int) -> str:
    """Remove fact number `index` (1 is the first)."""
    facts = await load(sm)
    if not 1 <= index <= len(facts):
        return "No such item."
    gone = facts.pop(index - 1)
    await _save(sm, facts)
    return f"Forgot: {gone}"
