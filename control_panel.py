"""Local desktop controls for MegaMind server and client instances."""

import asyncio
import json
import os
import queue
import re
import sys
import threading
import tkinter as tk
from contextlib import contextmanager, nullcontext
from tkinter import scrolledtext, ttk
from typing import Callable, TextIO
from urllib.parse import urlsplit

import websockets

# PyInstaller windowed builds have no console streams. Server/client libraries
# still write diagnostic banners, so provide safe sinks instead of None.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

import screens

# Screen metrics must be DPI-aware before Tk creates a window or server.py
# initializes pynput controllers.
screens.set_dpi_aware()

import client as megamind_client
from placement_grid import GRID_SIZE, assign_positions, build_layout, swap_positions


DEFAULT_PORT = megamind_client.SERVER_PORT
GRID_CELL_WIDTH = 150
GRID_CELL_HEIGHT = 100
PLACEMENT_CARD_WIDTH = 126
PLACEMENT_CARD_HEIGHT = 68


class RoleLogRouter:
    """Route complete stdout/stderr lines to the view for their local role."""

    _ROLE_PREFIX = re.compile(r"^\s*\[(server|client)\]", re.IGNORECASE)

    def __init__(self, destination: queue.Queue[tuple[str, str]]):
        self._destination = destination
        self._context = threading.local()
        self._buffers = threading.local()

    @contextmanager
    def for_role(self, role: str):
        if role not in {"server", "client"}:
            raise ValueError(f"Unknown log role: {role}")
        previous = getattr(self._context, "role", None)
        self._context.role = role
        try:
            yield
        finally:
            if previous is None:
                del self._context.role
            else:
                self._context.role = previous

    def write(self, stream_name: str, text: str) -> None:
        buffers = getattr(self._buffers, "by_stream", None)
        if buffers is None:
            buffers = self._buffers.by_stream = {}

        parts = (buffers.get(stream_name, "") + text).splitlines(keepends=True)
        if parts and not parts[-1].endswith(("\n", "\r")):
            buffers[stream_name] = parts.pop()
        else:
            buffers.pop(stream_name, None)

        for line in parts:
            role = self._role_for(line)
            if role is not None:
                self._destination.put((role, line))

    def flush(self, stream_name: str) -> None:
        buffers = getattr(self._buffers, "by_stream", {})
        remainder = buffers.pop(stream_name, "")
        if remainder:
            role = self._role_for(remainder)
            if role is not None:
                self._destination.put((role, remainder))

    def _role_for(self, line: str) -> str | None:
        role = getattr(self._context, "role", None)
        if role is not None:
            return role

        thread_name = threading.current_thread().name
        if thread_name.startswith("megamind-server"):
            return "server"
        if thread_name.startswith("megamind-client"):
            return "client"

        match = self._ROLE_PREFIX.match(line)
        return match.group(1).lower() if match else None


class RoutedLogStream:
    """Tee a text stream to its original destination and the role log queue."""

    def __init__(self, original: TextIO, router: RoleLogRouter, stream_name: str):
        self._original = original
        self._router = router
        self._stream_name = stream_name

    def write(self, text: str) -> int:
        if not isinstance(text, str):
            raise TypeError("write() argument must be str")
        self._router.write(self._stream_name, text)
        return self._original.write(text)

    def flush(self) -> None:
        self._router.flush(self._stream_name)
        self._original.flush()

    def isatty(self) -> bool:
        return self._original.isatty()

    def writable(self) -> bool:
        return True

    def __getattr__(self, name: str):
        return getattr(self._original, name)


def _log_role(router: RoleLogRouter | None, role: str):
    return router.for_role(role) if router is not None else nullcontext()


def normalize_server_url(value: str) -> str:
    """Normalize a host or HTTP(S) URL for the client's WebSocket connection."""
    address = value.strip()
    if not address:
        raise ValueError("Enter a server address or URL.")
    if "://" not in address:
        address = f"http://{address}"

    try:
        parsed = urlsplit(address)
        port = parsed.port
    except ValueError as e:
        raise ValueError("The server address contains an invalid port.") from e

    host = parsed.hostname
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Use an HTTP(S) URL or enter a host name or IP address.")
    if not host or parsed.username is not None or parsed.password is not None:
        raise ValueError("Enter a host name or IP address without credentials.")
    if any(char.isspace() for char in host):
        raise ValueError("The server host cannot contain spaces.")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("Enter only the server host and optional port.")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("The server port must be between 1 and 65535.")

    # urlsplit strips IPv6 brackets from hostname; restore them when rebuilding.
    netloc = f"[{host}]" if ":" in host else host
    netloc += f":{port or DEFAULT_PORT}"
    return f"{parsed.scheme}://{netloc}"


