"""Unified lighting tests: mock both vendors and forbid network access."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, call, patch

from core.ai_turn import process_turn
from core.client_state import ClientStateManager
from core.schemas import ToolIntent
from core.tool_execution import execute_tool
from core.tool_registry import ToolContext, ToolRegistry, ToolValidationError
from core.voice_pipeline import process_transcription
from features.lights import LightsCommands, handle_command
from features.lights.power import power_reply
from features.lights.tools import register_tools
from features.plugs import PlugCommands
from features.plugs.tools import register_tools as register_plug_tools
from integrations import tuya


PRIVATE = "PRIVATE_VENDOR_ID_TOKEN_SECRET"


class PowerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        client_type = tuya.TuyaClient
        factory = patch("integrations.tuya.TuyaClient")
        self.factory = factory.start()
        self.addCleanup(factory.stop)
        self.client = Mock(spec=client_type)
        self.factory.return_value = self.client
        self.client.get_device_info.return_value = SimpleNamespace(online=True, id=PRIVATE, name=PRIVATE)
        self.client.set_power.return_value = None
        self.client.is_on.side_effect = AssertionError("Power must not perform status reads")
        self.plugs = PlugCommands()
        self.addCleanup(self.plugs.close)
        self.govee = Mock()
        self.commands = LightsCommands(control_govee=self.govee, plugs=self.plugs)
        self.continuation = Mock(return_value={"listen_for_seconds": 10, "context_expires_in": 40})
        self.registry = ToolRegistry()
        register_tools(self.registry, control_lights=self.govee, power_commands=self.commands,
                       continuation=self.continuation)
        register_plug_tools(self.registry, commands=self.plugs, continuation=self.continuation,
                            include_power=False)
        guard = patch("socket.socket.connect", side_effect=AssertionError("Network forbidden"))
        guard.start()
        self.addCleanup(guard.stop)

    async def execute(self, target, state):
        intent = ToolIntent(type="tool", tool="lights.set_power", arguments={"target": target, "state": state})
        return await execute_tool(self.registry.validate_intent(intent), ToolContext("trusted-pi"))

    def test_exact_targets_and_on_off_delegation(self):
        mappings = {"all": ["govee", "lamp", "mirror", "mushroom"],
                    "tuya": ["lamp", "mirror", "mushroom"], "govee": ["govee"],
                    "lamp": ["lamp"], "mirror": ["mirror"], "mushroom": ["mushroom"]}
        for target, devices in mappings.items():
            for state in (True, False):
                with self.subTest(target=target, state=state):
                    self.govee.reset_mock()
                    self.client.reset_mock()
                    result = self.commands.set_power(target, state)
                    self.assertTrue(result["success"])
                    self.assertFalse(result["partial"])
                    self.assertEqual(list(result["devices"]), devices)
                    self.assertEqual(result["requested_state"], "on" if state else "off")
                    self.assertEqual(self.client.set_power.call_args_list,
                                     [call(name, state) for name in devices if name != "govee"])
                    if "govee" in devices:
                        self.govee.assert_called_once_with("on" if state else "off")
                    else:
                        self.govee.assert_not_called()
                    self.client.is_on.assert_not_called()
                    self.client.get_status.assert_not_called()
                    self.assertNotIn(PRIVATE, str(result))
                    self.assertEqual(power_reply(result)["response"], "Done.")

    def test_strict_feature_validation_before_operations(self):
        for target in ("ALL", " all", "unknown", PRIVATE, "bulb1", "", None, [], 1):
            with self.subTest(target=target), self.assertRaises(ValueError) as caught:
                self.commands.set_power(target, True)
            self.assertNotIn(PRIVATE, str(caught.exception))
        for state in ("on", "off", 0, 1, None, []):
            with self.subTest(state=state), self.assertRaises(ValueError):
                self.commands.set_power("all", state)
        self.factory.assert_not_called()
        self.govee.assert_not_called()

    def test_strict_tool_schema_and_single_exposed_power_tool(self):
        schema = self.registry.get("lights.set_power").parameters
        self.assertEqual(schema["required"], ["target", "state"])
        self.assertEqual(schema["properties"]["target"]["enum"],
                         ["all", "govee", "tuya", "lamp", "mirror", "mushroom"])
        self.assertEqual(schema["properties"]["state"]["enum"], ["on", "off"])
        self.assertFalse(schema["additionalProperties"])
        names = [tool.name for tool in self.registry.list_executable_tools()]
        self.assertEqual([name for name in names if name.endswith("set_power")], ["lights.set_power"])
        self.assertIn("plugs.get_state", names)
        for arguments in ({}, {"on": True}, {"target": "all"}, {"state": "on"},
                          {"target": PRIVATE, "state": "on"}, {"target": "all", "state": True},
                          {"target": "all", "state": "toggle"},
                          {"target": "lamp", "state": "on", "device_id": PRIVATE}):
            with self.assertRaises(ToolValidationError):
                self.registry.validate_intent(ToolIntent(type="tool", tool="lights.set_power", arguments=arguments))
        with self.assertRaises(ToolValidationError):
            self.registry.validate_intent(ToolIntent(type="tool", tool="plugs.set_power",
                                                     arguments={"plug": "lamp", "state": "on"}))
        self.factory.assert_not_called()

    async def test_registry_execution_returns_safe_aggregate_and_continuation(self):
        result = await self.execute("all", "on")
        self.assertTrue(result.success)
        reply = result.data["reply"]
        self.assertEqual(reply["response"], "Done.")
        self.assertEqual(reply["target"], "all")
        self.assertEqual(reply["action"], "lights_on")
        self.assertEqual(reply["listen_for_seconds"], 10)
        self.continuation.assert_called_once_with("trusted-pi")
        for outcome in reply["devices"].values():
            self.assertEqual(outcome, {"success": True, "accepted": True})
        serialized = result.model_dump_json()
        for private in (PRIVATE, "device_id", "switch_1", "access_token", "endpoint"):
            self.assertNotIn(private, serialized)

    async def test_offline_member_does_not_stop_later_devices(self):
        self.client.get_device_info.side_effect = lambda name: SimpleNamespace(online=name != "mirror")
        result = await self.execute("all", "off")
        self.assertFalse(result.success)
        self.assertEqual(result.error, "partial_failure")
        reply = result.data["reply"]
        self.assertTrue(reply["partial"])
        self.assertEqual(reply["devices"]["mirror"]["error"], "offline")
        self.assertEqual(reply["response"], "Done, but the mirror light is offline.")
        self.assertEqual(self.client.set_power.call_args_list, [call("lamp", False), call("mushroom", False)])
        self.govee.assert_called_once_with("off")

    async def test_govee_system_exit_is_safe_and_other_devices_execute(self):
        self.govee.side_effect = SystemExit(PRIVATE)
        result = await self.execute("all", "on")
        self.assertFalse(result.success)
        self.assertTrue(result.data["reply"]["partial"])
        self.assertEqual(result.data["reply"]["devices"]["govee"],
                         {"success": False, "accepted": False, "error": "configuration_error"})
        self.assertEqual(self.client.set_power.call_args_list,
                         [call(name, True) for name in ("lamp", "mirror", "mushroom")])
        self.assertNotIn(PRIVATE, result.model_dump_json())

    def test_system_exit_is_not_caught_outside_govee_boundary(self):
        self.plugs.set_plug_power = Mock(side_effect=SystemExit(PRIVATE))
        with self.assertRaises(SystemExit):
            self.commands.set_power("tuya", True)
        self.govee.assert_not_called()

    async def test_individual_and_total_failure_do_not_say_done(self):
        self.client.get_device_info.return_value.online = False
        result = await self.execute("mirror", "on")
        self.assertFalse(result.success)
        self.assertFalse(result.data["reply"]["partial"])
        self.assertEqual(result.data["reply"]["response"], "I couldn't reach the mirror.")
        result = await self.execute("tuya", "off")
        self.assertFalse(result.success)
        self.assertFalse(result.data["reply"]["partial"])
        self.assertNotIn("Done", result.data["reply"]["response"])
        self.assertEqual(len(result.data["reply"]["devices"]), 3)

    async def test_uncertain_write_is_not_retried_and_group_continues(self):
        def write(name, state):
            if name == "lamp":
                raise tuya.TuyaCommandUncertain(PRIVATE)
        self.client.set_power.side_effect = write
        result = await self.execute("tuya", "on")
        self.assertFalse(result.success)
        self.assertEqual(result.data["reply"]["devices"]["lamp"]["error"], "uncertain")
        self.assertIsNone(result.data["reply"]["devices"]["lamp"]["accepted"])
        self.assertEqual(self.client.set_power.call_count, 3)
        self.client.is_on.assert_not_called()
        self.assertNotIn(PRIVATE, result.model_dump_json())

    async def test_unconfigured_and_malformed_online_are_safe_failures(self):
        def info(name):
            if name == "lamp":
                raise tuya.TuyaDeviceNotConfigured(PRIVATE)
            return SimpleNamespace(online=None if name == "mirror" else True)
        self.client.get_device_info.side_effect = info
        result = await self.execute("tuya", "on")
        devices = result.data["reply"]["devices"]
        self.assertEqual(devices["lamp"]["error"], "unconfigured")
        self.assertEqual(devices["mirror"]["error"], "unavailable")
        self.assertTrue(devices["mushroom"]["success"])
        self.client.set_power.assert_called_once_with("mushroom", True)

    def test_failed_govee_response_text_is_never_exposed(self):
        self.govee.side_effect = RuntimeError(PRIVATE)
        result = self.commands.set_power("all", True)
        self.assertTrue(result["partial"])
        self.assertIsNone(result["devices"]["govee"]["accepted"])
        self.assertNotIn(PRIVATE, str(power_reply(result)))

    def test_unexpected_plug_failure_is_independent(self):
        def write(name, state, *, confirm):
            if name == "mirror":
                raise RuntimeError(PRIVATE)
            return {"success": True, "accepted": True, "vendor_payload": PRIVATE}
        self.plugs.set_plug_power = Mock(side_effect=write)
        result = self.commands.set_power("tuya", False)
        self.assertTrue(result["partial"])
        self.assertEqual(result["devices"]["mirror"]["error"], "uncertain")
        self.assertTrue(result["devices"]["mushroom"]["success"])
        self.assertNotIn(PRIVATE, str(result))
        self.assertNotIn("vendor_payload", str(result))

    def test_existing_fallback_power_uses_all_without_new_patterns(self):
        for text, state in (("Turn the lights on", True), ("Turn the lights off", False),
                            ("Lights on!", True), ("switch the bulbs off", False),
                            ("please turn the lights on", True), ("lights on please", True)):
            self.client.reset_mock()
            self.govee.reset_mock()
            result = handle_command(text, control_lights=self.govee, power_commands=self.commands)
            self.assertTrue(result["success"])
            self.assertEqual(result["target"], "all")
            self.govee.assert_called_once_with("on" if state else "off")
            self.assertEqual(self.client.set_power.call_count, 3)

    def test_fallback_scoped_or_embellished_phrases_do_not_match_fragments(self):
        for text in ("Govee lights on", "Tuya lights off", "Smart Life lights on",
                     "turn the Govee lights on", "mushroom lamp on", "lamp on",
                     "don't turn the lights on"):
            with self.subTest(text=text):
                self.assertIsNone(handle_command(text, control_lights=self.govee, power_commands=self.commands))
        self.govee.assert_not_called()
        self.factory.assert_not_called()

    async def test_fallback_partial_result_survives_pipeline(self):
        self.client.get_device_info.side_effect = lambda name: SimpleNamespace(online=name != "mushroom")
        result = await process_transcription(
            {"text": "lights on", "processing_time": 0.1}, "pi", voice_started=0,
            handle_command=lambda text, client_id: handle_command(
                text, control_lights=self.govee, power_commands=self.commands), continuation=Mock())
        self.assertFalse(result["success"])
        self.assertTrue(result["partial"])
        self.assertEqual(result["response"], "Done, but the mushroom light is offline.")
        self.assertEqual(result["devices"]["mushroom"]["error"], "offline")

    async def test_normal_ai_tool_path_uses_unified_registry_once(self):
        self.client.get_device_info.side_effect = lambda name: SimpleNamespace(online=name != "mirror")
        ai = SimpleNamespace(config=SimpleNamespace(url="http://ai.invalid"),
                             process_remote=AsyncMock(return_value=ToolIntent(
                                 type="tool", tool="lights.set_power", arguments={"target": "all", "state": "on"})))
        legacy = AsyncMock(side_effect=AssertionError("No legacy after tool intent"))
        result = await process_turn({"text": "turn the lights on", "processing_time": 0.1}, "pi",
                                    voice_started=0, ai_client=ai, client_states=ClientStateManager(),
                                    legacy=legacy, tool_registry=self.registry)
        self.assertFalse(result["success"])
        self.assertEqual(result["response"], "Done, but the mirror light is offline.")
        self.assertEqual(result["processing_source"], "ai_tool")
        advertised = {tool.name for tool in ai.process_remote.await_args.args[0].tools}
        self.assertIn("lights.set_power", advertised)
        self.assertNotIn("plugs.set_power", advertised)
        self.govee.assert_called_once_with("on")
        self.assertEqual(self.client.set_power.call_count, 2)
        legacy.assert_not_awaited()

    def test_acceptance_opt_out_preserves_default_plug_confirmation(self):
        self.client.is_on.side_effect = None
        self.client.is_on.return_value = False
        accepted = self.plugs.set_plug_power("lamp", True, confirm=False)
        self.assertTrue(accepted["success"])
        self.assertTrue(accepted["accepted"])
        self.assertFalse(accepted["confirmed"])
        self.client.is_on.assert_not_called()
        confirmed = self.plugs.set_plug_power("lamp", True)
        self.assertFalse(confirmed["success"])
        self.assertTrue(confirmed["accepted"])
        self.assertFalse(confirmed["confirmed"])
        self.client.is_on.assert_called_once_with("lamp")


class GoveeCompatibilityTests(unittest.TestCase):
    def test_existing_group_command_and_other_controls_are_unchanged(self):
        from integrations import govee
        from features.lights.govee_control import control_lights
        settings = {"sku": "TEST", "devices": {"bulb1": "DEVICE"},
                    "all_spots_group": {"sku": "SameModeGroup", "device": "GROUP"}}
        with patch.object(govee, "load_settings", return_value=settings), \
                patch.object(govee, "load_api_key", return_value="SYNTHETIC"), \
                patch.object(govee, "send_control") as send:
            for action, value in (("on", 1), ("off", 0)):
                send.reset_mock()
                control_lights(action)
                send.assert_called_once_with("SYNTHETIC", "SameModeGroup", "GROUP",
                                             "devices.capabilities.on_off", "powerSwitch", value)


if __name__ == "__main__":
    unittest.main()
