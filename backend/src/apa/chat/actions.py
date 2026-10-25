"""Write tools the chat agent may PROPOSE, never run. An MCP-style registry: each tool has a strict schema,
a description built by code from the database, and an executor.

Why a model cannot misuse it:
  * it picks a tool name and arguments; both are validated (unknown tool, extra field, bad type, out-of-range
    number, unknown transaction id: all refused before anything is shown);
  * nothing runs on the model's say-so: a proposal is stored under a random single-use nonce (10 minutes) and
    shown to the owner as a card whose text is built from the database, not from model prose;
  * it runs only when the owner taps ✅ (or types /confirm_<nonce>); the pending action is fetched with an
    atomic GETDEL, so a double tap, a replay or a forged callback runs it at most once;
  * 'delete' is the soft undo that already exists: the app's database role has no DELETE at all;
  * every mutation goes through the same code as the normal flows (effects, merchant learning, circuit
    breakers and the debate for new transactions) and writes a hash-chained audit row;
  * there is no raw SQL, no free-form table or column, and proposals are rate-limited.
"""

import json
import re
import secrets
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from apa.chat.batch import MAX_ITEMS, Item, clear_draft, load_draft
from apa.core.format import rupiah
from apa.core.logging import get_logger
from apa.core.settings import get_settings
from apa.db.models.budget import BudgetRule
from apa.db.models.ledger import Transaction
from apa.db.models.ops import InboundEvent
from apa.ingestion.merchants import record_visit, remove_visit, set_default_category
from apa.pipeline.categories import DEFAULT_CATEGORIES, canonicalize
from apa.pipeline.effects import apply_effects, tx_kind
from apa.pipeline.undo import label_of, undo_transaction
from apa.safety.audit_log import append_explainability

log = get_logger(__name__)

PENDING_PREFIX = "apa:pending:"
PENDING_TTL_S = 600
RATE_KEY = "apa:pending:rate"
RATE_PER_MIN = 6
MAX_AMOUNT = Decimal(1_000_000_000)

_NONCE_LEN = 12  # hex chars: 48 bits, single-use and short-lived


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class AddTransaction(_Args):
    amount: Decimal = Field(gt=0, le=MAX_AMOUNT)
    merchant: str = Field(min_length=1, max_length=80)
    direction: Literal["out", "in"] = "out"
    wallet: str = Field(default="unknown", pattern=r"^[a-z_]{2,24}$")
    note: str | None = Field(default=None, max_length=120)


class UndoTransaction(_Args):
    tx_id: int = Field(gt=0)


class EditTransaction(_Args):
    tx_id: int = Field(gt=0)
    extra_tx_ids: list[int] = Field(default_factory=list, max_length=5)  # the same change for these transactions too
    merchant: str | None = Field(default=None, min_length=1, max_length=80)
    category: str | None = Field(default=None, min_length=1, max_length=48)
    notes: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _something_changes(self):
        if self.merchant is None and self.category is None and self.notes is None:
            raise ValueError("nothing to change")
        self.extra_tx_ids = [i for i in dict.fromkeys(self.extra_tx_ids) if i > 0 and i != self.tx_id]
        return self


class SetBudget(_Args):
    category: str = Field(min_length=1, max_length=48)
    monthly_limit: Decimal = Field(gt=0, le=MAX_AMOUNT * 10)


class RecordBatch(_Args):
    """Internal: a chat draft the owner reviewed. Never offered to the model (not in TOOLS)."""

    rev: str = Field(pattern=r"^[0-9a-f]{12}$")
    items: list[Item] = Field(min_length=1, max_length=MAX_ITEMS)


TOOLS: dict[str, type[_Args]] = {  # the only tools a model may propose
    "add_transaction": AddTransaction,
    "undo_transaction": UndoTransaction,
    "edit_transaction": EditTransaction,
    "set_budget": SetBudget,
}
class McpCall(_Args):
    """Internal: a skill (a tool of a connected MCP server) that the owner is asked to confirm. Built by code, never proposed by the model."""

    server: str = Field(min_length=2, max_length=24)
    tool: str = Field(min_length=1, max_length=80)
    arguments: dict = Field(default_factory=dict)


