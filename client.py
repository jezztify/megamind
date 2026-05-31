"""
MegaMind Client — runs on any machine that should be controlled from the server.

Usage:
    python client.py http://<server-ip>:8080
"""

import asyncio
import json
import platform
import sys
import threading
import time

import websockets
from pynput import keyboard, mouse

import screens
import clipboard_sync

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EDGE_MARGIN: int = 3          # pixels from screen edge to trigger a return
POLL_INTERVAL: float = 1 / 60  # 60 fps edge polling

# macOS: use Quartz CGEventPost for smooth injection (avoids CGWarp snap-back)
_quartz = None
if platform.system() == "Darwin":
    try:
        import Quartz as _quartz
    except ImportError:
        pass

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

active: bool = False           # True when this client is receiving control
return_edge: str = ""          # which edge to watch for returning to server

_mouse_ctrl = mouse.Controller()
_keyboard_ctrl = keyboard.Controller()

_injecting: threading.Event = threading.Event()  # set while injecting to suppress re-capture

# Client-side authoritative cursor position. Seeded from the entry point on
# set_active, then advanced by incoming relative deltas. We never read the OS
# cursor position per-event (that lags and causes stutter), and clicks/scroll
# happen at this tracked position rather than at stale server coordinates.
_cur_x: float = 0.0
_cur_y: float = 0.0

# Virtual desktop bounds (span all monitors); origin may be negative.
v_min_x: int = 0
v_min_y: int = 0
screen_width: int = 0
screen_height: int = 0

_ws_connection = None          # active websocket
_edge_monitor_task: asyncio.Task | None = None
_loop: asyncio.AbstractEventLoop | None = None  # for thread-safe clipboard sends

# macOS mouse-button state, needed for proper drag + multi-click injection.
_buttons_down: set = set()     # button names currently pressed
_last_click_time: float = 0.0
_last_click_pos: tuple = (0.0, 0.0)
_last_click_button: str = ""
_click_state: int = 1          # 1=single, 2=double, 3=triple (kCGMouseEventClickState)
DOUBLE_CLICK_SECONDS: float = 0.5
DOUBLE_CLICK_DIST: int = 6

if _quartz is not None:
    # name -> (down event, up event, drag event, button constant)
    _MAC_BTN = {
        "left":   (_quartz.kCGEventLeftMouseDown,  _quartz.kCGEventLeftMouseUp,
                   _quartz.kCGEventLeftMouseDragged,  _quartz.kCGMouseButtonLeft),
        "right":  (_quartz.kCGEventRightMouseDown, _quartz.kCGEventRightMouseUp,
                   _quartz.kCGEventRightMouseDragged, _quartz.kCGMouseButtonRight),
        "middle": (_quartz.kCGEventOtherMouseDown, _quartz.kCGEventOtherMouseUp,
                   _quartz.kCGEventOtherMouseDragged, _quartz.kCGMouseButtonCenter),
    }


def _on_local_clipboard(text: str) -> None:
    """Clipboard monitor thread: local clipboard changed → send to server."""
    if _loop is None or _ws_connection is None:
        print(f"[client] clipboard changed but not connected — not sending")
        return
    print(f"[client] clipboard changed locally ({len(text)} chars) → sending")
    asyncio.run_coroutine_threadsafe(_send_clipboard(text), _loop)


async def _send_clipboard(text: str) -> None:
    ws = _ws_connection
    if ws is None:
        return
    try:
        await ws.send(json.dumps({"type": "clipboard", "text": text}))
    except Exception as e:
        print(f"[client] clipboard send failed: {e}")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def deserialize_key(data: dict):
    kind = data.get("kind")
    if kind == "char":
        return keyboard.KeyCode.from_char(data["char"])
    elif kind == "vk":
        return keyboard.KeyCode.from_vk(data["vk"])
    elif kind == "special":
        return getattr(keyboard.Key, data["name"], None)
    return None


def button_from_name(name: str) -> mouse.Button:
    mapping = {
        "left":   mouse.Button.left,
        "right":  mouse.Button.right,
        "middle": mouse.Button.middle,
    }
    return mapping.get(name, mouse.Button.left)


# ---------------------------------------------------------------------------
# Input injection
# ---------------------------------------------------------------------------

def _clamp_cursor() -> None:
    global _cur_x, _cur_y
    _cur_x = max(float(v_min_x), min(v_min_x + screen_width - 1, _cur_x))
    _cur_y = max(float(v_min_y), min(v_min_y + screen_height - 1, _cur_y))


