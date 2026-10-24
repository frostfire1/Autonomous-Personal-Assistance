"""Multi-image receipts against real Postgres: albums, page stitching, payment-slip verification."""

import asyncio
import hashlib
import itertools
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from arq import Retry
from sqlalchemy.exc import IntegrityError

from apa.bot import photo as bot_photo
from apa.bot.photo import DUPLICATE_NOTICE, LATE_NOTICE, handle_photo, too_many_notice
from apa.core.settings import get_settings
from apa.db.models.budget import BudgetRule
from apa.db.models.evidence import EvidenceBundle, EvidenceFile
from apa.db.models.ledger import Transaction, TransactionItem
from apa.db.models.ops import InboundEvent, TelegramMessage
from apa.ingestion.photo_intake import intake_photo
from apa.integrations.evidence_store import LocalEvidenceStore
from apa.pipeline.receipt import AlbumStillArriving, run_read_receipt
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
from tests.integration.test_receipts import FakeBot, Q, jpeg, photo_message
from tests.test_receipt_assemble import PAGE1, PAGE2, it
from tests.test_receipt_multi import page, reply, slip

pytestmark = pytest.mark.skipif(not HAVE_INFRA, reason="TEST_DATABASE_URL/TEST_REDIS_URL not set")

_msg = itertools.count(5000)
GROUP = "album-1"
FULL = [page(0, PAGE1), page(1, PAGE2, discount=6000, total=180000), slip()]  # 2 pages + a matching EDC slip


@pytest.fixture
def store(tmp_path):
    return LocalEvidenceStore(tmp_path / "evidence")


@pytest.fixture(autouse=True)
def _fresh():
    bot_photo._notified.clear()
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def ctx_for(stack, store, llm, notifier=None, q=None, job_try=1, settle=0):
    return {"sessionmaker": stack, "llm": llm, "store": store, "notifier": notifier or FakeNotifier(),
            "redis": q or Q(), "job_try": job_try, "album_settle_seconds": settle}


async def send_album(stack, store, q, photos, *, caption_on=None, group=GROUP, sequential=False):
    """`photos` is [(message_id, bytes)]. By default they are delivered concurrently, like Telegram does."""
    def one(mid, data):
        return intake_photo(stack, q, store, chat_id=1, message_id=mid, data=data, mime="image/jpeg",
                            caption="belanja bulanan" if mid == caption_on else None, sent_at=T0,
                            media_group_id=group)
    if sequential:
        return [await one(m, d) for m, d in photos]
    return await asyncio.gather(*(one(m, d) for m, d in photos))


def album_photos(n=3):
    base = next(_msg)
    return [(base + i, jpeg(100 + base + i)) for i in range(n)]


async def album_to_ledger(stack, store, llm, *, n=3, notifier=None, caption_on=None):
    q = Q()
    ctx = ctx_for(stack, store, llm, notifier, q)
    photos = album_photos(n)
    out = await send_album(stack, store, q, photos, caption_on=caption_on)
    assert {o.status for o in out} == {"queued"}
    eid = out[0].event_id
    read = await worker.read_receipt(ctx, eid)
    return eid, ctx, photos, read, (await worker.commit_event(ctx, eid) if read == "parsed" else None)


async def bank_tx(stack, *, amount="180000", wallet="gopay", merchant="WARUNG SEDERHANA", at=T0):
    eid = await new_event(stack, parsed(amount=amount, wallet=wallet, merchant=merchant), at=at)
    assert await commit(stack, eid) == "committed"
    return await tx_for(stack, eid)


async def all_tx(stack):
    return sorted(await rows(stack, Transaction), key=lambda t: t.id)


# ---- collecting an album ---------------------------------------------------------------------------------------


async def test_photos_that_arrive_concurrently_and_out_of_order_make_one_ordered_event_and_one_job(stack, store):
    q = Q()
    base = next(_msg)
    sent = [(base + 2, jpeg(3)), (base, jpeg(1)), (base + 1, jpeg(2))]  # arrival order is not send order
    out = await send_album(stack, store, q, sent, caption_on=base + 1)
    assert {o.status for o in out} == {"queued"}
    (ev,) = await rows(stack, InboundEvent)
    assert [a["message_id"] for a in ev.payload["attachments"]] == [base, base + 1, base + 2]  # message-id order
    assert ev.payload["caption"] == "belanja bulanan" and ev.payload["album"] is True and ev.status == "received"
    assert [j[0] for j in q.jobs] == ["read_receipt"]  # exactly one job, however many photos raced to be first
    assert q.kwargs[0]["_job_id"] == f"read_receipt:{ev.id}" and q.kwargs[0]["_defer_by"] == 3  # waits for stragglers


