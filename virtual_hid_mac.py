"""
virtual_hid_mac — inject keystrokes on macOS through a *real* HID keyboard.

Unlike Quartz CGEvents (which sit above the HID layer and are treated as
"synthetic" — they don't highlight on the Accessibility Keyboard, don't reliably
fire Mission Control shortcuts, and are rejected by secure input fields), this
module posts genuine USB-HID keyboard input reports through the
Karabiner-DriverKit-VirtualHIDDevice daemon. Those events enter the system at the
HID layer and are indistinguishable from a physical keyboard.

Requirements (macOS):
    Install the notarized driver package, then approve the system extension in
    System Settings > Privacy & Security:
        https://github.com/pqrs-org/Karabiner-DriverKit-VirtualHIDDevice

If the driver/daemon is not present, available() returns False and the caller
should fall back to its previous (pynput/Quartz) injection path.

Report format (generic keyboard, matches the driver's
`pqrs::karabiner::driverkit::virtual_hid_device_driver::hid_report::keyboard_input`):
    byte 0     : modifier bitmask (see _MOD_BITS)
    byte 1     : reserved (0)
    bytes 2..32: up to 30 simultaneously-held key usage codes (we use a slice)

We keep an in-memory snapshot of held keys + modifier state and push a fresh,
complete report on every change — exactly how a hardware keyboard reports state.
"""

from __future__ import annotations

import os
import socket
import struct
import threading
from typing import Optional


# ---------------------------------------------------------------------------
# Driver socket
# ---------------------------------------------------------------------------
# The Karabiner VirtualHIDDevice daemon listens on a UNIX domain socket. The
# path has been stable across recent driver versions; allow override via env for
# forward-compat.
_SOCKET_PATH = os.environ.get(
    "MEGAMIND_VHID_SOCKET",
    "/Library/Application Support/org.pqrs/tmp/rootonly/"
    "vhidd_server/vhidd_client.sock",
)

# Generic keyboard input report: report ID 1, then modifier, reserved, keys[].
# We cap simultaneous keys; 6 is the boot-protocol limit and plenty for typing.
_REPORT_ID = 1
_MAX_KEYS = 6


# ---------------------------------------------------------------------------
# Modifier bits (HID Usage Page 0x07 modifier byte)
# ---------------------------------------------------------------------------
_MOD_LCTRL = 0x01
_MOD_LSHIFT = 0x02
_MOD_LALT = 0x04
_MOD_LGUI = 0x08
_MOD_RCTRL = 0x10
_MOD_RSHIFT = 0x20
_MOD_RALT = 0x40
_MOD_RGUI = 0x80

# Map a forwarded "special" key name -> modifier bit. The server forwards the
# left/right variants; pynput's generic names (ctrl/shift/alt/cmd) map to left.
_MODIFIER_NAMES: dict[str, int] = {
    "ctrl": _MOD_LCTRL, "ctrl_l": _MOD_LCTRL, "ctrl_r": _MOD_RCTRL,
    "shift": _MOD_LSHIFT, "shift_l": _MOD_LSHIFT, "shift_r": _MOD_RSHIFT,
    "alt": _MOD_LALT, "alt_l": _MOD_LALT, "alt_r": _MOD_RALT,
    "alt_gr": _MOD_RALT,
    "cmd": _MOD_LGUI, "cmd_l": _MOD_LGUI, "cmd_r": _MOD_RGUI,
}


