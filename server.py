"""
MegaMind Server — runs on the machine with the physical keyboard and mouse.

Usage:
    python server.py

Web UI:  http://<this-machine-ip>:8080
"""

import asyncio
import json
import platform
import socket
import threading
from contextlib import asynccontextmanager
from typing import Optional

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

import screens
import clipboard_sync
# Opt into DPI awareness BEFORE pynput/tkinter touch screen coordinates, so the
# positions they report match the virtual-desktop metrics we read for edges.
screens.set_dpi_aware()

from pynput import keyboard, mouse

# Windows: capture true relative motion via Raw Input so the cursor can stay
# suppressed (frozen, no click leak) while we still read movement.
_rawinput = None
if platform.system() == "Windows":
    try:
        import rawinput_win as _rawinput
    except Exception:
        _rawinput = None

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EDGE_MARGIN: int = 3          # pixels from screen edge to trigger a switch
PORT: int = 8080

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

active: str = "server"        # "server" or client IP string
clients: dict[str, dict] = {} # ip -> {"ws": WebSocket, "screen_width": int, "screen_height": int}
layout: dict[str, dict] = {}  # e.g. {"server": {"right": "192.168.1.11"}, ...}

# Virtual desktop: origin (may be negative on multi-monitor) + total size.
v_min_x: int = 0
v_min_y: int = 0
screen_width: int = 0   # virtual desktop width  (spans all monitors)
screen_height: int = 0  # virtual desktop height (spans all monitors)

_mouse_listener: Optional[mouse.Listener] = None
_keyboard_listener: Optional[keyboard.Listener] = None
_listener_lock = threading.Lock()
_switching: bool = False  # guard against edge-trigger flood

# Output controller used to recenter/park the cursor while controlling a client.
_mouse_out = mouse.Controller()
_recentering: bool = False  # True while we warp the cursor back to center
_last_x: int = 0            # last cursor pos, for computing clean per-event deltas
_last_y: int = 0
RECENTER_MARGIN: int = 200  # warp back to center when this close to a screen edge
_active_direction: str = "right"  # edge we crossed to reach the active client

# When Raw Input is available (Windows) the mouse listener is SUPPRESSED while
# remote — the cursor freezes (no movement, no click leak) and motion comes from
# Raw Input instead. Elsewhere we fall back to the non-suppressed recenter trick.
_use_rawinput: bool = _rawinput is not None
_raw_stop = None  # callable to stop the Raw Input capture thread

_loop: Optional[asyncio.AbstractEventLoop] = None  # main event loop reference

ui_clients: list[WebSocket] = []  # browser websocket connections

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_virtual_bounds() -> None:
    """Populate the virtual-desktop globals from the actual monitor layout."""
    global v_min_x, v_min_y, screen_width, screen_height
    v_min_x, v_min_y, screen_width, screen_height = screens.get_virtual_bounds()


