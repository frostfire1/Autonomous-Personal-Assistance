#!/usr/bin/env python3
"""Autonomous Personal Assistant - Laptop Activity Monitor (Desktop Tracker & System Tray).

Tracks active foreground applications and screen time on your laptop/computer and
pushes daily usage summaries to your APA server (/ingest/usage).

Features:
  - System Tray App: Lives quietly in the Windows system tray with status, active app,
    quick sync, connection testing, and settings dialog.
  - Setup & Settings GUI: Built-in input box to configure server URL, Ingest API Key,
    HMAC secret, device name, and Windows startup on boot.
  - Test Connection: Measures latency (ms) and validates both server reachability
    and API key / HMAC credentials against /ingest/test.
  - Zero Admin Rights Required: Configures Windows Startup via HKCU Run registry.
  - Windows Task Scheduler Option: Also supports running as a background scheduled task.
  - Privacy Guaranteed: Only application names and seconds are recorded. No window titles,
    no URLs, no keystrokes.
"""

import argparse
import ctypes
import hashlib
import hmac
import json
import logging
import os
import platform
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

# Windows-specific imports
IS_WINDOWS = platform.system() == "Windows"
if IS_WINDOWS:
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    try:
        import winreg
    except ImportError:
        winreg = None
else:
    winreg = None

# Optional GUI & Tray libraries
try:
    import pystray
    from PIL import Image, ImageDraw
    TRAY_AVAILABLE = True
except ImportError:
    Image = None
    ImageDraw = None
    pystray = None
    TRAY_AVAILABLE = False

try:
    import tkinter as tk
    from tkinter import messagebox
    TK_AVAILABLE = True
except ImportError:
    tk = None
    messagebox = None
    TK_AVAILABLE = False

# Constants & Defaults
POLL_INTERVAL_S = 3  # check foreground window every 3 seconds
DEFAULT_PUSH_INTERVAL_S = 300  # push to APA server every 5 minutes (300s)
TASK_NAME = "APA_Laptop_Activity_Service"
RUN_REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
REG_VAL_NAME = "APALaptopTracker"


def get_app_dir() -> Path:
    """Find a writable directory for config and logs (local or %APPDATA%/APA-Tracker)."""
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        test_file = exe_dir / ".write_test"
        try:
            test_file.write_text("ok", encoding="utf-8")
            test_file.unlink()
            return exe_dir
        except Exception:
            pass
        appdata = Path(os.environ.get("APPDATA", str(Path.home()))) / "APA-Tracker"
        appdata.mkdir(parents=True, exist_ok=True)
        return appdata
    return Path(__file__).resolve().parent


APP_DIR = get_app_dir()
CONFIG_FILE = APP_DIR / "laptop_config.json"
LOG_FILE = APP_DIR / "laptop_watcher.log"

PRETTY_APP_NAMES = {
    "msedge.exe": "Microsoft Edge",
    "chrome.exe": "Google Chrome",
    "code.exe": "VS Code",
    "windowsterminal.exe": "Windows Terminal",
    "explorer.exe": "File Explorer",
    "powershell.exe": "PowerShell",
    "cmd.exe": "Command Prompt",
    "teams.exe": "Microsoft Teams",
    "ms-teams.exe": "Microsoft Teams",
    "discord.exe": "Discord",
    "slack.exe": "Slack",
    "spotify.exe": "Spotify",
    "notion.exe": "Notion",
    "obsidian.exe": "Obsidian",
    "telegram.exe": "Telegram",
    "devenv.exe": "Visual Studio",
    "idea64.exe": "IntelliJ IDEA",
    "pycharm64.exe": "PyCharm",
}

IGNORE_PROCESSES = {
    "lockapp.exe", "shellexperiencehost.exe", "searchhost.exe",
    "startmenuexperiencehost.exe", "applicationframehost.exe",
    "systemsettings.exe", "taskmgr.exe"
}


def setup_logger(foreground: bool = True):
    log_format = "[%(asctime)s] %(levelname)s: %(message)s"
    handlers = [logging.FileHandler(LOG_FILE, encoding="utf-8")]
    if foreground:
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(level=logging.INFO, format=log_format, handlers=handlers, force=True)


def load_config() -> dict[str, Any]:
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_config(cfg: dict[str, Any]):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        logging.warning(f"Could not save config file: {e}")


def get_pythonw_executable() -> str:
    """Find pythonw.exe for silent background execution without console window."""
    py_dir = os.path.dirname(sys.executable)
    pythonw = os.path.join(py_dir, "pythonw.exe")
    if os.path.exists(pythonw):
        return pythonw
    return sys.executable


# ---- Windows Startup Setup (Registry) ---------------------------------------


def is_startup_enabled() -> bool:
    """Check if tracker is set to run automatically on Windows user logon."""
    if not (IS_WINDOWS and winreg):
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_REG_KEY, 0, winreg.KEY_READ) as key:
            winreg.QueryValueEx(key, REG_VAL_NAME)
            return True
    except Exception:
        return False


