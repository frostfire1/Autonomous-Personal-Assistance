# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['C:/Users/cipto/Documents/a/Autonomous-Personal-Assistance/tools/laptop_watcher.py'],
    pathex=[],
    binaries=[],
    datas=[('C:/Users/cipto/Documents/a/Autonomous-Personal-Assistance/tools/tracker_icon.ico', '.')],
    hiddenimports=['pystray._win32', 'PIL', 'PIL.Image', 'PIL.ImageDraw', 'PIL.IcoImagePlugin', 'tkinter', 'tkinter.ttk', 'tkinter.messagebox'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='APA-Tracker',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['C:/Users/cipto/Documents/a/Autonomous-Personal-Assistance/tools/tracker_icon.ico'],
)