def _center() -> tuple[int, int]:
    return (v_min_x + screen_width // 2, v_min_y + screen_height // 2)


def get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def serialize_key(key) -> dict:
    if hasattr(key, "char") and key.char is not None:
        return {"kind": "char", "char": key.char}
    elif hasattr(key, "vk") and key.vk is not None:
        return {"kind": "vk", "vk": key.vk}
    elif hasattr(key, "name") and key.name is not None:
        return {"kind": "special", "name": key.name}
    return {"kind": "unknown"}


def state_snapshot() -> dict:
    return {
        "type": "state",
        "active": active,
        "server_ip": get_local_ip(),
        "clients": {
            ip: {"screen_width": info["screen_width"], "screen_height": info["screen_height"]}
            for ip, info in clients.items()
        },
        "layout": layout,
        "screen_width": screen_width,
        "screen_height": screen_height,
    }


def _run_coroutine_threadsafe(coro):
    """Schedule a coroutine onto the main asyncio event loop from a thread."""
    if _loop and not _loop.is_closed():
        asyncio.run_coroutine_threadsafe(coro, _loop)


# ---------------------------------------------------------------------------
# pynput listener management
# ---------------------------------------------------------------------------

def restart_mouse(suppress: bool) -> None:
    """(Re)start the mouse listener with the given suppression.

    With Raw Input (Windows) we SUPPRESS while remote: the cursor freezes (so the
    inactive machine's pointer doesn't move and clicks don't leak to the server),
    and motion is read from Raw Input instead. Without Raw Input we never suppress
    and rely on the recenter trick in on_move for motion."""
    global _mouse_listener
    with _listener_lock:
        if _mouse_listener is not None:
            try:
                _mouse_listener.stop()
            except Exception:
                pass
            _mouse_listener = None

        ml = mouse.Listener(
            on_move=on_move,
            on_click=on_click,
            on_scroll=on_scroll,
            suppress=suppress,
        )
        ml.start()
        _mouse_listener = ml


def mouse_suppress_when_remote() -> bool:
    """Mouse is suppressed while remote only when Raw Input can supply motion."""
    return _use_rawinput


def _on_raw_mouse(dx: int, dy: int) -> None:
    """Raw Input callback (capture thread): forward relative motion to client."""
    if active != "server":
        _run_coroutine_threadsafe(_forward_mouse_move_rel(dx, dy))


def restart_keyboard(suppress: bool) -> None:
    """(Re)start the keyboard listener. Suppressed while controlling a client
    so the user's typing goes to the client instead of the server."""
    global _keyboard_listener
    with _listener_lock:
        if _keyboard_listener is not None:
            try:
                _keyboard_listener.stop()
            except Exception:
                pass
            _keyboard_listener = None

        kl = keyboard.Listener(
            on_press=on_press,
            on_release=on_release,
            suppress=suppress,
        )
        kl.start()
        _keyboard_listener = kl


# ---------------------------------------------------------------------------
# Input event handlers (called from pynput threads)
# ---------------------------------------------------------------------------

def on_move(x: int, y: int) -> None:
    global _recentering, _last_x, _last_y
    if active == "server":
        _check_edges(x, y)
        return

    if _use_rawinput:
        # Motion comes from Raw Input; the suppressed cursor is frozen here.
        return

    # Fallback (non-Windows): cursor is NOT suppressed, so it moves with the
    # physical mouse and reports real positions; the per-event delta from the
    # last position is clean, smooth motion. We forward that delta.
    if _recentering:
        # Echo from our own warp-to-center: re-baseline, don't forward.
        _recentering = False
        _last_x, _last_y = x, y
        return

    dx = x - _last_x
    dy = y - _last_y
    _last_x, _last_y = x, y
    if dx or dy:
        _run_coroutine_threadsafe(_forward_mouse_move_rel(dx, dy))

    # Only when the cursor nears a screen edge do we warp it back to center, so
    # it never runs out of room. This happens rarely, so the warp isn't clobbered
    # by continuous motion (which is what broke recenter-every-event).
    if (x < v_min_x + RECENTER_MARGIN or x > v_min_x + screen_width - RECENTER_MARGIN or
            y < v_min_y + RECENTER_MARGIN or y > v_min_y + screen_height - RECENTER_MARGIN):
        _recentering = True
        cx, cy = _center()
        _last_x, _last_y = cx, cy
        _mouse_out.position = (cx, cy)


def on_click(x: int, y: int, button: mouse.Button, pressed: bool) -> None:
    if active != "server":
        _run_coroutine_threadsafe(_forward_mouse_click(x, y, button.name, pressed))


def on_scroll(x: int, y: int, dx: float, dy: float) -> None:
    if active != "server":
        _run_coroutine_threadsafe(_forward_mouse_scroll(x, y, dx, dy))


def on_press(key) -> None:
    if active != "server":
        _run_coroutine_threadsafe(_forward_key("key_press", key))


def on_release(key) -> None:
    if active != "server":
        _run_coroutine_threadsafe(_forward_key("key_release", key))


def _check_edges(x: int, y: int) -> None:
    """Determine if the cursor has hit a configured edge and initiate a switch."""
    directions_to_check = {
        "left":  x <= v_min_x + EDGE_MARGIN,
        "right": x >= v_min_x + screen_width - EDGE_MARGIN - 1,
        "top":   y <= v_min_y + EDGE_MARGIN,
        "bottom": y >= v_min_y + screen_height - EDGE_MARGIN - 1,
    }

    global _switching
    if _switching:
        return

    server_layout = layout.get("server", {})
    for direction, triggered in directions_to_check.items():
        if triggered and direction in server_layout:
            target_ip = server_layout[direction]
            if target_ip in clients:
                _switching = True
                _run_coroutine_threadsafe(_switch_to(target_ip, direction))
            return


# ---------------------------------------------------------------------------
# Async switching logic
# ---------------------------------------------------------------------------

async def _switch_to(ip: str, direction: str) -> None:
    global active, _switching

    if ip not in clients:
        _switching = False
        return

    active = ip

    # Warp cursor to center; subsequent moves are measured as deltas from here.
    global _recentering, _last_x, _last_y, _active_direction
    _active_direction = direction
    cx, cy = _center()
    _recentering = True
    _last_x, _last_y = cx, cy
    _mouse_out.position = (cx, cy)

    # Entry position on the target client, in the CLIENT's virtual-desktop
    # coordinates, on the edge OPPOSITE the one we crossed.
    client_info = clients[ip]
    cvx = client_info["v_min_x"]
    cvy = client_info["v_min_y"]
    cvw = client_info["screen_width"]
    cvh = client_info["screen_height"]

    opposite = {"left": "right", "right": "left", "top": "bottom", "bottom": "top"}
    return_edge = opposite[direction]

    entry_map = {
        "left":   (cvx + cvw - EDGE_MARGIN - 1, cvy + cvh // 2),
        "right":  (cvx + EDGE_MARGIN + 1,       cvy + cvh // 2),
        "top":    (cvx + cvw // 2, cvy + cvh - EDGE_MARGIN - 1),
        "bottom": (cvx + cvw // 2, cvy + EDGE_MARGIN + 1),
    }
    entry_x, entry_y = entry_map[direction]

    msg = json.dumps({
        "type": "set_active",
        "entry_x": entry_x,
        "entry_y": entry_y,
        "return_edge": return_edge,
    })

    try:
        await clients[ip]["ws"].send_text(msg)
    except Exception as e:
        print(f"[server] Failed to send set_active to {ip}: {e}")
        active = "server"
        _switching = False
        await _set_input_mode(remote=False)
        return

    await _set_input_mode(remote=True)
    await _broadcast_state()


async def _switch_to_server(cursor_x: int, cursor_y: int) -> None:
    global active, _switching, _last_x, _last_y

    active = "server"
    _switching = False

    # Place the cursor just inside the edge we originally crossed, so returning
    # feels continuous and we don't immediately re-trigger that edge. (The x,y
    # the client reports are in the CLIENT's coordinate space and not usable
    # directly on the server.)
    inset = EDGE_MARGIN + 40
    edge_pos = {
        "right":  (v_min_x + screen_width - inset, v_min_y + screen_height // 2),
        "left":   (v_min_x + inset, v_min_y + screen_height // 2),
        "top":    (v_min_x + screen_width // 2, v_min_y + inset),
        "bottom": (v_min_x + screen_width // 2, v_min_y + screen_height - inset),
    }
    px, py = edge_pos.get(_active_direction, _center())
    _last_x, _last_y = px, py
    _mouse_out.position = (px, py)

    await _set_input_mode(remote=False)
    await _broadcast_state()


async def _set_input_mode(remote: bool) -> None:
    """Reconfigure listeners for local vs remote control, off the event loop."""
    if remote:
        await _loop.run_in_executor(None, restart_mouse, mouse_suppress_when_remote())
        await _loop.run_in_executor(None, restart_keyboard, True)
    else:
        await _loop.run_in_executor(None, restart_mouse, False)
        await _loop.run_in_executor(None, restart_keyboard, False)


# ---------------------------------------------------------------------------
# Forwarding helpers
# ---------------------------------------------------------------------------

async def _forward_mouse_move_rel(dx: int, dy: int) -> None:
    if active not in clients:
        return
    msg = json.dumps({"type": "mouse_move_rel", "dx": dx, "dy": dy})
    await _safe_send(clients[active]["ws"], msg)


async def _forward_mouse_click(x: int, y: int, button: str, pressed: bool) -> None:
    if active not in clients:
        return
    msg = json.dumps({"type": "mouse_click", "x": x, "y": y, "button": button, "pressed": pressed})
    await _safe_send(clients[active]["ws"], msg)


async def _forward_mouse_scroll(x: int, y: int, dx: float, dy: float) -> None:
    if active not in clients:
        return
    msg = json.dumps({"type": "mouse_scroll", "x": x, "y": y, "dx": dx, "dy": dy})
    await _safe_send(clients[active]["ws"], msg)


async def _forward_key(event_type: str, key) -> None:
    if active not in clients:
        return
    serialized = serialize_key(key)
    if serialized["kind"] == "unknown":
        return
    msg = json.dumps({"type": event_type, "key": serialized})
    await _safe_send(clients[active]["ws"], msg)


async def _safe_send(ws: WebSocket, msg: str) -> None:
    try:
        await ws.send_text(msg)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Clipboard sync
# ---------------------------------------------------------------------------

async def _broadcast_clipboard(text: str, exclude_ip: Optional[str] = None) -> None:
    msg = json.dumps({"type": "clipboard", "text": text})
    for ip, info in list(clients.items()):
        if ip == exclude_ip:
            continue
        await _safe_send(info["ws"], msg)


def _on_local_clipboard(text: str) -> None:
    """Clipboard monitor thread: our own clipboard changed → push to all clients."""
    _run_coroutine_threadsafe(_broadcast_clipboard(text))


# ---------------------------------------------------------------------------
# UI broadcast
# ---------------------------------------------------------------------------

async def _broadcast_state() -> None:
    snap = json.dumps(state_snapshot())
    dead: list[WebSocket] = []
    for ws in ui_clients:
        try:
            await ws.send_text(snap)
        except Exception:
            dead.append(ws)
    for ws in dead:
        ui_clients.remove(ws)


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global screen_width, screen_height, _loop

    _loop = asyncio.get_running_loop()
    load_virtual_bounds()

    local_ip = get_local_ip()
    print("=" * 60)
    print("MegaMind Server")
    print(f"  Local IP    : {local_ip}")
    print(f"  Desktop     : {screen_width}x{screen_height} @ ({v_min_x},{v_min_y})")
    print(f"  Web UI      : http://{local_ip}:{PORT}")
    print(f"  Port        : {PORT}")
    print(f"  Mouse capture: {'Raw Input (frozen cursor)' if _use_rawinput else 'recenter (pynput)'}")
    print()

    if platform.system() == "Windows":
        print("  NOTE: Run as Administrator if suppression/capture does not work.")
        if not _use_rawinput:
            print("  WARNING: Raw Input unavailable — inactive cursor will drift and clicks leak.")
    elif platform.system() == "Darwin":
        print("  NOTE: macOS requires Accessibility permissions for pynput.")
        print("        System Preferences > Security & Privacy > Accessibility")

    print("=" * 60)

    global _raw_stop
    if _use_rawinput:
        _raw_stop = _rawinput.start_raw_mouse_capture(_on_raw_mouse)
    restart_mouse(suppress=False)
    restart_keyboard(suppress=False)

    if clipboard_sync.available():
        clipboard_sync.start_monitor(_on_local_clipboard)
        print("[server] Clipboard sync enabled.")
    else:
        print("[server] Clipboard sync unavailable (pyperclip missing).")

    yield

    # Cleanup
    if _raw_stop:
        try:
            _raw_stop()
        except Exception:
            pass
    with _listener_lock:
        if _mouse_listener:
            _mouse_listener.stop()
        if _keyboard_listener:
            _keyboard_listener.stop()


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def serve_ui():
    return FileResponse("static/index.html")


@app.websocket("/ws/client")
async def ws_client_endpoint(websocket: WebSocket):
    await websocket.accept()
    client_ip = websocket.client.host

    print(f"[server] Client connected: {client_ip}")

    try:
        # First message must be registration
        raw = await websocket.receive_text()
        data = json.loads(raw)

        if data.get("type") != "register":
            await websocket.close()
            return

        clients[client_ip] = {
            "ws": websocket,
            "v_min_x": data.get("v_min_x", 0),
            "v_min_y": data.get("v_min_y", 0),
            "screen_width": data.get("screen_width", 1920),
            "screen_height": data.get("screen_height", 1080),
        }

        print(f"[server] Registered {client_ip} "
              f"({data.get('screen_width')}x{data.get('screen_height')} "
              f"@ {data.get('v_min_x', 0)},{data.get('v_min_y', 0)})")
        await _broadcast_state()

        # Listen for messages from this client
        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)

            if msg.get("type") == "switch_to_server":
                x = msg.get("x", screen_width // 2)
                y = msg.get("y", screen_height // 2)
                print(f"[server] Switching back to server (cursor at {x},{y})")
                await _switch_to_server(x, y)

            elif msg.get("type") == "clipboard":
                # Apply locally, then fan out to every OTHER client.
                text = msg.get("text", "")
                clipboard_sync.apply_remote(text)
                await _broadcast_clipboard(text, exclude_ip=client_ip)

    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"[server] Client {client_ip} error: {e}")
    finally:
        if client_ip in clients:
            del clients[client_ip]
            print(f"[server] Client disconnected: {client_ip}")

            # If this client was active, switch back to server
            global active, _switching
            if active == client_ip:
                active = "server"
                _switching = False
                await _set_input_mode(remote=False)

            await _broadcast_state()


@app.websocket("/ws/ui")
async def ws_ui_endpoint(websocket: WebSocket):
    await websocket.accept()
    ui_clients.append(websocket)
    print(f"[server] UI connected: {websocket.client.host}")

    try:
        # Send current state immediately
        await websocket.send_text(json.dumps(state_snapshot()))

        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)

            if msg.get("type") == "layout_update":
                layout.clear()
                layout.update(msg.get("layout", {}))
                print(f"[server] Layout updated: {layout}")
                await _broadcast_state()

            elif msg.get("type") == "force_switch":
                target = msg.get("target")
                if target == "server":
                    await _switch_to_server(screen_width // 2, screen_height // 2)
                elif target in clients:
                    direction = next(
                        (d for d, ip in layout.get("server", {}).items() if ip == target),
                        "right",
                    )
                    await _switch_to(target, direction)

    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"[server] UI error: {e}")
    finally:
        if websocket in ui_clients:
            ui_clients.remove(websocket)
        print(f"[server] UI disconnected")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
