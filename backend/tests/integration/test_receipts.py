"""Receipt photos end to end against real Postgres: intake, vision reading, items, evidence, fusion."""

import asyncio
import itertools
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from arq import Retry
from sqlalchemy.exc import IntegrityError

from apa.bot.commands import handle_command
from apa.bot.photo import DUPLICATE_NOTICE, handle_photo
from apa.db.models.audit import ExplainabilityLog
from apa.db.models.budget import BudgetRule
from apa.db.models.evidence import EvidenceBundle, EvidenceFile
from apa.db.models.ledger import Transaction, TransactionItem
from apa.db.models.ops import InboundEvent, TelegramMessage
from apa.ingestion.photo_intake import intake_photo
from apa.integrations.evidence_store import LocalEvidenceStore
from apa.pipeline.commit import run_release
from apa.pipeline.undo import undo_transaction
from apa.safety.audit_log import verify_log
from apa.workers import settings as worker
from tests.integration.conftest import HAVE_INFRA
from tests.integration.helpers import (
    T0,
    TZ,
    FakeLLM,
    FakeNotifier,
    FlowLLM,
    commit,
    new_event,
    parsed,
    rows,
    tx_for,
)
from tests.test_receipt_extract import receipt as receipt_json

pytestmark = pytest.mark.skipif(not HAVE_INFRA, reason="TEST_DATABASE_URL/TEST_REDIS_URL not set")

MONTH = T0.date().replace(day=1)
_ids = itertools.count(1000)


class Q:
    def __init__(self):
        self.jobs: list[tuple] = []
        self.kwargs: list[dict] = []  # e.g. _job_id / _defer_by for album jobs

    async def enqueue_job(self, name, *args, **kw):
        self.jobs.append((name, *args))
        self.kwargs.append(kw)


def jpeg(n=1):
    return b"\xff\xd8\xff\xe0" + f"receipt-photo-{n}-".encode() * 40


@pytest.fixture
def store(tmp_path):
    return LocalEvidenceStore(tmp_path / "evidence")


def ctx_for(stack, store, llm, notifier=None, q=None, job_try=1):
    return {"sessionmaker": stack, "llm": llm, "store": store, "notifier": notifier or FakeNotifier(),
            "redis": q or Q(), "job_try": job_try}


async def send(stack, store, q, data, *, caption=None, message_id=None, sent_at=T0):
    return await intake_photo(stack, q, store, chat_id=1, message_id=message_id or next(_ids), data=data,
                              mime="image/jpeg", caption=caption, sent_at=sent_at)


async def process(ctx, eid):
    """What the two worker jobs do for one photo; returns (read status, commit status or None)."""
    read = await worker.read_receipt(ctx, eid)
    return read, (await worker.commit_event(ctx, eid) if read == "parsed" else None)


async def photo_to_ledger(stack, store, llm, *, data=None, caption=None, notifier=None, sent_at=T0):
    q = Q()
    ctx = ctx_for(stack, store, llm, notifier, q)
    intake = await send(stack, store, q, data or jpeg(), caption=caption, sent_at=sent_at)
    assert intake.status == "queued", intake
    return intake.event_id, ctx, await process(ctx, intake.event_id)


async def bank_tx(stack, *, amount="58300", wallet="gopay", merchant="KOPI KENANGAN", at=T0):
    eid = await new_event(stack, parsed(amount=amount, wallet=wallet, merchant=merchant), at=at)
    assert await commit(stack, eid) == "committed"
    return await tx_for(stack, eid)


async def seed_budget(stack):
    async with stack() as s:
        s.add(BudgetRule(category="Food & Dining", period_month=MONTH, base_limit=Decimal(2_000_000),
                         current_limit=Decimal(2_000_000)))
        await s.commit()


async def all_tx(stack):
    return sorted(await rows(stack, Transaction), key=lambda t: t.id)


async def audit(stack, decision):
    return [r for r in await rows(stack, ExplainabilityLog) if r.decision_type == decision]


# ---- the whole path ----------------------------------------------------------------------------------------


