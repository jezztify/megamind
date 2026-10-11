"""
MegaMind Client — runs on any machine that should be controlled from the server.

Usage:
    python client.py http://<server-ip>:9090
"""

import asyncio
import ipaddress
import json
import platform
import socket
import sys
import threading
import time
from typing import Callable

import websockets
from pynput import keyboard, mouse

import screens
import clipboard_sync

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EDGE_MARGIN: int = 3          # pixels from screen edge to trigger a return
POLL_INTERVAL: float = 1 / 60  # 60 fps edge polling
StatusCallback = Callable[[str, str], None]

# macOS: use Quartz CGEventPost for smooth injection (avoids CGWarp snap-back)
_quartz = None
if platform.system() == "Darwin":
    try:
        import Quartz as _quartz
    except ImportError:
        pass

# macOS: prefer a real virtual-HID keyboard (Karabiner DriverKit) when present.
# Its events enter at the HID layer — they highlight on the Accessibility
# Keyboard, fire Mission Control / Space-switch shortcuts natively, and work in
# secure input fields, none of which synthetic Quartz/pynput key events do.
# If the driver isn't installed, _vhid stays None and we fall back to pynput.
_vhid = None
if platform.system() == "Darwin":
    try:
        import virtual_hid_mac as _vhid
        if not _vhid.available():
            _vhid = None
    except Exception:
        _vhid = None

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
_clipboard_monitor_started: bool = False

# macOS mouse-button state, needed for proper drag + multi-click injection.
_buttons_down: set = set()     # button names currently pressed
_last_click_time: float = 0.0
_last_click_pos: tuple = (0.0, 0.0)
_last_click_button: str = ""
_click_state: int = 1          # 1=single, 2=double, 3=triple (kCGMouseEventClickState)
DOUBLE_CLICK_SECONDS: float = 0.5
DOUBLE_CLICK_DIST: int = 6

# Keyboard modifier state, tracked from forwarded modifier key events.
# pynput reports the BASE character for the number row even when Shift is held
# (e.g. Shift+2 -> '2', not '@'), and its character injection then normalizes
# the modifier away. We resolve the shifted symbol ourselves before injecting.
_shift_down: bool = False
_remapped_chars: dict = {}     # base char -> injected char, for press/release symmetry
_SHIFT_NAMES = {"shift", "shift_l", "shift_r"}
_SHIFT_MAP = {
    "1": "!", "2": "@", "3": "#", "4": "$", "5": "%",
    "6": "^", "7": "&", "8": "*", "9": "(", "0": ")",
    "`": "~", "-": "_", "=": "+", "[": "{", "]": "}",
    "\\": "|", ";": ":", "'": '"', ",": "<", ".": ">", "/": "?",
}

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


def _track_shift(key_data: dict, down: bool) -> None:
    global _shift_down
    if key_data.get("kind") == "special" and key_data.get("name") in _SHIFT_NAMES:
        _shift_down = down


def _resolve_press(key_data: dict) -> dict:
    """When Shift is held, map a base symbol to its shifted form (US layout)."""
    if key_data.get("kind") != "char":
        return key_data
    ch = key_data["char"]
    if _shift_down and ch in _SHIFT_MAP:
        shifted = _SHIFT_MAP[ch]
        _remapped_chars[ch] = shifted
        return {"kind": "char", "char": shifted}
    return key_data


def _resolve_release(key_data: dict) -> dict:
    """Release the same character we pressed (in case Shift changed meanwhile)."""
    if key_data.get("kind") != "char":
        return key_data
    ch = key_data["char"]
    shifted = _remapped_chars.pop(ch, None)
    if shifted is not None:
        return {"kind": "char", "char": shifted}
    return key_data


def inject_key_press(key_data: dict) -> None:
    # Real virtual-HID path (macOS w/ Karabiner driver): send the base physical
    # key and let separately-forwarded modifiers (Ctrl/Shift/...) ride in the
    # HID modifier byte. These are real key presses, so Ctrl+Arrow switches
    # Spaces natively and no Shift/symbol pre-resolution is needed.
    if _vhid is not None and _vhid.press(key_data):
        return

    _track_shift(key_data, down=True)
    key = deserialize_key(_resolve_press(key_data))
    if key is None:
        return
    _injecting.set()
    try:
        _keyboard_ctrl.press(key)
    finally:
        _injecting.clear()


