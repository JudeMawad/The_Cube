"""Real registry and HTTP broker tests. No client hardware is contacted."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from core.tool_registry import ToolRegistry, ToolContext, ToolValidationError
from core.tool_execution import execute_tool
from core.schemas import ToolIntent
from features.cube import ControlChannel, CubeCommands, control_router
from features.cube.protocol import Command, Result, Status, VolumeStatus
from features.cube.tools import register_tools
from features.cube.routes import authenticate

SESSION = "a" * 32
REQUEST = "b" * 32


class ToolsTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.channel = Mock()
        self.registry = ToolRegistry()
        register_tools(self.registry, commands=CubeCommands(self.channel))
        self.context = ToolContext("pi", SESSION, REQUEST)

    async def run_tool(self, operation, arguments=None, context=None):
        bound = self.registry.validate_intent(ToolIntent(type="tool", tool="cube." + operation,
                                                        arguments=arguments or {}))
        return await execute_tool(bound, context or self.context)

    def test_exact_schemas(self):
        self.assertEqual([tool.name for tool in self.registry.list_executable_tools()], [
            "cube.set_display", "cube.set_brightness", "cube.shutdown", "cube.reboot", "cube.get_status",
            "cube.get_volume", "cube.set_volume", "cube.adjust_volume", "cube.mute", "cube.unmute"])
        for tool in self.registry.list_executable_tools():
            schema = tool.parameters
            self.assertIs(schema["additionalProperties"], False)
            if "state" in schema["properties"]:
                self.assertEqual(schema["properties"]["state"]["enum"], ["on", "off"])
            elif "percent" in schema["properties"]:
                field = schema["properties"]["percent"]
                self.assertEqual((field["type"], field["minimum"], field["maximum"]), ("integer", 0, 100))
            elif "delta" in schema["properties"]:
                field = schema["properties"]["delta"]
                self.assertEqual((field["type"], field["minimum"], field["maximum"]), ("integer", -20, 20))
            else:
                self.assertEqual(schema["properties"], {})

    async def test_scoped_dispatch_and_safe_results(self):
        self.channel.request.return_value = Result(session_id=SESSION, success=True)
        for operation, arguments in [("set_display", {"state": "off"}), ("set_display", {"state": "on"}),
                                     ("set_brightness", {"percent": 30}), ("shutdown", {}), ("reboot", {})]:
            with self.subTest(operation=operation, arguments=arguments):
                result = await self.run_tool(operation, arguments)
                self.assertTrue(result.success)
                self.channel.request.assert_called_with("pi", SESSION, REQUEST, operation, **arguments)
                self.assertNotIn(SESSION, result.model_dump_json())
                self.assertNotIn(REQUEST, result.model_dump_json())
                self.assertNotIn("command_id", result.model_dump_json())
        self.assertEqual(result.data["reply"]["response"], "Restarting.")

    async def test_status_mapping(self):
        status = Status(display_available=True, display_enabled=False,
                        master_brightness_percent=30, uptime_seconds=123.0)
        self.channel.request.return_value = Result(session_id=SESSION, success=True, status=status)
        result = await self.run_tool("get_status")
        self.assertTrue(result.success)
        self.assertEqual(result.data["status"]["master_brightness_percent"], 30)
        self.assertIn("brightness is 30 percent", result.data["reply"]["response"])
        self.assertIn("display is off", result.data["reply"]["response"])

    async def test_volume_tools_return_live_pi_readback(self):
        for operation, arguments, muted, expected in [
            ("get_volume", {}, False, "Volume is 45 percent."),
            ("set_volume", {"percent": 45}, False, "Volume 45 percent."),
            ("adjust_volume", {"delta": 5}, False, "Volume 45 percent."),
            ("mute", {}, True, "Muted."),
            ("unmute", {}, False, "Unmuted. Volume 45 percent."),
        ]:
            with self.subTest(operation=operation):
                volume = VolumeStatus(sink="alsa_output.reSpeaker", channels={"mono": 45}, muted=muted)
                self.channel.request.return_value = Result(session_id=SESSION, success=True, volume=volume)
                result = await self.run_tool(operation, arguments)
                self.assertTrue(result.success)
                self.assertEqual(result.data["reply"]["response"], expected)
                self.assertEqual(result.data["volume"]["channels"], {"mono": 45})
                self.channel.request.assert_called_with("pi", SESSION, REQUEST, operation, **arguments)
        self.channel.request.return_value = Result(session_id=SESSION, success=False, error="audio_unavailable")
        result = await self.run_tool("get_volume")
        self.assertFalse(result.success)
        self.assertIn("selected speaker output", result.data["reply"]["response"])

    def test_status_validates_both_brightness_fields(self):
        data = dict(display_available=True, display_enabled=True,
                    master_brightness_percent=50)
        self.assertEqual(Status(**data).master_brightness_percent, 50)
        for field in ("master_brightness_percent",):
            for invalid in (None, True, -1, 101, "50"):
                with self.subTest(field=field, invalid=invalid), self.assertRaises(ValidationError):
                    Status(**{**data, field: invalid})

    async def test_unavailable_and_failed_results(self):
        from features.cube.channel import ClientUnavailable
        self.channel.request.side_effect = ClientUnavailable()
        result = await self.run_tool("shutdown")
        self.assertFalse(result.success)
        self.assertEqual(result.data["reply"]["response"], "I couldn't reach the Cube.")
        self.channel.request.reset_mock(side_effect=True)
        result = await self.run_tool("shutdown", context=ToolContext("pi"))
        self.assertFalse(result.success)
        self.channel.request.assert_not_called()
        self.channel.request.return_value = Result(session_id=SESSION, success=False, error="power_unavailable")
        result = await self.run_tool("reboot")
        self.assertFalse(result.success)
        self.assertEqual(result.error, "power_unavailable")

    async def test_invalid_arguments_never_dispatch(self):
        for operation, arguments in [("set_display", {"state": "toggle"}), ("set_animation", {"state": False}),
                                     ("shutdown", {"command": "reboot"}), ("reboot", {"client_id": "other"})] + [
                ("set_brightness", {"percent": x}) for x in [-1, 101, 30.0, True, "30"]] + [
                ("set_volume", {"percent": x}) for x in [-1, 101, 30.0, True, "30"]] + [
                ("adjust_volume", {"delta": x}) for x in [-21, 0, 21, True, 5.0, "5"]]:
            with self.subTest(operation=operation, arguments=arguments), self.assertRaises(ToolValidationError):
                await self.run_tool(operation, arguments)
        self.channel.request.assert_not_called()

    async def test_other_tools_are_unaffected(self):
        from features.lights.tools import register_tools as lights
        govee = Mock()
        lights(self.registry, control_lights=govee, power_commands=Mock(), continuation=lambda _: {})
        bound = self.registry.validate_intent(ToolIntent(type="tool", tool="lights.set_brightness", arguments={"percent": 30}))
        await execute_tool(bound, ToolContext("pi"))
        self.channel.request.assert_not_called()
        govee.assert_called_once()


class ChannelTests(TestCase):
    def setUp(self):
        self.channel = ControlChannel(timeout=0.3)
        self.addCleanup(self.channel.close)
        self.app = FastAPI()
        self.app.include_router(control_router(self.channel))
        self.app.dependency_overrides[authenticate] = lambda: None
        self.web = TestClient(self.app)
        self.addCleanup(self.web.close)
        self.headers = {"X-Cube-Client-ID": "pi"}
        self.assertEqual(self.web.post("/cube/control/session", json={"session_id": SESSION}, headers=self.headers).status_code, 200)

    def test_http_delivery_once_and_matching_result(self):
        with ThreadPoolExecutor() as pool:
            future = pool.submit(self.channel.request, "pi", SESSION, REQUEST, "set_display", state="off")
            delivery = self.web.get("/cube/control/next", params={"session_id": SESSION}, headers=self.headers)
            self.assertEqual(delivery.status_code, 200)
            command = delivery.json()
            self.assertEqual(command["operation"], "set_display")
            self.assertEqual(command["state"], "off")
            self.assertEqual(self.web.get("/cube/control/next", params={"session_id": SESSION, "wait": 0}, headers=self.headers).status_code, 204)
            url = "/cube/control/" + command["command_id"] + "/result"
            payload = {"session_id": SESSION, "success": True}
            self.assertEqual(self.web.post(url, json=payload, headers={"X-Cube-Client-ID": "other"}).status_code, 409)
            self.assertEqual(self.web.post(url, json=payload, headers=self.headers).status_code, 200)
            self.assertTrue(future.result().success)
            self.assertEqual(self.web.post(url, json=payload, headers=self.headers).status_code, 409)

    def test_session_replacement_discards_power(self):
        from features.cube.channel import ClientUnavailable
        with ThreadPoolExecutor() as pool:
            future = pool.submit(self.channel.request, "pi", SESSION, REQUEST, "shutdown")
            command = self.channel.next("pi", SESSION)
            self.channel.register("pi", "c" * 32)
            with self.assertRaises(ClientUnavailable):
                future.result()
            with self.assertRaises(ClientUnavailable):
                self.channel.complete("pi", command.command_id, Result(session_id=SESSION, success=True))
            self.assertIsNone(self.channel.next("pi", "c" * 32, 0))

    def test_timeout_does_not_leave_queued_command(self):
        from features.cube.channel import ClientUnavailable
        self.channel.timeout = 0.001
        with self.assertRaises(ClientUnavailable):
            self.channel.request("pi", SESSION, REQUEST, "reboot")
        self.assertIsNone(self.channel.next("pi", SESSION, 0))

    def test_volume_wire_rejects_invalid_and_incomplete_results(self):
        valid = dict(session_id=SESSION, request_id=REQUEST, command_id="c" * 32)
        self.assertEqual(Command(**valid, operation="set_volume", percent=0).percent, 0)
        self.assertEqual(Command(**valid, operation="adjust_volume", delta=5).delta, 5)
        for operation, arguments in [
            ("set_volume", {"percent": 101}), ("set_volume", {"percent": True}),
            ("adjust_volume", {"delta": 0}), ("adjust_volume", {"delta": 21}),
            ("mute", {"percent": 50}), ("get_volume", {"delta": 5}),
        ]:
            with self.subTest(operation=operation, arguments=arguments), self.assertRaises(ValidationError):
                Command(**valid, operation=operation, **arguments)
        with self.assertRaises(ValidationError):
            VolumeStatus(sink="alsa_output.reSpeaker", channels={"mono": True}, muted=False)
        with self.assertRaises(ValidationError):
            Result(session_id=SESSION, success=False, error="audio_unavailable",
                   volume=VolumeStatus(sink="alsa_output.reSpeaker", channels={"mono": 40}, muted=False))

    def test_authentication_and_strict_results(self):
        self.app.dependency_overrides.clear()
        with patch("pathlib.Path.read_text", return_value="t" * 40):
            response = self.web.get("/cube/control/next", params={"session_id": SESSION, "wait": 0}, headers=self.headers)
            self.assertEqual(response.status_code, 401)
            response = self.web.get("/cube/control/next", params={"session_id": SESSION, "wait": 0}, headers={**self.headers, "X-Cube-Token": "t" * 40})
            self.assertEqual(response.status_code, 204)
        for payload in [{"success": True, "error": "control_failed"}, {"success": True, "status": {"display_available": True}},
                        {"success": False, "error": "PRIVATE_VENDOR_BODY"}, {"success": True, "command": "poweroff"}]:
            with self.assertRaises(ValidationError):
                Result(session_id=SESSION, **payload)