async def test_a_receipt_photo_becomes_a_transaction_with_items_and_evidence(stack, store):
    await seed_budget(stack)
    llm, notifier = FlowLLM(receipt=receipt_json(), debate=FakeLLM()), FakeNotifier()
    eid, ctx, (read, done) = await photo_to_ledger(stack, store, llm, notifier=notifier)
    assert (read, done) == ("parsed", "committed")

    (tx,) = await all_tx(stack)
    assert (tx.source_channel, tx.merchant, tx.total_paid_amount, tx.payment_wallet, tx.status) == (
        "multi_image_receipt", "WARTEG BU SITI", Decimal(58300), "unknown", "committed")
    assert tx.timestamp == T0 + timedelta(minutes=20)  # the receipt's own time (12:20), not the send time
    assert tx.primary_category == "Food & Dining"
    assert (await rows(stack, BudgetRule))[0].spent_amount == Decimal(58300)

    items = sorted(await rows(stack, TransactionItem), key=lambda i: i.id)
    assert [(i.item_name, i.quantity, i.unit_price, i.total_price) for i in items] == [
        ("Nasi Ayam Goreng", 2, 20000, 40000), ("Es Teh Manis", 2, 5000, 10000), ("Tempe Goreng", 1, 3000, 3000)]
    assert items[0].normalized_item_name == "nasi ayam goreng"

    (bundle,) = await rows(stack, EvidenceBundle)
    (file,) = await rows(stack, EvidenceFile)
    assert (bundle.transaction_id, bundle.cross_verification_status, bundle.total_attachments) == (
        tx.id, "single_verified", 1)
    assert tx.evidence_bundle_id == bundle.id and file.bundle_id == bundle.id
    assert file.file_role == "receipt_primary" and file.ocr_payload["tax"] == "5300"
    assert all(i.evidence_file_ref == file.id for i in items)
    assert await store.get(file.file_storage_uri) == jpeg()  # the image is retrievable by its URI

    # one vision call with exactly one image, then the full debate, which saw the items
    assert llm.vision_calls == [1] and len(llm.calls) == 5
    prompt = llm.calls[0][1]
    assert "Nasi Ayam Goreng x2 = Rp 40.000" in prompt and "paid by qris" in prompt
    assert prompt.index("untrusted text begins") < prompt.index("Nasi Ayam Goreng") < prompt.index("untrusted text ends")
    assert "Kasir: Belum diketahui" in notifier.sent[0] and "/undo_" in notifier.sent[0]
    async with stack() as s:
        assert await verify_log(s) is None


async def test_the_image_bytes_never_enter_the_queue_or_the_events_table(stack, store):
    q = Q()
    intake = await send(stack, store, q, jpeg(), caption="makan siang")
    assert q.jobs == [("read_receipt", intake.event_id)]  # an id, not an image
    (ev,) = await rows(stack, InboundEvent)
    (att,) = ev.payload["attachments"]
    assert set(att) == {"uri", "sha256", "mime", "size", "message_id"} and att["size"] == len(jpeg())
    assert "receipt-photo" not in str(ev.payload)


# ---- the same photo must never count twice ------------------------------------------------------------------


async def test_resending_the_same_photo_is_refused_at_every_stage(stack, store):
    q = Q()
    assert (await send(stack, store, q, jpeg())).status == "queued"
    assert (await send(stack, store, q, jpeg())).status == "duplicate"  # while queued

    ctx = ctx_for(stack, store, FlowLLM(receipt=receipt_json()), q=q)
    await process(ctx, q.jobs[0][1])
    assert (await send(stack, store, q, jpeg())).status == "duplicate"  # after it was recorded
    assert len(await rows(stack, InboundEvent)) == 1 and len(await all_tx(stack)) == 1
    assert (await send(stack, store, q, jpeg(2))).status == "queued"  # a different photo is fine


async def test_a_telegram_redelivery_of_one_update_is_queued_once(stack, store):
    q = Q()
    first = await send(stack, store, q, jpeg(), message_id=77)
    again = await send(stack, store, q, jpeg(2), message_id=77)  # same chat + message id
    assert (first.status, again.status) == ("queued", "replayed")
    assert len(q.jobs) == 1 and len(await rows(stack, InboundEvent)) == 1


async def test_a_photo_that_was_rejected_can_be_sent_again(stack, store):
    llm = FlowLLM(receipt=receipt_json(total=85300))  # arithmetic gate rejects it
    eid, ctx, (read, _) = await photo_to_ledger(stack, store, llm)
    assert read == "needs_review"
    again = await send(stack, store, ctx["redis"], jpeg())
    assert again.status == "queued"  # a rejected photo is not "counted": a resend may deserve another try


