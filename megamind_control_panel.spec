# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the local MegaMind desktop control panel.
# Build: pyinstaller megamind_control_panel.spec --clean

import sys as _sys

block_cipher = None

_is_mac = _sys.platform == "darwin"
_is_win = _sys.platform == "win32"

_pynput_hidden = []
if _is_win:
    _pynput_hidden = ["pynput.keyboard._win32", "pynput.mouse._win32"]
elif _is_mac:
    _pynput_hidden = ["pynput.keyboard._darwin", "pynput.mouse._darwin"]

_platform_hidden = []
if _is_win:
    _platform_hidden = ["rawinput_win"]
elif _is_mac:
    _platform_hidden = ["Quartz", "keyboard_capture_mac", "virtual_hid_mac"]

a = Analysis(
    ["control_panel.py"],
    pathex=[],
    binaries=[],
    datas=[("static", "static")],
    hiddenimports=[
        "screens",
        "clipboard_sync",
        # Uvicorn loads its event loop, protocol, and lifespan implementations dynamically.
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.loops.asyncio",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.http.h11_impl",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.protocols.websockets.websockets_impl",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
    ] + _platform_hidden + _pynput_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="MegaMind_Control_Panel",
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
    uac_admin=_is_win,
)

if _is_mac:
    app = BUNDLE(
        exe,
        name="MegaMind_Control_Panel.app",
        bundle_identifier="com.megamind.controlpanel",
    )