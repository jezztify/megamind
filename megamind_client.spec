# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for MegaMind_Client (Windows .exe / macOS binary for .pkg)
# Build: pyinstaller megamind_client.spec --clean

import sys as _sys

block_cipher = None

_is_mac = _sys.platform == "darwin"
_is_win = _sys.platform == "win32"

_pynput_hidden = []
if _is_win:
    _pynput_hidden = ["pynput.keyboard._win32", "pynput.mouse._win32"]
elif _is_mac:
    _pynput_hidden = ["pynput.keyboard._darwin", "pynput.mouse._darwin"]

a = Analysis(
    ["client.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        "screens",
        "clipboard_sync",
    ] + _pynput_hidden,
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
    name="MegaMind_Client",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    uac_admin=_is_win,  # Windows: request admin for input injection
)
