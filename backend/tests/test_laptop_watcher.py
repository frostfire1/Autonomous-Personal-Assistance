import io
import json
import sys
import urllib.error
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add tools to path
TOOLS_DIR = Path(__file__).resolve().parent.parent.parent / "tools"
sys.path.insert(0, str(TOOLS_DIR))

import laptop_watcher  # noqa: E402


def test_test_connection_success():
    mock_health = MagicMock()
    mock_health.read.return_value = b'{"status":"ok"}'
    mock_health.__enter__.return_value = mock_health

    mock_auth = MagicMock()
    mock_auth.read.return_value = b'{"ok":true}'
    mock_auth.__enter__.return_value = mock_auth

    with patch("urllib.request.urlopen", side_effect=[mock_health, mock_auth]):
        ok, msg, lat = laptop_watcher.test_connection("http://127.0.0.1:8000", "valid-key-xyz")
        assert ok is True
        assert "Connected & Authenticated" in msg
        assert lat >= 0.0


def test_test_connection_auth_failure():
    mock_health = MagicMock()
    mock_health.read.return_value = b'{"status":"ok"}'
    mock_health.__enter__.return_value = mock_health

    err_401 = urllib.error.HTTPError("http://127.0.0.1:8000/ingest/test", 401, "Unauthorized", {}, io.BytesIO(b"bad key"))

    with patch("urllib.request.urlopen", side_effect=[mock_health, err_401]):
        ok, msg, lat = laptop_watcher.test_connection("http://127.0.0.1:8000", "bad-key")
        assert ok is False
        assert "HTTP 401" in msg


def test_test_connection_server_unreachable():
    err_unreachable = urllib.error.URLError("Connection refused")

    with patch("urllib.request.urlopen", side_effect=err_unreachable):
        ok, msg, lat = laptop_watcher.test_connection("http://127.0.0.1:9999", "key")
        assert ok is False
        assert "unreachable" in msg.lower()


def test_test_connection_validation_probe_fallback():
    # 422 Unprocessable Entity means auth succeeded
    mock_health = MagicMock()
    mock_health.read.return_value = b'{"status":"ok"}'
    mock_health.__enter__.return_value = mock_health

    err_422 = urllib.error.HTTPError("http://127.0.0.1:8000/ingest/test", 422, "Unprocessable Entity", {}, io.BytesIO(b"validation error"))

    with patch("urllib.request.urlopen", side_effect=[mock_health, err_422]):
        ok, msg, lat = laptop_watcher.test_connection("http://127.0.0.1:8000", "valid-key")
        assert ok is True
        assert "Connected & Authenticated" in msg


def test_push_usage_filters_short_apps():
    mock_resp = MagicMock()
    mock_resp.read.return_value = b'{"ok":true,"apps":1}'
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
        apps = {
            "VS Code": {"seconds": 120, "first_at": None, "last_at": None},
            "Flicker": {"seconds": 2, "first_at": None, "last_at": None},  # < 5s should be filtered
        }
        res = laptop_watcher.push_usage("http://127.0.0.1:8000", "valid-key-xyz", "", "test-device", "desktop", date.today(), apps)
        assert res is True
        # Verify body sent
        args, kwargs = mock_open.call_args
        req = args[0]
        data = json.loads(req.data.decode("utf-8"))
        assert len(data["apps"]) == 1
        assert data["apps"][0]["app"] == "VS Code"


def test_config_load_and_save(tmp_path, monkeypatch):
    test_cfg_file = tmp_path / "test_config.json"
    monkeypatch.setattr(laptop_watcher, "CONFIG_FILE", test_cfg_file)

    laptop_watcher.save_config({"server": "http://10.0.0.5:8000", "key": "k1", "device": "d1"})
    loaded = laptop_watcher.load_config()
    assert loaded["server"] == "http://10.0.0.5:8000"
    assert loaded["key"] == "k1"
    assert loaded["device"] == "d1"


def test_push_activitywatch_offline():
    # If ActivityWatch is not running, push_activitywatch should return False quietly
    err = urllib.error.URLError("Connection refused")
    with patch("urllib.request.urlopen", side_effect=err):
        ok = laptop_watcher.push_activitywatch("http://127.0.0.1:8000", "test-key", "", "laptop")
        assert ok is False


def test_push_activitywatch_success():
    mock_info = MagicMock()
    mock_info.read.return_value = b'{"hostname":"my-pc"}'
    mock_info.__enter__.return_value = mock_info

    mock_buckets = MagicMock()
    mock_buckets.read.return_value = b'{"aw-watcher-window_pc":{},"aw-watcher-afk_pc":{}}'
    mock_buckets.__enter__.return_value = mock_buckets

    mock_afk = MagicMock()
    mock_afk.read.return_value = b'[{"timestamp":"2026-10-27T17:15:00Z","data":{"status":"not-afk"}}]'
    mock_afk.__enter__.return_value = mock_afk

    mock_win = MagicMock()
    mock_win.read.return_value = b'[{"timestamp":"2026-10-27T17:16:00Z","data":{"app":"Code.exe","title":"main.py"}}]'
    mock_win.__enter__.return_value = mock_win

    mock_server_post = MagicMock()
    mock_server_post.read.return_value = b'{"ok":true}'
    mock_server_post.__enter__.return_value = mock_server_post

    side_effects = [mock_info, mock_buckets, mock_afk, mock_win, mock_server_post]
    with patch("urllib.request.urlopen", side_effect=side_effects):
        ok = laptop_watcher.push_activitywatch("http://127.0.0.1:8000", "test-key", "", "my-pc")
        assert ok is True
