"""
Cross-platform text-clipboard synchronization.

Each machine runs a monitor thread that watches its local clipboard. When the
clipboard changes locally, `on_local_change(text)` fires so the caller can send
it to peers. When a peer's clipboard arrives, `apply_remote(text)` writes it to
the local clipboard WITHOUT re-triggering a broadcast (loop prevention).

Loop prevention: a single `_shared` value records the last text we synced
(either sent or received). The monitor only reports a change when the live
clipboard differs from `_shared`; apply_remote updates `_shared` before writing,
so the value it writes is never reported back as a local change.

Text only — images/files are out of scope. Requires `pyperclip`
(Windows: built-in; macOS: pbcopy/pbpaste; Linux: xclip/xsel).
"""

import threading
import time

try:
    import pyperclip
except Exception:
    pyperclip = None

_shared = None              # last text synced in either direction
_lock = threading.Lock()


def available() -> bool:
    if pyperclip is None:
        return False
    # pyperclip raises if no clipboard mechanism is present (e.g. headless Linux).
    try:
        pyperclip.paste()
        return True
    except Exception:
        return False


def apply_remote(text: str) -> None:
    """Write a peer's clipboard locally without echoing it back as a change."""
    global _shared
    if pyperclip is None or text is None:
        return
    with _lock:
        _shared = text
    try:
        pyperclip.copy(text)
    except Exception:
        pass


def start_monitor(on_local_change, interval: float = 0.6):
    """Begin watching the local clipboard. Returns the monitor Thread (daemon)."""
    if pyperclip is None:
        return None

    def loop():
        global _shared
        # Seed _shared with the current clipboard so we don't broadcast whatever
        # happened to be on the clipboard at startup.
        try:
            with _lock:
                _shared = pyperclip.paste()
        except Exception:
            pass

        while True:
            time.sleep(interval)
            try:
                cur = pyperclip.paste()
            except Exception:
                continue
            if not cur:
                continue
            with _lock:
                changed = cur != _shared
                if changed:
                    _shared = cur
            if changed:
                try:
                    on_local_change(cur)
                except Exception:
                    pass

    t = threading.Thread(target=loop, daemon=True, name="clipboard-monitor")
    t.start()
    return t