@pytest.mark.parametrize(("data", "mime", "why"), [(b"", "image/jpeg", "empty"), (b"x" * 50, "text/plain", "unsupported")])
async def test_an_unusable_upload_is_rejected_before_anything_is_stored(stack, store, data, mime, why):
    out = await intake_photo(stack, Q(), store, chat_id=1, message_id=5, data=data, mime=mime, caption=None,
                             sent_at=T0)
    assert out.status == "rejected" and why in out.detail and await rows(stack, InboundEvent) == []


async def test_the_database_itself_refuses_one_image_backing_two_transactions(stack, store):
    """Intake checks for duplicates, but two photos can race past a check; the unique index is the backstop."""
    llm = FlowLLM(receipt=receipt_json())
    eid, ctx, _ = await photo_to_ledger(stack, store, llm)
    (ev,) = await rows(stack, InboundEvent)
    async with stack() as s:  # a second event that claims the very same image
        clone = InboundEvent(idempotency_key="clone", source="telegram_photo", status="parsed",
                             payload={**ev.payload, "sent_at": (T0 + timedelta(days=3)).isoformat(),
                                      "parsed": {**ev.payload["parsed"], "timestamp": (T0 + timedelta(days=3)).isoformat()}})
        s.add(clone)
        await s.commit()
        clone_id = clone.id
    with pytest.raises(IntegrityError):
        await worker.commit_event(ctx, clone_id)
    assert len(await all_tx(stack)) == 1  # nothing was double counted
    assert (await rows(stack, InboundEvent, InboundEvent.id == clone_id))[0].status == "failed"


# ---- fusion: a receipt and a bank notification for one purchase -------------------------------------------------


async def test_a_receipt_after_the_bank_notification_is_attached_not_added(stack, store):
    await seed_budget(stack)
    bank = await bank_tx(stack, at=T0)
    assert (await rows(stack, BudgetRule))[0].spent_amount == Decimal(58300)

    llm, notifier = FlowLLM(receipt=receipt_json(), debate=FakeLLM()), FakeNotifier()
    eid, ctx, (read, done) = await photo_to_ledger(stack, store, llm, notifier=notifier)
    assert (read, done) == ("parsed", "committed")

    (tx,) = await all_tx(stack)  # still ONE transaction
    assert tx.id == bank.id and tx.source_channel == "android_listener" and tx.payment_wallet == "gopay"
    assert (await rows(stack, BudgetRule))[0].spent_amount == Decimal(58300)  # not charged twice
    assert llm.calls == []  # no debate for the receipt: the purchase was already decided
    assert len(await rows(stack, TransactionItem)) == 3
    (bundle,) = await rows(stack, EvidenceBundle)
    assert (bundle.transaction_id, bundle.cross_verification_status) == (tx.id, "dual_verified")
    assert tx.evidence_bundle_id == bundle.id
    (fusion,) = await audit(stack, "fusion")
    assert fusion.verdict == "fuse:receipt->bank" and f"#{tx.id}" in fusion.justification
    assert (await rows(stack, InboundEvent, InboundEvent.id == eid))[0].payload["fused_into"] == tx.id
    (reply,) = await rows(stack, TelegramMessage, TelegramMessage.kind == "reply")
    assert "🧾 Struk dilampirkan ke KOPI KENANGAN" in reply.body and "3 item" in reply.body


async def test_a_bank_notification_after_the_receipt_completes_its_wallet(stack, store):
    await seed_budget(stack)
    notifier = FakeNotifier()
    llm = FlowLLM(receipt=receipt_json(), debate=FakeLLM())
    _, _, (_, done) = await photo_to_ledger(stack, store, llm, notifier=notifier)
    (receipt_tx,) = await all_tx(stack)
    assert (done, receipt_tx.payment_wallet) == ("committed", "unknown")
    sent_before = len(notifier.sent)

    bank_llm = FakeLLM()
    eid = await new_event(stack, parsed(amount="58300", wallet="gopay", merchant="QRIS WARTEG"), at=T0 + timedelta(minutes=25))
    assert await commit(stack, eid, bank_llm, notifier) == "committed"

    (tx,) = await all_tx(stack)  # still ONE transaction
    assert (tx.id, tx.payment_wallet) == (receipt_tx.id, "gopay") and "confirmed by bank" in tx.notes
    assert (await rows(stack, BudgetRule))[0].spent_amount == Decimal(58300)  # not charged twice
    assert bank_llm.calls == [] and len(notifier.sent) == sent_before  # no debate, no second announcement
    (fusion,) = await audit(stack, "fusion")
    assert fusion.verdict == "fuse:bank->receipt"
    assert len(await rows(stack, TransactionItem)) == 3  # the receipt's items are untouched


