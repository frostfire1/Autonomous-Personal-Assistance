from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from apa.chat import master
from apa.chat.agent import plan_action
from apa.pipeline import chat


@pytest.mark.asyncio
async def test_plan_action_blocks_add_transaction_without_digits():
    """If an LLM hallucinates an add_transaction tool call when the user message had no digits,

    plan_action must block it and return a clarification instead of proposing a ghost transaction.
    """
    mock_llm = MagicMock()
    # LLM hallucinates add_transaction with 5,000,000 when user simply typed 'Yang'
    mock_llm.complete = AsyncMock(
        return_value=type(
            "Res",
            (),
            {
                "content": '{"tool": "add_transaction", "args": {"amount": 5000000, "merchant": "Yang", "direction": "out"}}',
                "prompt_tokens": 100,
                "completion_tokens": 20,
            },
        )()
    )

    tz = ZoneInfo("Asia/Jakarta")
    reply, tokens = await plan_action(
        sm=MagicMock(),
        llm=mock_llm,
        text="Yang",
        history=[],
        tz=tz,
        redis=None,
        facts="Recent transactions: #34 in 5000000",
    )

    # Must be blocked and NOT contain confirmation card or confirm command
    assert "/confirm_" not in reply
    assert "tidak menemukan nominal" in reply
    assert tokens > 0


@pytest.mark.asyncio
async def test_execute_yang_transaksi_terbaru_itu_gojek():
    """User clarifying 'Yang transaksi terbaru itu gojek' must propose edit_transaction, never add_transaction."""
    tz = ZoneInfo("Asia/Jakarta")
    now = datetime.now(tz)
    p = master.Plan(intent="action")

    with (
        patch("apa.pipeline.chat._described_transaction", new_callable=AsyncMock, return_value=24),
        patch("apa.pipeline.chat.plan_action", new_callable=AsyncMock) as mock_plan_action,
    ):
        mock_plan_action.return_value = ("❓ Ubah transaksi #24: merchant gojek, kategori Transport? /confirm_123", 10)
        reply, role = await chat._execute(
            sm=MagicMock(),
            llm=MagicMock(),
            redis=None,
            p=p,
            text="Yang transaksi terbaru itu gojek",
            history=[],
            draft=None,
            tz=tz,
            sent_at=now,
        )

        assert role == "action"
        mock_plan_action.assert_called_once()
        hint = mock_plan_action.call_args[0][2]
        # Verify hint told model to edit transaction #24 and DO NOT add a new transaction
        assert "describing transaction #24" in hint or "updating recent transaction #24" in hint
        assert "DO NOT add a new transaction" in hint
        assert "Yang transaksi terbaru itu gojek" in hint


@pytest.mark.asyncio
async def test_execute_duplicate_complaint_transaksi_log_kok_ada_2():
    """User complaining 'transaksi log kok ada 2 yang terbaru' must trigger duplicate resolution, never add_transaction."""
    tz = ZoneInfo("Asia/Jakarta")
    now = datetime.now(tz)
    p = master.Plan(intent="action")

    with patch("apa.pipeline.chat.plan_action", new_callable=AsyncMock) as mock_plan_action:
        mock_plan_action.return_value = ("❓ Batalkan duplikat transaksi #26? /confirm_dup", 10)
        reply, role = await chat._execute(
            sm=MagicMock(),
            llm=MagicMock(),
            redis=None,
            p=p,
            text="transaksi log kok ada 2 yang terbaru",
            history=[],
            draft=None,
            tz=tz,
            sent_at=now,
        )

        assert role == "action"
        mock_plan_action.assert_called_once()
        hint = mock_plan_action.call_args[0][2]
        # Verify duplicate interception hint was injected
        assert "reporting a possible duplicate transaction" in hint
        assert "undo_transaction" in hint
        assert "Do not add a new transaction" in hint


@pytest.mark.asyncio
async def test_execute_vague_fragment_routed_to_converse():
    """Single words like 'Yang' or 'itu' must not propose any action; they must go to converse."""
    tz = ZoneInfo("Asia/Jakarta")
    now = datetime.now(tz)
    p = master.Plan(intent="action")

    with patch("apa.pipeline.chat.converse", new_callable=AsyncMock) as mock_converse:
        mock_converse.return_value = ("Ada yang mau dicek atau diubah, Joe?", 10)
        reply, role = await chat._execute(
            sm=MagicMock(),
            llm=MagicMock(),
            redis=None,
            p=p,
            text="Yang",
            history=[],
            draft=None,
            tz=tz,
            sent_at=now,
        )

        assert role == "general"
        mock_converse.assert_called_once()


@pytest.mark.asyncio
async def test_execute_log_question_routed_to_converse():
    """Inquiries like 'log?' must go to converse so assistant shows log or answers question, not add_transaction."""
    tz = ZoneInfo("Asia/Jakarta")
    now = datetime.now(tz)
    p = master.Plan(intent="action")

    with patch("apa.pipeline.chat.converse", new_callable=AsyncMock) as mock_converse:
        mock_converse.return_value = ("Berikut riwayat transaksi terakhir...", 10)
        reply, role = await chat._execute(
            sm=MagicMock(),
            llm=MagicMock(),
            redis=None,
            p=p,
            text="log?",
            history=[],
            draft=None,
            tz=tz,
            sent_at=now,
        )

        assert role == "general"
        mock_converse.assert_called_once()
