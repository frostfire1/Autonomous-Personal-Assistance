"""Tests for the GitHub self-update checker, client downloads (APK & EXE),
and Telegram download menu views.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from starlette.responses import FileResponse, RedirectResponse

from apa.bot.menu import BOT_COMMANDS, download_view, menu_key
from apa.gateway.routers import update
from apa.integrations.notify import keyboard


@pytest.mark.asyncio
async def test_check_for_updates_github_mock():
    """Verify check_for_updates parses GitHub commit data and compare output without 409 error."""
    mock_commit = {
        "sha": "489bc75c5f629da4d7978d5e4b0ce23c88eed66c",
        "commit": {
            "message": "Add phone watch and client downloads\n\nFull details here",
            "committer": {"date": "2026-10-29T10:00:00Z"},
            "author": {"name": "Frost_Fire1"},
        },
    }
    mock_compare = {
        "ahead_by": 2,
    }

    class MockResponse:
        def __init__(self, json_data, status_code=200):
            self._json = json_data
            self.status_code = status_code

        def json(self):
            return self._json

    async def mock_get(url, *args, **kwargs):
        if "compare" in url:
            return MockResponse(mock_compare)
        return MockResponse(mock_commit)

    with patch("httpx.AsyncClient.get", side_effect=mock_get):
        with patch.object(update, "_get_local_sha", return_value="c5fd761"):
            res = await update.check_for_updates()

    assert res["up_to_date"] is False
    assert res["behind"] == 2
    assert res["local_sha"] == "c5fd761"
    assert res["remote_sha"] == "489bc75"
    assert res["latest_commit"]["message"] == "Add phone watch and client downloads"
    assert res["latest_commit"]["author"] == "Frost_Fire1"
    assert "releases/latest/download/apa-latest.apk" in res["apk_url"]
    assert "releases/latest/download/APA-Tracker.exe" in res["exe_url"]


@pytest.mark.asyncio
async def test_check_for_updates_up_to_date():
    """Verify check_for_updates recognizes matching local and remote commits."""
    mock_commit = {
        "sha": "489bc75c5f629da4d7978d5e4b0ce23c88eed66c",
        "commit": {
            "message": "Latest release commit",
            "committer": {"date": "2026-10-29T10:00:00Z"},
            "author": {"name": "Frost_Fire1"},
        },
    }

    class MockResponse:
        status_code = 200

        def json(self):
            return mock_commit

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = MockResponse()
        with patch.object(update, "_get_local_sha", return_value="489bc75"):
            res = await update.check_for_updates()

    assert res["up_to_date"] is True
    assert res["behind"] == 0
    assert res["remote_sha"] == "489bc75"


@pytest.mark.asyncio
async def test_check_for_updates_offline_fallback():
    """Verify check_for_updates gracefully falls back without 409 Conflict if GitHub API is unreachable."""
    with patch("httpx.AsyncClient.get", side_effect=Exception("Connection refused")):
        with patch.object(update, "_get_local_sha", return_value="unknown"):
            with patch("pathlib.Path.exists", return_value=False):
                res = await update.check_for_updates()

    assert "repo_url" in res
    assert "apk_url" in res
    assert "exe_url" in res
    assert res["remote_sha"] == "unknown"


@pytest.mark.asyncio
async def test_client_downloads_endpoint():
    """Verify GET /update/downloads returns valid metadata for APK and EXE."""
    data = await update.client_downloads()
    assert "apk" in data and "exe" in data
    assert data["apk"]["filename"] == "apa-latest.apk"
    assert data["exe"]["filename"] == "APA-Tracker.exe"
    assert data["apk"]["url"].endswith("apa-latest.apk")
    assert data["exe"]["url"].endswith("APA-Tracker.exe")


@pytest.mark.asyncio
async def test_download_apk_redirect_and_file():
    """Verify APK endpoint serves local file or redirects to GitHub Releases."""
    # Test redirect when no local APK exists
    with patch.object(update, "_find_local_apk", return_value=None):
        resp = await update.download_apk()
        assert isinstance(resp, RedirectResponse)
        assert resp.status_code == 307
        assert "releases/latest/download/apa-latest.apk" in resp.headers["location"]

    # Test file response when local APK exists
    dummy_apk = Path(__file__).resolve()
    with patch.object(update, "_find_local_apk", return_value=dummy_apk):
        resp = await update.download_apk()
        assert isinstance(resp, FileResponse)


@pytest.mark.asyncio
async def test_download_exe_redirect_and_file():
    """Verify EXE endpoint serves local file or redirects to GitHub Releases."""
    # Test redirect when no local EXE exists
    with patch.object(update, "_find_local_exe", return_value=None):
        resp = await update.download_exe()
        assert isinstance(resp, RedirectResponse)
        assert resp.status_code == 307
        assert "releases/latest/download/APA-Tracker.exe" in resp.headers["location"]

    # Test file response when local EXE exists
    dummy_exe = Path(__file__).resolve()
    with patch.object(update, "_find_local_exe", return_value=dummy_exe):
        resp = await update.download_exe()
        assert isinstance(resp, FileResponse)


@pytest.mark.asyncio
async def test_apply_update_container_stream():
    """Verify apply_update streams instructions instead of 409 in container mode."""
    with patch("pathlib.Path.exists", return_value=False):
        lines = []
        async for chunk in update._stream_update(Path("/nonexistent"), "up"):
            lines.append(chunk)

    combined = "".join(lines)
    assert "[info] Server is running in container mode" in combined
    assert "git pull --rebase && bash deploy/deploy.sh up" in combined
    assert "data: [done] 0\n\n" in combined


def test_telegram_download_views_and_url_buttons():
    """Verify Telegram menu has download entries and url: buttons convert to InlineKeyboardButton(url=...)."""
    assert menu_key("/download") == "download"
    assert menu_key("/unduh") == "download"
    assert menu_key("/apk") == "download"
    assert menu_key("/exe") == "download"
    assert any(cmd == "download" for cmd, _ in BOT_COMMANDS)

    # Check download view structure
    v = download_view()
    assert "Android App" in v.text
    assert "Windows Tracker" in v.text
    labels = [btn[0] for row in v.rows for btn in row]
    targets = [btn[1] for row in v.rows for btn in row]
    assert any("Android APK" in lbl for lbl in labels)
    assert any("Windows Tracker" in lbl for lbl in labels)
    assert any(tgt.startswith("url:") and "apa-latest.apk" in tgt for tgt in targets)
    assert any(tgt.startswith("url:") and "APA-Tracker.exe" in tgt for tgt in targets)

    # Check keyboard rendering with url:
    kb = keyboard(v.rows)
    url_buttons = [btn for row in kb.inline_keyboard for btn in row if btn.url]
    assert len(url_buttons) >= 3
    assert any("apa-latest.apk" in (btn.url or "") for btn in url_buttons)
    assert any("APA-Tracker.exe" in (btn.url or "") for btn in url_buttons)
