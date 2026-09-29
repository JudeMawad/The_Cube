"""Backend volume routing and safety with a mocked Pi and AI node."""
from time import perf_counter
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from core.ai_turn import process_turn
from core.client_state import ClientStateManager
from core.schemas import ConversationResult, ToolIntent
from core.tool_registry import ToolRegistry
from features.cube.tools import register_tools
from features.cube import volume_commands


class FakeCube:
    def __init__(self):
        self.level = 40
        self.muted = False
        self.calls = []

    def execute(self, context, operation, **arguments):
        self.calls.append((context.client_id, operation, arguments))
        if operation == "set_volume":
            self.level = arguments["percent"]
        elif operation == "adjust_volume":
            self.level = max(0, min(100, self.level + arguments["delta"]))
        elif operation == "mute":
            self.muted = True
        elif operation == "unmute":
            self.muted = False
        return {"success": True, "volume": {
            "sink": "alsa_output.reSpeaker", "channels": {"mono": self.level}, "muted": self.muted,
        }}


class VolumeRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = 1.0
        self.states = ClientStateManager(clock=lambda: self.now)
        self.cube = FakeCube()
        self.registry = ToolRegistry()
        register_tools(self.registry, commands=self.cube)
        self.ai = SimpleNamespace(config=SimpleNamespace(url="http://ai.invalid"),
                                  process_remote=AsyncMock(return_value=ConversationResult(
                                      type="conversation", response="Let's talk.")))
        self.legacy = AsyncMock(return_value={"transcript": "unhandled", "type": "unhandled",
                                              "success": True, "response": None,
                                              "listen_for_seconds": 0, "processing_time": 0.01})

    async def turn(self, text):
        return await process_turn(
            {"text": text, "processing_time": 0.01}, "pi",
            voice_started=perf_counter(), ai_client=self.ai, client_states=self.states,
            legacy=self.legacy, tool_registry=self.registry, local_intent=volume_commands.match,
        )

    async def test_catalog_aliases_and_numbers_skip_llm(self):
        cases = [
            ("Volume up", 50), ("Turn down the volume", 40),
            ("A little louder", 45), ("A bit quieter", 40),
            ("Slightly increase the volume", 45), ("Speak quieter", 35),
            ("Volume 50%", 50), ("Set your volume to 30 percent", 30),
            ("Make your volume 70%", 70), ("Maximum volume", 100),
            ("Minimum volume", 0), ("Could you turn up the volume?", 10),
        ]
        for phrase, expected in cases:
            with self.subTest(phrase=phrase):
                result = await self.turn(phrase)
                self.assertEqual(result["processing_source"], "local_fast")
                self.assertTrue(result["success"])
                self.assertEqual(self.cube.level, expected)
        self.ai.process_remote.assert_not_called()
        self.legacy.assert_not_called()
        self.assertEqual(len(self.cube.calls), len(cases))

    async def test_every_catalog_exact_alias_is_live(self):
        for rule in volume_commands.RULES.values():
            for alias in rule.get("exact", []):
                with self.subTest(rule=rule["id"], alias=alias):
                    candidate = volume_commands.match(alias, previous_direction=1)
                    self.assertIsInstance(candidate, ToolIntent)
        for pattern in volume_commands.RULES["volume_set"]["patterns"]:
            for example in pattern.get("examples", []):
                self.assertEqual(volume_commands.match(example).tool, "cube.set_volume")

    async def test_volume_reply_is_in_existing_history_for_llm(self):
        await self.turn("Volume up")
        await self.turn("How are you?")
        history = self.ai.process_remote.call_args.args[0].history
        self.assertEqual([(message.role, message.content) for message in history], [
            ("user", "Volume up"), ("assistant", "Volume 50 percent."),
        ])

    async def test_mute_unmute_preserve_volume_and_live_status(self):
        await self.turn("volume 45%")
        muted = await self.turn("Mute yourself")
        self.assertEqual(muted["response"], "Muted.")
        self.assertTrue(self.cube.muted)
        self.assertEqual((await self.turn("What's your volume?"))["response"], "Volume is muted.")
        self.assertEqual(self.cube.level, 45)
        self.assertEqual((await self.turn("Turn your sound back on"))["response"],
                         "Unmuted. Volume 45 percent.")
        self.cube.level = 37
        self.assertEqual((await self.turn("What volume are you at?"))["response"],
                         "Volume is 37 percent.")
        self.ai.process_remote.assert_not_called()

    async def test_common_volume_status_alias_executes_locally(self):
        result = await self.turn("What is the volume?")
        self.assertEqual(result["processing_source"], "local_fast")
        self.assertEqual(result["action"], "cube_get_volume")
        self.assertEqual(result["response"], "Volume is 40 percent.")
        self.assertEqual(self.cube.calls, [("pi", "get_volume", {})])
        self.ai.process_remote.assert_not_called()

    async def test_directional_followup_immediate_only_and_expires(self):
        await self.turn("Increase the volume")
        self.assertEqual((await self.turn("A little more"))["response"], "Volume 55 percent.")
        self.assertEqual(self.cube.level, 55)
        await self.turn("Turn down the volume")
        await self.turn("A bit more")
        self.assertEqual(self.cube.level, 40)
        self.now += 31
        count = len(self.cube.calls)
        self.assertEqual((await self.turn("A little more"))["response"],
                         "Do you mean louder or quieter?")
        self.assertEqual(len(self.cube.calls), count)
        await self.turn("Volume down")
        await self.turn("How are you?")
        count = len(self.cube.calls)
        await self.turn("A little more")
        self.assertEqual(len(self.cube.calls), count)

    async def test_invalid_numbers_reject_without_guessing_or_ai(self):
        for phrase in ("Volume 101%", "Set the volume to -1 percent",
                       "Volume 50.5%", "Make your volume 1,000%",
                       "Set volume to 50 percentish", "Volume 50 and 60"):
            with self.subTest(phrase=phrase):
                result = await self.turn(phrase)
                self.assertIn("whole volume percentage", result["response"])
        self.assertEqual(self.cube.calls, [])
        self.ai.process_remote.assert_not_called()

    async def test_nonaction_requests_never_execute_even_if_ai_returns_tool(self):
        self.ai.process_remote.return_value = ToolIntent(
            type="tool", tool="cube.adjust_volume", arguments={"delta": 10})
        for phrase in ("Don't increase the volume", "What happens if I mute you?",
                       "Why is your volume so low?", 'What does "volume up" mean?',
                       "Can you explain how volume control works?"):
            with self.subTest(phrase=phrase):
                result = await self.turn(phrase)
                self.assertFalse(result["success"])
        self.assertEqual(self.cube.calls, [])
        self.assertTrue(all(not request.args[0].tools or
                            all(tool.name == "cube.get_volume" or tool.name == "cube.get_status"
                                for tool in request.args[0].tools)
                            for request in self.ai.process_remote.call_args_list))

    async def test_ambiguous_phrase_uses_llm_and_validated_tool_once(self):
        self.ai.process_remote.return_value = ToolIntent(
            type="tool", tool="cube.adjust_volume", arguments={"delta": 7})
        result = await self.turn("Could you make the sound a bit stronger?")
        self.assertEqual(result["processing_source"], "ai_tool")
        self.assertEqual(self.cube.level, 47)
        self.assertEqual(len(self.cube.calls), 1)
        self.assertEqual((await self.turn("A little more"))["response"], "Volume 52 percent.")
        self.assertEqual(len(self.cube.calls), 2)
        self.ai.process_remote.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
