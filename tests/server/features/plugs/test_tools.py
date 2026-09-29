from contextlib import redirect_stdout
import importlib.util
import io
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from core.schemas import ToolIntent
from core.tool_execution import execute_tool
from core.tool_registry import ToolContext, ToolRegistry, ToolValidationError
from features.cube.tools import tool_definitions as cube_definitions
from features.lights import LightsCommands
from features.lights.tools import register_tools as register_light_tools, tool_definitions as light_definitions
from features.media.tools import MediaTools, tool_definitions as media_definitions
from features.plugs import PlugCommands, tool_definitions
from features.plugs.tools import register_tools
from integrations import tuya
from tests.paths import ROOT


PRIVATE = "PRIVATE_DEVICE_SECRET_TOKEN_SENTINEL"


class PlugToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        client_type = tuya.TuyaClient
        factory = patch("integrations.tuya.TuyaClient")
        self.factory = factory.start()
        self.addCleanup(factory.stop)
        self.client = Mock(spec=client_type)
        self.factory.return_value = self.client
        self.client.get_device_info.return_value = SimpleNamespace(online=True, id=PRIVATE, name=PRIVATE)
        self.client.is_on.return_value = True
        self.client.set_power.return_value = None
        self.commands = PlugCommands()
        self.addCleanup(self.commands.close)
        self.continuation = Mock(return_value={"listen_for_seconds": 10, "context_expires_in": 40})
        self.registry = ToolRegistry()
        register_tools(self.registry, commands=self.commands, continuation=self.continuation)
        guard = patch("socket.socket.connect", side_effect=AssertionError("Network forbidden"))
        guard.start()
        self.addCleanup(guard.stop)

    async def execute(self, name="plugs.set_power", **arguments):
        intent = ToolIntent(type="tool", tool=name, arguments=arguments)
        bound = self.registry.validate_intent(intent)
        return await execute_tool(bound, ToolContext("trusted-pi"))

    def test_registration_and_strict_schemas_without_io(self):
        definitions = self.registry.list_executable_tools()
        self.assertEqual([tool.name for tool in definitions], ["plugs.set_power", "plugs.get_state"])
        for definition in definitions:
            schema = definition.parameters
            self.assertFalse(schema["additionalProperties"])
            self.assertEqual(schema["properties"]["plug"]["enum"], ["lamp", "mirror", "mushroom"])
            if definition.name == "plugs.set_power":
                self.assertEqual(schema["properties"]["state"]["enum"], ["on", "off"])
                self.assertEqual(set(schema["required"]), {"plug", "state"})
            else:
                self.assertEqual(schema["required"], ["plug"])
        self.factory.assert_not_called()
        self.assertEqual(definitions, tool_definitions())

    async def test_invalid_tool_arguments_never_execute(self):
        cases = [{}, {"plug": "lamp"}, {"plug": "unknown", "state": "on"},
                 {"plug": PRIVATE, "state": "on"}, {"plug": "Lamp", "state": "on"},
                 {"plug": "lamp", "state": "toggle"}, {"plug": "lamp", "state": True},
                 {"plug": "lamp", "state": "on", "device_id": PRIVATE},
                 {"plug": "lamp", "state": "on", "client_id": PRIVATE}]
        for arguments in cases:
            with self.subTest(arguments=arguments), self.assertRaises(ToolValidationError):
                await self.execute(**arguments)
        for arguments in ({}, {"plug": "all"}, {"plug": "lamp", "state": "on"},
                          {"plug": "lamp", "device_id": PRIVATE}):
            with self.assertRaises(ToolValidationError):
                await self.execute("plugs.get_state", **arguments)
        self.factory.assert_not_called()

    async def test_successful_power_execution_and_boolean_conversion(self):
        for plug in ("lamp", "mirror", "mushroom"):
            for state in ("on", "off"):
                self.client.reset_mock()
                self.client.is_on.return_value = state == "on"
                result = await self.execute(plug=plug, state=state)
                self.assertTrue(result.success)
                self.assertIsNone(result.error)
                reply = result.data["reply"]
                self.assertEqual(reply["plug"], plug)
                self.assertEqual(reply["state"], state)
                self.assertEqual(reply["requested_state"], state)
                self.assertIs(reply["accepted"], True)
                self.assertIs(reply["confirmed"], True)
                self.assertEqual(reply["response"], f"The {plug} plug reports that it's {state}.")
                self.assertEqual(reply["response_options"], [reply["response"]])
                self.client.set_power.assert_called_once_with(plug, state == "on")
                self.client.is_on.assert_called_once_with(plug)
                self.assertNotIn(PRIVATE, result.model_dump_json())
        self.continuation.assert_called_with("trusted-pi")
        self.assertEqual(reply["listen_for_seconds"], 10)
        self.assertEqual(reply["context_expires_in"], 40)

    async def test_successful_state_execution(self):
        self.client.is_on.return_value = False
        result = await self.execute("plugs.get_state", plug="lamp")
        self.assertTrue(result.success)
        reply = result.data["reply"]
        self.assertEqual(reply["state"], "off")
        self.assertNotIn("accepted", reply)
        self.assertNotIn("confirmed", reply)
        self.client.set_power.assert_not_called()

    async def test_accepted_unconfirmed_uses_uncertainty_wording(self):
        for observed in (False, None):
            self.client.reset_mock()
            self.client.is_on.return_value = observed
            result = await self.execute(plug="lamp", state="on")
            self.assertFalse(result.success)
            self.assertEqual(result.error, "state_not_confirmed")
            reply = result.data["reply"]
            self.assertIs(reply["accepted"], True)
            self.assertIs(reply["confirmed"], False)
            self.assertIn("was accepted", reply["response"])
            self.assertNotIn("failed", reply["response"])
            self.assertNotIn("couldn't turn", reply["response"])
            if observed is False:
                self.assertIn("unconfirmed", reply["response"])
                self.assertIn("still reports off", reply["response"])
            else:
                self.assertIn("couldn't confirm", reply["response"])
            self.client.set_power.assert_called_once()
            self.client.is_on.assert_called_once()

    async def test_failed_followup_read_is_unconfirmed_not_rejected(self):
        self.client.is_on.side_effect = tuya.TuyaTransportError()
        result = await self.execute(plug="lamp", state="off")
        reply = result.data["reply"]
        self.assertFalse(result.success)
        self.assertIs(reply["accepted"], True)
        self.assertIs(reply["confirmed"], False)
        self.assertIn("was accepted", reply["response"])
        self.assertIn("couldn't confirm", reply["response"])

    async def test_uncertain_write_is_not_retried(self):
        self.client.set_power.side_effect = tuya.TuyaCommandUncertain(PRIVATE)
        result = await self.execute(plug="lamp", state="on")
        self.assertFalse(result.success)
        self.assertEqual(result.error, "command_uncertain")
        self.assertIsNone(result.data["reply"]["accepted"])
        self.assertIn("Check its state", result.data["reply"]["response"])
        self.client.set_power.assert_called_once()
        self.client.is_on.assert_not_called()
        self.assertNotIn(PRIVATE, result.model_dump_json())

    async def test_failed_execution_has_safe_reasons_and_wording(self):
        for error, reason, words in (
            (tuya.TuyaDeviceNotConfigured(PRIVATE), "plug_not_configured", "lamp plug is not configured"),
            (tuya.TuyaConfigError(PRIVATE), "plug_configuration_error", "configuration needs checking"),
            (tuya.TuyaCapabilityError(PRIVATE), "plug_unsupported", "safely identify"),
            (tuya.TuyaAPIError(1106), "plug_unavailable", "unavailable"),
            (RuntimeError(PRIVATE), "plug_unavailable", "unavailable"),
        ):
            self.client.get_device_info.side_effect = error
            result = await self.execute("plugs.get_state", plug="lamp")
            self.assertFalse(result.success)
            self.assertEqual(result.error, reason)
            self.assertIn(words, result.data["reply"]["response"])
            serialized = result.model_dump_json()
            for private in (PRIVATE, "1106", "device_id", "switch_1", "endpoint", "access_token"):
                self.assertNotIn(private, serialized)

    async def test_offline_and_malformed_online_are_distinct(self):
        for online, reason in ((False, "plug_offline"), (None, "plug_unavailable"), (0, "plug_unavailable")):
            self.client.get_device_info.return_value = SimpleNamespace(online=online)
            result = await self.execute(plug="lamp", state="on")
            self.assertFalse(result.success)
            self.assertEqual(result.error, reason)
            self.assertEqual("offline" in result.data["reply"]["response"], online is False)
        self.client.set_power.assert_not_called()

    async def test_light_and_media_tools_remain_unchanged_and_executable(self):
        control = Mock()
        media = Mock()
        media.movie_status.return_value = {"success": True, "action": "movie_status", "response": "Available."}
        registry = ToolRegistry()
        register_light_tools(registry, control_lights=control,
                             power_commands=LightsCommands(control_govee=control, plugs=self.commands),
                             continuation=self.continuation)
        MediaTools(media).register(registry)
        original = registry.list_executable_tools()
        register_tools(registry, commands=self.commands, continuation=self.continuation)
        self.assertEqual(registry.list_executable_tools()[:len(original)], original)
        for intent in (ToolIntent(type="tool", tool="lights.set_power", arguments={"target": "govee", "state": "on"}),
                       ToolIntent(type="tool", tool="media.movie_status", arguments={})):
            result = await execute_tool(registry.validate_intent(intent), ToolContext("trusted-pi"))
            self.assertTrue(result.success)
        control.assert_called_once_with("on")
        media.movie_status.assert_called_once_with("trusted-pi")
        self.factory.assert_not_called()

    async def test_application_registers_tools_and_closes_feature(self):
        # Load real composition in an isolated module; never construct inference
        # models, a real HTTP client, or persistent media storage.
        spec = importlib.util.spec_from_file_location("plug_server_composition", ROOT / "server/server.py")
        module = importlib.util.module_from_spec(spec)
        with patch.dict("sys.modules", {
            spec.name: module,
            "faster_whisper": SimpleNamespace(WhisperModel=Mock()),
            "tts.kokoro_tts": SimpleNamespace(KokoroTTS=Mock()),
        }), patch("features.media.EventStore"), patch("httpx.AsyncClient", autospec=True), \
                patch.dict("os.environ", {"CUBE_AI_NODE_URL": ""}), redirect_stdout(io.StringIO()):
            spec.loader.exec_module(module)
            names = {item.name for item in module.tool_registry.list_executable_tools()}
            self.assertEqual(names, {item.name for item in light_definitions() + media_definitions() + tool_definitions(include_power=False) + cube_definitions()})
            self.factory.assert_not_called()
            self.assertIsNone(module.plug_commands._client)
            with patch.object(module.plug_commands, "close") as close:
                async with module.lifespan(module.app):
                    pass
                close.assert_called_once()
            module.remote_tts_client.aclose.side_effect = RuntimeError("Other cleanup failed")
            with patch.object(module.plug_commands, "close") as close:
                with self.assertRaises(RuntimeError):
                    async with module.lifespan(module.app):
                        pass
                close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
