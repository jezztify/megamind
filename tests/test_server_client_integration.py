import socket
import threading
import time
import unittest
from unittest.mock import patch
from urllib.request import urlopen

import client
import server
import uvicorn

from control_panel import ClientRunner, DashboardRunner


class ServerClientIntegrationTests(unittest.TestCase):
    def test_client_registers_places_machine_and_disconnects(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]

        app_server = uvicorn.Server(
            uvicorn.Config(
                server.app,
                host="127.0.0.1",
                port=port,
                log_level="critical",
            )
        )
        server_thread = threading.Thread(target=app_server.run, daemon=True)
        runner = ClientRunner()
        dashboard_states = []
        dashboard_state_received = threading.Event()
        placement_state_received = threading.Event()
        empty_layout_received = threading.Event()
        force_switch_received = threading.Event()
        client_switch_received = threading.Event()
        client_switch_calls = []

        async def record_force_switch(_x, _y):
            force_switch_received.set()

        async def record_client_switch(machine_id, direction):
            client_switch_calls.append((machine_id, direction))
            client_switch_received.set()

        def on_dashboard_state(snapshot):
            dashboard_states.append(snapshot)
            dashboard_state_received.set()
            if snapshot.get("layout", {}).get("server", {}).get("left") == "127.0.0.1":
                placement_state_received.set()
            if snapshot.get("layout") == {}:
                empty_layout_received.set()

        dashboard_runner = DashboardRunner(on_state=on_dashboard_state)

        with (
            patch.object(server, "PORT", port),
            patch.object(server, "_use_rawinput", False),
            patch.object(server, "restart_mouse"),
            patch.object(server, "restart_keyboard"),
            patch.object(server, "_switch_to_server", record_force_switch),
            patch.object(server, "_switch_to", record_client_switch),
            patch.object(server.clipboard_sync, "available", return_value=False),
            patch.object(client.clipboard_sync, "available", return_value=False),
        ):
            server_thread.start()
            try:
                self.assertTrue(self._wait_for(lambda: app_server.started, timeout=5))
                self.assertTrue(runner.start(f"http://127.0.0.1:{port}"))
                self.assertTrue(self._wait_for(lambda: runner.state == "connected", timeout=5))
                self.assertTrue(dashboard_runner.start(f"http://127.0.0.1:{port}"))
                self.assertTrue(dashboard_state_received.wait(5))
                self.assertIn("127.0.0.1", dashboard_states[-1]["clients"])
                self.assertTrue(dashboard_runner.send({
                    "type": "layout_update",
                    "layout": {
                        "server": {"left": "127.0.0.1"},
                        "127.0.0.1": {"right": "server"},
                    },
                }))
                self.assertTrue(placement_state_received.wait(5))
                self.assertTrue(dashboard_runner.send({
                    "type": "force_switch",
                    "target": "server",
                }))
                self.assertTrue(force_switch_received.wait(5))
                self.assertTrue(dashboard_runner.send({
                    "type": "force_switch",
                    "target": "127.0.0.1",
                }))
                self.assertTrue(client_switch_received.wait(5))
                self.assertEqual(client_switch_calls, [("127.0.0.1", "left")])
                empty_layout_received.clear()
                client_switch_received.clear()
                self.assertTrue(dashboard_runner.send({
                    "type": "layout_update",
                    "layout": {},
                }))
                self.assertTrue(empty_layout_received.wait(5))
                self.assertTrue(dashboard_runner.send({
                    "type": "force_switch",
                    "target": "127.0.0.1",
                }))
                self.assertTrue(client_switch_received.wait(5))
                self.assertEqual(
                    client_switch_calls,
                    [("127.0.0.1", "left"), ("127.0.0.1", "right")],
                )

                with urlopen(f"http://127.0.0.1:{port}/", timeout=2) as response:
                    self.assertEqual(response.status, 200)
                    self.assertIn(b'id="workspace"', response.read())

                runner.stop()
                runner.join(5)
                self.assertFalse(runner.is_running)
                dashboard_runner.stop()
                dashboard_runner.join(5)
                self.assertFalse(dashboard_runner.is_running)
                self.assertEqual(dashboard_runner.state, "stopped")
                self.assertTrue(
                    self._wait_for(lambda: "127.0.0.1" not in server.clients, timeout=5)
                )
            finally:
                runner.stop()
                runner.join(5)
                dashboard_runner.stop()
                dashboard_runner.join(5)
                app_server.should_exit = True
                server_thread.join(5)
                server.layout.clear()

        self.assertFalse(server_thread.is_alive())

    @staticmethod
    def _wait_for(predicate, timeout):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            threading.Event().wait(0.02)
        return predicate()


if __name__ == "__main__":
    unittest.main()