class RoutineAdd(_Args):
    """Internal: a routine (a scheduled reminder, task or skill call) the owner is asked to confirm."""

    name: str = Field(min_length=1, max_length=60)
    kind: str = Field(pattern="^(remind|ask|tool)$")
    at: str = Field(default="", max_length=5)
    days: list[int] = Field(default_factory=list, max_length=7)
    every_min: int = Field(default=0, ge=0, le=1440)
    text: str = Field(default="", max_length=400)
    server: str = Field(default="", max_length=24)
    tool: str = Field(default="", max_length=80)
    arguments: dict = Field(default_factory=dict)
    notify: str = Field(default="always", pattern="^(always|error)$")


class RoutineDelete(_Args):
    name: str = Field(min_length=1, max_length=60)


INTERNAL_TOOLS: dict[str, type[_Args]] = {"record_batch": RecordBatch, "mcp_call": McpCall, "routine_add": RoutineAdd, "routine_delete": RoutineDelete}  # built by code, confirmed by the owner

TOOL_SPEC = """Tools (reply with exactly one):
- add_transaction {"amount": number (rupiah), "merchant": string, "direction": "out"|"in", "wallet": "cash_wallet"|"gopay"|"ovo"|"dana"|"bank_bca"|... (optional), "note": string (optional)}
- undo_transaction {"tx_id": number}  (delete = reverse a recorded transaction; use the #id from RECENT TRANSACTIONS)
- edit_transaction {"tx_id": number, "extra_tx_ids": [number]? (the SAME change for these other transactions: use it when the owner means several, e.g. "both" or "the last two"), "merchant": string?, "category": string?, "notes": string?}
- set_budget {"category": string, "monthly_limit": number}
If the request is unclear, or the transaction it refers to is not in the data, reply {"tool": "none", "say": "<one short question or explanation>"}.
Output JSON only: {"tool": "<name>", "args": {...}} or {"tool": "none", "say": "..."}."""


class Refused(Exception):
    """A proposal that must not even be shown. The message is safe to show the owner."""


def _categories(budgets: list[str]) -> list[str]:
    return list(dict.fromkeys([*DEFAULT_CATEGORIES, *budgets]))


async def _allowed_categories(sm: async_sessionmaker) -> list[str]:
    async with sm() as s:
        names = (await s.execute(select(BudgetRule.category).distinct())).scalars().all()
    return _categories(list(names))


def parse_call(raw: str) -> tuple[str, dict] | str:
    """Model output -> (tool, args), or a reply string when it asked a question or produced garbage."""
    try:
        d = json.loads(raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip())
        tool, args = d.get("tool"), d.get("args") or {}
    except (ValueError, AttributeError):
        return "Maaf, saya belum paham perintahnya. Coba jelaskan lagi dengan lebih singkat."
    if tool == "none":
        return str(d.get("say") or "Bisa dijelaskan lebih lanjut?")[:300]
    if tool not in TOOLS or not isinstance(args, dict):
        return "Maaf, saya belum bisa melakukan itu."
    return tool, args


def _validate(tool: str, args: dict) -> _Args:
    try:
        return {**TOOLS, **INTERNAL_TOOLS}[tool](**args)
    except (ValidationError, TypeError) as e:
        log.info("action_args_rejected", tool=tool, error=str(e)[:200])
        raise Refused(
            "Detail perintahnya belum lengkap atau tidak valid. Coba sebutkan nominal, nama, atau nomor transaksinya."
        ) from None


async def _tx(sm: async_sessionmaker, tx_id: int) -> Transaction | None:
    async with sm() as s:
        return await s.get(Transaction, tx_id)


