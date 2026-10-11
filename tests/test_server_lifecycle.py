import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import server


class ServerLifespanTests(unittest.IsolatedAsyncioTestCase):
    async def test_switching_between_clients_deactivates_previous_client(self):
        previous_socket = AsyncMock()
        target_socket = AsyncMock()
        clients = {
            "previous": {"ws": previous_socket},
            "target": {
                "ws": target_socket,
                "v_min_x": 0,
                "v_min_y": 0,
                "screen_width": 1920,
                "screen_height": 1080,
            },
        }
        with (
            patch.dict(server.clients, clients, clear=True),
            patch.object(server, "active", "previous"),
            patch.object(server, "_switching", False),
            patch.object(server, "_center", return_value=(960, 540)),
            patch.object(server, "_mouse_out", SimpleNamespace(position=(0, 0))),
            patch.object(server, "_set_input_mode", new=AsyncMock()),
            patch.object(server, "_broadcast_state", new=AsyncMock()),
        ):
            await server._switch_to("target", "right")

        previous_socket.send_text.assert_awaited_once_with('{"type": "set_inactive"}')
        target_message = target_socket.send_text.await_args.args[0]
        self.assertEqual(json.loads(target_message)["type"], "set_active")

    async def test_stale_client_return_edge_does_not_switch_control_to_server(self):
        switch_to_server = AsyncMock()
        with (
            patch.object(server, "active", "current-client"),
            patch.object(server, "_switch_to_server", new=switch_to_server),
        ):
            handled = await server._switch_back_to_server_if_active(
                "previous-client",
                100,
                200,
            )

        self.assertFalse(handled)
        switch_to_server.assert_not_awaited()

    async def test_lifespan_stops_raw_capture_and_input_listeners(self):
        mouse_listener = Mock()
        keyboard_listener = Mock()
        stop_raw_capture = Mock()
        rawinput = SimpleNamespace(
            start_raw_mouse_capture=Mock(return_value=stop_raw_capture)
        )

        with (
            patch.object(server.screens, "get_virtual_bounds", return_value=(0, 0, 1920, 1080)),
            patch.object(server, "get_local_ip", return_value="192.168.1.10"),
            patch.object(server, "_use_rawinput", True),
            patch.object(server, "_rawinput", rawinput),
            patch.object(server, "restart_mouse"),
            patch.object(server, "restart_keyboard"),
            patch.object(server.clipboard_sync, "available", return_value=False),
            patch.object(server, "_mouse_listener", mouse_listener),
            patch.object(server, "_keyboard_listener", keyboard_listener),
        ):
            async with server.lifespan(server.app):
                pass

        rawinput.start_raw_mouse_capture.assert_called_once()
        stop_raw_capture.assert_called_once()
        mouse_listener.stop.assert_called_once()
        keyboard_listener.stop.assert_called_once()

    async def test_lifespan_stops_clipboard_monitor(self):
        mouse_listener = Mock()
        keyboard_listener = Mock()
        clipboard_monitor = Mock()
        with (
            patch.object(server.screens, "get_virtual_bounds", return_value=(0, 0, 1920, 1080)),
            patch.object(server, "get_local_ip", return_value="192.168.1.10"),
            patch.object(server, "_use_rawinput", False),
            patch.object(server, "restart_mouse"),
            patch.object(server, "restart_keyboard"),
            patch.object(server.clipboard_sync, "available", return_value=True),
            patch.object(server.clipboard_sync, "start_monitor", return_value=clipboard_monitor) as start_monitor,
            patch.object(server, "_mouse_listener", mouse_listener),
            patch.object(server, "_keyboard_listener", keyboard_listener),
        ):
            async with server.lifespan(server.app):
                stop_event = start_monitor.call_args.kwargs["stop_event"]
                self.assertFalse(stop_event.is_set())

        self.assertTrue(stop_event.is_set())
        clipboard_monitor.join.assert_called_once_with()

    async def test_lifespan_stops_clipboard_monitor_when_shutdown_raises(self):
        clipboard_monitor = Mock()
        with (
            patch.object(server.screens, "get_virtual_bounds", return_value=(0, 0, 1920, 1080)),
            patch.object(server, "get_local_ip", return_value="192.168.1.10"),
            patch.object(server, "_use_rawinput", False),
            patch.object(server, "restart_mouse"),
            patch.object(server, "restart_keyboard"),
            patch.object(server.clipboard_sync, "available", return_value=True),
            patch.object(server.clipboard_sync, "start_monitor", return_value=clipboard_monitor) as start_monitor,
            patch.object(server, "_mouse_listener", None),
            patch.object(server, "_keyboard_listener", None),
        ):
            with self.assertRaisesRegex(RuntimeError, "shutdown failure"):
                async with server.lifespan(server.app):
                    stop_event = start_monitor.call_args.kwargs["stop_event"]
                    raise RuntimeError("shutdown failure")

        self.assertTrue(stop_event.is_set())
        clipboard_monitor.join.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()