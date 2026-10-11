import asyncio
import io
import os
import queue
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import client
from control_panel import (
    ClientRunner,
    RoleLogRouter,
    RoutedLogStream,
    ServerRunner,
    normalize_server_url,
)


class FakeWebSocket:
    def __init__(self, response=None):
        self.response = response
        self.sent = []

    async def send(self, message):
        self.sent.append(message)

    async def recv(self):
        if self.response is None:
            await asyncio.Future()
        return self.response


class FakeServer:
    def __init__(self):
        self.should_exit = False
        self.started = False
        self.started_event = threading.Event()
        self.cleaned_up = threading.Event()

    def run(self):
        self.started = True
        self.started_event.set()
        while not self.should_exit:
            self.cleaned_up.wait(0.01)
        self.cleaned_up.set()


class LoggingFakeServer:
    def __init__(self):
        self.should_exit = False
        self.started = False
        self.started_event = threading.Event()
        self._wait_event = threading.Event()

    def run(self):
        self.started = True
        print("server stdout")
        print("server stderr", file=sys.stderr)
        self.started_event.set()
        while not self.should_exit:
            self._wait_event.wait(0.01)


class FakeLogView:
    def __init__(self):
        self.content = ""
        self.state = "disabled"
        self.scrolled_to = None

    def configure(self, *, state):
        self.state = state

    def insert(self, _index, text):
        self.content += text

    def see(self, index):
        self.scrolled_to = index


class FakeRoot:
    def __init__(self):
        self.scheduled = []

    def after(self, delay, callback):
        self.scheduled.append((delay, callback))


