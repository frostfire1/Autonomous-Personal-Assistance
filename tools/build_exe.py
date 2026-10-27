#!/usr/bin/env python3
"""Build script for APA-Tracker.exe standalone Windows binary.

Produces a single-file, windowless Windows executable in `tools/dist/APA-Tracker.exe`
equipped with:
  - Windows System Tray icon
  - Setup & Settings GUI (Tkinter) with Server URL, Ingest Key, and Startup Setup
  - Real-time Connection & Auth Testing (with ms latency calculation)
  - Silent foreground tracking & periodic push to /ingest/usage
"""

import subprocess
import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TOOLS_DIR.parent
ICON_FILE = TOOLS_DIR / "tracker_icon.ico"
DIST_DIR = TOOLS_DIR / "dist"
BUILD_DIR = TOOLS_DIR / "build"
SCRIPT_TARGET = TOOLS_DIR / "laptop_watcher.py"


def main():
    print("=" * 60)
    print(" Building APA Desktop Tracker Standalone Executable (.exe)")
    print("=" * 60)

    if not ICON_FILE.exists():
        sys.exit(f"Error: Icon file not found at {ICON_FILE}")

    # Locate pyinstaller in venv or current python
    venv_pyinstaller = PROJECT_ROOT / "backend" / ".venv" / "Scripts" / "pyinstaller.exe"
    if venv_pyinstaller.exists():
        pyinstaller_cmd = str(venv_pyinstaller)
    else:
        pyinstaller_cmd = "pyinstaller"

    cmd = [
        pyinstaller_cmd,
        "--noconfirm",
        "--clean",
        "--onefile",
        "--windowed",
        "--name", "APA-Tracker",
        "--icon", str(ICON_FILE),
        "--add-data", f"{ICON_FILE};.",
        "--hidden-import", "pystray._win32",
        "--hidden-import", "PIL",
        "--hidden-import", "PIL.Image",
        "--hidden-import", "PIL.ImageDraw",
        "--hidden-import", "PIL.IcoImagePlugin",
        "--hidden-import", "tkinter",
        "--hidden-import", "tkinter.ttk",
        "--hidden-import", "tkinter.messagebox",
        "--distpath", str(DIST_DIR),
        "--workpath", str(BUILD_DIR),
        "--specpath", str(TOOLS_DIR),
        str(SCRIPT_TARGET),
    ]

    print(f"Running command:\n{' '.join(cmd)}\n")
    res = subprocess.run(cmd)

    if res.returncode == 0:
        exe_path = DIST_DIR / "APA-Tracker.exe"
        size_mb = exe_path.stat().st_size / (1024 * 1024) if exe_path.exists() else 0
        print("\n" + "=" * 60)
        print(" [SUCCESS] Executable built successfully!")
        print(f" Location: {exe_path}")
        print(f" File Size: {size_mb:.2f} MB")
        print("=" * 60)
        print("\nHow to use:")
        print("  1. Double-click APA-Tracker.exe to launch into your Windows System Tray.")
        print("  2. If first time running, the Setup dialog appears to enter Server URL & Ingest Key.")
        print("  3. Check 'Start automatically with Windows' to keep it running silently.")
    else:
        print(f"\n[ERROR] PyInstaller failed with exit code {res.returncode}")
        sys.exit(res.returncode)


if __name__ == "__main__":
    main()