def inject_key_release(key_data: dict) -> None:
    if _vhid is not None and _vhid.release(key_data):
        return

    _track_shift(key_data, down=False)
    key = deserialize_key(_resolve_release(key_data))
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
    global active, return_edge, _edge_monitor_task

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
        entry_x = msg.get("entry_x", v_min_x + screen_width // 2)
        entry_y = msg.get("entry_y", v_min_y + screen_height // 2)
        return_edge = msg.get("return_edge", "left")

        print(f"[client] Now active — entry ({entry_x},{entry_y}), watch {return_edge} edge")

        if _edge_monitor_task and not _edge_monitor_task.done():
            _edge_monitor_task.cancel()

        warp_cursor(entry_x, entry_y)
        active = True

        _edge_monitor_task = asyncio.ensure_future(edge_monitor_loop(ws))

    elif mtype == "set_inactive":
        active = False
        if _edge_monitor_task and not _edge_monitor_task.done():
            _edge_monitor_task.cancel()
        print("[client] No longer active")


# ---------------------------------------------------------------------------
# Main WebSocket loop
# ---------------------------------------------------------------------------

def _notify_status(callback: StatusCallback | None, status: str, detail: str = "") -> None:
    if callback is None:
        return
    try:
        callback(status, detail)
    except Exception as e:
        print(f"[client] Status callback failed: {e}")


async def register_with_server(ws, registration: dict, timeout: float = 5.0) -> None:
    """Register this client and reject WebSocket endpoints that are not MegaMind."""
    await ws.send(json.dumps(registration))
    try:
        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
    except asyncio.TimeoutError as e:
        raise ConnectionError("server did not confirm MegaMind registration") from e

    try:
        response = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as e:
        raise ConnectionError("server returned an invalid registration response") from e
    if not isinstance(response, dict) or response.get("type") != "registered":
        raise ConnectionError("endpoint is not a MegaMind server")


async def _receive_client_messages(ws) -> None:
    async for raw in ws:
        await handle_message(raw, ws)


async def _wait_for_stop(stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        await asyncio.sleep(0.1)


async def _receive_until_stopped(ws, stop_event: threading.Event) -> bool:
    """Receive server messages until disconnect, or close the socket on request."""
    receiver = asyncio.create_task(_receive_client_messages(ws))
    stopper = asyncio.create_task(_wait_for_stop(stop_event))
    try:
        done, _ = await asyncio.wait(
            (receiver, stopper), return_when=asyncio.FIRST_COMPLETED
        )
        if stopper in done:
            await ws.close()
            if not receiver.done():
                receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)
            return True
        await receiver
        return False
    finally:
        if not stopper.done():
            stopper.cancel()
        await asyncio.gather(stopper, return_exceptions=True)


async def _retry_delay(stop_event: threading.Event | None, delay: float = 3.0) -> None:
    if stop_event is None:
        await asyncio.sleep(delay)
        return
    deadline = asyncio.get_running_loop().time() + delay
    while not stop_event.is_set():
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return
        await asyncio.sleep(min(0.1, remaining))


async def run(
    server_url: str,
    stop_event: threading.Event | None = None,
    on_status: StatusCallback | None = None,
) -> None:
    global screen_width, screen_height, v_min_x, v_min_y, _loop
    global _clipboard_monitor_started

    _loop = asyncio.get_running_loop()
    v_min_x, v_min_y, screen_width, screen_height = screens.get_virtual_bounds()

    # Convert http:// to ws://
    ws_url = server_url.replace("http://", "ws://").replace("https://", "wss://")
    ws_url = ws_url.rstrip("/") + "/ws/client"

    local_ip = _get_local_ip() or "unknown"

    if platform.system() == "Darwin":
        backend = "Quartz CGEvent (smooth)" if _quartz is not None else "pynput FALLBACK"
    else:
        backend = "pynput"

    if _vhid is not None:
        kbd_backend = "virtual HID (Karabiner DriverKit)"
    else:
        kbd_backend = backend

    print("=" * 60)
    print("MegaMind Client")
    print(f"  Local IP    : {local_ip}")
    print(f"  Desktop     : {screen_width}x{screen_height} @ ({v_min_x},{v_min_y})")
    print(f"  Server      : {server_url}")
    print(f"  Mouse inject: {backend}")
    print(f"  Key inject  : {kbd_backend}")
    print()

    if platform.system() == "Darwin":
        print("  NOTE: macOS requires Accessibility permissions for pynput.")
        print("        System Preferences > Security & Privacy > Accessibility")
        if _quartz is None:
            print("  WARNING: Quartz not installed — cursor will stutter/snap back.")
            print("           Fix:  pip install pyobjc-framework-Quartz")
        if _vhid is None:
            print("  NOTE: Virtual-HID keyboard not active — keys are synthetic")
            print("        (Ctrl+Arrow won't switch Spaces, secure fields reject them).")
            print("        Install + approve Karabiner-DriverKit-VirtualHIDDevice:")
            print("        https://github.com/pqrs-org/Karabiner-DriverKit-VirtualHIDDevice")
    elif platform.system() == "Windows":
        print("  NOTE: Run as Administrator if input injection does not work.")

    if clipboard_sync.available():
        if not _clipboard_monitor_started:
            clipboard_sync.start_monitor(_on_local_clipboard)
            _clipboard_monitor_started = True
        print("  Clipboard  : sync enabled")
    else:
        print("  Clipboard  : unavailable (pip install pyperclip)")

    print("=" * 60)

    global _ws_connection, active
    connected_once = False
    try:
        while stop_event is None or not stop_event.is_set():
            _notify_status(on_status, "connecting", server_url)
            try:
                print(f"[client] Connecting to {ws_url} ...")
                async with websockets.connect(ws_url) as ws:
                    _ws_connection = ws
                    registration = {
                        "type": "register",
                        "v_min_x": v_min_x,
                        "v_min_y": v_min_y,
                        "screen_width": screen_width,
                        "screen_height": screen_height,
                    }
                    await register_with_server(ws, registration)
                    connected_once = True
                    _notify_status(on_status, "connected", server_url)
                    print("[client] Connected and registered.")

                    if stop_event is None:
                        await _receive_client_messages(ws)
                    elif await _receive_until_stopped(ws, stop_event):
                        break
                    if stop_event is None or not stop_event.is_set():
                        _notify_status(
                            on_status,
                            "disconnected" if connected_once else "failed",
                            "server closed the WebSocket connection",
                        )

            except (websockets.exceptions.ConnectionClosed,
                    websockets.exceptions.WebSocketException,
                    OSError,
                    ConnectionError,
                    asyncio.TimeoutError) as e:
                print(f"[client] Disconnected ({e}). Retrying in 3 seconds...")
                if stop_event is None or not stop_event.is_set():
                    _notify_status(
                        on_status,
                        "disconnected" if connected_once else "failed",
                        str(e),
                    )
            except Exception as e:
                print(f"[client] Unexpected error: {e}. Retrying in 3 seconds...")
                if stop_event is None or not stop_event.is_set():
                    _notify_status(
                        on_status,
                        "disconnected" if connected_once else "failed",
                        str(e),
                    )
            finally:
                _ws_connection = None
                active = False
                # Drop any keys the virtual HID device still holds so none stay
                # stuck down after a mid-keystroke disconnect.
                if _vhid is not None:
                    _vhid.release_all()

            if stop_event is not None and stop_event.is_set():
                break
            await _retry_delay(stop_event)
    finally:
        _ws_connection = None
        active = False
        _notify_status(on_status, "stopped", "")


# ---------------------------------------------------------------------------
# Server discovery
# ---------------------------------------------------------------------------

SERVER_PORT: int = 9090


def _get_local_ip() -> str | None:
    """Return the IPv4 address used for the default outbound route, if known."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return None


async def _probe(ip: str, timeout: float = 0.35) -> str | None:
    """Return ip if port SERVER_PORT is open, else None."""
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, SERVER_PORT), timeout=timeout
        )
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return ip
    except Exception:
        return None


async def _scan_subnet() -> list[str]:
    """Concurrently probe every host on the local /24 subnet."""
    local_ip = _get_local_ip()
    if local_ip is None:
        return []

    network = ipaddress.IPv4Network(f"{local_ip}/24", strict=False)
    tasks = [_probe(str(h)) for h in network.hosts() if str(h) != local_ip]
    results = await asyncio.gather(*tasks)
    return [ip for ip in results if ip is not None]


async def scan_servers() -> list[str]:
    """Find hosts on the current IPv4 /24 with MegaMind's TCP port open."""
    return await _scan_subnet()


def select_server() -> str:
    """Scan the local network, list found servers, and return the chosen URL.

    If a URL is passed on the command line it is used directly without prompting.
    """
    print("Scanning local network for MegaMind servers...")
    found: list[str] = asyncio.run(_scan_subnet())

    print()
    if found:
        print(f"Found {len(found)} server(s):")
        for i, ip in enumerate(found, 1):
            print(f"  [{i}] http://{ip}:{SERVER_PORT}")
    else:
        print("No servers found on the local /24 subnet.")
    print(f"  [0] Enter address manually")
    print()

    while True:
        try:
            raw = input("Select [0]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            raise SystemExit(0)

        if raw == "" or raw == "0":
            try:
                addr = input("Server IP or URL: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                raise SystemExit(0)
            if not addr:
                continue
            if not addr.startswith("http"):
                addr = f"http://{addr}:{SERVER_PORT}"
            return addr

        try:
            idx = int(raw) - 1
            if 0 <= idx < len(found):
                return f"http://{found[idx]}:{SERVER_PORT}"
        except ValueError:
            pass

        print(f"  Enter a number between 0 and {len(found)}.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) >= 2:
        server_url = sys.argv[1]
    else:
        server_url = select_server()

    try:
        asyncio.run(run(server_url))
    except KeyboardInterrupt:
        print("\n[client] Exiting.")