class ServerRunner:
    """Run Uvicorn in a managed thread so its ASGI lifespan shuts down cleanly."""

    def __init__(
        self,
        server_factory: Callable[[], object] | None = None,
        log_router: RoleLogRouter | None = None,
    ):
        self._server_factory = server_factory or self._create_server
        self._log_router = log_router
        self._server = None
        self._thread: threading.Thread | None = None
        self._error: str | None = None
        self._lock = threading.Lock()

    @staticmethod
    def _create_server():
        import server as megamind_server
        import uvicorn

        config = uvicorn.Config(
            megamind_server.app,
            host="0.0.0.0",
            port=megamind_server.PORT,
            log_level="warning",
        )
        return uvicorn.Server(config)

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def state(self) -> str:
        if self._thread is None:
            return "failed" if self._error else "stopped"
        if self._thread.is_alive():
            if getattr(self._server, "should_exit", False):
                return "stopping"
            return "running" if getattr(self._server, "started", False) else "starting"
        if self._error or not getattr(self._server, "started", False):
            return "failed"
        return "stopped"

    @property
    def detail(self) -> str:
        if self._error:
            return self._error
        if self.state == "failed":
            return "Server exited before startup completed; check port 9090 and permissions."
        return ""

    def start(self) -> bool:
        with self._lock:
            if self.is_running:
                return False
            self._error = None
            try:
                with _log_role(self._log_router, "server"):
                    self._server = self._server_factory()
            except Exception as e:
                self._error = f"{type(e).__name__}: {e}"
                self._thread = None
                return False
            self._thread = threading.Thread(
                target=self._run,
                name="megamind-server",
                daemon=True,
            )
            self._thread.start()
            return True

    def _run(self) -> None:
        try:
            with _log_role(self._log_router, "server"):
                self._server.run()
        except Exception as e:
            self._error = f"{type(e).__name__}: {e}"

    def stop(self) -> bool:
        if not self.is_running:
            return False
        self._server.should_exit = True
        return True

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)


class ClientRunner:
    """Run a client connection in a stoppable background thread."""

    def __init__(
        self,
        run_client: Callable | None = None,
        on_status: Callable[[str, str], None] | None = None,
        log_router: RoleLogRouter | None = None,
    ):
        self._run_client = run_client or megamind_client.run
        self._on_status = on_status
        self._log_router = log_router
        self._stop_event: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self._state = "stopped"
        self._detail = ""
        self._lock = threading.Lock()

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def detail(self) -> str:
        with self._lock:
            return self._detail

    def start(self, server_url: str) -> bool:
        if self.is_running:
            return False
        self._stop_event = threading.Event()
        self._set_status("connecting", server_url)
        self._thread = threading.Thread(
            target=self._run,
            args=(server_url, self._stop_event),
            name="megamind-client",
            daemon=True,
        )
        self._thread.start()
        return True

    def _run(self, server_url: str, stop_event: threading.Event) -> None:
        try:
            with _log_role(self._log_router, "client"):
                asyncio.run(
                    self._run_client(
                        server_url,
                        stop_event=stop_event,
                        on_status=self._set_status,
                    )
                )
        except Exception as e:
            self._set_status("failed", f"{type(e).__name__}: {e}")
        finally:
            if stop_event.is_set():
                self._set_status("stopped", "")
            elif self.state not in {"failed", "disconnected"}:
                self._set_status("disconnected", "Client connection ended.")

    def _set_status(self, state: str, detail: str = "") -> None:
        with self._lock:
            self._state = state
            self._detail = detail
        if self._on_status is not None:
            try:
                self._on_status(state, detail)
            except Exception:
                pass

    def stop(self) -> bool:
        if not self.is_running or self._stop_event is None:
            return False
        self._set_status("stopping", "Closing the WebSocket connection.")
        self._stop_event.set()
        return True

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)