@pytest.mark.parametrize(
    ("label", "bank_kw"),
    [
        ("different amount", {"amount": "58400"}),
        ("outside the 12 h window", {"at": T0 + timedelta(hours=13, minutes=20)}),
    ],
)
async def test_no_match_means_a_normal_new_transaction(stack, store, label, bank_kw):
    await bank_tx(stack, **bank_kw)
    llm = FlowLLM(receipt=receipt_json(), debate=FakeLLM())
    _, _, (_, done) = await photo_to_ledger(stack, store, llm)
    assert done == "committed" and len(await all_tx(stack)) == 2, label
    assert len(llm.calls) == 5  # a full debate: it is a new purchase
    assert await audit(stack, "fusion") == []


async def test_the_window_edge_is_twelve_hours(stack, store):
    await bank_tx(stack, at=T0 + timedelta(minutes=20) + timedelta(hours=12))  # exactly 12 h after 12:20
    _, _, (_, done) = await photo_to_ledger(stack, store, FlowLLM(receipt=receipt_json(), debate=FakeLLM()))
    assert done == "committed" and len(await all_tx(stack)) == 1  # fused: the boundary is inclusive


async def test_a_cash_receipt_never_fuses_with_a_bank_notification(stack, store):
    await bank_tx(stack)  # a same-amount GoPay payment
    llm = FlowLLM(receipt=receipt_json(payment_method="cash"), debate=FakeLLM())
    _, _, (_, done) = await photo_to_ledger(stack, store, llm)
    txs = await all_tx(stack)
    assert done == "committed" and len(txs) == 2 and txs[1].payment_wallet == "cash_wallet"


async def test_a_named_wallet_must_agree_with_the_bank_transaction(stack, store):
    await bank_tx(stack, wallet="gopay")
    llm = FlowLLM(receipt=receipt_json(), debate=FakeLLM())
    # the caption says BCA but the notification was GoPay: two different purchases
    _, _, (_, done) = await photo_to_ledger(stack, store, llm, caption="makan siang pake bca")
    assert done == "committed" and len(await all_tx(stack)) == 2
    # the caption names the right wallet: now they fuse
    await bank_tx(stack, amount="64000", wallet="gopay", merchant="LAIN")
    llm2 = FlowLLM(receipt=receipt_json(items=[{"name": "Paket", "quantity": 1, "total_price": 64000}],
                                        subtotal=64000, tax=0, total=64000), debate=FakeLLM())
    _, _, (_, done2) = await photo_to_ledger(stack, store, llm2, data=jpeg(2), caption="paket pake gopay 64rb")
    assert done2 == "committed" and len(await all_tx(stack)) == 3  # 2 bank + the first receipt; the 2nd fused
    assert len(await audit(stack, "fusion")) == 1


async def test_a_receipt_already_attached_cannot_be_attached_to_the_same_transaction_twice(stack, store):
    await bank_tx(stack)
    await photo_to_ledger(stack, store, FlowLLM(receipt=receipt_json(), debate=FakeLLM()))
    # a second, different photo of the same amount: the one bank tx already has evidence, so this is new
    _, _, (_, done) = await photo_to_ledger(stack, store, FlowLLM(receipt=receipt_json(), debate=FakeLLM()),
                                            data=jpeg(2))
    assert done == "committed" and len(await all_tx(stack)) == 2


async def test_an_ambiguous_receipt_is_held_for_the_owner_not_guessed(stack, store):
    a = await bank_tx(stack, merchant="TOKO A", at=T0)
    b = await bank_tx(stack, merchant="TOKO B", at=T0 + timedelta(minutes=30))
    notifier = FakeNotifier()
    llm = FlowLLM(receipt=receipt_json(), debate=FakeLLM())
    eid, ctx, (read, done) = await photo_to_ledger(stack, store, llm, notifier=notifier)
    assert (read, done) == ("parsed", "quarantined")

    txs = await all_tx(stack)
    held = txs[-1]
    assert len(txs) == 3 and held.status == "quarantined" and held.notes == "quarantined: ambiguous_fusion"
    assert {a.evidence_bundle_id, b.evidence_bundle_id} == {None}  # neither bank tx was touched
    assert llm.calls == []
    alert = (await rows(stack, TelegramMessage, TelegramMessage.kind == "alert"))[0]
    assert "lebih dari satu catatan" in alert.body and f"/release_{alert.undo_token}" in alert.body

    # the owner decides: release commits it as its own purchase WITH its items and evidence
    assert await run_release(stack, FakeLLM(), notifier, held.id, tz=TZ) == "committed"
    assert len(await rows(stack, TransactionItem)) == 3
    (bundle,) = await rows(stack, EvidenceBundle)
    assert (bundle.transaction_id, bundle.cross_verification_status) == (held.id, "single_verified")


