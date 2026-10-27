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

import laptop_watcher


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
