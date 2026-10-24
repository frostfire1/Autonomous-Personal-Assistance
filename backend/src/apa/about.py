"""One dataset about the owner: who they are, how they like to be talked to, what they enjoy, how they live.

Everything the assistant knows is brought together here as ONE card made of domains (identity, comfort, personality, hobbies,
tastes, routines, wellbeing, money, people, goals, plans). Each entry says where it came from and how sure it is:

- FACTS (`memory_items`, kind "fact"): key/value entries the owner set (locked: nothing but the owner changes them) or the
  assistant wrote with evidence (the `about_set` tool, the nightly digest). One entry per (domain, key).
- NOTES and LEARNED items: what the owner asked it to remember and what it noticed in chats (editable here too).
- TRAITS: what code computed from the owner's data (sleep, food, usage, style, mood...) and what the AI wrote from it (persona,
  media taste). Read-only; the owner corrects one by writing a fact with the same domain and key, which then wins.

The owner reads, edits, locks and deletes everything on the dashboard (About me). The assistant reads the whole card as one
block (`person_card`), and writes through two tools. Everything is encrypted like the diary; `export_rows` gives a flat,
stable schema (domain, key, value, source, confidence, evidence, updated) for retrieval or training a model later.
Private journal entries are never part of it."""

import json
import re
from datetime import date
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from apa import assistant_memory as mem
from apa.db.models.intel import MemoryItem, ProfileTrait

FACT = "fact"
DOMAINS = ("identity", "comfort", "personality", "hobbies", "tastes", "routines", "wellbeing", "money", "people", "goals", "plans", "notes", "other")
MAX_FACTS = 300
MAX_VALUE = 400
LEARNED_DOMAIN = {"person": "people", "preference": "tastes", "goal": "goals", "routine": "routines", "plan": "plans", "health": "wellbeing", "other": "other"}
TRAIT_DOMAIN = {
    "style": "comfort", "sleep": "wellbeing", "steps": "wellbeing", "stress": "wellbeing", "food": "tastes", "likes": "tastes",
    "spending.pattern": "money", "activity": "routines", "screen_time": "routines", "interests.topics": "hobbies", "hobbies": "hobbies",
    "youtube": "hobbies", "habits": "hobbies", "ai.content": "hobbies", "mood": "personality", "ai.persona": "personality",
}
SOURCES = ("owner", "ai", "telegram", "computed")


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:40]


def _dom(domain: str) -> str:
    d = slug(domain)
    return d if d in DOMAINS else "other"


def _val(v: str) -> str:
    return mem.clean(" ".join(str(v).split()), MAX_VALUE)


async def _fact_rows(sm: async_sessionmaker) -> list[tuple[MemoryItem, dict]]:
    out = []
    for r in await mem._items(sm, MemoryItem.kind == FACT):
        try:
            d = json.loads(mem._open(r))
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("key"):
            out.append((r, d))
    return out


def _entry(r: MemoryItem, d: dict) -> dict:
    return {"ref": f"fact:{r.id}", "domain": d["domain"], "key": d["key"], "value": d.get("value", ""), "why": d.get("why", ""),
            "source": d.get("src", "ai"), "confidence": d.get("conf", 60), "locked": bool(d.get("locked")),
            "updated": r.day.isoformat() if r.day else None, "editable": True}


async def facts(sm: async_sessionmaker) -> list[dict]:
    return [_entry(r, d) for r, d in await _fact_rows(sm)]


async def set_fact(sm: async_sessionmaker, domain: str, key: str, value: str, *, src: str = "ai", conf: int = 60, why: str = "",
                   locked: bool | None = None, today: date | None = None) -> str:
    """Write one fact. -> "saved", "locked" (the owner owns it: an AI or computed write never changes it), "empty" or "off" (no
    SECRETS_KEY). The owner's own writes are locked unless they say otherwise; a re-write replaces the fact of that domain and key."""
    domain, key, value = _dom(domain), slug(key), _val(value)
    if not key or len(value) < 1:
        return "empty"
    src = src if src in SOURCES else "ai"
    rows = await _fact_rows(sm)
    old = next(((r, d) for r, d in rows if d["domain"] == domain and d["key"] == key), None)
    if old and old[1].get("locked") and src != "owner":
        return "locked"
    body = {"domain": domain, "key": key, "value": value, "why": mem.clean(why, 160), "conf": max(0, min(100, conf)), "src": src,
            "locked": (src == "owner") if locked is None else locked}
    try:
        row = mem._row(FACT, json.dumps(body, ensure_ascii=False), day=today or date.today(), source="owner" if src == "owner" else "auto")
    except mem.SecretsUnavailable:
        return "off"
    async with sm() as s:
        if old:  # in place, so the entry keeps its id (the dashboard refers to it)
            await s.execute(update(MemoryItem).where(MemoryItem.id == old[0].id).values(text=row.text, day=row.day, source=row.source, enc=True))
        else:
            s.add(row)
            await s.flush()
            await mem._trim(s, MemoryItem.kind == FACT, MAX_FACTS)
        await s.commit()
    return "saved"


async def forget_fact(sm: async_sessionmaker, domain: str, key: str, *, owner: bool = False) -> str:
    """Remove a fact. The assistant cannot remove a locked one (the owner can, on the dashboard)."""
    domain, key = _dom(domain), slug(key)
    for r, d in await _fact_rows(sm):
        if d["domain"] == domain and d["key"] == key:
            if d.get("locked") and not owner:
                return "locked"
            async with sm() as s:
                await s.execute(delete(MemoryItem).where(MemoryItem.id == r.id))
                await s.commit()
            return "forgotten"
    return "missing"