async def test_an_ambiguous_bank_notification_is_held_and_dismiss_discards_it(stack, store):
    for n in (1, 2):  # two receipt transactions with the same total and unknown wallets
        await photo_to_ledger(stack, store, FlowLLM(receipt=receipt_json(), debate=FakeLLM()), data=jpeg(n))
    eid = await new_event(stack, parsed(amount="58300", wallet="gopay", merchant="QRIS"), at=T0 + timedelta(minutes=25))
    assert await commit(stack, eid) == "quarantined"
    held = (await all_tx(stack))[-1]
    assert held.notes == "quarantined: ambiguous_fusion" and held.source_channel == "android_listener"
    alert = (await rows(stack, TelegramMessage, TelegramMessage.kind == "alert"))[0]
    assert (await handle_command(stack, Q(), "dismiss", alert.undo_token)).startswith("🗑️ Dibuang")
    assert (await rows(stack, Transaction, Transaction.id == held.id))[0].status == "rolled_back"


# ---- when the photo cannot be turned into a transaction -------------------------------------------------------


@pytest.mark.parametrize(
    ("reply", "why"),
    [
        (receipt_json(total=85300), "total tidak cocok dengan rincian barang"),
        (receipt_json(confidence=0.5), "foto kurang jelas"),
        (receipt_json(items=[], subtotal=None), "rincian barang tidak terbaca"),
        ({"is_receipt": False, "confidence": 0.9}, "itu tidak terbaca sebagai struk"),
        ({"junk": True}, "tidak bisa dipahami"),
    ],
)
async def test_an_unreadable_receipt_gets_one_reply_and_records_nothing(stack, store, reply, why):
    notifier = FakeNotifier()
    eid, ctx, (read, done) = await photo_to_ledger(stack, store, FlowLLM(receipt=reply), notifier=notifier)
    assert read in ("needs_review", "skipped") and done is None
    assert await all_tx(stack) == [] and await rows(stack, EvidenceFile) == [] and await rows(stack, TransactionItem) == []
    (r,) = await rows(stack, TelegramMessage, TelegramMessage.kind == "reply")
    assert why in r.body and notifier.sent == [r.body]
    assert ctx["redis"].jobs == [("read_receipt", eid)]  # commit_event was never queued


async def test_a_caption_that_contradicts_the_receipt_is_not_recorded(stack, store):
    _, _, (read, _) = await photo_to_ledger(stack, store, FlowLLM(receipt=receipt_json()), caption="makan 35rb")
    assert read == "needs_review" and await all_tx(stack) == []
    (r,) = await rows(stack, TelegramMessage, TelegramMessage.kind == "reply")
    assert "nominal di keterangan berbeda dengan struk" in r.body


async def test_a_vision_outage_retries_then_parks_the_photo_and_says_so(stack, store):
    notifier = FakeNotifier()
    down = FlowLLM(receipt=TimeoutError("45s"))
    q = Q()
    intake = await send(stack, store, q, jpeg())
    for attempt in (1, 2):
        with pytest.raises(Retry) as exc:
            await worker.read_receipt(ctx_for(stack, store, down, notifier, q, job_try=attempt), intake.event_id)
        assert exc.value.defer_score == worker.EXTRACT_RETRY_DELAY_S * 1000
        assert (await rows(stack, InboundEvent))[0].status == "reading"
    assert notifier.sent == []
    last = ctx_for(stack, store, down, notifier, q, job_try=3)
    assert await worker.read_receipt(last, intake.event_id) == "needs_review"
    assert "layanan AI sedang tidak tersedia" in notifier.sent[0] and await all_tx(stack) == []


async def test_a_missing_image_is_reported_not_crashed_on(stack, store, tmp_path):
    q = Q()
    intake = await send(stack, store, q, jpeg())
    for f in (tmp_path / "evidence").rglob("*.jpg"):
        f.unlink()
    notifier = FakeNotifier()
    assert await worker.read_receipt(ctx_for(stack, store, FlowLLM(receipt=receipt_json()), notifier, q),
                                     intake.event_id) == "needs_review"
    assert "fotonya tidak bisa dibuka lagi" in notifier.sent[0]