def _mac_post(event_type, button_const, dx: int = 0, dy: int = 0,
              click_state: int = 0) -> None:
    """Post a Quartz mouse event at the tracked position."""
    evt = _quartz.CGEventCreateMouseEvent(
        None, event_type, _quartz.CGPointMake(_cur_x, _cur_y), button_const)
    if dx or dy:
        _quartz.CGEventSetIntegerValueField(evt, _quartz.kCGMouseEventDeltaX, int(dx))
        _quartz.CGEventSetIntegerValueField(evt, _quartz.kCGMouseEventDeltaY, int(dy))
    if click_state:
        _quartz.CGEventSetIntegerValueField(evt, _quartz.kCGMouseEventClickState, click_state)
    _quartz.CGEventPost(_quartz.kCGHIDEventTap, evt)


def _place_cursor(dx: int = 0, dy: int = 0) -> None:
    """Move the OS cursor to the tracked (_cur_x, _cur_y) position (no buttons)."""
    if _quartz is not None:
        _mac_post(_quartz.kCGEventMouseMoved, _quartz.kCGMouseButtonLeft, dx, dy)
    else:
        _injecting.set()
        try:
            _mouse_ctrl.position = (int(_cur_x), int(_cur_y))
        finally:
            _injecting.clear()


def inject_mouse_move_rel(dx: int, dy: int) -> None:
    global _cur_x, _cur_y
    _cur_x += dx
    _cur_y += dy
    _clamp_cursor()

    if _quartz is not None and _buttons_down:
        # A button is held: macOS needs a *drag* event (not a plain move) for the
        # drag to track, otherwise it snaps to the release point on mouse-up.
        for name in ("left", "right", "middle"):
            if name in _buttons_down:
                _, _, drag_t, btn = _MAC_BTN[name]
                _mac_post(drag_t, btn, dx, dy)
                return
    _place_cursor(dx, dy)


def inject_mouse_click(button_name: str, pressed: bool) -> None:
    global _click_state, _last_click_time, _last_click_pos, _last_click_button

    if _quartz is not None:
        down_t, up_t, _, btn = _MAC_BTN.get(button_name, _MAC_BTN["left"])
        if pressed:
            # Compute multi-click state: consecutive clicks of the same button,
            # close in time and space, increment the click state (1->2->3) so
            # macOS recognizes double/triple clicks.
            now = time.time()
            dist = abs(_cur_x - _last_click_pos[0]) + abs(_cur_y - _last_click_pos[1])
            if (button_name == _last_click_button
                    and now - _last_click_time < DOUBLE_CLICK_SECONDS
                    and dist < DOUBLE_CLICK_DIST):
                _click_state += 1
            else:
                _click_state = 1
            _last_click_time = now
            _last_click_pos = (_cur_x, _cur_y)
            _last_click_button = button_name
            _buttons_down.add(button_name)
            _mac_post(down_t, btn, click_state=_click_state)
        else:
            _buttons_down.discard(button_name)
            _mac_post(up_t, btn, click_state=_click_state)
        return

    # Non-macOS: pynput handles drag/double-click natively via the OS.
    _place_cursor()
    _injecting.set()
    try:
        b = button_from_name(button_name)
        if pressed:
            _mouse_ctrl.press(b)
        else:
            _mouse_ctrl.release(b)
    finally:
        _injecting.clear()


def inject_mouse_scroll(dx: float, dy: float) -> None:
    # Scroll worked already; keep it on pynput to avoid a direction regression.
    _place_cursor()
    _injecting.set()
    try:
        _mouse_ctrl.scroll(int(dx), int(dy))
    finally:
        _injecting.clear()


def inject_key_press(key_data: dict) -> None:
    key = deserialize_key(key_data)
    if key is None:
        return
    _injecting.set()
    try:
        _keyboard_ctrl.press(key)
    finally:
        _injecting.clear()


def inject_key_release(key_data: dict) -> None:
    key = deserialize_key(key_data)
    if key is None:
        return
    _injecting.set()
    try:
        _keyboard_ctrl.release(key)
    finally:
        _injecting.clear()


def warp_cursor(x: int, y: int) -> None:
    global _cur_x, _cur_y
    _cur_x, _cur_y = float(x), float(y)
    _clamp_cursor()
    _place_cursor()


# ---------------------------------------------------------------------------
# Edge monitor
# ---------------------------------------------------------------------------

async def edge_monitor_loop(ws) -> None:
    """Poll cursor position and send switch_to_server when return_edge is hit."""
    global active

    while active:
        await asyncio.sleep(POLL_INTERVAL)

        if not active:
            break

        cx, cy = _cur_x, _cur_y

        hit = False
        if return_edge == "left" and cx <= v_min_x + EDGE_MARGIN:
            hit = True
        elif return_edge == "right" and cx >= v_min_x + screen_width - EDGE_MARGIN - 1:
            hit = True
        elif return_edge == "top" and cy <= v_min_y + EDGE_MARGIN:
            hit = True
        elif return_edge == "bottom" and cy >= v_min_y + screen_height - EDGE_MARGIN - 1:
            hit = True

        if hit:
            active = False
            msg = json.dumps({
                "type": "switch_to_server",
                "x": cx,
                "y": cy,
            })
            try:
                await ws.send(msg)
                print("[client] Returned control to server")
            except Exception as e:
                print(f"[client] Failed to send switch_to_server: {e}")
            break


