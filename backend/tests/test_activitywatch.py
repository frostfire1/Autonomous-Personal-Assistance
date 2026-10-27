import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from apa.activitywatch import (
    _SEEN_EVENT_HASHES,
    clean_youtube_title,
    fetch_aw_data,
    pretty_app,
    process_aw_payload,
    sync_activitywatch_to_memory,
)


def test_clean_youtube_title():
    assert clean_youtube_title("(39) Emerald - Tatsuya Kitani - YouTube") == "Emerald - Tatsuya Kitani"
    assert clean_youtube_title("(1) Hati-Hati Di Twitter and 9 more pages - Personal - Microsoft Edge") == "Hati-Hati Di Twitter"
    assert clean_youtube_title("Lagu Santai - YouTube Music") == "Lagu Santai"
    assert clean_youtube_title("Simple Title") == "Simple Title"
    assert clean_youtube_title("") == ""
    # Test zero-width space and browser suffix removal
    assert clean_youtube_title("Video Title - Personal - Microsoft\u200b Edge") == "Video Title"
    assert clean_youtube_title("My Song - Google Chrome") == "My Song"


def test_pretty_app():
    assert pretty_app("msedge.exe") == "Microsoft Edge"
    assert pretty_app("chrome.exe") == "Google Chrome"
    assert pretty_app("code.exe") == "VS Code"
    assert pretty_app("antigravity.exe") == "Antigravity"
    assert pretty_app("custom_tool.exe") == "Custom_tool"
    assert pretty_app("") == ""


@pytest.mark.asyncio
async def test_process_aw_payload_and_deduplication():
    _SEEN_EVENT_HASHES.clear()
    tz = ZoneInfo("Asia/Jakarta")

    raw_data = {
        "hostname": "test-laptop",
        "afk_events": [
            {
                "timestamp": "2026-10-27T17:15:00+07:00",
                "data": {"status": "not-afk"},
            }
        ],
        "win_events": [
            {
                "timestamp": "2026-10-27T17:16:00+07:00",
                "data": {"app": "msedge.exe", "title": "Hati-Hati Di Twitter - YouTube - Microsoft Edge"},
            }
        ],
        "web_events": [
            {
                "_bucket": "aw-watcher-web-chrome_test",
                "timestamp": "2026-10-27T17:16:00+07:00",
                "data": {
                    "url": "https://www.youtube.com/watch?v=12345",
                    "title": "Hati-Hati Di Twitter - YouTube",
                    "audible": True,
                },
            }
        ],
    }

    mock_sm = MagicMock()
    mock_redis = AsyncMock()

    with patch("apa.assistant_memory.add_playing", new_callable=AsyncMock) as mock_add_playing:
        mock_add_playing.return_value = 1
        live = await process_aw_payload(mock_sm, mock_redis, tz, raw_data)

        assert live["online"] is True
        assert live["hostname"] == "test-laptop"
        assert live["is_awake"] is True
        assert live["active_window"]["app"] == "Microsoft Edge"
        assert live["latest_youtube"]["title"] == "Hati-Hati Di Twitter"
        assert live["latest_youtube"]["audible"] is True
        assert live["new_media_synced"] == 1
        mock_add_playing.assert_called_once()
        mock_redis.set.assert_called_once()

    # Second pass: same payload should be deduplicated
    with patch("apa.assistant_memory.add_playing", new_callable=AsyncMock) as mock_add_playing:
        live2 = await process_aw_payload(mock_sm, mock_redis, tz, raw_data)
        assert live2["new_media_synced"] == 0
        mock_add_playing.assert_not_called()


@pytest.mark.asyncio
async def test_process_aw_payload_afk_detection():
    tz = ZoneInfo("Asia/Jakarta")
    raw_data = {
        "hostname": "test-laptop",
        "afk_events": [
            {
                "timestamp": "2026-10-27T17:00:00+07:00",
                "data": {"status": "afk"},
            }
        ],
        "win_events": [],
        "web_events": [],
    }

    live = await process_aw_payload(None, None, tz, raw_data)
    assert live["is_awake"] is False
    assert live["afk_status"] == "afk"


@pytest.mark.asyncio
async def test_fetch_aw_data_offline():
    res = await fetch_aw_data("http://127.0.0.1:59999", timeout_s=0.2)
    assert res is None


@pytest.mark.asyncio
async def test_sync_activitywatch_to_memory_cached_fallback():
    mock_redis = AsyncMock()
    mock_redis.get.return_value = json.dumps({"online": True, "hostname": "cached-pc", "is_awake": True})

    with patch("apa.activitywatch.fetch_aw_data", new_callable=AsyncMock, return_value=None):
        res = await sync_activitywatch_to_memory(None, mock_redis, ZoneInfo("Asia/Jakarta"))
        assert res["online"] is True
        assert res["hostname"] == "cached-pc"


@pytest.mark.asyncio
async def test_tool_activity_formatting():
    from apa.chat.tools import run_read

    mock_sm = MagicMock()
    live_state = {
        "online": True,
        "hostname": "Tribe",
        "is_awake": True,
        "afk_status": "not-afk",
        "afk_since": "17:15",
        "active_window": {"app": "Microsoft Edge", "title": "Hati-Hati Di Twitter", "time": "17:16"},
        "recent_apps": [{"app": "Microsoft Edge", "time": "17:16"}],
        "latest_youtube": {"title": "Hati-Hati Di Twitter", "time": "17:16", "audible": True},
        "recent_youtube": [],
        "updated_at": "17:16:30",
    }
    tz = ZoneInfo("Asia/Jakarta")
    now = datetime.now(tz)

    with (
        patch("apa.activitywatch.sync_activitywatch_to_memory", new_callable=AsyncMock, return_value=live_state),
        patch("apa.usage.recent", new_callable=AsyncMock, return_value=[]),
        patch("apa.assistant_memory.watched", new_callable=AsyncMock, return_value=[]),
    ):
        result = await run_read(mock_sm, tz, now, "activity", {})
        assert "STATUS AKTIF SAAT INI (REAL-TIME ACTIVITYWATCH)" in result
        assert "laptop Tribe" in result
        assert "AKTIF & MELEK" in result
        assert "Microsoft Edge" in result
        assert "Hati-Hati Di Twitter" in result