class ControlPanelTests(unittest.TestCase):
    def test_normalize_server_address_or_url(self):
        self.assertEqual(
            normalize_server_url(" 192.168.1.10 "),
            "http://192.168.1.10:9090",
        )
        self.assertEqual(
            normalize_server_url("https://megamind.example:9443/"),
            "https://megamind.example:9443",
        )
        self.assertEqual(
            normalize_server_url("http://[::1]"),
            "http://[::1]:9090",
        )

    def test_reject_invalid_manual_server_urls(self):
        for value in ("", "ftp://host", "host/path", "host:70000", "http://user@host"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_server_url(value)

    def test_remote_placement_button_uses_selected_client_server_address(self):
        from control_panel import ControlPanel

        panel = ControlPanel.__new__(ControlPanel)
        panel.address = Mock()
        panel.address.get.return_value = "https://megamind.example:9443/"
        panel._show_placement_view = Mock()
        panel._apply_client_status = Mock()

        panel._open_remote_layout()

        panel._show_placement_view.assert_called_once_with(
            "https://megamind.example:9443"
        )
        panel._apply_client_status.assert_not_called()

    def test_scan_servers_probes_current_ipv4_subnet_and_excludes_local_host(self):
        async def fake_probe(ip):
            return ip if ip in {"192.168.1.10", "192.168.1.25"} else None

        with (
            patch("client._get_local_ip", return_value="192.168.1.25"),
            patch("client._probe", side_effect=fake_probe),
        ):
            found = asyncio.run(client.scan_servers())

        self.assertEqual(found, ["192.168.1.10"])

    def test_probe_reports_open_and_closed_tcp_ports(self):
        async def verify_probe():
            async def close_connection(reader, writer):
                writer.close()
                await writer.wait_closed()

            listener = await asyncio.start_server(close_connection, "127.0.0.1", 0)
            port = listener.sockets[0].getsockname()[1]
            try:
                with patch("client.SERVER_PORT", port):
                    self.assertEqual(await client._probe("127.0.0.1", timeout=1), "127.0.0.1")
                listener.close()
                await listener.wait_closed()
                with patch("client.SERVER_PORT", port):
                    self.assertIsNone(await client._probe("127.0.0.1", timeout=0.1))
            finally:
                listener.close()
                await listener.wait_closed()

        asyncio.run(verify_probe())

    def test_registration_requires_megamind_acknowledgement(self):
        websocket = FakeWebSocket('{"type":"registered"}')
        asyncio.run(client.register_with_server(websocket, {"type": "register"}))
        self.assertEqual(len(websocket.sent), 1)

        impostor = FakeWebSocket('{"type":"not_megamind"}')
        with self.assertRaises(ConnectionError):
            asyncio.run(client.register_with_server(impostor, {"type": "register"}))

    def test_registration_times_out_without_server_acknowledgement(self):
        websocket = FakeWebSocket()
        with self.assertRaises(ConnectionError):
            asyncio.run(
                client.register_with_server(
                    websocket,
                    {"type": "register"},
                    timeout=0.01,
                )
            )

    def test_client_runner_disconnects_cleanly_when_stopped(self):
        async def fake_run(server_url, *, stop_event, on_status):
            on_status("connected", server_url)
            while not stop_event.is_set():
                await asyncio.sleep(0.01)
            on_status("stopped", "")

        runner = ClientRunner(run_client=fake_run)
        self.assertTrue(runner.start("http://192.168.1.10:9090"))
        self.assertTrue(self._wait_for(lambda: runner.state == "connected"))

        runner.stop()
        runner.join(1)

        self.assertFalse(runner.is_running)
        self.assertEqual(runner.state, "stopped")

    def test_concurrent_runners_route_stdout_and_stderr_to_their_own_logs(self):
        log_queue = queue.Queue()
        router = RoleLogRouter(log_queue)
        stdout = RoutedLogStream(io.StringIO(), router, "stdout")
        stderr = RoutedLogStream(io.StringIO(), router, "stderr")
        fake_server = LoggingFakeServer()

        async def fake_client(server_url, *, stop_event, on_status):
            print("client stdout")
            print("client stderr", file=sys.stderr)
            on_status("connected", server_url)
            while not stop_event.is_set():
                await asyncio.sleep(0.01)

        def create_server():
            print("server factory stdout")
            print("server factory stderr", file=sys.stderr)
            return fake_server

        server_runner = ServerRunner(
            server_factory=create_server,
            log_router=router,
        )
        client_runner = ClientRunner(run_client=fake_client, log_router=router)

        try:
            with patch("sys.stdout", stdout), patch("sys.stderr", stderr):
                self.assertTrue(server_runner.start())
                self.assertTrue(client_runner.start("http://localhost:9090"))
                self.assertTrue(
                    self._wait_for(
                        lambda: server_runner.state == "running"
                        and client_runner.state == "connected"
                    )
                )
                self.assertTrue(self._wait_for(lambda: log_queue.qsize() >= 6))
                client_runner.stop()
                server_runner.stop()
                client_runner.join(1)
                server_runner.join(1)

            captured = {"server": [], "client": []}
            while not log_queue.empty():
                role, line = log_queue.get_nowait()
                captured[role].append(line)

            self.assertIn("server stdout\n", captured["server"])
            self.assertIn("server stderr\n", captured["server"])
            self.assertIn("server factory stdout\n", captured["server"])
            self.assertIn("server factory stderr\n", captured["server"])
            self.assertIn("client stdout\n", captured["client"])
            self.assertIn("client stderr\n", captured["client"])
            self.assertFalse(server_runner.is_running)
            self.assertFalse(client_runner.is_running)
        finally:
            client_runner.stop()
            server_runner.stop()
            client_runner.join(1)
            server_runner.join(1)

    def test_prefixed_background_output_is_routed_without_runner_context(self):
        log_queue = queue.Queue()
        router = RoleLogRouter(log_queue)
        stdout = RoutedLogStream(io.StringIO(), router, "stdout")

        threads = [
            threading.Thread(
                target=stdout.write,
                args=(f"[{role}] background output\n",),
            )
            for role in ("server", "client")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(1)

        captured = {"server": [], "client": []}
        while not log_queue.empty():
            role, line = log_queue.get_nowait()
            captured[role].append(line)
        self.assertEqual(captured["server"], ["[server] background output\n"])
        self.assertEqual(captured["client"], ["[client] background output\n"])

    def test_log_stream_captures_output_when_windowed_console_is_suppressed(self):
        log_queue = queue.Queue()
        router = RoleLogRouter(log_queue)

        with open(os.devnull, "w", encoding="utf-8") as devnull:
            stream = RoutedLogStream(devnull, router, "stderr")
            with router.for_role("client"):
                stream.write("windowed client error\n")

        self.assertEqual(
            log_queue.get_nowait(),
            ("client", "windowed client error\n"),
        )

    def test_log_queue_updates_separate_scrollable_views_without_blocking(self):
        from control_panel import ControlPanel

        panel = ControlPanel.__new__(ControlPanel)
        panel.root = FakeRoot()
        panel.log_queue = queue.Queue()
        panel.log_views = {
            "server": FakeLogView(),
            "client": FakeLogView(),
        }
        panel._closed = False
        panel.log_queue.put(("server", "server line\n"))
        panel.log_queue.put(("client", "client line\n"))

        panel._drain_log_queue()

        self.assertEqual(panel.log_views["server"].content, "server line\n")
        self.assertEqual(panel.log_views["client"].content, "client line\n")
        for view in panel.log_views.values():
            self.assertEqual(view.state, "disabled")
            self.assertEqual(view.scrolled_to, "end")
        self.assertEqual(len(panel.root.scheduled), 1)
        self.assertEqual(panel.root.scheduled[0][0], 50)

    def test_placement_click_sends_force_switch_message(self):
        from control_panel import ControlPanel

        panel = ControlPanel.__new__(ControlPanel)
        panel.placement_canvas = Mock()
        panel.placement_status = Mock()
        panel.dashboard_runner = Mock()
        panel.dashboard_runner.send.return_value = True
        panel._placement_drag_machine = "192.168.1.10"
        panel._placement_dragged = False
        panel._placement_drag_origin = (30, 30)

        panel._placement_mouse_up(SimpleNamespace(x=30, y=30))

        panel.dashboard_runner.send.assert_called_once_with({
            "type": "force_switch",
            "target": "192.168.1.10",
        })
        panel.placement_canvas.configure.assert_called_once_with(cursor="")

    def test_client_set_inactive_cancels_return_edge_monitor(self):
        edge_monitor = Mock()
        edge_monitor.done.return_value = False
        with (
            patch.object(client, "active", True),
            patch.object(client, "_edge_monitor_task", edge_monitor),
        ):
            asyncio.run(client.handle_message('{"type":"set_inactive"}', object()))

            self.assertFalse(client.active)
            edge_monitor.cancel.assert_called_once_with()

    def test_placement_drag_sends_swapped_layout_update(self):
        from control_panel import ControlPanel

        panel = ControlPanel.__new__(ControlPanel)
        panel.placement_canvas = Mock()
        panel.placement_canvas.winfo_width.return_value = 450
        panel.placement_canvas.winfo_height.return_value = 300
        panel.placement_status = Mock()
        panel.dashboard_runner = Mock()
        panel.dashboard_runner.send.return_value = True
        panel.placement_positions = {
            "server": (1, 1),
            "192.168.1.10": (1, 2),
        }
        panel._placement_pending_layouts = []
        panel._placement_drag_machine = "192.168.1.10"
        panel._placement_dragged = True
        panel._placement_drag_origin = (375, 150)
        panel._render_placement_grid = Mock()

        panel._placement_mouse_up(SimpleNamespace(x=225, y=150))

        panel.dashboard_runner.send.assert_called_once_with({
            "type": "layout_update",
            "layout": {
                "server": {"left": "192.168.1.10"},
                "192.168.1.10": {"right": "server"},
            },
        })
        self.assertEqual(
            panel.placement_positions,
            {"server": (1, 2), "192.168.1.10": (1, 1)},
        )
        panel._render_placement_grid.assert_called_once_with()

    def test_external_placement_layout_update_repositions_existing_machine(self):
        from control_panel import ControlPanel

        panel = ControlPanel.__new__(ControlPanel)
        panel.placement_positions = {
            "server": (1, 1),
            "192.168.1.10": (1, 2),
        }
        panel._placement_last_layout = {
            "server": {"right": "192.168.1.10"},
            "192.168.1.10": {"left": "server"},
        }
        panel._placement_pending_layouts = []
        panel._render_placement_grid = Mock()

        panel._apply_dashboard_state({
            "clients": {"192.168.1.10": {"screen_width": 1920, "screen_height": 1080}},
            "layout": {
                "server": {"left": "192.168.1.10"},
                "192.168.1.10": {"right": "server"},
            },
        })

        self.assertEqual(
            panel.placement_positions,
            {"server": (1, 1), "192.168.1.10": (1, 0)},
        )
        self.assertEqual(panel._placement_last_layout["server"], {"left": "192.168.1.10"})
        panel._render_placement_grid.assert_called_once_with()

    def test_rapid_local_layout_updates_keep_the_latest_positions(self):
        from control_panel import ControlPanel

        machine_id = "192.168.1.10"
        first_local_layout = {
            "server": {"left": machine_id},
            machine_id: {"right": "server"},
        }
        latest_local_layout = {
            "server": {"top": machine_id},
            machine_id: {"bottom": "server"},
        }
        panel = ControlPanel.__new__(ControlPanel)
        panel.placement_positions = {"server": (1, 1), machine_id: (0, 1)}
        panel._placement_last_layout = {
            "server": {"right": machine_id},
            machine_id: {"left": "server"},
        }
        panel._placement_pending_layouts = [
            first_local_layout,
            latest_local_layout,
        ]
        panel._render_placement_grid = Mock()
        clients = {machine_id: {"screen_width": 1920, "screen_height": 1080}}

        panel._apply_dashboard_state({"clients": clients, "layout": first_local_layout})
        panel._apply_dashboard_state({"clients": clients, "layout": latest_local_layout})

        self.assertEqual(
            panel.placement_positions,
            {"server": (1, 1), machine_id: (0, 1)},
        )
        self.assertEqual(panel._placement_pending_layouts, [])
        panel._render_placement_grid.assert_has_calls([call(), call()])

    def test_server_runner_stop_allows_service_cleanup(self):
        fake_server = FakeServer()
        runner = ServerRunner(server_factory=lambda: fake_server)
        self.assertTrue(runner.start())
        self.assertTrue(fake_server.started_event.wait(1))
        self.assertTrue(self._wait_for(lambda: runner.state == "running"))

        runner.stop()
        runner.join(1)

        self.assertFalse(runner.is_running)
        self.assertTrue(fake_server.cleaned_up.is_set())
        self.assertEqual(runner.state, "stopped")

    @staticmethod
    def _wait_for(predicate, timeout=1):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            threading.Event().wait(0.01)
        return predicate()


if __name__ == "__main__":
    unittest.main()