# ---------------------------------------------------------------------------
# HID Usage IDs (Usage Page 0x07, Keyboard/Keypad)
# ---------------------------------------------------------------------------
# Named "special" keys forwarded by the server.
_SPECIAL_USAGE: dict[str, int] = {
    "enter": 0x28, "return": 0x28,
    "esc": 0x29, "escape": 0x29,
    "backspace": 0x2A,
    "tab": 0x2B,
    "space": 0x2C,
    "caps_lock": 0x39,
    "f1": 0x3A, "f2": 0x3B, "f3": 0x3C, "f4": 0x3D, "f5": 0x3E, "f6": 0x3F,
    "f7": 0x40, "f8": 0x41, "f9": 0x42, "f10": 0x43, "f11": 0x44, "f12": 0x45,
    "print_screen": 0x46, "scroll_lock": 0x47, "pause": 0x48,
    "insert": 0x49, "home": 0x4A, "page_up": 0x4B,
    "delete": 0x4C, "end": 0x4D, "page_down": 0x4E,
    "right": 0x4F, "left": 0x50, "down": 0x51, "up": 0x52,
    "num_lock": 0x53,
    "menu": 0x65,
}

# Unshifted character -> usage code. Letters resolve case via the Shift modifier
# (forwarded separately), so we map both cases to the same physical key.
_CHAR_USAGE: dict[str, int] = {}
for _i, _c in enumerate("abcdefghijklmnopqrstuvwxyz"):
    _CHAR_USAGE[_c] = 0x04 + _i           # a..z -> 0x04..0x1D
# Uppercase letters resolve to the same physical key; case comes from the
# separately-forwarded Shift modifier (usage_for lowercases as a fallback too).
# Digit row (unshifted). 1..9 -> 0x1E..0x26, 0 -> 0x27.
for _i, _c in enumerate("123456789"):
    _CHAR_USAGE[_c] = 0x1E + _i
_CHAR_USAGE["0"] = 0x27
# Punctuation (unshifted glyphs on a US ANSI layout).
_CHAR_USAGE.update({
    "-": 0x2D, "=": 0x2E, "[": 0x2F, "]": 0x30, "\\": 0x31,
    ";": 0x33, "'": 0x34, "`": 0x35, ",": 0x36, ".": 0x37, "/": 0x38,
    " ": 0x2C, "\t": 0x2B, "\n": 0x28, "\r": 0x28,
})

# Shifted glyph -> the unshifted physical key that produces it (US ANSI). The
# server's _SHIFT_MAP may forward these pre-shifted; we reverse them to a base
# key and let the separately-forwarded Shift modifier do the rest.
_SHIFTED_TO_BASE: dict[str, str] = {
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5",
    "^": "6", "&": "7", "*": "8", "(": "9", ")": "0",
    "~": "`", "_": "-", "+": "=", "{": "[", "}": "]",
    "|": "\\", ":": ";", '"': "'", "<": ",", ">": ".", "?": "/",
}

# Windows VK codes (server sends these for the numpad) -> usage codes.
_VK_USAGE: dict[int, int] = {
    96: 0x62, 97: 0x59, 98: 0x5A, 99: 0x5B, 100: 0x5C, 101: 0x5D,  # numpad 0-5
    102: 0x5E, 103: 0x5F, 104: 0x60, 105: 0x61,                    # numpad 6-9
    106: 0x55,  # multiply
    107: 0x57,  # add
    109: 0x56,  # subtract
    110: 0x63,  # decimal
    111: 0x54,  # divide
}


def usage_for(key_data: dict) -> Optional[int]:
    """Resolve a forwarded key dict -> HID usage code, or None if it is a pure
    modifier (handled via the modifier byte) or unmappable."""
    kind = key_data.get("kind")
    if kind == "special":
        name = key_data.get("name")
        if name in _MODIFIER_NAMES:
            return None  # modifier — not a key usage
        return _SPECIAL_USAGE.get(name)
    if kind == "char":
        ch = key_data.get("char")
        if ch is None:
            return None
        if ch in _CHAR_USAGE:
            return _CHAR_USAGE[ch]
        base = _SHIFTED_TO_BASE.get(ch)
        if base is not None:
            return _CHAR_USAGE.get(base)
        # Letters arrive as their pressed case; normalize to lowercase key.
        if ch.lower() in _CHAR_USAGE:
            return _CHAR_USAGE[ch.lower()]
        return None
    if kind == "vk":
        return _VK_USAGE.get(key_data.get("vk"))
    return None