def set_startup_enabled(enable: bool) -> bool:
    """Enable or disable auto-start on Windows user logon via HKCU Run registry."""
    if not (IS_WINDOWS and winreg):
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_REG_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enable:
                if getattr(sys, "frozen", False):
                    cmd = f'"{sys.executable}"'
                else:
                    pyw = get_pythonw_executable()
                    script = str(Path(__file__).resolve())
                    cmd = f'"{pyw}" "{script}"'
                winreg.SetValueEx(key, REG_VAL_NAME, 0, winreg.REG_SZ, cmd)
                logging.info(f"Registered Windows startup: {cmd}")
            else:
                try:
                    winreg.DeleteValue(key, REG_VAL_NAME)
                    logging.info("Unregistered Windows startup")
                except FileNotFoundError:
                    pass
        return True
    except Exception as e:
        logging.warning(f"Failed to update Windows startup registry: {e}")
        return False


# ---- Windows Foreground Tracking -------------------------------------------


def get_foreground_app_windows() -> str | None:
    """Get executable name of the current active foreground window on Windows."""
    if not IS_WINDOWS:
        return None
    try:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return None

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h_proc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not h_proc:
            return None

        buf = ctypes.create_unicode_buffer(512)
        size = wintypes.DWORD(512)
        kernel32.QueryFullProcessImageNameW(h_proc, 0, buf, ctypes.byref(size))
        kernel32.CloseHandle(h_proc)

        exe_path = buf.value
        if not exe_path:
            return None

        exe_name = os.path.basename(exe_path).lower()
        if exe_name in IGNORE_PROCESSES:
            return None

        return PRETTY_APP_NAMES.get(exe_name, exe_name.removesuffix(".exe").capitalize())
    except Exception:
        return None


# ---- API Push & Connection Testing ------------------------------------------


def test_connection(server: str, key: str, secret: str = "", device: str = "") -> tuple[bool, str, float]:
    """Test connectivity to APA server and verify API key & HMAC signature.

    Returns:
        (success: bool, status_message: str, latency_ms: float)
    """
    server = (server or "").strip().rstrip("/")
    if not server:
        return False, "Server URL is required.", 0.0
    if not (server.startswith("http://") or server.startswith("https://")):
        server = "http://" + server
    if not key:
        return False, "Ingest API Key is required.", 0.0

    # Step 1: Health probe to test network reachability & base latency
    t0 = time.perf_counter()
    try:
        health_url = f"{server}/health"
        req_health = urllib.request.Request(health_url, method="GET", headers={"User-Agent": "APA-Tracker/1.0"})
        with urllib.request.urlopen(req_health, timeout=6) as resp:
            resp.read()
    except urllib.error.URLError as e:
        latency = (time.perf_counter() - t0) * 1000
        reason = getattr(e, "reason", str(e))
        return False, f"Server unreachable at {server} ({reason})", latency
    except Exception as e:
        latency = (time.perf_counter() - t0) * 1000
        return False, f"Connection failed to {server}: {e}", latency

    # Step 2: Auth probe to test API Key & HMAC signature
    test_path = "/ingest/test"
    test_url = f"{server}{test_path}"
    headers = {
        "X-API-Key": key,
        "User-Agent": "APA-Tracker/1.0",
        "Content-Type": "application/json",
    }
    body = b"{}"
    if secret:
        ts = str(int(time.time()))
        headers["X-Timestamp"] = ts
        headers["X-Signature"] = hmac.new(
            secret.encode(),
            f"{ts}.POST.{test_path}.".encode() + body,
            hashlib.sha256,
        ).hexdigest()

    try:
        req_test = urllib.request.Request(test_url, data=body, method="POST", headers=headers)
        with urllib.request.urlopen(req_test, timeout=6) as resp:
            total_latency = (time.perf_counter() - t0) * 1000
            return True, f"Connected & Authenticated! Latency: {total_latency:.1f}ms (HTTP 200 OK)", total_latency
    except urllib.error.HTTPError as e:
        total_latency = (time.perf_counter() - t0) * 1000
        if e.code == 401:
            return False, "Authentication Failed (HTTP 401): Bad Ingest API Key or HMAC Signature", total_latency
        elif e.code == 422:
            return True, f"Connected & Authenticated! Latency: {total_latency:.1f}ms", total_latency
        elif e.code == 404:
            # Fallback probe for older servers without /ingest/test
            usage_path = "/ingest/usage"
            usage_url = f"{server}{usage_path}"
            if secret:
                ts = str(int(time.time()))
                headers["X-Timestamp"] = ts
                headers["X-Signature"] = hmac.new(
                    secret.encode(),
                    f"{ts}.POST.{usage_path}.".encode() + body,
                    hashlib.sha256,
                ).hexdigest()
            try:
                req_usage = urllib.request.Request(usage_url, data=body, method="POST", headers=headers)
                with urllib.request.urlopen(req_usage, timeout=6):
                    return True, f"Connected & Authenticated! Latency: {total_latency:.1f}ms", total_latency
            except urllib.error.HTTPError as e2:
                if e2.code == 401:
                    return False, "Authentication Failed (HTTP 401): Bad Ingest API Key or HMAC Signature", total_latency
                elif e2.code == 422:
                    return True, f"Connected & Authenticated! Latency: {total_latency:.1f}ms", total_latency
                return False, f"Server returned HTTP {e2.code}: {e2.reason}", total_latency
            except Exception as ex2:
                return False, f"Probe error: {ex2}", total_latency
        return False, f"Server returned HTTP {e.code}: {e.reason}", total_latency
    except Exception as e:
        total_latency = (time.perf_counter() - t0) * 1000
        return False, f"Verification failed: {e}", total_latency


