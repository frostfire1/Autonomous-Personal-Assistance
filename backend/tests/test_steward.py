from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from apa.db.models.ops import InboundEvent
from apa.steward import finder, fixers, runner
from apa.steward.finder import Ticket


class MockSession:
    def __init__(self, event=None, events=None):
        self.event = event
        self.events = events if events is not None else ([event] if event else [])
        self.committed = False
        self.added = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass

    async def execute(self, stmt, *args, **kwargs):
        res = MagicMock()
        res.scalar_one_or_none.return_value = self.event
        res.scalar_one.return_value = 1
        scalars = MagicMock()
        scalars.all.return_value = self.events
        res.scalars.return_value = scalars
        return res

    async def commit(self):
        self.committed = True

    async def get(self, model, ident):
        return self.event


def _make_sm(session):
    def sm():
        return session
    return sm


@pytest.mark.asyncio
async def test_fix_stuck_unhandled_source_marks_failed_and_reported():
    """When a stuck event has an unhandled source, it must be marked failed & reported so it stops retrying forever."""
    ev = InboundEvent(
        id=264,
        source="unknown_device",
        status="received",
        payload={"text": "hello"},
        received_at=datetime.now(UTC) - timedelta(minutes=20),
    )
    session = MockSession(event=ev)
    sm = _make_sm(session)

    t = Ticket("stuck", event_id=264, status="received")
    redis = AsyncMock()

    # First run: returns unfixable and marks in DB
    fix = await fixers.fix_stuck(sm, redis, t)
    assert fix.ok is False
    assert fix.unfixable is True
    assert "pesan #264 macet" in fix.line
    assert session.committed is True

    # Verify event state in DB: must be failed, with gave_up=True, reported=True
    assert ev.status == "failed"
    st = ev.payload.get("steward", {})
    assert st.get("gave_up") is True
    assert st.get("reported") is True

    # Second run: event is marked reported and gave_up -> returns silent Fix("", True)
    fix2 = await fixers.fix_stuck(sm, redis, t)
    assert fix2.line == ""


@pytest.mark.asyncio
async def test_fix_stuck_telegram_file_reenqueued():
    """telegram_file in received status is re-enqueued to file_event."""
    ev = InboundEvent(
        id=265,
        source="telegram_file",
        status="received",
        payload={"name": "test.pdf"},
        received_at=datetime.now(UTC) - timedelta(minutes=20),
    )
    session = MockSession(event=ev)
    sm = _make_sm(session)

    t = Ticket("stuck", event_id=265, status="received")
    redis = AsyncMock()

    fix = await fixers.fix_stuck(sm, redis, t)
    assert fix.ok is True
    assert fix.unfixable is False
    assert "pesan #265 yang macet diproses ulang" in fix.line
    assert session.committed is True
    redis.enqueue_job.assert_called_once_with("file_event", 265)


@pytest.mark.asyncio
async def test_fix_image_no_receipt_page_marks_reported():
    """no_receipt_page is non-retryable and must be marked gave_up=True & reported=True so it alerts at most once."""
    ev = InboundEvent(
        id=261,
        source="telegram_photo",
        status="needs_review",
        payload={"skip_reason": "no_receipt_page"},
        received_at=datetime.now(UTC) - timedelta(hours=1),
    )
    session = MockSession(event=ev)
    sm = _make_sm(session)

    t = Ticket("image", event_id=261, reason="no_receipt_page", gave_up=False, reported=False)
    redis = AsyncMock()

    # First run: notifies owner once
    fix = await fixers.fix_image(sm, None, None, redis, t, ZoneInfo("Asia/Jakarta"))
    assert fix is not None
    assert fix.unfixable is True
    assert "foto #261: no_receipt_page" in fix.line
    assert session.committed is True

    # Verify event updated in DB
    st = ev.payload.get("steward", {})
    assert st.get("gave_up") is True
    assert st.get("reported") is True

    # Next ticket: gave_up=True, reported=True -> returns None
    t2 = Ticket("image", event_id=261, reason="no_receipt_page", gave_up=True, reported=True)
    fix2 = await fixers.fix_image(sm, None, None, redis, t2, ZoneInfo("Asia/Jakarta"))
    assert fix2 is None


@pytest.mark.asyncio
async def test_finder_skips_reported_and_gave_up_tickets():
    """find() must not include events that have already been reported and gave_up."""
    now = datetime.now(UTC)
    # Event 1: unhandled photo, already reported and gave up
    ev1 = InboundEvent(
        id=261,
        source="telegram_photo",
        status="needs_review",
        payload={"review_reason": "no_receipt_page", "steward": {"gave_up": True, "reported": True}},
        received_at=now - timedelta(hours=2),
    )
    # Event 2: fresh failed photo, not yet reported
    ev2 = InboundEvent(
        id=262,
        source="telegram_photo",
        status="needs_review",
        payload={"review_reason": "low_confidence"},
        received_at=now - timedelta(hours=1),
    )
    session = MockSession(events=[ev1, ev2])
    sm = _make_sm(session)

    tickets = await finder.find(sm, now)
    image_event_ids = [t.event_id for t in tickets if t.kind == "image"]
    assert 261 not in image_event_ids
    assert 262 in image_event_ids


@pytest.mark.asyncio
async def test_runner_suppresses_duplicate_needs_you():
    """runner must suppress identical repeated needs_you warning messages in Redis."""
    redis = AsyncMock()
    redis.set.return_value = True
    redis.get.return_value = "cached_hash"  # simulates matching hash

    report = runner.Report(needs_you=["pesan #264 macet di status received"])
    assert report.text() is not None

    with (
        patch("apa.steward.finder.find", new_callable=AsyncMock, return_value=[]),
        patch("apa.pipeline.commit.record_reply", new_callable=AsyncMock) as mock_reply,
        patch("hashlib.sha256") as mock_sha,
    ):
        mock_sha.return_value.hexdigest.return_value = "cached_hash"
        mock_notifier = MagicMock()
        mock_sm = _make_sm(MockSession())

        with patch("apa.steward.fixers.fix_stuck", new_callable=AsyncMock, return_value=fixers.Fix("stuck", False, "pesan #264 macet di status received", unfixable=True)):
            with patch("apa.steward.finder.find", new_callable=AsyncMock, return_value=[Ticket("stuck", event_id=264, status="received")]):
                await runner.run_steward(mock_sm, None, None, mock_notifier, redis, tz=ZoneInfo("Asia/Jakarta"))
                mock_reply.assert_not_called()