async def test_two_workers_reading_one_photo_make_one_vision_call(stack, store):
    q = Q()
    intake = await send(stack, store, q, jpeg())
    llm = FlowLLM(receipt=receipt_json(), delay=0.3)  # slow enough that worker 2 arrives mid-call
    ctx = ctx_for(stack, store, llm, q=q)
    results = await asyncio.gather(worker.read_receipt(ctx, intake.event_id), worker.read_receipt(ctx, intake.event_id))
    assert sorted(results) == ["parsed", "reading"] and llm.vision_calls == [1]
    assert [j for j in q.jobs if j[0] == "commit_event"] == [("commit_event", intake.event_id)]


async def test_a_redelivered_read_job_does_not_call_the_model_again(stack, store):
    q = Q()
    intake = await send(stack, store, q, jpeg())
    llm = FlowLLM(receipt=receipt_json())
    ctx = ctx_for(stack, store, llm, q=q)
    assert [await worker.read_receipt(ctx, intake.event_id) for _ in range(3)] == ["parsed"] * 3
    assert llm.vision_calls == [1]


# ---- the receipt is an ordinary transaction afterwards ---------------------------------------------------------


async def test_undoing_a_receipt_transaction_reverses_the_budget_and_keeps_the_evidence(stack, store):
    await seed_budget(stack)
    await photo_to_ledger(stack, store, FlowLLM(receipt=receipt_json(), debate=FakeLLM()))
    (tx,) = await all_tx(stack)
    assert (await undo_transaction(stack, tx.id, tz=TZ)).status == "ok"
    assert (await rows(stack, BudgetRule))[0].spent_amount == 0
    assert len(await rows(stack, TransactionItem)) == 3 and len(await rows(stack, EvidenceFile)) == 1  # kept


async def test_a_held_receipt_gets_its_items_and_evidence_when_released(stack, store):
    big = receipt_json(items=[{"name": "Laptop", "quantity": 1, "unit_price": 3_000_000, "total_price": 3_000_000}],
                       subtotal=3_000_000, tax=0, total=3_000_000)
    _, _, (read, done) = await photo_to_ledger(stack, store, FlowLLM(receipt=big, debate=FakeLLM()))
    assert (read, done) == ("parsed", "quarantined")  # over the single-transaction ceiling
    held = (await all_tx(stack))[0]
    assert await rows(stack, TransactionItem) == []  # nothing is applied while it is held
    assert await run_release(stack, FakeLLM(), FakeNotifier(), held.id, tz=TZ) == "committed"
    assert [i.item_name for i in await rows(stack, TransactionItem)] == ["Laptop"]
    assert (await rows(stack, EvidenceBundle))[0].transaction_id == held.id


# ---- the Telegram handler ----------------------------------------------------------------------------------------


class FakeBot:
    def __init__(self, data=None):
        self.data, self.downloads = (jpeg() if data is None else data), 0

    async def download(self, photo, destination):
        self.downloads += 1
        destination.write(self.data)


def photo_message(*, message_id=None, group=None, size=None, caption=None):
    return SimpleNamespace(
        photo=[SimpleNamespace(file_size=1000), SimpleNamespace(file_size=size or 90_000)],
        media_group_id=group, chat=SimpleNamespace(id=1), message_id=message_id or next(_ids),
        caption=caption, date=T0)


async def test_the_bot_handler_queues_silently_and_refuses_duplicates_and_giants(stack, store):
    q, bot = Q(), FakeBot()
    assert await handle_photo(stack, q, store, bot, photo_message()) is None  # queued: silence is the success
    assert q.jobs[0][0] == "read_receipt" and bot.downloads == 1
    assert await handle_photo(stack, q, store, bot, photo_message()) == DUPLICATE_NOTICE  # same bytes again
    assert bot.downloads == 2  # a duplicate is only recognisable after the bytes are fetched

    big = await handle_photo(stack, q, store, bot, photo_message(size=50 * 1024 * 1024))
    assert "terlalu besar" in big and bot.downloads == 2  # refused from the size Telegram reports, unfetched
    empty = await handle_photo(stack, q, store, FakeBot(data=b""), photo_message())
    assert "Foto tidak bisa diproses" in empty and len(q.jobs) == 1