async def test_pages_are_ordered_by_message_id_even_when_they_arrive_in_reverse(stack, store):
    """Sequential and reversed, so arrival order is certain: the stored order must still be the send order."""
    q = Q()
    base = next(_msg)
    out = await send_album(stack, store, q, [(base + 2, jpeg(3)), (base + 1, jpeg(2)), (base, jpeg(1))],
                           sequential=True)
    assert {o.status for o in out} == {"queued"}
    (ev,) = await rows(stack, InboundEvent)
    assert [a["message_id"] for a in ev.payload["attachments"]] == [base, base + 1, base + 2]

    # and the vision model is shown the pages in that order, which is what stitching depends on
    llm = FlowLLM(receipt=reply(FULL))
    await worker.read_receipt(ctx_for(stack, store, llm, q=q), ev.id)
    assert llm.vision_images[0] == [(jpeg(1), "image/jpeg"), (jpeg(2), "image/jpeg"), (jpeg(3), "image/jpeg")]


async def test_the_same_photo_delivered_twice_in_an_album_is_added_once(stack, store):
    q = Q()
    base = next(_msg)
    out = await send_album(stack, store, q, [(base, jpeg(1)), (base + 1, jpeg(1))], sequential=True)
    assert [o.status for o in out] == ["queued", "replayed"]
    (ev,) = await rows(stack, InboundEvent)
    assert len(ev.payload["attachments"]) == 1


async def test_a_fifth_photo_is_refused_and_a_late_one_is_told_to_resend(stack, store):
    q = Q()
    photos = album_photos(5)
    out = await send_album(stack, store, q, photos, sequential=True)
    assert [o.status for o in out] == ["queued"] * 4 + ["too_many"] and out[4].detail == "4"
    (ev,) = await rows(stack, InboundEvent)
    assert len(ev.payload["attachments"]) == 4

    ctx = ctx_for(stack, store, FlowLLM(receipt=reply(FULL[:2] + [{"index": 2, "kind": "other"},
                                                                   {"index": 3, "kind": "other"}])), q=q)
    await worker.read_receipt(ctx, ev.id)  # the reading has started...
    late = await send_album(stack, store, q, [(photos[0][0] + 9, jpeg(999))], sequential=True)
    assert late[0].status == "late"  # ...so a straggler missed the album
    assert len((await rows(stack, InboundEvent))[0].payload["attachments"]) == 4


async def test_a_photo_that_already_backs_a_transaction_is_refused_inside_an_album(stack, store):
    llm = FlowLLM(receipt=reply([page(0, [it("Nasi", 1, 58300)], total=58300), {"index": 1, "kind": "other"},
                                 {"index": 2, "kind": "other"}]), debate=FakeLLM())
    eid, ctx, photos, read, done = await album_to_ledger(stack, store, llm)
    assert (read, done) == ("parsed", "committed")
    q = Q()
    out = await send_album(stack, store, q, [(next(_msg), photos[0][1])], group="album-2")  # the first page again
    assert out[0].status == "duplicate" and q.jobs == []


# ---- waiting for the album to finish arriving -------------------------------------------------------------------------


async def test_the_reading_waits_while_photos_are_still_arriving_and_late_ones_can_still_join(stack, store):
    q = Q()
    photos = album_photos(3)
    await send_album(stack, store, q, photos[:2], sequential=True)
    eid = q.jobs[0][1]
    ctx = ctx_for(stack, store, FlowLLM(receipt=reply(FULL)), q=q, settle=30)  # needs 30 s of quiet
    with pytest.raises(AlbumStillArriving) as exc:
        await run_read_receipt(stack, ctx["llm"], ctx["notifier"], store, q, eid, settle_seconds=30)
    assert 0 < exc.value.wait <= 30
    assert (await rows(stack, InboundEvent))[0].status == "received"  # nothing was claimed
    assert ctx["llm"].vision_calls == []  # and no tokens were spent on a partial album

    # the third photo arrives during the wait and joins
    assert (await send_album(stack, store, q, photos[2:], sequential=True))[0].status == "queued"
    assert len((await rows(stack, InboundEvent))[0].payload["attachments"]) == 3
    assert [j for j in q.jobs if j[0] == "read_receipt"] == [("read_receipt", eid)]  # still one job

    # on the last try it reads what it has rather than waiting forever
    last = await run_read_receipt(stack, ctx["llm"], ctx["notifier"], store, q, eid, job_try=3, max_tries=3,
                                  settle_seconds=30)
    assert last == "parsed" and ctx["llm"].vision_calls == [3]