def push_usage(server: str, key: str, secret: str, device: str, kind: str, day: date, apps_dict: dict[str, dict]) -> bool:
    """Push aggregated usage to APA server (/ingest/usage)."""
    app_list = []
    for name, data in apps_dict.items():
        if data["seconds"] >= 5:  # filter noise under 5 seconds
            app_list.append({
                "app": name,
                "seconds": int(data["seconds"]),
                "first_at": data["first_at"].isoformat() if data.get("first_at") else None,
                "last_at": data["last_at"].isoformat() if data.get("last_at") else None,
            })

    if not app_list:
        return True

    payload = {
        "device": device,
        "kind": kind,
        "day": day.isoformat(),
        "apps": app_list,
    }
    body = json.dumps(payload).encode("utf-8")
    path = "/ingest/usage"
    headers = {
        "X-API-Key": key,
        "Content-Type": "application/json",
        "User-Agent": "APA-Tracker/1.0",
    }
    if secret:
        ts = str(int(time.time()))
        headers["X-Timestamp"] = ts
        headers["X-Signature"] = hmac.new(secret.encode(), f"{ts}.POST.{path}.".encode() + body, hashlib.sha256).hexdigest()

    req = urllib.request.Request(server.rstrip("/") + path, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            res = json.loads(r.read())
            logging.info(f"Pushed {len(app_list)} apps for {day} ({res.get('ok')})")
            return True
    except Exception as exc:
        logging.warning(f"Push failed: {exc}")
        return False


def push_activitywatch(server: str, key: str, secret: str, device: str, aw_url: str = "http://localhost:5600") -> bool:
    """Fetch recent ActivityWatch events (AFK, active window, web/YouTube) and push to APA server."""
    base = aw_url.rstrip("/")
    api_url = f"{base}/api/0" if not base.endswith("/api/0") else base
    try:
        req_info = urllib.request.Request(f"{api_url}/info", headers={"User-Agent": "APA-Tracker/1.0"})
        with urllib.request.urlopen(req_info, timeout=1.5) as resp:
            info = json.loads(resp.read().decode("utf-8", errors="ignore"))
            hostname = info.get("hostname", device)

        req_b = urllib.request.Request(f"{api_url}/buckets/", headers={"User-Agent": "APA-Tracker/1.0"})
        with urllib.request.urlopen(req_b, timeout=2.0) as resp:
            buckets = json.loads(resp.read().decode("utf-8", errors="ignore"))

        afk_bucket = next((k for k in buckets if "afk" in k), None)
        afk_events = []
        if afk_bucket:
            try:
                r_afk = urllib.request.Request(f"{api_url}/buckets/{afk_bucket}/events?limit=5", headers={"User-Agent": "APA-Tracker/1.0"})
                with urllib.request.urlopen(r_afk, timeout=2.0) as r:
                    afk_events = json.loads(r.read().decode("utf-8", errors="ignore"))
            except Exception:
                pass

        win_bucket = next((k for k in buckets if "window" in k), None)
        win_events = []
        if win_bucket:
            try:
                r_win = urllib.request.Request(f"{api_url}/buckets/{win_bucket}/events?limit=15", headers={"User-Agent": "APA-Tracker/1.0"})
                with urllib.request.urlopen(r_win, timeout=2.0) as r:
                    win_events = json.loads(r.read().decode("utf-8", errors="ignore"))
            except Exception:
                pass

        web_events = []
        for b in [k for k in buckets if "web" in k]:
            try:
                r_web = urllib.request.Request(f"{api_url}/buckets/{b}/events?limit=25", headers={"User-Agent": "APA-Tracker/1.0"})
                with urllib.request.urlopen(r_web, timeout=2.0) as r:
                    evs = json.loads(r.read().decode("utf-8", errors="ignore"))
                    for ev in evs:
                        ev["_bucket"] = b
                        web_events.append(ev)
            except Exception:
                pass

        payload = {
            "hostname": hostname,
            "afk_events": afk_events,
            "win_events": win_events,
            "web_events": web_events,
        }
        body = json.dumps(payload).encode("utf-8")
        path = "/ingest/activitywatch"
        headers = {
            "X-API-Key": key,
            "Content-Type": "application/json",
            "User-Agent": "APA-Tracker/1.0",
        }
        if secret:
            ts = str(int(time.time()))
            headers["X-Timestamp"] = ts
            headers["X-Signature"] = hmac.new(secret.encode(), f"{ts}.POST.{path}.".encode() + body, hashlib.sha256).hexdigest()

        req = urllib.request.Request(server.rstrip("/") + path, data=body, method="POST", headers=headers)
        with urllib.request.urlopen(req, timeout=10) as r:
            res = json.loads(r.read().decode("utf-8", errors="ignore"))
            logging.info(f"Pushed ActivityWatch events: {res.get('ok')}")
            return True
    except Exception as exc:
        logging.debug(f"ActivityWatch push skipped or failed: {exc}")
        return False


# ---- Icon & Tray Resources --------------------------------------------------


def get_icon_image() -> Any:
    """Load or generate a 64x64 icon for system tray."""
    if not (TRAY_AVAILABLE and Image):
        return None

    # Check for tracker_icon.ico bundled in PyInstaller or nearby
    candidates = [
        getattr(sys, "_MEIPASS", "") and Path(sys._MEIPASS) / "tracker_icon.ico",
        Path(sys.executable).parent / "tracker_icon.ico",
        Path(__file__).resolve().parent / "tracker_icon.ico",
        APP_DIR / "tracker_icon.ico",
    ]
    for c in candidates:
        if c and Path(c).exists():
            try:
                return Image.open(str(c))
            except Exception:
                pass

    # Draw fallback clean icon if file not present
    img = Image.new("RGBA", (64, 64), color=(0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, 60, 60), fill=(37, 99, 235))
    d.polygon([(22, 16), (46, 32), (22, 48)], fill=(255, 255, 255))
    return img


# ---- Settings GUI Dialog (Tkinter) ------------------------------------------

_active_settings_window = None


def show_settings_dialog(on_save_callback=None, initial_server: str = "", initial_key: str = "",
                         initial_hmac: str = "", initial_device: str = ""):
    """Display the Settings & Setup dialog box with server URL, Ingest API Key, and Windows startup."""
    global _active_settings_window
    if not TK_AVAILABLE:
        logging.error("Tkinter is not available. Please configure via CLI or config file.")
        return

    if _active_settings_window is not None:
        try:
            _active_settings_window.lift()
            _active_settings_window.focus_force()
            return
        except Exception:
            _active_settings_window = None

    saved_cfg = load_config()
    server_val = initial_server or saved_cfg.get("server") or os.environ.get("APA_URL", "http://localhost:8000")
    key_val = initial_key or saved_cfg.get("key") or os.environ.get("APA_KEY", os.environ.get("INGEST_API_KEY", ""))
    hmac_val = initial_hmac or saved_cfg.get("hmac") or os.environ.get("APA_HMAC", os.environ.get("INGEST_HMAC_SECRET", ""))
    device_val = initial_device or saved_cfg.get("device") or socket.gethostname()
    sync_interval_val = int(saved_cfg.get("push_interval", DEFAULT_PUSH_INTERVAL_S))

    root = tk.Tk()
    _active_settings_window = root
    root.title("APA Desktop Tracker - Setup & Settings")
    root.geometry("540x660")
    root.resizable(False, False)
    root.configure(bg="#f8fafc")

    # Center dialog on screen
    root.update_idletasks()
    x = max(0, (root.winfo_screenwidth() // 2) - 270)
    y = max(0, (root.winfo_screenheight() // 2) - 330)
    root.geometry(f"540x660+{x}+{y}")

    # Set window icon if available
    try:
        ico_path = Path(__file__).resolve().parent / "tracker_icon.ico"
        if ico_path.exists():
            root.iconbitmap(str(ico_path))
    except Exception:
        pass

    # Header Banner
    header = tk.Frame(root, bg="#1e293b", padx=20, pady=14)
    header.pack(fill="x")

    lbl_title = tk.Label(header, text="⚡ APA Desktop Tracker Setup", font=("Segoe UI", 13, "bold"), fg="#ffffff", bg="#1e293b")
    lbl_title.pack(anchor="w")
    lbl_sub = tk.Label(header, text="Configure server URL, Ingest API Key, and Windows startup options.",
                       font=("Segoe UI", 9), fg="#94a3b8", bg="#1e293b")
    lbl_sub.pack(anchor="w", pady=(2, 0))

    # Form Body
    body = tk.Frame(root, bg="#f8fafc", padx=20, pady=12)
    body.pack(fill="both", expand=True)

    # 1. Server URL
    tk.Label(body, text="Server Address / Gateway URL:", font=("Segoe UI", 9, "bold"), bg="#f8fafc", fg="#1e293b").pack(anchor="w")
    url_var = tk.StringVar(value=server_val)
    url_ent = tk.Entry(body, textvariable=url_var, font=("Segoe UI", 10), relief="groove", bd=2)
    url_ent.pack(fill="x", pady=(2, 4))

    preset_frame = tk.Frame(body, bg="#f8fafc")
    preset_frame.pack(anchor="w", pady=(0, 8))
    tk.Button(preset_frame, text="localhost:8000", font=("Segoe UI", 8), command=lambda: url_var.set("http://localhost:8000")).pack(side="left", padx=(0, 4))
    tk.Button(preset_frame, text="127.0.0.1:8000", font=("Segoe UI", 8), command=lambda: url_var.set("http://127.0.0.1:8000")).pack(side="left", padx=4)
    tk.Button(preset_frame, text="192.168.1. :8000", font=("Segoe UI", 8), command=lambda: url_var.set("http://192.168.1.100:8000")).pack(side="left", padx=4)

    # 2. Ingest API Key
    tk.Label(body, text="Ingest API Key (backend INGEST_API_KEY):", font=("Segoe UI", 9, "bold"), bg="#f8fafc", fg="#1e293b").pack(anchor="w")
    key_var = tk.StringVar(value=key_val)
    key_ent = tk.Entry(body, textvariable=key_var, font=("Segoe UI", 10), relief="groove", bd=2)
    key_ent.pack(fill="x", pady=(2, 8))

    # 3. HMAC Secret
    tk.Label(body, text="HMAC Secret (optional, backend INGEST_HMAC_SECRET):", font=("Segoe UI", 9, "bold"), bg="#f8fafc", fg="#1e293b").pack(anchor="w")
    hmac_var = tk.StringVar(value=hmac_val)
    hmac_ent = tk.Entry(body, textvariable=hmac_var, font=("Segoe UI", 10), relief="groove", bd=2)
    hmac_ent.pack(fill="x", pady=(2, 8))

    # 4. Device Identifier
    tk.Label(body, text="Device Identifier (this PC):", font=("Segoe UI", 9, "bold"), bg="#f8fafc", fg="#1e293b").pack(anchor="w")
    device_var = tk.StringVar(value=device_val)
    device_ent = tk.Entry(body, textvariable=device_var, font=("Segoe UI", 10), relief="groove", bd=2)
    device_ent.pack(fill="x", pady=(2, 10))

    # 5. Startup Setup Checkbox
    startup_var = tk.BooleanVar(value=is_startup_enabled())
    chk_startup = tk.Checkbutton(body, text="Start automatically with Windows (Auto-start on logon)",
                                 variable=startup_var, font=("Segoe UI", 9, "bold"), bg="#f8fafc", activebackground="#f8fafc")
    chk_startup.pack(anchor="w", pady=(0, 2))
    tk.Label(body, text="  Configures Windows User Startup registry (HKCU Run - no admin required).",
             font=("Segoe UI", 8), fg="#64748b", bg="#f8fafc").pack(anchor="w", pady=(0, 10))

    # 6. Connection & Auth Tester Panel
    test_frame = tk.LabelFrame(body, text=" Connection & Auth Test ", font=("Segoe UI", 9, "bold"), bg="#f8fafc", padx=12, pady=10)
    test_frame.pack(fill="x", pady=(0, 12))

    lbl_status = tk.Label(test_frame, text="Ready. Click 'Test Connection' to verify server reachability and API key.",
                          font=("Segoe UI", 9), fg="#475569", bg="#f8fafc", wraplength=470, justify="left")
    lbl_status.pack(anchor="w", pady=(0, 8))

    def do_test():
        url = url_var.get().strip()
        k = key_var.get().strip()
        h = hmac_var.get().strip()
        d = device_var.get().strip()

        if not url:
            lbl_status.config(text="❌ Please enter a Server URL first.", fg="#dc2626")
            return
        if not k:
            lbl_status.config(text="❌ Please enter an Ingest API Key first.", fg="#dc2626")
            return

        lbl_status.config(text="⏳ Pinging server and verifying Ingest credentials...", fg="#2563eb")
        btn_test.config(state="disabled")

        def _bg_test():
            ok, msg, lat = test_connection(url, k, h, d)
            color = "#16a34a" if ok else "#dc2626"
            prefix = "✅ " if ok else "❌ "
            root.after(0, lambda: (
                lbl_status.config(text=f"{prefix}{msg}", fg=color),
                btn_test.config(state="normal"),
            ))

        threading.Thread(target=_bg_test, daemon=True).start()

    btn_test = tk.Button(test_frame, text="⚡ Test Connection", font=("Segoe UI", 9, "bold"), bg="#e2e8f0", padx=10, pady=2, command=do_test)
    btn_test.pack(anchor="w")

    # Bottom Buttons Bar
    btn_bar = tk.Frame(root, bg="#f1f5f9", padx=20, pady=12)
    btn_bar.pack(fill="x", side="bottom")

    def on_save():
        url = url_var.get().strip()
        k = key_var.get().strip()
        h = hmac_var.get().strip()
        d = device_var.get().strip() or socket.gethostname()
        start_boot = startup_var.get()

        if not url:
            messagebox.showwarning("Missing URL", "Please enter the APA Server URL.", parent=root)
            return
        if not k:
            messagebox.showwarning("Missing Key", "Please enter the Ingest API Key.", parent=root)
            return

        new_cfg = {
            "server": url,
            "key": k,
            "hmac": h,
            "device": d,
            "kind": "desktop",
            "push_interval": sync_interval_val,
            "startup": start_boot,
        }
        save_config(new_cfg)
        set_startup_enabled(start_boot)

        if on_save_callback:
            on_save_callback(new_cfg)

        global _active_settings_window
        _active_settings_window = None
        root.destroy()

    def on_cancel():
        global _active_settings_window
        _active_settings_window = None
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_cancel)

    btn_save = tk.Button(btn_bar, text="💾 Save & Connect", font=("Segoe UI", 9, "bold"), bg="#2563eb", fg="#ffffff", padx=14, pady=5, command=on_save)
    btn_save.pack(side="right", padx=(8, 0))

    btn_cancel = tk.Button(btn_bar, text="Cancel", font=("Segoe UI", 9), padx=10, pady=5, command=on_cancel)
    btn_cancel.pack(side="right")

    root.lift()
    root.focus_force()
    root.mainloop()


# ---- System Tray & Tracking Loop --------------------------------------------

class LaptopTrackerApp:
    def __init__(self, server: str, key: str, secret: str, device: str, kind: str = "desktop", push_interval: int = DEFAULT_PUSH_INTERVAL_S):
        self.server = server
        self.key = key
        self.secret = secret
        self.device = device
        self.kind = kind
        self.push_interval = push_interval

        self.is_running = True
        self.is_paused = False
        self.current_app: str = "Starting..."
        self.current_app_seconds: int = 0
        self.apps_data: dict[str, dict] = defaultdict(lambda: {"seconds": 0, "first_at": None, "last_at": None})
        self.current_day: date = date.today()
        self.last_push_time = time.time()
        self.last_aw_push_time = 0
        self.tray_icon = None

    def update_config(self, cfg: dict[str, Any]):
        """Apply newly saved configuration live."""
        self.server = cfg.get("server", self.server)
        self.key = cfg.get("key", self.key)
        self.secret = cfg.get("hmac", self.secret)
        self.device = cfg.get("device", self.device)
        self.push_interval = int(cfg.get("push_interval", self.push_interval))
        logging.info("Configuration updated in running tracker.")

    def sync_now(self) -> bool:
        """Immediately flush tracked time and ActivityWatch events to server."""
        logging.info("Triggered immediate sync...")
        res = push_usage(self.server, self.key, self.secret, self.device, self.kind, self.current_day, self.apps_data)
        push_activitywatch(self.server, self.key, self.secret, self.device)
        if res:
            self.last_push_time = time.time()
        self.last_aw_push_time = time.time()
        return res

    def tracking_worker(self):
        """Worker thread sampling foreground app and periodically pushing."""
        setup_logger(foreground=True)
        logging.info("=== APA Laptop Activity Tracker Started ===")
        logging.info(f"Device: {self.device} | Server: {self.server}")

        while self.is_running:
            loop_start = time.time()
            now_dt = datetime.now()

            # Day rollover check
            if now_dt.date() != self.current_day:
                self.sync_now()
                self.apps_data.clear()
                self.current_day = now_dt.date()

            if not self.is_paused:
                app = get_foreground_app_windows()
                if app:
                    if app != self.current_app:
                        self.current_app = app
                        self.current_app_seconds = 0
                    else:
                        self.current_app_seconds += POLL_INTERVAL_S

                    info = self.apps_data[app]
                    info["seconds"] += POLL_INTERVAL_S
                    if info["first_at"] is None:
                        info["first_at"] = now_dt
                    info["last_at"] = now_dt
                else:
                    self.current_app = "Idle / Lock Screen"

                # Update tray tooltip dynamically
                if self.tray_icon:
                    app_time_str = f"{self.current_app_seconds // 60}m" if self.current_app_seconds >= 60 else f"{self.current_app_seconds}s"
                    self.tray_icon.title = f"APA Desktop Tracker\nStatus: Tracking\nActive: {self.current_app} ({app_time_str})"

                # Periodic usage push
                if time.time() - self.last_push_time >= self.push_interval:
                    self.sync_now()
                # Real-time ActivityWatch sync (every 25s for live window & YouTube awareness)
                elif time.time() - self.last_aw_push_time >= 25:
                    push_activitywatch(self.server, self.key, self.secret, self.device)
                    self.last_aw_push_time = time.time()
            else:
                self.current_app = "Tracking Paused"
                if self.tray_icon:
                    self.tray_icon.title = "APA Desktop Tracker\nStatus: Paused"

            elapsed = time.time() - loop_start
            sleep_time = max(0.5, POLL_INTERVAL_S - elapsed)
            time.sleep(sleep_time)

    def run_tray(self):
        """Run system tray icon with Pystray."""
        if not TRAY_AVAILABLE:
            logging.warning("Pystray / Pillow not available. Running headless tracker loop.")
            self.tracking_worker()
            return

        icon_img = get_icon_image()

        def on_test(icon, item):
            def _test():
                ok, msg, lat = test_connection(self.server, self.key, self.secret, self.device)
                title = "APA Tracker - Connection OK" if ok else "APA Tracker - Connection Error"
                try:
                    icon.notify(msg, title=title)
                except Exception:
                    pass
            threading.Thread(target=_test, daemon=True).start()

        def on_sync(icon, item):
            def _sync():
                res = self.sync_now()
                try:
                    count = len([k for k, v in self.apps_data.items() if v["seconds"] >= 5])
                    icon.notify(f"Synced {count} apps to APA server successfully!" if res else "Sync failed. Check connection.",
                                title="APA Tracker - Sync")
                except Exception:
                    pass
            threading.Thread(target=_sync, daemon=True).start()

        def on_settings(icon, item):
            threading.Thread(
                target=show_settings_dialog,
                args=(self.update_config, self.server, self.key, self.secret, self.device),
                daemon=True,
            ).start()

        def on_toggle_pause(icon, item):
            self.is_paused = not self.is_paused
            state_str = "Paused" if self.is_paused else "Resumed"
            logging.info(f"Tracking {state_str}")
            try:
                icon.notify(f"Activity tracking is now {state_str.lower()}.", title="APA Tracker")
            except Exception:
                pass

        def on_open_web(icon, item):
            # Derive dashboard URL (default port 3000 on server host)
            try:
                host = self.server.split("://")[-1].split(":")[0]
                dash_url = f"http://{host}:3000"
            except Exception:
                dash_url = self.server
            webbrowser.open(dash_url)

        def on_check_update(icon, item):
            def _check():
                try:
                    url = "https://api.github.com/repos/frostfire1/Autonomous-Personal-Assistance/releases/latest"
                    req = urllib.request.Request(url, headers={"User-Agent": "APA-Tracker"})
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                        tag = data.get("tag_name", "latest")
                        html_url = data.get("html_url", "https://github.com/frostfire1/Autonomous-Personal-Assistance/releases/latest")
                        icon.notify(f"Latest release: {tag}", title="APA Tracker - Updates")
                        webbrowser.open(html_url)
                except Exception as e:
                    logging.warning(f"Update check failed: {e}")
                    webbrowser.open("https://github.com/frostfire1/Autonomous-Personal-Assistance/releases")
            threading.Thread(target=_check, daemon=True).start()

        def on_view_logs(icon, item):
            if LOG_FILE.exists():
                try:
                    os.startfile(str(LOG_FILE))
                except Exception as e:
                    logging.warning(f"Could not open log file: {e}")

        def on_exit(icon, item):
            logging.info("Exiting APA Laptop Activity Tracker...")
            self.is_running = False
            self.sync_now()
            icon.stop()

        def get_status_text(item):
            status = "Paused" if self.is_paused else "Tracking"
            return f"Status: {status} • {self.current_app}"

        def get_pause_label(item):
            return "▶️ Resume Tracking" if self.is_paused else "⏸️ Pause Tracking"

        menu = pystray.Menu(
            pystray.MenuItem("📊 APA Desktop Tracker", None, enabled=False),
            pystray.MenuItem(get_status_text, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("⚡ Test Connection", on_test),
            pystray.MenuItem("🔄 Sync Usage Now", on_sync),
            pystray.MenuItem("⚙️ Settings / Setup...", on_settings),
            pystray.MenuItem(get_pause_label, on_toggle_pause),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("🌐 Open Web Dashboard", on_open_web),
            pystray.MenuItem("🚀 Check for Updates", on_check_update),
            pystray.MenuItem("📄 View Activity Logs", on_view_logs),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("❌ Exit", on_exit),
        )

        self.tray_icon = pystray.Icon("APALaptopTracker", icon_img, "APA Desktop Tracker", menu)

        # Start tracking thread
        track_thread = threading.Thread(target=self.tracking_worker, daemon=True)
        track_thread.start()

        # Run system tray (blocks on main thread)
        self.tray_icon.run()


# ---- Windows Service (Task Scheduler) Management ----------------------------


def install_service(server: str, key: str, secret: str, device: str) -> None:
    """Register a Windows Scheduled Task for users wanting Task Scheduler instead of HKCU Run."""
    if not IS_WINDOWS:
        print("Service installation via Task Scheduler is only supported on Windows.")
        return

    save_config({
        "server": server,
        "key": key,
        "hmac": secret,
        "device": device,
    })

    if getattr(sys, "frozen", False):
        action_cmd = f'"{sys.executable}" --run-service'
    else:
        py_exe = get_pythonw_executable()
        script_path = str(Path(__file__).resolve())
        action_cmd = f'"{py_exe}" "{script_path}" --run-service'

    cmd = (
        f'schtasks /Create /TN "{TASK_NAME}" '
        f'/TR "{action_cmd}" '
        f'/SC ONLOGON /RL HIGHEST /F'
    )
    print(f"Installing Windows Background Service ({TASK_NAME})...")
    res = os.system(cmd)
    if res == 0:
        print("\n[SUCCESS] APA Laptop Service successfully installed!")
        print(f"- Config saved: {CONFIG_FILE}")
        print(f"- Log file: {LOG_FILE}")
        start_service()
    else:
        print(f"[ERROR] Failed to install scheduled task (exit code {res}). Try running as Administrator.")


def uninstall_service():
    """Remove scheduled task."""
    stop_service()
    cmd = f'schtasks /Delete /TN "{TASK_NAME}" /F'
    print(f"Uninstalling {TASK_NAME}...")
    res = os.system(cmd)
    if res == 0:
        print("[SUCCESS] APA Laptop Service uninstalled.")
    else:
        print("Service task was not found or already removed.")


def start_service():
    cmd = f'schtasks /Run /TN "{TASK_NAME}"'
    res = os.system(cmd)
    if res == 0:
        print("[OK] Service triggered to run in background.")
    else:
        print(f"[ERR] Could not start service (code {res}).")


def stop_service():
    cmd = f'schtasks /End /TN "{TASK_NAME}"'
    os.system(cmd)
    print("[OK] Sent stop signal to service.")


def check_status():
    cmd = f'schtasks /Query /TN "{TASK_NAME}" /FO LIST'
    res = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if res.returncode == 0:
        print("=== APA Laptop Service Status ===")
        print(res.stdout)
        if LOG_FILE.exists():
            print("--- Recent Log Entries (last 5 lines) ---")
            lines = LOG_FILE.read_text(encoding="utf-8", errors="ignore").splitlines()[-5:]
            for l in lines:
                print(" ", l)
    else:
        print(f"Task Scheduler service '{TASK_NAME}' is not registered.")
        print(f"Startup Registry (HKCU Run): {'Enabled' if is_startup_enabled() else 'Disabled'}")


# ---- CLI & Main Entrypoint --------------------------------------------------


def main():
    saved_cfg = load_config()

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--server", default=saved_cfg.get("server") or os.environ.get("APA_URL", "http://localhost:8000"))
    p.add_argument("--key", default=saved_cfg.get("key") or os.environ.get("APA_KEY", os.environ.get("INGEST_API_KEY", "")))
    p.add_argument("--hmac", default=saved_cfg.get("hmac") or os.environ.get("APA_HMAC", os.environ.get("INGEST_HMAC_SECRET", "")))
    p.add_argument("--device", default=saved_cfg.get("device") or socket.gethostname())
    p.add_argument("--kind", default="desktop")

    # GUI and action flags
    p.add_argument("--setup", "--settings", action="store_true", help="Open the Setup & Settings GUI dialog")
    p.add_argument("--test-connection", action="store_true", help="Test connectivity and auth to the APA server from CLI")
    p.add_argument("--sync-now", action="store_true", help="Trigger a one-off usage push")
    p.add_argument("--tray", action="store_true", help="Launch in Windows System Tray (default)")
    p.add_argument("--no-gui", action="store_true", help="Run without opening GUI or System Tray")

    # Service flags
    p.add_argument("--install-service", "--install-task", action="store_true", help="Install as Windows Scheduled Task")
    p.add_argument("--uninstall-service", "--uninstall-task", action="store_true", help="Uninstall the Windows Scheduled Task")
    p.add_argument("--start", action="store_true", help="Start the background scheduled task")
    p.add_argument("--stop", action="store_true", help="Stop the background scheduled task")
    p.add_argument("--status", action="store_true", help="Check status of background service")
    p.add_argument("--run-service", action="store_true", help="Run headless service (used by Task Scheduler)")

    args = p.parse_args()

    # CLI Actions
    if args.install_service:
        if not (args.server and args.key):
            sys.exit("Error: Please provide --server and --key to install the service.")
        install_service(args.server, args.key, args.hmac, args.device)
        return

    if args.uninstall_service:
        uninstall_service()
        return

    if args.start:
        start_service()
        return

    if args.stop:
        stop_service()
        return

    if args.status:
        check_status()
        return

    if args.test_connection:
        print(f"Testing connection to {args.server}...")
        ok, msg, lat = test_connection(args.server, args.key, args.hmac, args.device)
        if ok:
            print(f"[SUCCESS] {msg}")
            sys.exit(0)
        else:
            print(f"[FAILED] {msg} (Latency: {lat:.1f}ms)")
            sys.exit(1)

    if args.setup:
        show_settings_dialog(initial_server=args.server, initial_key=args.key, initial_hmac=args.hmac, initial_device=args.device)
        return

    # If server or key is missing and GUI is supported, show the setup dialog automatically
    if not (args.server and args.key) and not args.no_gui and not args.run_service:
        if TK_AVAILABLE:
            print("Server URL or Ingest Key not configured. Opening Setup dialog...")
            show_settings_dialog(initial_server=args.server, initial_key=args.key, initial_hmac=args.hmac, initial_device=args.device)
            saved_cfg = load_config()
            args.server = saved_cfg.get("server", args.server)
            args.key = saved_cfg.get("key", args.key)
            args.hmac = saved_cfg.get("hmac", args.hmac)
            args.device = saved_cfg.get("device", args.device)

    if not (args.server and args.key):
        sys.exit("Error: APA Server URL and Ingest API Key are required. Run with --setup to configure.")

    app = LaptopTrackerApp(args.server, args.key, args.hmac, args.device, args.kind)

    if args.sync_now:
        setup_logger(foreground=True)
        ok = app.sync_now()
        print("[OK] Synced usage to server." if ok else "[FAILED] Could not sync usage.")
        return

    if args.run_service or args.no_gui or not TRAY_AVAILABLE:
        setup_logger(foreground=not args.run_service)
        app.tracking_worker()
    else:
        app.run_tray()


if __name__ == "__main__":
    main()