# ---------------------------------------------------------------------------
# Message handler
# ---------------------------------------------------------------------------

async def handle_message(raw: str, ws) -> None:
    global active, return_edge

    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        return

    mtype = msg.get("type")

    if mtype == "mouse_move_rel":
        inject_mouse_move_rel(msg["dx"], msg["dy"])

    elif mtype == "mouse_click":
        inject_mouse_click(msg["button"], msg["pressed"])

    elif mtype == "mouse_scroll":
        inject_mouse_scroll(msg["dx"], msg["dy"])

    elif mtype == "key_press":
        inject_key_press(msg["key"])

    elif mtype == "key_release":
        inject_key_release(msg["key"])

    elif mtype == "clipboard":
        text = msg.get("text", "")
        print(f"[client] clipboard received from server ({len(text)} chars) → applying")
        clipboard_sync.apply_remote(text)

    elif mtype == "set_active":
        global _edge_monitor_task
        entry_x = msg.get("entry_x", v_min_x + screen_width // 2)
        entry_y = msg.get("entry_y", v_min_y + screen_height // 2)
        return_edge = msg.get("return_edge", "left")

        print(f"[client] Now active — entry ({entry_x},{entry_y}), watch {return_edge} edge")

        if _edge_monitor_task and not _edge_monitor_task.done():
            _edge_monitor_task.cancel()

        warp_cursor(entry_x, entry_y)
        active = True

        _edge_monitor_task = asyncio.ensure_future(edge_monitor_loop(ws))


# ---------------------------------------------------------------------------
# Main WebSocket loop
# ---------------------------------------------------------------------------

async def run(server_url: str) -> None:
    global screen_width, screen_height, v_min_x, v_min_y, _loop

    _loop = asyncio.get_running_loop()
    v_min_x, v_min_y, screen_width, screen_height = screens.get_virtual_bounds()

    # Convert http:// to ws://
    ws_url = server_url.replace("http://", "ws://").replace("https://", "wss://")
    ws_url = ws_url.rstrip("/") + "/ws/client"

    local_ip = None
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        local_ip = "unknown"

    if platform.system() == "Darwin":
        backend = "Quartz CGEvent (smooth)" if _quartz is not None else "pynput FALLBACK"
    else:
        backend = "pynput"

    print("=" * 60)
    print("MegaMind Client")
    print(f"  Local IP    : {local_ip}")
    print(f"  Desktop     : {screen_width}x{screen_height} @ ({v_min_x},{v_min_y})")
    print(f"  Server      : {server_url}")
    print(f"  Inject mode : {backend}")
    print()

    if platform.system() == "Darwin":
        print("  NOTE: macOS requires Accessibility permissions for pynput.")
        print("        System Preferences > Security & Privacy > Accessibility")
        if _quartz is None:
            print("  WARNING: Quartz not installed — cursor will stutter/snap back.")
            print("           Fix:  pip install pyobjc-framework-Quartz")
    elif platform.system() == "Windows":
        print("  NOTE: Run as Administrator if input injection does not work.")

    if clipboard_sync.available():
        clipboard_sync.start_monitor(_on_local_clipboard)
        print("  Clipboard  : sync enabled")
    else:
        print("  Clipboard  : unavailable (pip install pyperclip)")

    print("=" * 60)

    global _ws_connection
    while True:
        try:
            print(f"[client] Connecting to {ws_url} ...")
            async with websockets.connect(ws_url) as ws:
                _ws_connection = ws
                # Register
                reg = json.dumps({
                    "type": "register",
                    "v_min_x": v_min_x,
                    "v_min_y": v_min_y,
                    "screen_width": screen_width,
                    "screen_height": screen_height,
                })
                await ws.send(reg)
                print("[client] Connected and registered.")

                # Message loop
                async for raw in ws:
                    await handle_message(raw, ws)

        except (websockets.exceptions.ConnectionClosed,
                websockets.exceptions.WebSocketException,
                OSError) as e:
            print(f"[client] Disconnected ({e}). Retrying in 3 seconds...")
        except Exception as e:
            print(f"[client] Unexpected error: {e}. Retrying in 3 seconds...")

        _ws_connection = None
        global active
        active = False
        await asyncio.sleep(3)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python client.py http://<server-ip>:8080")
        sys.exit(1)

    server_url = sys.argv[1]

    try:
        asyncio.run(run(server_url))
    except KeyboardInterrupt:
        print("\n[client] Exiting.")