class DashboardRunner:
    """Maintain a UI WebSocket connection to a MegaMind placement server."""

    def __init__(
        self,
        on_state: Callable[[dict], None] | None = None,
        on_status: Callable[[str, str], None] | None = None,
    ):
        self._on_state = on_state
        self._on_status = on_status
        self._thread: threading.Thread | None = None
        self._stop_event: threading.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._websocket = None
        self._server_url: str | None = None
        self._state = "stopped"
        self._detail = ""
        self._lock = threading.RLock()

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def detail(self) -> str:
        with self._lock:
            return self._detail

    @property
    def server_url(self) -> str | None:
        with self._lock:
            return self._server_url

    def start(self, server_url: str) -> bool:
        normalized_url = normalize_server_url(server_url)
        with self._lock:
            if self.is_running or self._state == "connecting":
                return False
            self._stop_event = threading.Event()
            self._server_url = normalized_url
            self._state = "connecting"
            self._detail = normalized_url
            self._thread = threading.Thread(
                target=self._run,
                args=(normalized_url, self._stop_event),
                name="megamind-dashboard",
                daemon=True,
            )
            thread = self._thread
            self._notify_status("connecting", normalized_url)
            thread.start()
        return True

    def _run(self, server_url: str, stop_event: threading.Event) -> None:
        asyncio.run(self._run_async(server_url, stop_event))

    async def _run_async(self, server_url: str, stop_event: threading.Event) -> None:
        connected = False
        error: Exception | None = None
        try:
            async with websockets.connect(
                _dashboard_websocket_url(server_url),
                open_timeout=5,
            ) as websocket:
                with self._lock:
                    self._loop = asyncio.get_running_loop()
                    self._websocket = websocket
                if not stop_event.is_set():
                    connected = True
                    self._set_status("connected", server_url)
                    async for raw in websocket:
                        if stop_event.is_set():
                            break
                        try:
                            snapshot = json.loads(raw)
                        except (TypeError, json.JSONDecodeError):
                            continue
                        if isinstance(snapshot, dict) and self._on_state is not None:
                            try:
                                self._on_state(snapshot)
                            except Exception:
                                pass
        except Exception as e:
            error = e
        finally:
            with self._lock:
                self._loop = None
                self._websocket = None
            if stop_event.is_set():
                self._set_status("stopped", "")
            elif error is not None and connected:
                self._set_status("disconnected", f"{type(error).__name__}: {error}")
            elif error is not None:
                self._set_status("failed", f"{type(error).__name__}: {error}")
            elif connected:
                self._set_status("disconnected", "The placement connection closed.")

    def send(self, message: dict) -> bool:
        try:
            payload = json.dumps(message)
        except (TypeError, ValueError):
            return False
        with self._lock:
            loop = self._loop
            websocket = self._websocket
            if (
                self._state != "connected"
                or loop is None
                or loop.is_closed()
                or websocket is None
            ):
                return False
            try:
                future = asyncio.run_coroutine_threadsafe(
                    websocket.send(payload), loop
                )
            except RuntimeError:
                return False
        future.add_done_callback(self._report_send_error)
        return True

    def _report_send_error(self, future) -> None:
        try:
            future.result()
        except Exception as e:
            self._set_status("failed", f"Could not send placement update: {e}")

    def stop(self) -> bool:
        with self._lock:
            if not self.is_running or self._stop_event is None:
                return False
            if (
                self._state in {"stopped", "disconnected", "failed"}
                and self._loop is None
                and self._websocket is None
            ):
                return False
            stop_event = self._stop_event
            loop = self._loop
            websocket = self._websocket
            self._state = "stopping"
            self._detail = "Closing the placement connection."
            self._notify_status("stopping", self._detail)
            stop_event.set()
        if loop is not None and websocket is not None and not loop.is_closed():
            try:
                asyncio.run_coroutine_threadsafe(websocket.close(), loop)
            except RuntimeError:
                pass
        return True

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def _set_status(self, state: str, detail: str = "") -> None:
        with self._lock:
            self._state = state
            self._detail = detail
        self._notify_status(state, detail)

    def _notify_status(self, state: str, detail: str) -> None:
        if self._on_status is not None:
            try:
                self._on_status(state, detail)
            except Exception:
                pass


def _dashboard_websocket_url(server_url: str) -> str:
    parsed = urlsplit(normalize_server_url(server_url))
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return f"{scheme}://{parsed.netloc}/ws/ui"