def modifier_for(key_data: dict) -> Optional[int]:
    """Return the modifier bit for a forwarded modifier key, else None."""
    if key_data.get("kind") == "special":
        return _MODIFIER_NAMES.get(key_data.get("name"))
    return None


# ---------------------------------------------------------------------------
# Device — owns the socket and the current report state
# ---------------------------------------------------------------------------
class _VirtualKeyboard:
    def __init__(self) -> None:
        self._sock: Optional[socket.socket] = None
        self._lock = threading.Lock()
        self._mods = 0                 # current modifier bitmask
        self._keys: list[int] = []     # currently-held key usage codes (order = press order)

    # -- connection --------------------------------------------------------
    def connect(self) -> bool:
        if self._sock is not None:
            return True
        if not os.path.exists(_SOCKET_PATH):
            return False
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(_SOCKET_PATH)
            self._sock = s
            return True
        except OSError:
            self._sock = None
            return False

    def close(self) -> None:
        with self._lock:
            if self._sock is not None:
                try:
                    # Release everything before dropping the connection so no
                    # key is left stuck down in the driver.
                    self._mods = 0
                    self._keys.clear()
                    self._send_report_locked()
                    self._sock.close()
                except OSError:
                    pass
                self._sock = None

    # -- report ------------------------------------------------------------
    def _build_report(self) -> bytes:
        keys = self._keys[:_MAX_KEYS]
        keys = keys + [0] * (_MAX_KEYS - len(keys))
        # report id, modifier, reserved, keys[_MAX_KEYS]
        return struct.pack(
            "BBB" + "B" * _MAX_KEYS,
            _REPORT_ID, self._mods & 0xFF, 0, *keys,
        )

    def _send_report_locked(self) -> None:
        if self._sock is None:
            return
        report = self._build_report()
        # Length-prefixed frame (uint32 LE length, then payload) — the daemon
        # reads framed messages.
        frame = struct.pack("<I", len(report)) + report
        try:
            self._sock.sendall(frame)
        except OSError:
            # Connection dropped (driver restarted / extension reloaded).
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    # -- key ops -----------------------------------------------------------
    def press(self, key_data: dict) -> bool:
        """Apply a key/modifier press. Returns True if handled by the HID path."""
        with self._lock:
            if self._sock is None and not self.connect():
                return False
            mod = modifier_for(key_data)
            if mod is not None:
                self._mods |= mod
                self._send_report_locked()
                return True
            usage = usage_for(key_data)
            if usage is None:
                return False
            if usage not in self._keys:
                self._keys.append(usage)
            self._send_report_locked()
            return True

    def release(self, key_data: dict) -> bool:
        with self._lock:
            if self._sock is None and not self.connect():
                return False
            mod = modifier_for(key_data)
            if mod is not None:
                self._mods &= ~mod
                self._send_report_locked()
                return True
            usage = usage_for(key_data)
            if usage is None:
                return False
            if usage in self._keys:
                self._keys.remove(usage)
            self._send_report_locked()
            return True

    def release_all(self) -> None:
        with self._lock:
            self._mods = 0
            self._keys.clear()
            self._send_report_locked()


_kbd = _VirtualKeyboard()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def available() -> bool:
    """True if the Karabiner VirtualHIDDevice daemon socket is reachable."""
    return _kbd.connect()


def press(key_data: dict) -> bool:
    """Press a forwarded key via the virtual HID device. Returns False (and does
    nothing) if the device is unavailable so the caller can fall back."""
    return _kbd.press(key_data)


def release(key_data: dict) -> bool:
    return _kbd.release(key_data)


def release_all() -> None:
    """Release every held key/modifier — call on disconnect to avoid stuck keys."""
    _kbd.release_all()


def close() -> None:
    _kbd.close()
