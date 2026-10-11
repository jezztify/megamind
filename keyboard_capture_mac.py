"""
keyboard_capture_mac — Low-level keyboard capture on macOS via Quartz CGEventTap.

pynput's keyboard listener on macOS may miss certain key events when modifier
keys are held (e.g. Ctrl+Arrow), because macOS intercepts them for Mission
Control shortcuts before pynput's event tap processes them.

This module creates a CGEventTap at kCGHIDEventTap (the lowest user-space
interception point), which receives ALL keyboard events before the system
dispatches them to Mission Control or other consumers.

Usage:
    from keyboard_capture_mac import MacKeyboardCapture

    cap = MacKeyboardCapture(on_press=my_press_cb, on_release=my_release_cb, suppress=True)
    cap.start()
    ...
    cap.stop()

The callbacks receive pynput-compatible key objects (keyboard.Key or
keyboard.KeyCode) so existing handlers work unchanged.
"""

from __future__ import annotations

import threading
from typing import Callable, Optional

import Quartz
from Quartz import (
    CGEventTapCreate,
    CGEventTapEnable,
    CGEventGetIntegerValueField,
    CGEventGetFlags,
    kCGHIDEventTap,
    kCGHeadInsertEventTap,
    kCGEventTapOptionDefault,
    kCGEventKeyDown,
    kCGEventKeyUp,
    kCGEventFlagsChanged,
    kCGKeyboardEventKeycode,
)
from Foundation import CFMachPortCreateRunLoopSource, CFRunLoopGetCurrent, CFRunLoopAddSource, CFRunLoopRun, CFRunLoopStop, kCFRunLoopDefaultMode

from pynput.keyboard import Key, KeyCode


# ---------------------------------------------------------------------------
# macOS virtual keycode → pynput Key mapping
# ---------------------------------------------------------------------------
_KEYCODE_TO_PYNPUT: dict[int, Key] = {
    # Arrow keys
    123: Key.left,
    124: Key.right,
    125: Key.down,
    126: Key.up,
    # Modifiers
    54: Key.cmd_r,
    55: Key.cmd,      # cmd_l
    56: Key.shift,    # shift_l
    57: Key.caps_lock,
    58: Key.alt,      # alt_l (option)
    59: Key.ctrl,     # ctrl_l
    60: Key.shift_r,
    61: Key.alt_r,    # alt_r (option_r)
    62: Key.ctrl_r,
    63: Key.cmd,      # fn (mapped to cmd for compat)
    # Function keys
    122: Key.f1,
    120: Key.f2,
    99: Key.f3,
    118: Key.f4,
    96: Key.f5,
    97: Key.f6,
    98: Key.f7,
    100: Key.f8,
    101: Key.f9,
    109: Key.f10,
    103: Key.f11,
    111: Key.f12,
    # Navigation
    115: Key.home,
    119: Key.end,
    116: Key.page_up,
    121: Key.page_down,
    117: Key.delete,   # forward delete
    51: Key.backspace,
    # Misc
    36: Key.enter,
    48: Key.tab,
    49: Key.space,
    53: Key.esc,
}

# macOS virtual keycode → character (for printable keys, US layout baseline)
_KEYCODE_TO_CHAR: dict[int, str] = {
    0: "a", 1: "s", 2: "d", 3: "f", 4: "h", 5: "g", 6: "z", 7: "x",
    8: "c", 9: "v", 11: "b", 12: "q", 13: "w", 14: "e", 15: "r",
    16: "y", 17: "t", 18: "1", 19: "2", 20: "3", 21: "4", 22: "6",
    23: "5", 24: "=", 25: "9", 26: "7", 27: "-", 28: "8", 29: "0",
    30: "]", 31: "o", 32: "u", 33: "[", 34: "i", 35: "p",
    37: "l", 38: "j", 39: "'", 40: "k", 41: ";", 42: "\\",
    43: ",", 44: "/", 45: "n", 46: "m", 47: ".",
    50: "`",
}

# Modifier flag bits in CGEventFlags
_MOD_FLAG_SHIFT   = 0x00020000
_MOD_FLAG_CTRL    = 0x00040000
_MOD_FLAG_ALT     = 0x00080000
_MOD_FLAG_CMD     = 0x00100000

# Modifier keycodes
_MODIFIER_KEYCODES = {54, 55, 56, 57, 58, 59, 60, 61, 62, 63}