async def describe(sm: async_sessionmaker, tool: str, a: _Args) -> str:
    """The card text, built from the database and the validated arguments only."""
    if isinstance(a, AddTransaction):
        sign = "pemasukan" if a.direction == "in" else "pengeluaran"
        return f"Catat {sign} baru: {a.merchant} · {rupiah(a.amount)} · dompet {a.wallet}" + (
            f" · «{a.note}»" if a.note else ""
        )
    if isinstance(a, (RoutineAdd, RoutineDelete)):
        from apa import mcp_client, routines

        if isinstance(a, RoutineDelete):
            return f"Hapus rutinitas «{a.name}»"
        try:
            r = routines.check(routines.Routine(id="x" + routines.slug(a.name)[:20] or "xx", **a.model_dump()))
        except ValueError as e:
            raise Refused(str(e).replace("1 validation error for Routine", "").strip()[:200]) from None
        if r.kind == "tool":
            c = next((x for x in await mcp_client.current(sm) if x.id == r.server and x.enabled), None)
            if c is None or r.tool not in {t["name"] for t in c.tools} or r.tool in c.off:
                raise Refused("Skill itu tidak tersedia. Pilih dari daftar skill yang tersambung.")
        return routines.describe(r) + (" · berjalan sendiri tanpa bertanya" if r.kind == "tool" else "")
    if isinstance(a, McpCall):
        shown = json.dumps(a.arguments, ensure_ascii=False)
        return f"Jalankan skill «{a.server}/{a.tool}»" + (f" dengan: {shown[:220]}" if a.arguments else "")
    if isinstance(a, RecordBatch):
        total = sum((i.amount for i in a.items if i.direction == "out"), Decimal(0))
        return f"Catat {len(a.items)} pembayaran sekaligus (keluar {rupiah(total)})"
    if isinstance(a, SetBudget):
        cats = await _allowed_categories(sm)
        cat = canonicalize(a.category, cats)
        if cat is None:
            raise Refused(f"Kategori «{a.category[:30]}» tidak dikenal. Pilihan: " + ", ".join(cats[:10]))
        return f"Atur anggaran {cat} bulan ini menjadi {rupiah(a.monthly_limit)}"
    tx = await _tx(sm, a.tx_id)  # Undo / Edit
    if tx is None:
        raise Refused(f"Transaksi #{a.tx_id} tidak ditemukan.")
    if tx.status != "committed":
        raise Refused(
            f"Transaksi #{a.tx_id} ({label_of(tx)}) tidak berstatus tercatat, jadi tidak bisa diubah."
        )
    if isinstance(a, UndoTransaction):
        return f"Batalkan transaksi #{tx.id}: {label_of(tx)} (saldo dan anggaran dikembalikan)"
    for other_id in a.extra_tx_ids:  # an edit of several transactions: every one must exist and be recorded
        other = await _tx(sm, other_id)
        if other is None or other.status != "committed":
            raise Refused(f"Transaksi #{other_id} tidak ditemukan atau tidak berstatus tercatat, jadi tidak bisa diubah.")
    changes = []
    if a.merchant is not None:
        changes.append(f"nama → {a.merchant}")
    if a.category is not None:
        cat = canonicalize(a.category, await _allowed_categories(sm))
        if cat is None:
            raise Refused(f"Kategori «{a.category[:30]}» tidak dikenal.")
        changes.append(f"kategori → {cat}")
    if a.notes is not None:
        changes.append(f"catatan → «{a.notes}»")
    if a.extra_tx_ids:
        ids = ", ".join(f"#{i}" for i in [tx.id, *a.extra_tx_ids])
        return f"Ubah {1 + len(a.extra_tx_ids)} transaksi ({ids}) sekaligus: " + ", ".join(changes)
    return f"Ubah transaksi #{tx.id} ({label_of(tx)}): " + ", ".join(changes)