async def test_the_worker_turns_still_arriving_into_a_retry(stack, store):
    q = Q()
    await send_album(stack, store, q, album_photos(2), sequential=True)
    ctx = ctx_for(stack, store, FlowLLM(receipt=reply(FULL[:2])), q=q, settle=30)
    with pytest.raises(Retry) as exc:
        await worker.read_receipt(ctx, q.jobs[0][1])
    assert 1000 <= exc.value.defer_score <= 30_000  # arq stores milliseconds


async def test_an_album_that_has_been_quiet_long_enough_is_read_at_once(stack, store):
    q = Q()
    await send_album(stack, store, q, album_photos(2), sequential=True)
    ev = (await rows(stack, InboundEvent))[0]
    async with stack() as s:
        row = await s.get(InboundEvent, ev.id)
        row.payload = {**row.payload, "last_append_at": (datetime.now(UTC) - timedelta(seconds=60)).isoformat()}
        await s.commit()
    llm = FlowLLM(receipt=reply([page(0, [it("Nasi", 1, 58300)], total=58300), slip(1, 58300)]))
    assert await run_read_receipt(stack, llm, FakeNotifier(), store, q, ev.id, settle_seconds=3) == "parsed"


# ---- the reading, end to end ---------------------------------------------------------------------------------------------


async def test_a_long_receipt_and_its_slip_become_one_transaction_with_every_image_linked(stack, store):
    llm, notifier = FlowLLM(receipt=reply(FULL), debate=FakeLLM()), FakeNotifier()
    eid, ctx, photos, read, done = await album_to_ledger(stack, store, llm, notifier=notifier)
    assert (read, done) == ("parsed", "committed")

    (tx,) = await all_tx(stack)
    assert (tx.total_paid_amount, tx.payment_wallet, tx.source_channel) == (Decimal(180000), "unknown", "multi_image_receipt")
    assert tx.timestamp == datetime(2026, 10, 23, 12, 41, tzinfo=TZ)  # the slip's authorisation time

    files = sorted(await rows(stack, EvidenceFile), key=lambda f: f.sequence_index)
    assert [(f.sequence_index, f.file_role) for f in files] == [
        (0, "receipt_primary"), (1, "receipt_continuation"), (2, "payment_proof_slip")]
    assert [f.sha256_checksum for f in files] == [hashlib.sha256(d).hexdigest() for _, d in photos]
    assert files[2].ocr_payload["card_last4"] == "4321" and files[0].ocr_payload["pages"] == 2
    (bundle,) = await rows(stack, EvidenceBundle)
    assert (bundle.cross_verification_status, bundle.total_attachments) == ("dual_verified", 3)

    items = sorted(await rows(stack, TransactionItem), key=lambda i: i.id)
    assert [i.item_name for i in items][-2:] == ["Sabun", "Teh Celup"] and len(items) == 7  # overlap removed
    by_file = {f.id: f.sequence_index for f in files}
    assert [by_file[i.evidence_file_ref] for i in items] == [0, 0, 0, 0, 0, 1, 1]  # each line links to its page
    assert sum(i.total_price for i in items) - 6000 == tx.total_paid_amount  # 186.000 - 6.000 discount

    assert llm.vision_calls == [3] and len(llm.calls) == 5  # ONE vision call with all three, then the debate
    assert llm.vision_images[0] == [(d, "image/jpeg") for _, d in photos]  # sent in message-id order
    assert "Gula 1kg x1" in llm.calls[0][1]
    assert "Kasir: Belum diketahui" in notifier.sent[0]


async def test_a_tip_that_only_the_slip_shows_is_recorded_as_its_own_line_linked_to_the_slip(stack, store):
    steak = [page(0, [it("Steak", 1, 90000), it("Es Teh", 2, 10000)], total=100000), slip(1, 110000)]
    llm = FlowLLM(receipt=reply(steak), debate=FakeLLM())
    _, _, _, read, done = await album_to_ledger(stack, store, llm, n=2)
    assert (read, done) == ("parsed", "committed")
    (tx,) = await all_tx(stack)
    assert tx.total_paid_amount == Decimal(110000)  # what was actually charged
    items = sorted(await rows(stack, TransactionItem), key=lambda i: i.id)
    assert [i.item_name[:3] for i in items] == ["Ste", "Es ", "Tip"] and items[-1].total_price == Decimal(10000)
    files = {f.id: f for f in await rows(stack, EvidenceFile)}
    assert files[items[-1].evidence_file_ref].file_role == "payment_proof_slip"  # the tip came from the slip
    assert sum(i.total_price for i in items) == tx.total_paid_amount
    assert (await rows(stack, EvidenceBundle))[0].cross_verification_status == "split_reconciled"