def _keycode_to_pynput(keycode: int):
    """Convert a macOS virtual keycode to a pynput Key or KeyCode."""
    if keycode in _KEYCODE_TO_PYNPUT:
        return _KEYCODE_TO_PYNPUT[keycode]
    if keycode in _KEYCODE_TO_CHAR:
        return KeyCode.from_char(_KEYCODE_TO_CHAR[keycode])
    # Unknown key — return a KeyCode with vk set
    return KeyCode.from_vk(keycode)


class MacKeyboardCapture:
    """Low-level macOS keyboard capture using CGEventTap at kCGHIDEventTap."""

    def __init__(
        self,
        on_press: Callable,
        on_release: Callable,
        suppress: bool = False,
    ):
        self._on_press = on_press
        self._on_release = on_release
        self._suppress = suppress
        self._thread: Optional[threading.Thread] = None
        self._run_loop = None
        self._running = threading.Event()
        # Track modifier state to detect press vs release from FlagsChanged
        self._modifier_state: dict[int, bool] = {}

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="MacKbdCapture")
        self._thread.start()
        self._running.wait(timeout=5.0)

    def stop(self) -> None:
        if self._run_loop is not None:
            CFRunLoopStop(self._run_loop)
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        self._run_loop = None

    def _run(self) -> None:
        event_mask = (
            (1 << kCGEventKeyDown) |
            (1 << kCGEventKeyUp) |
            (1 << kCGEventFlagsChanged)
        )

        tap = CGEventTapCreate(
            kCGHIDEventTap,
            kCGHeadInsertEventTap,
            kCGEventTapOptionDefault,
            event_mask,
            self._tap_callback,
            None,
        )

        if tap is None:
            print("[keyboard_capture_mac] ERROR: Failed to create event tap.")
            print("  Grant Accessibility permission to this app in:")
            print("  System Settings > Privacy & Security > Accessibility")
            self._running.set()
            return

        source = CFMachPortCreateRunLoopSource(None, tap, 0)
        self._run_loop = CFRunLoopGetCurrent()
        CFRunLoopAddSource(self._run_loop, source, kCFRunLoopDefaultMode)
        CGEventTapEnable(tap, True)

        self._running.set()
        CFRunLoopRun()

    def _tap_callback(self, proxy, event_type, event, refcon):
        """CGEventTap callback — fires for every keyboard event."""
        # If the tap gets disabled (system timeout), re-enable it
        if event_type == Quartz.kCGEventTapDisabledByTimeout:
            CGEventTapEnable(proxy, True)
            return event

        if event_type == Quartz.kCGEventTapDisabledByUserInput:
            CGEventTapEnable(proxy, True)
            return event

        keycode = CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode)

        if event_type == kCGEventFlagsChanged:
            # Modifier key: determine press or release from whether the
            # keycode's modifier flag is now set.
            was_down = self._modifier_state.get(keycode, False)
            # A simple heuristic: if the key was not previously tracked as down,
            # it's a press; otherwise it's a release.
            # More robustly, check the actual flags for this specific modifier.
            is_down = self._is_modifier_down(keycode, event)
            self._modifier_state[keycode] = is_down

            key = _keycode_to_pynput(keycode)
            if is_down and not was_down:
                self._on_press(key)
            elif not is_down and was_down:
                self._on_release(key)

        elif event_type == kCGEventKeyDown:
            key = _keycode_to_pynput(keycode)
            self._on_press(key)

        elif event_type == kCGEventKeyUp:
            key = _keycode_to_pynput(keycode)
            self._on_release(key)

        # Suppress: return None to eat the event, or return event to pass through
        if self._suppress:
            return None
        return event

    def _is_modifier_down(self, keycode: int, event) -> bool:
        """Check if the modifier for the given keycode is currently pressed."""
        flags = CGEventGetFlags(event)
        if keycode in (59, 62):  # ctrl_l, ctrl_r
            return bool(flags & _MOD_FLAG_CTRL)
        if keycode in (56, 60):  # shift_l, shift_r
            return bool(flags & _MOD_FLAG_SHIFT)
        if keycode in (58, 61):  # alt_l, alt_r
            return bool(flags & _MOD_FLAG_ALT)
        if keycode in (54, 55):  # cmd_r, cmd_l
            return bool(flags & _MOD_FLAG_CMD)
        if keycode == 57:  # caps_lock (toggle)
            return bool(flags & 0x00010000)
        return False