class ControlPanel:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.log_queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self.log_router = RoleLogRouter(self.log_queue)
        self._original_stdout = sys.stdout
        self._original_stderr = sys.stderr
        self._stdout_log_stream = RoutedLogStream(
            self._original_stdout, self.log_router, "stdout"
        )
        self._stderr_log_stream = RoutedLogStream(
            self._original_stderr, self.log_router, "stderr"
        )
        sys.stdout = self._stdout_log_stream
        sys.stderr = self._stderr_log_stream

        self.server_runner = ServerRunner(log_router=self.log_router)
        self.client_runner = ClientRunner(
            on_status=self._queue_client_status,
            log_router=self.log_router,
        )
        self.dashboard_runner = DashboardRunner(
            on_state=self._queue_dashboard_state,
            on_status=self._queue_dashboard_status,
        )
        self.server_status = tk.StringVar(value="Stopped")
        self.client_status = tk.StringVar(value="Stopped")
        self.client_detail = tk.StringVar(value="")
        self.scan_status = tk.StringVar(value="")
        self.address = tk.StringVar(value="")
        self.candidate = tk.StringVar(value="")
        self.placement_address = tk.StringVar(
            value=f"http://127.0.0.1:{DEFAULT_PORT}"
        )
        self.placement_status = tk.StringVar(value="Not connected")
        self.placement_snapshot: dict = {}
        self.placement_positions: dict[str, tuple[int, int]] = {}
        self._placement_last_layout: dict | None = None
        self._placement_pending_layouts: list[dict] = []
        self._placement_pending_url: str | None = None
        self._placement_drag_machine: str | None = None
        self._placement_drag_origin: tuple[float, float] | None = None
        self._placement_dragged = False
        self._placement_items: dict[str, tuple[int, int]] = {}
        self._scan_in_progress = False
        self._closing = False
        self._closed = False

        root.title("MegaMind Control Panel")
        root.geometry("700x650")
        root.minsize(620, 560)
        root.protocol("WM_DELETE_WINDOW", self._close)
        self._build_ui()
        self.log_views = {"server": self.server_log, "client": self.client_log}
        root.after(50, self._drain_log_queue)

    def _build_ui(self) -> None:
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")

        container = ttk.Frame(self.root, padding=18)
        container.pack(fill="both", expand=True)
        ttk.Label(
            container,
            text="MegaMind",
            font=("Segoe UI", 21, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            container,
            text="Start a local role, discover servers, and arrange your screens.",
        ).pack(anchor="w", pady=(2, 14))

        self.notebook = ttk.Notebook(container)
        self.notebook.pack(fill="both", expand=True)
        self._build_server_tab()
        self._build_client_tab()
        self._build_placement_tab()

        ttk.Label(
            container,
            text="Discovery checks reachable TCP port 9090 hosts on this IPv4 /24; verify by connecting.",
            wraplength=570,
        ).pack(anchor="w", pady=(12, 0))

    def _build_server_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=18)
        self.notebook.add(tab, text="Server")
        ttk.Label(tab, text="Run a MegaMind server on this machine.").pack(anchor="w")
        ttk.Label(tab, text="Server status:", font=("Segoe UI", 10, "bold")).pack(
            anchor="w", pady=(22, 4)
        )
        ttk.Label(tab, textvariable=self.server_status, wraplength=520).pack(anchor="w")

        buttons = ttk.Frame(tab)
        buttons.pack(anchor="w", pady=(18, 0))
        self.start_server_button = ttk.Button(
            buttons, text="Start Server", command=self._start_server
        )
        self.start_server_button.pack(side="left", padx=(0, 8))
        self.stop_server_button = ttk.Button(
            buttons, text="Stop Server", command=self._stop_server, state="disabled"
        )
        self.stop_server_button.pack(side="left", padx=(0, 8))
        self.open_local_layout_button = ttk.Button(
            buttons,
            text="Manage Placements",
            command=self._open_local_layout,
            state="disabled",
        )
        self.open_local_layout_button.pack(side="left")
        self.server_log = self._build_log_view(tab, "Server logs")

    def _build_client_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=18)
        self.notebook.add(tab, text="Client")
        ttk.Label(tab, text="Connect this machine to a MegaMind server.").pack(anchor="w")

        ttk.Label(tab, text="Server address or URL").pack(anchor="w", pady=(16, 4))
        address_row = ttk.Frame(tab)
        address_row.pack(fill="x")
        self.address_entry = ttk.Entry(address_row, textvariable=self.address)
        self.address_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.scan_button = ttk.Button(
            address_row, text="Scan LAN", command=self._scan_lan
        )
        self.scan_button.pack(side="left")

        ttk.Label(tab, text="Reachable hosts").pack(anchor="w", pady=(12, 4))
        self.candidate_box = ttk.Combobox(
            tab,
            textvariable=self.candidate,
            state="readonly",
            values=(),
        )
        self.candidate_box.pack(fill="x")
        self.candidate_box.bind("<<ComboboxSelected>>", self._use_candidate)
        ttk.Label(tab, textvariable=self.scan_status).pack(anchor="w", pady=(4, 0))

        ttk.Label(tab, text="Client status:", font=("Segoe UI", 10, "bold")).pack(
            anchor="w", pady=(16, 4)
        )
        ttk.Label(tab, textvariable=self.client_status).pack(anchor="w")
        ttk.Label(tab, textvariable=self.client_detail, wraplength=520).pack(
            anchor="w", pady=(3, 0)
        )

        buttons = ttk.Frame(tab)
        buttons.pack(anchor="w", pady=(16, 0))
        self.connect_button = ttk.Button(
            buttons, text="Connect", command=self._connect_client
        )
        self.connect_button.pack(side="left", padx=(0, 8))
        self.disconnect_button = ttk.Button(
            buttons,
            text="Disconnect",
            command=self._disconnect_client,
            state="disabled",
        )
        self.disconnect_button.pack(side="left", padx=(0, 8))
        self.open_remote_layout_button = ttk.Button(
            buttons,
            text="Manage Placements",
            command=self._open_remote_layout,
        )
        self.open_remote_layout_button.pack(side="left")
        self.client_log = self._build_log_view(tab, "Client logs")

    def _build_placement_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=18)
        self.placement_tab = tab
        self.notebook.add(tab, text="Placement")
        ttk.Label(
            tab,
            text="Arrange connected screens. Drag a machine to swap cells; click it to switch.",
            wraplength=590,
        ).pack(anchor="w")

        ttk.Label(tab, text="Server address or URL").pack(anchor="w", pady=(14, 4))
        connection_row = ttk.Frame(tab)
        connection_row.pack(fill="x")
        self.placement_address_entry = ttk.Entry(
            connection_row,
            textvariable=self.placement_address,
        )
        self.placement_address_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.placement_connect_button = ttk.Button(
            connection_row,
            text="Connect",
            command=self._connect_placement,
        )
        self.placement_connect_button.pack(side="left", padx=(0, 8))
        self.placement_disconnect_button = ttk.Button(
            connection_row,
            text="Disconnect",
            command=self._disconnect_placement,
            state="disabled",
        )
        self.placement_disconnect_button.pack(side="left")
        ttk.Label(tab, textvariable=self.placement_status).pack(
            anchor="w", pady=(8, 0)
        )

        self.placement_canvas = tk.Canvas(
            tab,
            width=GRID_SIZE * GRID_CELL_WIDTH,
            height=GRID_SIZE * GRID_CELL_HEIGHT,
            background="#f8fafc",
            highlightthickness=0,
        )
        self.placement_canvas.pack(fill="both", expand=True, pady=(10, 0))
        self.placement_canvas.bind("<Configure>", self._render_placement_grid)
        self.placement_canvas.bind("<ButtonPress-1>", self._placement_mouse_down)
        self.placement_canvas.bind("<B1-Motion>", self._placement_mouse_move)
        self.placement_canvas.bind("<ButtonRelease-1>", self._placement_mouse_up)

    @staticmethod
    def _build_log_view(parent: tk.Widget, label: str) -> scrolledtext.ScrolledText:
        ttk.Label(parent, text=label, font=("Segoe UI", 9, "bold")).pack(
            anchor="w", pady=(14, 4)
        )
        view = scrolledtext.ScrolledText(
            parent,
            height=8,
            wrap="word",
            state="disabled",
            font=("Consolas", 9),
        )
        view.pack(fill="both", expand=True)
        return view

    def _drain_log_queue(self, schedule: bool = True) -> None:
        pending: dict[str, list[str]] = {"server": [], "client": []}
        for _ in range(500):
            try:
                role, line = self.log_queue.get_nowait()
            except queue.Empty:
                break
            if role in pending:
                pending[role].append(line)

        for role, lines in pending.items():
            if not lines:
                continue
            view = self.log_views[role]
            view.configure(state="normal")
            view.insert(tk.END, "".join(lines))
            view.see(tk.END)
            view.configure(state="disabled")

        if schedule and not self._closed:
            try:
                self.root.after(50, self._drain_log_queue)
            except tk.TclError:
                self._closed = True

    def _restore_log_streams(self) -> None:
        if sys.stdout is self._stdout_log_stream:
            sys.stdout = self._original_stdout
        if sys.stderr is self._stderr_log_stream:
            sys.stderr = self._original_stderr

    def _start_server(self) -> None:
        if self._closing:
            return
        self.server_status.set("Starting server…")
        self.start_server_button.configure(state="disabled")
        self.stop_server_button.configure(state="normal")
        if not self.server_runner.start():
            self.server_status.set(self.server_runner.detail or "Server is already running.")
        self._refresh_server_status()

    def _stop_server(self) -> None:
        if self.server_runner.stop():
            self.server_status.set("Stopping server and releasing input capture…")
            self.open_local_layout_button.configure(state="disabled")
            if self.dashboard_runner.server_url == f"http://127.0.0.1:{DEFAULT_PORT}":
                self.dashboard_runner.stop()
            self.root.after(100, self._refresh_server_status)

    def _refresh_server_status(self) -> None:
        state = self.server_runner.state
        if state == "starting":
            display = "Starting server…"
        elif state == "running":
            display = "Running"
        elif state == "stopping":
            display = "Stopping server and releasing input capture…"
        elif state == "failed":
            display = f"Failed: {self.server_runner.detail}"
        else:
            display = "Stopped"
        self.server_status.set(display)
        self.start_server_button.configure(
            state="disabled" if state in {"starting", "running", "stopping"} else "normal"
        )
        self.stop_server_button.configure(
            state="normal" if state in {"starting", "running", "stopping"} else "disabled"
        )
        self.open_local_layout_button.configure(
            state="normal" if state == "running" else "disabled"
        )
        if self.server_runner.is_running and not self._closing:
            self.root.after(250, self._refresh_server_status)

    def _scan_lan(self) -> None:
        if self._scan_in_progress or self._closing:
            return
        self._scan_in_progress = True
        self.scan_button.configure(state="disabled")
        self.scan_status.set("Scanning the local IPv4 /24 on TCP port 9090…")

        def scan() -> None:
            try:
                found = asyncio.run(megamind_client.scan_servers())
                error = ""
            except Exception as e:
                found = []
                error = f"{type(e).__name__}: {e}"
            try:
                self.root.after(0, lambda: self._show_scan_results(found, error))
            except tk.TclError:
                pass

        threading.Thread(target=scan, name="megamind-server-scan", daemon=True).start()

    def _show_scan_results(self, found: list[str], error: str) -> None:
        self._scan_in_progress = False
        self.scan_button.configure(state="normal")
        candidates = [f"http://{ip}:{DEFAULT_PORT}" for ip in found]
        self.candidate_box.configure(values=candidates)
        self.candidate.set("")
        if error:
            self.scan_status.set(f"Scan failed: {error}")
        elif candidates:
            self.scan_status.set(f"Found {len(candidates)} reachable host(s).")
        else:
            self.scan_status.set("No reachable hosts found; enter a server address manually.")

    def _use_candidate(self, _event=None) -> None:
        self.address.set(self.candidate.get())

    def _connect_client(self) -> None:
        if self._closing or self.client_runner.is_running:
            return
        try:
            server_url = normalize_server_url(self.address.get())
        except ValueError as e:
            self._apply_client_status("failed", str(e))
            return
        self.address.set(server_url)
        if self.client_runner.start(server_url):
            self._update_client_buttons()

    def _disconnect_client(self) -> None:
        if self.client_runner.stop():
            self.client_status.set("Disconnecting…")
            self._update_client_buttons()

    def _queue_client_status(self, state: str, detail: str) -> None:
        try:
            self.root.after(0, lambda: self._apply_client_status(state, detail))
        except tk.TclError:
            pass

    def _apply_client_status(self, state: str, detail: str) -> None:
        labels = {
            "connecting": "Connecting…",
            "connected": "Connected",
            "disconnected": "Disconnected; retrying when available",
            "failed": "Connection failed",
            "stopping": "Disconnecting…",
            "stopped": "Stopped",
        }
        self.client_status.set(labels.get(state, state.title()))
        self.client_detail.set(detail)
        self._update_client_buttons()

    def _update_client_buttons(self) -> None:
        running = self.client_runner.is_running
        self.connect_button.configure(state="disabled" if running else "normal")
        self.disconnect_button.configure(state="normal" if running else "disabled")

    def _open_local_layout(self) -> None:
        self._show_placement_view(f"http://127.0.0.1:{DEFAULT_PORT}")

    def _open_remote_layout(self) -> None:
        try:
            server_url = normalize_server_url(self.address.get())
        except ValueError as e:
            self._apply_client_status("failed", str(e))
            return
        self._show_placement_view(server_url)

    def _show_placement_view(self, server_url: str) -> None:
        self.placement_address.set(server_url)
        self.notebook.select(self.placement_tab)
        self._request_placement_connection(server_url)

    def _connect_placement(self) -> None:
        try:
            server_url = normalize_server_url(self.placement_address.get())
        except ValueError as e:
            self.placement_status.set(str(e))
            return
        self.placement_address.set(server_url)
        self._request_placement_connection(server_url)

    def _request_placement_connection(self, server_url: str) -> None:
        if self._closing:
            return
        if self.dashboard_runner.is_running:
            if self.dashboard_runner.server_url == server_url:
                if self.dashboard_runner.state == "stopping":
                    self._placement_pending_url = server_url
                    self._wait_for_placement_disconnect()
                else:
                    self._placement_pending_url = None
                self._update_placement_buttons()
                return
            self._placement_pending_url = server_url
            self.placement_status.set("Switching placement server…")
            self.dashboard_runner.stop()
            self._update_placement_buttons()
            self._wait_for_placement_disconnect()
            return
        self._start_placement_connection(server_url)

    def _start_placement_connection(self, server_url: str) -> None:
        self.placement_snapshot = {}
        self.placement_positions = {}
        self._placement_last_layout = None
        self._placement_pending_layouts = []
        self._render_placement_grid()
        if not self.dashboard_runner.start(server_url):
            self.placement_status.set("A placement connection is already starting.")
        self._update_placement_buttons()

    def _wait_for_placement_disconnect(self) -> None:
        if self._closing or self._placement_pending_url is None:
            return
        if self.dashboard_runner.is_running:
            self.root.after(50, self._wait_for_placement_disconnect)
            return
        server_url = self._placement_pending_url
        self._placement_pending_url = None
        self._start_placement_connection(server_url)

    def _disconnect_placement(self) -> None:
        self._placement_pending_url = None
        if self.dashboard_runner.stop():
            self.placement_status.set("Disconnecting from placement server…")
        self._update_placement_buttons()

    def _update_placement_buttons(self) -> None:
        running = self.dashboard_runner.is_running
        pending = self._placement_pending_url is not None
        self.placement_connect_button.configure(
            state="disabled" if running or pending else "normal"
        )
        self.placement_disconnect_button.configure(
            state="normal" if running else "disabled"
        )

    def _queue_dashboard_state(self, snapshot: dict) -> None:
        try:
            self.root.after(0, lambda: self._apply_dashboard_state(snapshot))
        except tk.TclError:
            pass

    def _queue_dashboard_status(self, state: str, detail: str) -> None:
        try:
            self.root.after(0, lambda: self._apply_dashboard_status(state, detail))
        except tk.TclError:
            pass

    def _apply_dashboard_status(self, state: str, detail: str) -> None:
        if state == "connecting":
            display = f"Connecting to {detail}…"
        elif state == "connected":
            display = f"Connected to {detail}"
        elif state == "stopping":
            display = detail
        elif state == "failed":
            display = f"Connection failed: {detail}"
        elif state == "disconnected":
            display = f"Disconnected: {detail}"
        else:
            display = "Not connected"
        self.placement_status.set(display)
        self._update_placement_buttons()
        if state in {"stopped", "failed", "disconnected"}:
            self.root.after(50, self._update_placement_buttons)

    def _apply_dashboard_state(self, snapshot: dict) -> None:
        clients = snapshot.get("clients", {})
        if not isinstance(clients, dict):
            clients = {}
        self.placement_snapshot = dict(snapshot)
        self.placement_snapshot["clients"] = clients
        machine_ids = ["server", *clients.keys()]
        layout = snapshot.get("layout", {})
        if not isinstance(layout, dict):
            layout = {}
        pending_index = next(
            (
                index
                for index, pending_layout in enumerate(self._placement_pending_layouts)
                if layout == pending_layout
            ),
            None,
        )
        local_update = pending_index is not None
        previous_positions = self.placement_positions
        if local_update:
            self._placement_pending_layouts = self._placement_pending_layouts[
                pending_index + 1 :
            ]
        elif (
            self._placement_last_layout is not None
            and layout != self._placement_last_layout
        ):
            previous_positions = {}
            self._placement_pending_layouts.clear()
        self.placement_positions = assign_positions(
            machine_ids,
            layout,
            previous_positions,
        )
        self._placement_last_layout = dict(layout)
        self._render_placement_grid()

    def _placement_grid_origin(self) -> tuple[int, int]:
        canvas = self.placement_canvas
        width = max(canvas.winfo_width(), GRID_SIZE * GRID_CELL_WIDTH)
        height = max(canvas.winfo_height(), GRID_SIZE * GRID_CELL_HEIGHT)
        return (
            max(0, (width - GRID_SIZE * GRID_CELL_WIDTH) // 2),
            max(0, (height - GRID_SIZE * GRID_CELL_HEIGHT) // 2),
        )

    def _render_placement_grid(self, _event=None) -> None:
        canvas = self.placement_canvas
        canvas.delete("all")
        self._placement_items.clear()
        origin_x, origin_y = self._placement_grid_origin()
        for row in range(GRID_SIZE):
            for column in range(GRID_SIZE):
                left = origin_x + column * GRID_CELL_WIDTH
                top = origin_y + row * GRID_CELL_HEIGHT
                canvas.create_rectangle(
                    left,
                    top,
                    left + GRID_CELL_WIDTH,
                    top + GRID_CELL_HEIGHT,
                    fill="#eef2f7",
                    outline="#cbd5e1",
                    width=2,
                )

        for machine_id, (row, column) in self.placement_positions.items():
            left = origin_x + column * GRID_CELL_WIDTH + 12
            top = origin_y + row * GRID_CELL_HEIGHT + 16
            card = canvas.create_rectangle(
                left,
                top,
                left + PLACEMENT_CARD_WIDTH,
                top + PLACEMENT_CARD_HEIGHT,
                fill="#dbeafe" if machine_id == "server" else "#dcfce7",
                outline="#3b82f6" if machine_id == "server" else "#22a06b",
                width=2,
            )
            if machine_id == "server":
                detail = self.placement_snapshot.get("server_ip", "")
                label = f"Server\n{detail}"
            else:
                info = self.placement_snapshot.get("clients", {}).get(machine_id, {})
                resolution = ""
                if isinstance(info, dict):
                    width = info.get("screen_width")
                    height = info.get("screen_height")
                    if width and height:
                        resolution = f"{width} × {height}"
                label = f"{machine_id}\n{resolution}".rstrip()
            text = canvas.create_text(
                left + PLACEMENT_CARD_WIDTH // 2,
                top + PLACEMENT_CARD_HEIGHT // 2,
                text=label,
                fill="#0f172a",
                width=PLACEMENT_CARD_WIDTH - 10,
                justify="center",
                font=("Segoe UI", 9, "bold"),
            )
            self._placement_items[machine_id] = (card, text)

    def _machine_at(self, x: float, y: float) -> str | None:
        for machine_id, (card, _text) in self._placement_items.items():
            bounds = self.placement_canvas.coords(card)
            if len(bounds) == 4 and bounds[0] <= x <= bounds[2] and bounds[1] <= y <= bounds[3]:
                return machine_id
        return None

    def _placement_cell_at(self, x: float, y: float) -> tuple[int, int] | None:
        origin_x, origin_y = self._placement_grid_origin()
        relative_x = x - origin_x
        relative_y = y - origin_y
        grid_width = GRID_SIZE * GRID_CELL_WIDTH
        grid_height = GRID_SIZE * GRID_CELL_HEIGHT
        if not (0 <= relative_x < grid_width and 0 <= relative_y < grid_height):
            return None
        return (
            int(relative_y // GRID_CELL_HEIGHT),
            int(relative_x // GRID_CELL_WIDTH),
        )

    def _placement_mouse_down(self, event) -> None:
        self._placement_drag_machine = self._machine_at(event.x, event.y)
        self._placement_drag_origin = (event.x, event.y)
        self._placement_dragged = False
        if self._placement_drag_machine is not None:
            self.placement_canvas.configure(cursor="hand2")

    def _placement_mouse_move(self, event) -> None:
        machine_id = self._placement_drag_machine
        if machine_id is None or self._placement_drag_origin is None:
            return
        start_x, start_y = self._placement_drag_origin
        if not self._placement_dragged and max(abs(event.x - start_x), abs(event.y - start_y)) < 6:
            return
        self._placement_dragged = True
        card, text = self._placement_items[machine_id]
        half_width = PLACEMENT_CARD_WIDTH // 2
        half_height = PLACEMENT_CARD_HEIGHT // 2
        self.placement_canvas.coords(
            card,
            event.x - half_width,
            event.y - half_height,
            event.x + half_width,
            event.y + half_height,
        )
        self.placement_canvas.coords(text, event.x, event.y)

    def _placement_mouse_up(self, event) -> None:
        machine_id = self._placement_drag_machine
        dragged = self._placement_dragged
        self._placement_drag_machine = None
        self._placement_drag_origin = None
        self._placement_dragged = False
        self.placement_canvas.configure(cursor="")
        if machine_id is None:
            return

        if not dragged:
            if not self.dashboard_runner.send({"type": "force_switch", "target": machine_id}):
                self.placement_status.set("Connect to a placement server before switching screens.")
            return

        target = self._placement_cell_at(event.x, event.y)
        if target is None:
            self._render_placement_grid()
            return
        try:
            updated_positions = swap_positions(
                self.placement_positions,
                machine_id,
                target,
            )
        except (KeyError, ValueError):
            self._render_placement_grid()
            return
        if updated_positions == self.placement_positions:
            self._render_placement_grid()
            return
        layout = build_layout(updated_positions)
        if not self.dashboard_runner.send({"type": "layout_update", "layout": layout}):
            self.placement_status.set("Connect to a placement server before rearranging screens.")
            self._render_placement_grid()
            return
        self._placement_pending_layouts.append(layout)
        self.placement_positions = updated_positions
        self._render_placement_grid()

    def _close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self.server_status.set("Stopping local services…")
        self.client_status.set("Stopping local services…")
        self.server_runner.stop()
        self.client_runner.stop()
        self.dashboard_runner.stop()
        self._placement_pending_url = None
        self.root.after(100, self._finish_close)

    def _finish_close(self) -> None:
        if (
            self.server_runner.is_running
            or self.client_runner.is_running
            or self.dashboard_runner.is_running
        ):
            self.root.after(100, self._finish_close)
            return
        self.server_runner.join(0)
        self.client_runner.join(0)
        self.dashboard_runner.join(0)
        self._drain_log_queue(schedule=False)
        self._closed = True
        self._restore_log_streams()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    ControlPanel(root)
    root.mainloop()


if __name__ == "__main__":
    main()