async def test_stitched_pages_without_a_slip_are_marked_stitched(stack, store):
    pages = [page(0, PAGE1), page(1, PAGE2, discount=6000, total=180000)]
    _, _, _, read, done = await album_to_ledger(stack, store, FlowLLM(receipt=reply(pages), debate=FakeLLM()), n=2)
    assert (read, done) == ("parsed", "committed")
    assert (await rows(stack, EvidenceBundle))[0].cross_verification_status == "stitched_multi_page"
    assert len(await rows(stack, TransactionItem)) == 7


async def test_a_stray_photo_in_the_album_is_not_filed_as_evidence(stack, store):
    llm = FlowLLM(receipt=reply([page(0, [it("Nasi", 1, 58300)], total=58300), {"index": 1, "kind": "other"}]),
                  debate=FakeLLM())
    _, _, _, _, done = await album_to_ledger(stack, store, llm, n=2)
    assert done == "committed"
    assert [f.file_role for f in await rows(stack, EvidenceFile)] == ["receipt_primary"]
    assert (await rows(stack, EvidenceBundle))[0].total_attachments == 1


async def test_the_caption_wallet_applies_to_an_album(stack, store):
    llm = FlowLLM(receipt=reply(FULL), debate=FakeLLM())
    _, _, _, _, done = await album_to_ledger(stack, store, llm, caption_on=None)
    assert done == "committed" and (await all_tx(stack))[0].payment_wallet == "unknown"


# ---- fusion with a bank notification ----------------------------------------------------------------------------------------


async def test_an_album_fuses_into_the_bank_transaction_and_keeps_the_slip_verification(stack, store):
    async with stack() as s:
        s.add(BudgetRule(category="Food & Dining", period_month=T0.date().replace(day=1),
                         base_limit=Decimal(2_000_000), current_limit=Decimal(2_000_000)))
        await s.commit()
    bank = await bank_tx(stack, at=T0)
    llm = FlowLLM(receipt=reply(FULL), debate=FakeLLM())
    _, _, _, read, done = await album_to_ledger(stack, store, llm)
    assert (read, done) == ("parsed", "committed")

    (tx,) = await all_tx(stack)  # still one transaction
    assert tx.id == bank.id and tx.payment_wallet == "gopay" and llm.calls == []
    assert (await rows(stack, BudgetRule))[0].spent_amount == Decimal(180000)  # not charged twice
    assert len(await rows(stack, EvidenceFile)) == 3 and len(await rows(stack, TransactionItem)) == 7
    (bundle,) = await rows(stack, EvidenceBundle)
    assert (bundle.transaction_id, bundle.cross_verification_status) == (tx.id, "dual_verified")
    (reply_row,) = await rows(stack, TelegramMessage, TelegramMessage.kind == "reply")
    assert "7 item" in reply_row.body


async def test_stitched_pages_fused_with_a_bank_transaction_are_upgraded_to_dual_verified(stack, store):
    await bank_tx(stack, at=T0)
    pages = [page(0, PAGE1), page(1, PAGE2, discount=6000, total=180000)]
    _, _, _, _, done = await album_to_ledger(stack, store, FlowLLM(receipt=reply(pages), debate=FakeLLM()), n=2)
    assert done == "committed" and len(await all_tx(stack)) == 1
    assert (await rows(stack, EvidenceBundle))[0].cross_verification_status == "dual_verified"


async def test_the_slip_time_is_what_places_the_album_inside_the_fusion_window(stack, store):
    await bank_tx(stack, at=T0 + timedelta(hours=20))  # a same-amount payment the next morning
    llm = FlowLLM(receipt=reply(FULL), debate=FakeLLM())  # the slip says 12:41 the day before
    _, _, _, _, done = await album_to_ledger(stack, store, llm)
    assert done == "committed" and len(await all_tx(stack)) == 2  # outside 12 h: a separate purchase