def _split_ref(ref: str) -> tuple[str, str]:
    kind, _, ident = ref.partition(":")
    return kind, ident


async def card(sm: async_sessionmaker) -> list[dict]:
    """Every entry of the dataset, as one flat list (see `entry` fields), owner-locked first inside each domain."""
    out = [e for e in await facts(sm)]
    taken = {(e["domain"], e["key"]) for e in out}
    for r in await mem._items(sm, MemoryItem.kind == "note"):
        out.append({"ref": f"item:{r.id}", "domain": "notes", "key": f"note_{r.id}", "value": mem._open(r), "why": "", "source": "owner",
                    "confidence": 100, "locked": False, "updated": r.day.isoformat() if r.day else r.created_at.date().isoformat(), "editable": True})
    for r in await mem._items(sm, mem._LEARNED):
        out.append({"ref": f"item:{r.id}", "domain": LEARNED_DOMAIN.get(r.kind, "other"), "key": f"learned_{r.id}", "value": mem._open(r), "why": "noticed in chat",
                    "source": "ai", "confidence": 60, "locked": False, "updated": r.day.isoformat() if r.day else None, "editable": True})
    async with sm() as s:
        traits = (await s.execute(select(ProfileTrait))).scalars().all()
    for t in traits:
        domain = TRAIT_DOMAIN.get(t.key, "other")
        if (domain, slug(t.key)) in taken or (domain, slug(t.key.split(".")[-1])) in taken:
            continue  # the owner wrote a fact for it: that fact wins
        out.append({"ref": f"trait:{t.key}", "domain": domain, "key": t.key, "value": t.summary, "why": f"{t.evidence} observations", "source": "ai" if t.key.startswith("ai.") else "computed",
                    "confidence": min(95, 40 + t.evidence), "locked": False, "updated": t.computed_at.date().isoformat(), "editable": False})
    return sorted(out, key=lambda e: (DOMAINS.index(e["domain"]), not e["locked"], e["key"]))


async def update_item(sm: async_sessionmaker, ref: str, value: str, locked: bool | None = None) -> bool:
    """The owner corrects an entry: a fact (value and lock), a note or a learned item (text). A trait is corrected by writing a fact."""
    kind, ident = _split_ref(ref)
    if not ident.isdigit():
        return False
    item_id = int(ident)
    if kind == "fact":
        for r, d in await _fact_rows(sm):
            if r.id == item_id:
                return await set_fact(sm, d["domain"], d["key"], value or d["value"], src="owner", why=d.get("why", ""),
                                      locked=True if locked is None else locked) == "saved"  # an edit by the owner locks it
        return False
    if kind == "item":
        text = mem.clean(value)
        if len(text) < 3:
            return False
        async with sm() as s:
            row = (await s.execute(select(MemoryItem).where(MemoryItem.id == item_id, MemoryItem.kind.in_(("note",) + tuple(mem.KINDS))))).scalar_one_or_none()
            if row is None:
                return False
            row.text, row.enc = mem._seal(text), True
            await s.commit()
        return True
    return False


async def delete_item(sm: async_sessionmaker, ref: str) -> bool:
    kind, ident = _split_ref(ref)
    if not ident.isdigit() or kind not in ("fact", "item"):
        return False
    async with sm() as s:
        kinds = (FACT,) if kind == "fact" else ("note",) + tuple(mem.KINDS)
        res = await s.execute(delete(MemoryItem).where(MemoryItem.id == int(ident), MemoryItem.kind.in_(kinds)))
        await s.commit()
    return bool(res.rowcount)


async def comfort(sm: async_sessionmaker) -> dict[str, str]:
    """The owner's stated comfort settings (domain comfort): language, tone, length, emoji, name, nickname... (key -> value)."""
    return {d["key"]: d["value"] for _, d in await _fact_rows(sm) if d["domain"] in ("comfort", "identity")}


def render(entries: list[dict], limit: int = 2600) -> str:
    """The card as text for the assistant: one block per domain, the owner's own words first."""
    lines: list[str] = []
    domain = None
    for e in entries:
        if e["domain"] != domain:
            domain = e["domain"]
            lines.append(f"[{domain}]")
        tag = " (set by the owner)" if e["locked"] else ""
        text = e["value"] if e["ref"].startswith(("item", "trait")) else f"{e['key'].replace('_', ' ')}: {e['value']}"
        lines.append(f"- {text}{tag}")
    return "\n".join(lines)[:limit]


async def person_card(sm: async_sessionmaker, limit: int = 2600) -> str:
    entries = [e for e in await card(sm) if e["domain"] != "notes"]  # numbered notes are shown separately (WHAT YOU REMEMBER)
    return render(entries, limit)


async def note_name(sm: async_sessionmaker, first_name: str | None) -> None:
    """The name Telegram shows for the owner, kept as identity.name until the owner (or the assistant, on their word) sets one."""
    name = _val(first_name or "")
    if name:
        existing = [d for _, d in await _fact_rows(sm) if d["domain"] == "identity" and d["key"] == "name"]
        if not existing:
            await set_fact(sm, "identity", "name", name, src="telegram", conf=90, why="Telegram profile name")


async def export_rows(sm: async_sessionmaker) -> list[dict[str, Any]]:
    """The whole dataset in one flat, stable shape (for retrieval or for training a model later)."""
    return [{"id": e["ref"], "domain": e["domain"], "key": e["key"], "value": e["value"], "source": e["source"], "confidence": e["confidence"],
             "locked": e["locked"], "evidence": e["why"], "updated": e["updated"]} for e in await card(sm)]


def export_jsonl(rows: list[dict[str, Any]]) -> str:
    return "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + ("\n" if rows else "")