async def stash(redis: Any, tool: str, a: _Args) -> str:
    """Store a validated proposal under a fresh single-use nonce and return the nonce."""
    nonce = secrets.token_hex(_NONCE_LEN // 2)
    await redis.set(
        PENDING_PREFIX + nonce,
        json.dumps({"tool": tool, "args": a.model_dump(mode="json"), "owner": get_settings().telegram_owner_id}),
        ex=PENDING_TTL_S,
    )
    return nonce


async def propose(sm: async_sessionmaker, redis: Any, raw_call: str) -> str:
    """Validate a model's tool call and, if sound, store it and return the confirmation card text.
    Returns plain reply text when the model asked a question or the call was refused."""
    parsed = parse_call(raw_call)
    if isinstance(parsed, str):
        return parsed
    tool, args = parsed
    return await _propose_validated(sm, redis, tool, args)


async def propose_internal(sm: async_sessionmaker, redis: Any, tool: str, args: dict) -> str:
    """A confirmation card for an action that code (not the model's own wording) built, for example a skill call."""
    if tool not in INTERNAL_TOOLS:
        return "Maaf, saya belum bisa melakukan itu."
    return await _propose_validated(sm, redis, tool, args)


async def _propose_validated(sm: async_sessionmaker, redis: Any, tool: str, args: dict) -> str:
    try:
        a = _validate(tool, args)
        card = await describe(sm, tool, a)
    except Refused as r:
        return str(r)
    if redis is None:
        return "Konfirmasi tidak tersedia saat ini."
    count = await redis.incr(RATE_KEY)
    if count == 1:
        await redis.expire(RATE_KEY, 60)
    if count > RATE_PER_MIN:
        return "Terlalu banyak permintaan perubahan dalam semenit. Tunggu sebentar."
    nonce = await stash(redis, tool, a)
    log.info("action_proposed", tool=tool)
    return f"🛠️ {card}\nLanjutkan? /confirm_{nonce} · /cancel_{nonce} (berlaku 10 menit)"


# ---- execution ---------------------------------------------------------------------------------


async def _exec_add(sm, redis, a: AddTransaction, nonce: str, tz: ZoneInfo) -> str:
    now = datetime.now(tz)
    parsed = {
        "wallet": a.wallet,
        "direction": a.direction,
        "amount": str(a.amount),
        "channel": None,
        "merchant": a.merchant,
        "description": a.note,
        "fee": None,
        "confidence": 0.9,
        "tags": [],
        "mentions": [],
    }
    payload = {
        "text": f"{a.merchant} {a.amount}",
        "sent_at": now.isoformat(),
        "parsed": parsed,
        "via": "chat_action",
    }
    async with sm() as s:
        event_id = (
            await s.execute(
                insert(InboundEvent)
                .values(
                    idempotency_key=f"chat-add:{nonce}",
                    source="telegram_shorthand",
                    payload=payload,
                    status="parsed",
                    injection_flags=[],
                )
                .on_conflict_do_nothing(index_elements=["idempotency_key"])
                .returning(InboundEvent.id)
            )
        ).scalar_one_or_none()
        await s.commit()
    if event_id is None:
        return "Permintaan ini sudah diproses."
    await redis.enqueue_job("commit_event", event_id)  # the normal breakers + debate + summary follow
    return "⏳ Diproses lewat pemeriksaan biasa (batas risiko dan debat agen). Ringkasannya menyusul."


async def _exec_batch(sm, redis, a: RecordBatch, nonce: str, tz: ZoneInfo) -> str:
    """Every item becomes an ordinary parsed event: breakers, debate, quorum and the summary all still run."""
    draft = await load_draft(redis)
    if draft is None or draft.rev != a.rev:  # an older version of the draft: it was edited or already recorded
        return "Draft sudah berubah atau kedaluwarsa. Kirim «draft» atau tulis ulang daftarnya."
    await clear_draft(redis)
    batch_id = str(uuid.uuid4())
    now = datetime.now(tz)
    ids: list[int] = []
    async with sm() as s:
        for n, it in enumerate(a.items, 1):
            parsed = {
                "wallet": it.wallet, "direction": it.direction, "amount": str(it.amount), "channel": None,
                "merchant": it.merchant, "description": None, "fee": None, "confidence": 0.9,
                "tags": [f"#{re.sub(r'[^A-Za-z0-9_]+', '_', it.label)}"] if it.label else [], "mentions": [],
                "timestamp": it.time.isoformat(), "category": it.category, "batch_id": batch_id,
            }
            payload = {"text": f"{it.merchant} {it.amount}", "sent_at": now.isoformat(), "parsed": parsed,
                       "via": "chat_batch"}
            event_id = (
                await s.execute(
                    insert(InboundEvent)
                    .values(idempotency_key=f"chat-batch:{a.rev}:{n}", source="telegram_shorthand",
                            payload=payload, status="parsed", injection_flags=[])
                    .on_conflict_do_nothing(index_elements=["idempotency_key"])
                    .returning(InboundEvent.id)
                )
            ).scalar_one_or_none()
            if event_id is not None:
                ids.append(event_id)
        await s.commit()
    for event_id in ids:
        await redis.enqueue_job("commit_event", event_id)
    log.info("batch_confirmed", items=len(ids), batch_id=batch_id)
    return (f"⏳ {len(ids)} pembayaran diproses lewat pemeriksaan biasa (batas risiko dan debat agen). "
            "Ringkasan tiap transaksi menyusul.")


async def _exec_undo(sm, redis, a: UndoTransaction, nonce: str, tz: ZoneInfo) -> str:
    r = await undo_transaction(sm, a.tx_id, via="telegram", tz=tz)  # chat is the Telegram channel
    if r.status == "ok":
        return f"↩️ Dibatalkan: {r.label}"
    return {
        "not_found": "Transaksi tidak ditemukan.",
        "wrong_state": "Transaksi ini sudah dibatalkan atau belum tercatat.",
        "expired": "Batas waktu pembatalan sudah lewat.",
    }[r.status]


async def _exec_edit(sm, redis, a: EditTransaction, nonce: str, tz: ZoneInfo) -> str:
    """One confirmed edit, applied to the transaction (and to each extra one the owner named) in turn."""
    results = [await _edit_one(sm, a, tx_id, tz) for tx_id in [a.tx_id, *a.extra_tx_ids]]
    return results[0] if len(results) == 1 else "\n".join(results)


async def _edit_one(sm, a: EditTransaction, tx_id: int, tz: ZoneInfo) -> str:
    cats = await _allowed_categories(sm)
    new_cat = canonicalize(a.category, cats) if a.category is not None else None
    if a.category is not None and new_cat is None:
        return f"Kategori «{a.category[:30]}» tidak dikenal."
    now = datetime.now(UTC)
    async with sm() as s:
        tx = (
            await s.execute(select(Transaction).where(Transaction.id == tx_id).with_for_update())
        ).scalar_one_or_none()
        if tx is None or tx.status != "committed":
            return f"Transaksi #{tx_id} tidak ditemukan atau tidak berstatus tercatat."
        expense = tx_kind(tx.direction, tx.execution_mode) == "expense"
        old_cat, old_merchant = tx.primary_category, tx.merchant
        changed: list[str] = []
        if new_cat is not None and new_cat != old_cat:
            await apply_effects(s, tx, sign=-1, tz=tz, now=now)  # take it out of the old budget...
            tx.primary_category = new_cat
            await s.flush()
            await apply_effects(s, tx, sign=+1, tz=tz, now=now)  # ...and put it in the new one
            changed.append(f"kategori {old_cat or '-'} → {new_cat}")
            if expense and tx.merchant:  # the owner's correction becomes what the fast path will use
                await set_default_category(s, tx.merchant, new_cat, now)
        if a.merchant is not None and a.merchant != old_merchant:
            if expense and old_merchant:
                await remove_visit(s, old_merchant, tx.net_user_expense)
            if expense:
                await record_visit(
                    s, a.merchant, tx.net_user_expense, tx.timestamp, category=tx.primary_category
                )
            tx.merchant = a.merchant
            changed.append(f"nama {old_merchant or '-'} → {a.merchant}")
        if a.notes is not None and a.notes != tx.notes:
            tx.notes = a.notes
            changed.append("catatan")
        if not changed:
            return "Tidak ada yang berubah."
        await append_explainability(
            s,
            decision_type="edit",
            transaction_id=tx.id,
            verdict="edit:applied",
            justification="owner-confirmed edit via chat: " + "; ".join(changed),
        )
        label = label_of(tx)
        await s.commit()
    return f"✏️ Diubah: {label} ({'; '.join(changed)})"


async def _exec_budget(sm, redis, a: SetBudget, nonce: str, tz: ZoneInfo) -> str:
    cat = canonicalize(a.category, await _allowed_categories(sm))
    if cat is None:
        return f"Kategori «{a.category[:30]}» tidak dikenal."
    month = datetime.now(tz).date().replace(day=1)
    async with sm() as s:
        rule = (
            await s.execute(
                select(BudgetRule)
                .where(BudgetRule.category == cat, BudgetRule.period_month == month)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if rule is None:
            spent = (
                await s.execute(
                    select(func.coalesce(func.sum(Transaction.net_user_expense), 0)).where(
                        Transaction.status == "committed",
                        Transaction.direction == "out",
                        Transaction.primary_category == cat,
                        func.date(func.timezone(str(tz), Transaction.timestamp)) >= month,
                    )
                )
            ).scalar_one()
            s.add(
                BudgetRule(
                    category=cat,
                    period_month=month,
                    base_limit=a.monthly_limit,
                    current_limit=a.monthly_limit,
                    spent_amount=spent,
                )
            )
            before = None
        else:
            before = rule.current_limit
            rule.base_limit = rule.current_limit = a.monthly_limit
        await append_explainability(
            s,
            decision_type="budget_set",
            verdict="budget:set",
            justification=f"owner-confirmed via chat: {cat} {month:%m/%Y} limit "
            f"{'-' if before is None else rupiah(before)} -> {rupiah(a.monthly_limit)}",
        )
        await s.commit()
    return f"💰 Anggaran {cat} bulan ini: {rupiah(a.monthly_limit)}"


async def _exec_mcp(sm, redis, a: McpCall, nonce: str, tz: ZoneInfo) -> str:
    from apa import mcp_client

    try:
        text = await mcp_client.run(sm, a.server, a.tool, a.arguments)
    except mcp_client.McpError as e:
        return f"⚠️ {e}"
    return f"✅ Skill «{a.server}/{a.tool}» selesai:\n{text[:1500]}"


async def _exec_routine_add(sm, redis, a: RoutineAdd, nonce: str, tz: ZoneInfo) -> str:
    from apa import routines

    try:
        r = await routines.add(sm, **a.model_dump())
    except ValueError as e:
        return f"⚠️ {str(e)[:200]}"
    return f"✅ Rutinitas «{r.name}» dibuat ({r.when()}). Kelola di dashboard (Routines)."


async def _exec_routine_delete(sm, redis, a: RoutineDelete, nonce: str, tz: ZoneInfo) -> str:
    from apa import routines

    return f"✅ Rutinitas «{a.name}» dihapus." if await routines.remove(sm, a.name) else "Rutinitas itu tidak ditemukan."


_EXEC = {
    "routine_add": _exec_routine_add,
    "routine_delete": _exec_routine_delete,
    "mcp_call": _exec_mcp,
    "record_batch": _exec_batch,
    "add_transaction": _exec_add,
    "undo_transaction": _exec_undo,
    "edit_transaction": _exec_edit,
    "set_budget": _exec_budget,
}


async def resolve(
    sm: async_sessionmaker,
    redis: Any,
    nonce: str,
    accept: bool,
    *,
    actor: int | None,
    tz: ZoneInfo | None = None,
) -> str:
    """The owner answered a confirmation card. Single use: the pending action is removed atomically first."""
    tz = tz or ZoneInfo(get_settings().timezone)
    if actor != get_settings().telegram_owner_id:
        return "Tidak diizinkan."
    if len(nonce) != _NONCE_LEN or any(c not in "0123456789abcdef" for c in nonce):
        return "Kode konfirmasi tidak valid."
    raw = await redis.getdel(PENDING_PREFIX + nonce)
    if raw is None:
        return "Permintaan sudah kedaluwarsa atau sudah diproses."
    if not accept:
        if b'"record_batch"' in (raw if isinstance(raw, bytes) else raw.encode()):
            await clear_draft(redis)  # cancelling a batch card drops the draft it belonged to
        return "👍 Dibatalkan, tidak ada yang diubah."
    try:
        d = json.loads(raw)
        if d.get("owner") != actor:
            return "Tidak diizinkan."
        a = _validate(d["tool"], d["args"])  # re-validated: what runs is what the schema allows, nothing else
    except (Refused, ValueError, KeyError):
        return "Permintaan tidak valid."
    log.info("action_confirmed", tool=d["tool"])
    return await _EXEC[d["tool"]](sm, redis, a, nonce, tz)


def new_nonce() -> str:  # exposed for tests
    return uuid.uuid4().hex[:_NONCE_LEN]