# ---- refusals ---------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("images", "n", "why"),
    [
        ([page(0, PAGE1), page(1, PAGE2, total=123456), slip()], 3, "total tidak cocok dengan rincian barang"),
        ([page(0, PAGE1), page(1, PAGE2, discount=6000, total=180000), slip(total=300000)], 3,
         "nominal bukti pembayaran tidak cocok dengan struk"),
        ([page(0, PAGE1), page(1, PAGE2, discount=6000, total=180000), slip(total=None)], 3,
         "nominal di bukti pembayaran tidak terbaca"),
        ([page(0, PAGE1), page(1, PAGE2)], 2, "total akhir tidak terlihat di foto mana pun"),
        ([slip(0, 58300), {"index": 1, "kind": "other"}], 2, "tidak ada foto struk, hanya bukti pembayaran"),
    ],
)
async def test_an_album_that_cannot_be_trusted_records_nothing_and_says_why(stack, store, images, n, why):
    notifier = FakeNotifier()
    eid, ctx, photos, read, done = await album_to_ledger(stack, store, FlowLLM(receipt=reply(images)), n=n,
                                                         notifier=notifier)
    assert read == "needs_review" and done is None
    assert await all_tx(stack) == [] and await rows(stack, EvidenceFile) == [] and await rows(stack, TransactionItem) == []
    (r,) = await rows(stack, TelegramMessage, TelegramMessage.kind == "reply")
    assert why in r.body and notifier.sent == [r.body]
    # nothing was counted, so the same album can be sent again
    again = await send_album(stack, store, Q(), photos, group="album-retry", sequential=True)
    assert {o.status for o in again} == {"queued"}


@pytest.mark.parametrize(("shared", "role"), [(0, "first page"), (1, "continuation page"), (2, "payment slip")])
async def test_the_database_refuses_any_single_image_backing_two_transactions(stack, store, shared, role):
    """The clone event reuses exactly ONE of the three images and brings fresh ones for the rest, so only
    that image's uniqueness can stop it: a reused slip would 'verify' a second purchase."""
    llm = FlowLLM(receipt=reply(FULL), debate=FakeLLM())
    eid, ctx, photos, _, _ = await album_to_ledger(stack, store, llm)
    (ev,) = await rows(stack, InboundEvent)
    later = (T0 + timedelta(days=3)).isoformat()
    fresh = [{**a, "sha256": f"{i:02x}" * 32, "uri": f"local:zz/{i}.jpg"} if i != shared else a
             for i, a in enumerate(ev.payload["attachments"])]
    async with stack() as s:
        clone = InboundEvent(idempotency_key="clone", source="telegram_photo", status="parsed",
                             payload={**ev.payload, "sent_at": later, "attachments": fresh,
                                      "parsed": {**ev.payload["parsed"], "timestamp": later}})
        s.add(clone)
        await s.commit()
        clone_id = clone.id
    with pytest.raises(IntegrityError):
        await worker.commit_event(ctx, clone_id)
    assert len(await all_tx(stack)) == 1, f"the {role} was reused and a second transaction got through"


# ---- the Telegram handler ------------------------------------------------------------------------------------------------------------


async def test_the_bot_collects_an_album_silently_and_reports_a_duplicate_once_per_album(stack, store):
    q = Q()
    base = next(_msg)
    msgs = [photo_message(message_id=base + i, group="g-a") for i in range(3)]
    bots = [FakeBot(data=jpeg(500 + base + i)) for i in range(3)]
    replies = await asyncio.gather(*(handle_photo(stack, q, store, b, m) for b, m in zip(bots, msgs, strict=True)))
    assert replies == [None, None, None]  # an album is collected without a word
    assert [j[0] for j in q.jobs] == ["read_receipt"] and len((await rows(stack, InboundEvent))[0].payload["attachments"]) == 3

    # a second album whose photos were all sent before: one notice, not three
    dup = [photo_message(message_id=next(_msg), group="g-b") for _ in range(3)]
    out = [await handle_photo(stack, q, store, FakeBot(data=jpeg(500 + base + i)), m) for i, m in enumerate(dup)]
    assert out == [DUPLICATE_NOTICE, None, None]


async def test_the_bot_tells_you_once_when_an_album_has_too_many_photos_or_a_late_one(stack, store):
    q = Q()
    base = next(_msg)
    out = [await handle_photo(stack, q, store, FakeBot(data=jpeg(700 + i)), photo_message(message_id=base + i, group="g-c"))
           for i in range(7)]
    assert out[:4] == [None] * 4 and out[4] == too_many_notice(4) and out[5:] == [None, None]  # said once
    ctx = ctx_for(stack, store, FlowLLM(receipt=reply(FULL[:2] + [{"index": 2, "kind": "other"}, {"index": 3, "kind": "other"}])), q=q)
    await worker.read_receipt(ctx, q.jobs[0][1])
    late = await handle_photo(stack, q, store, FakeBot(data=jpeg(799)), photo_message(message_id=base + 20, group="g-c"))
    assert late == LATE_NOTICE
