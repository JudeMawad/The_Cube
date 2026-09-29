"""Local control routing without an LLM; hardware adapters are mocked."""
from time import perf_counter
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from core.ai_turn import process_turn
from core.client_state import ClientStateManager
from core import local_commands
from core.schemas import ToolIntent
from core.tool_registry import ToolContext, ToolRegistry
from features.cube.tools import register_tools as register_cube
from features.lights.tools import register_tools as register_lights
from features.plugs.tools import register_tools as register_plugs


class LocalCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.registry = ToolRegistry()
        self.cube = Mock()
        self.cube.execute.return_value = {"success": True, "status": {
            "display_available": True, "display_enabled": True,
            "master_brightness_percent": 20,
        }, "volume": {"channels": {"mono": 40}, "muted": False}}
        register_cube(self.registry, commands=self.cube)
        self.lights = Mock()
        self.lights.set_power.side_effect = lambda target, state: {
            "target": target, "requested_state": "on" if state else "off",
            "success": True, "partial": False, "devices": {},
        }
        self.govee = Mock()
        register_lights(self.registry, control_lights=self.govee,
                        power_commands=self.lights, continuation=lambda _: {})
        self.plugs = Mock()
        self.plugs.get_plug_state.side_effect = lambda plug: {
            "plug": plug, "state": True, "success": True,
        }
        register_plugs(self.registry, commands=self.plugs,
                       continuation=lambda _: {}, include_power=False)
        self.states = ClientStateManager()
        self.legacy = AsyncMock(return_value={"type": "unhandled", "success": True, "response": None})
        self.offline_ai = SimpleNamespace(
            config=SimpleNamespace(url="http://ai.invalid"),
            process_remote=AsyncMock(side_effect=AssertionError("Local command reached AI")))
        self.context = ToolContext(client_id="pi", control_session="session", request_id="request")

    async def turn(self, text, ai=None):
        return await process_turn(
            {"text": text, "processing_time": 0.01}, "pi", voice_started=perf_counter(),
            ai_client=ai, client_states=self.states, legacy=self.legacy,
            tool_registry=self.registry, tool_context=self.context, local_intent=local_commands.match)

    async def test_cube_controls_execute_once_with_ai_disabled_or_unreachable(self):
        cases = [
            ("Turn off your display", "set_display", {"state": "off"}),
            ("Please turn on the cube display", "set_display", {"state": "on"}),
            ("Set your display brightness to 20 percent", "set_brightness", {"percent": 20}),
            ("Set the cube brightness to 0%", "set_brightness", {"percent": 0}),
            ("Set your display to 100 percent", "set_brightness", {"percent": 100}),
            ("Reboot Cube", "reboot", {}),
            ("Reboot", "reboot", {}),
            ("Shut down Cube", "shutdown", {}),
            ("What's your display status?", "get_status", {}),
        ]
        for ai in (None, self.offline_ai):
            for text, operation, arguments in cases:
                with self.subTest(text=text, configured=ai is not None):
                    self.cube.reset_mock()
                    result = await self.turn(text, ai)
                    self.assertTrue(result["success"])
                    self.assertEqual(result["processing_source"], "local_fast")
                    self.assertEqual(result["action"], "cube_" + operation)
                    self.cube.execute.assert_called_once_with(self.context, operation, **arguments)
        self.offline_ai.process_remote.assert_not_called()
        self.legacy.assert_not_called()
        self.lights.set_power.assert_not_called()

    async def test_named_lights_and_groups_reach_only_requested_target(self):
        for ai in (None, self.offline_ai):
            for phrase, target in [
                ("the lamp", "lamp"), ("the mirror light", "mirror"),
                ("the mushroom lamp", "mushroom"), ("the govee lights", "govee"),
                ("the tuya lights", "tuya"), ("the small lights", "tuya"), ("the smart life lights", "tuya"),
                ("all lights", "all"),
            ]:
                for state in ("on", "off"):
                    for text in (f"Turn {state} {phrase}", f"Switch {phrase} {state}"):
                        with self.subTest(text=text):
                            self.lights.reset_mock()
                            result = await self.turn(text, ai)
                            self.assertEqual(result["processing_source"], "local_fast")
                            self.assertTrue(result["success"])
                            self.lights.set_power.assert_called_once_with(target, state == "on")
        self.govee.assert_not_called()
        self.legacy.assert_not_called()
        self.offline_ai.process_remote.assert_not_called()

    async def test_plug_questions_remain_read_only_including_light_word(self):
        for ai in (None, self.offline_ai):
            for text, plug in [
                ("Is the lamp plug on?", "lamp"), ("Is the mirror off?", "mirror"),
                ("Is the mushroom lamp on?", "mushroom"), ("Is the mirror light on?", "mirror"),
                ("Could you check the lamp plug?", "lamp"),
                ("What is the state of the mushroom lamp?", "mushroom"),
            ]:
                with self.subTest(text=text):
                    self.plugs.reset_mock()
                    result = await self.turn(text, ai)
                    self.assertEqual(result["processing_source"], "local_fast")
                    self.assertTrue(result["success"])
                    self.assertEqual(result["response"], f"The {plug} plug reports that it's on.")
                    self.plugs.get_plug_state.assert_called_once_with(plug)
        self.lights.set_power.assert_not_called()
        self.plugs.set_plug_power.assert_not_called()
        self.offline_ai.process_remote.assert_not_called()
        self.legacy.assert_not_called()

    async def test_negated_quoted_hypothetical_and_incomplete_commands_do_not_execute(self):
        for text in (
            "Don't reboot Cube", "Could you not shut down Cube?", "What if you reboot Cube?",
            'What does "turn off your display" mean?', '"cube display off"',
            "I said turn off the mushroom lamp", "Turn off the mirror tomorrow",
            "Reboot the", "Shut down", "Turn off the server", "Turn off your display and reboot Cube",
            "Is the mirror light on and turn it off", "Is your display on and reboot Cube",
        ):
            with self.subTest(text=text):
                self.assertIsNone(local_commands.match(text))
                await self.turn(text)
        self.cube.execute.assert_not_called()
        self.lights.set_power.assert_not_called()
        self.plugs.get_plug_state.assert_not_called()

    async def test_invalid_brightness_never_reaches_ai_or_hardware(self):
        for value in ("-1", "101", "50.5", "1,000", "+20", "50 percentish", "50 and 60"):
            with self.subTest(value=value):
                result = await self.turn("Set your display brightness to " + value, self.offline_ai)
                self.assertEqual(result["processing_source"], "local_clarification")
                self.assertIn("whole display brightness percentage", result["response"])
        self.cube.execute.assert_not_called()
        self.offline_ai.process_remote.assert_not_called()

    async def test_failed_hardware_command_is_not_retried_or_sent_to_ai(self):
        self.cube.execute.return_value = {"success": False, "error": "cube_unavailable"}
        result = await self.turn("Reboot Cube", self.offline_ai)
        self.assertFalse(result["success"])
        self.assertEqual(result["response"], "I couldn't reach the Cube.")
        self.cube.execute.assert_called_once()
        self.offline_ai.process_remote.assert_not_called()
        self.legacy.assert_not_called()

    async def test_existing_volume_commands_use_same_local_entrypoint(self):
        await self.turn("Volume up", self.offline_ai)
        self.cube.execute.assert_called_once_with(self.context, "adjust_volume", delta=10)
        self.cube.reset_mock()
        await self.turn("A little more", self.offline_ai)
        self.cube.execute.assert_called_once_with(self.context, "adjust_volume", delta=5)
        self.offline_ai.process_remote.assert_not_called()

    async def test_every_new_catalog_example_executes_its_validated_tool(self):
        for key, rule in local_commands.RULES.items():
            if key[0] == "music":
                # Music has its own registry/authorization fixture and free-form
                # non-fast patterns in test_music_routing.
                continue
            phrases = rule.get("exact", []) + [example for pattern in rule.get("patterns", [])
                                               for example in pattern.get("examples", [])]
            for text in phrases:
                with self.subTest(rule=key, text=text):
                    candidate = local_commands.match(text)
                    self.assertIsInstance(candidate, ToolIntent)
                    self.registry.validate_intent(candidate)
                    result = await self.turn(text, self.offline_ai)
                    self.assertTrue(result["success"])
                    self.assertEqual(result["processing_source"], "local_fast")
        self.offline_ai.process_remote.assert_not_called()

    def test_ambiguous_catalog_matches_do_not_pick_an_action(self):
        rules = {key: dict(rule) for key, rule in local_commands.RULES.items()}
        rules[("cube", "reboot")]["exact"] = ["turn off your display"]
        with patch.object(local_commands, "RULES", rules):
            self.assertIsNone(local_commands.match("turn off your display"))
