"""Offline coverage for the backend hybrid routing boundary."""
import json
from time import perf_counter
from typing import Literal
import unittest
from unittest.mock import Mock

import httpx

from core.ai_client import AIClient
from core.ai_turn import process_turn
from core.client_state import ClientStateManager
from core.config import AINodeConfig
from core.schemas import ToolDefinition, ToolResult
from core.tool_registry import ToolArguments, ToolRegistry
from features.lights.commands import handle_command, is_direct_command


class ColorArguments(ToolArguments):
    color: Literal["blue"]


class StatusArguments(ToolArguments):
    pass


class HybridRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.states = ClientStateManager()
        self.control = Mock()
        self.tool_action = Mock()
        self.status_action = Mock()
        self.legacy_calls = 0
        self.requests = []
        self.remote_reply = {"type": "conversation", "response": "Hello."}
        self.registry = ToolRegistry()
        self.registry.register(
            ToolDefinition(name="lights.set_color", description="Set color", parameters={}),
            arguments_model=ColorArguments,
            handler=self.execute_color,
        )
        self.registry.register(
            ToolDefinition(name="media.movie_status", description="Read movie status", parameters={}),
            arguments_model=StatusArguments, handler=self.execute_status,
        )

    def execute_color(self, arguments, context):
        self.tool_action(arguments.color)
        return ToolResult(
            tool="lights.set_color", success=True,
            data={"reply": {"action": "lights_color", "value": arguments.color,
                            "response": "Lights set to blue.", "listen_for_seconds": 0}},
        )

    def execute_status(self, arguments, context):
        self.status_action()
        return ToolResult(
            tool="media.movie_status", success=True,
            data={"reply": {"action": "movie_status", "response": "Still downloading.",
                            "listen_for_seconds": 10}},
        )

    def remote(self, request):
        payload = json.loads(request.content)
        self.requests.append(payload)
        if self.remote_reply is None:
            return httpx.Response(503)
        return httpx.Response(200, json=self.remote_reply)

    async def run_turn(self, text, *, ai=True, continuation=None):
        async def legacy():
            self.legacy_calls += 1
            command = handle_command(text, control_lights=self.control)
            if command is None:
                return {"transcript": text, "type": "unhandled", "success": True,
                        "response": None, "listen_for_seconds": 0, "processing_time": 0.1}
            return {"transcript": text, "type": "command", "success": True,
                    "listen_for_seconds": 0, "processing_time": 0.1, **command}

        client = AIClient(AINodeConfig("http://ai.invalid"),
                          transport=httpx.MockTransport(self.remote)) if ai else None
        return await process_turn(
            {"text": text, "processing_time": 0.1}, "cube",
            voice_started=perf_counter(), ai_client=client, client_states=self.states,
            legacy=legacy, tool_registry=self.registry, local_command=is_direct_command,
            continuation=continuation,
        )

    async def test_clear_light_phrases_execute_once_without_ai(self):
        cases = [
            ("Lights on!", "on"), ("switch the bulbs off", "off"),
            ("Could you turn on the lights?", "on"),
            ("please turn off the spots", "off"),
            ("spots 1%", "brightness"), ("set the lights to 50 percent", "brightness"),
            ("make the lights blue", "color"),
        ]
        for text, action in cases:
            with self.subTest(text=text):
                before = self.legacy_calls
                self.control.reset_mock()
                result = await self.run_turn(text)
                self.assertEqual(self.legacy_calls, before + 1)
                self.control.assert_called_once()
                self.assertEqual(self.control.call_args.args[0], action)
                self.assertEqual(result["processing_source"], "local_fast")
                self.assertEqual(result["type"], "command")
                self.assertEqual(result["transcript"], text)
                self.assertIn("processing_time", result)
                self.assertIn("listen_for_seconds", result)
        self.assertEqual(self.requests, [])

    async def test_unmatched_ambiguous_and_stt_variants_reach_ai(self):
        for text in ("hello Cube", "lite on", "maybe lights on", "make the lights", "lights blue"):
            with self.subTest(text=text):
                result = await self.run_turn(text)
                self.assertEqual(result["processing_source"], "ai_conversation")
        self.assertEqual([item["transcript"] for item in self.requests],
                         ["hello Cube", "lite on", "maybe lights on", "make the lights", "lights blue"])
        self.assertEqual(self.legacy_calls, 0)
        self.control.assert_not_called()

    async def test_non_action_utterances_withhold_tools_and_block_legacy(self):
        unsafe = (
            "Why are the lights blue?", "Are the lights on?",
            "Don't turn on the lights", "Could you not turn off the lights?",
            "What if the lights were set to 50 percent?",
            'What does "turn off the lights" mean?',
            "Imagine you download Dune", "I said lights on",
            "No, turn the lights off", "Could you turn on the lights in a hypothetical scenario?",
            "Don't reboot the Cube",
        )
        self.remote_reply = {"type": "tool", "tool": "lights.set_color",
                             "arguments": {"color": "blue"}}
        for text in unsafe:
            with self.subTest(text=text):
                result = await self.run_turn(text)
                self.assertEqual(result["processing_source"], "ai_tool")
                self.assertFalse(result["success"])
                self.assertEqual(self.requests[-1]["tools"], [])
        self.assertEqual(self.legacy_calls, 0)
        self.control.assert_not_called()
        self.tool_action.assert_not_called()

    async def test_non_action_stays_safe_when_ai_is_unavailable(self):
        self.remote_reply = None
        for text in ("Don't set the lights to 50 percent",
                     "Why are the lights blue?",
                     'What does "turn off the lights" mean?',
                     "What if the lights were set to 50 percent?"):
            with self.subTest(text=text):
                result = await self.run_turn(text)
                self.assertEqual(result["type"], "unhandled")
                self.assertEqual(result["processing_source"], "safety")
                self.assertIsNone(result["response"])
                self.assertEqual(result["listen_for_seconds"], 0)
        self.assertEqual(self.legacy_calls, 0)
        self.control.assert_not_called()

    async def test_safety_fallback_keeps_pending_movie_continuation(self):
        continuation = Mock(return_value={"listen_for_seconds": 10, "context_expires_in": 42})
        result = await self.run_turn("Why are the lights blue?", ai=False,
                                     continuation=continuation)
        self.assertEqual(result["listen_for_seconds"], 10)
        self.assertEqual(result["context_expires_in"], 42)
        self.assertEqual(result["processing_source"], "safety")
        self.assertEqual(self.legacy_calls, 0)

    async def test_llm_tool_still_validates_and_executes_once(self):
        self.remote_reply = {"type": "tool", "tool": "lights.set_color",
                             "arguments": {"color": "blue"}}
        result = await self.run_turn("Could you give the room a blue glow?")
        self.assertEqual(result["processing_source"], "ai_tool")
        self.assertEqual(result["action"], "lights_color")
        self.assertEqual(result["response"], "Lights set to blue.")
        self.assertEqual(self.requests[-1]["tools"][0]["name"], "lights.set_color")
        self.tool_action.assert_called_once_with("blue")
        self.assertEqual(self.legacy_calls, 0)

        self.remote_reply = {"type": "tool", "tool": "lights.set_color",
                             "arguments": {"color": "green"}}
        rejected = await self.run_turn("Give the room a green glow")
        self.assertFalse(rejected["success"])
        self.assertEqual(rejected["processing_source"], "ai_tool")
        self.tool_action.assert_called_once_with("blue")
        self.assertEqual(self.legacy_calls, 0)

    async def test_movie_status_question_cannot_invoke_mutating_tool(self):
        self.remote_reply = {"type": "tool", "tool": "lights.set_color",
                             "arguments": {"color": "blue"}}
        rejected = await self.run_turn("Did you cancel it?")
        self.assertEqual([tool["name"] for tool in self.requests[-1]["tools"]],
                         ["media.movie_status"])
        self.assertFalse(rejected["success"])
        self.tool_action.assert_not_called()
        self.status_action.assert_not_called()
        self.assertEqual(self.legacy_calls, 0)
        polite_info = await self.run_turn("Could you tell me whether you cancelled it?")
        self.assertEqual([tool["name"] for tool in self.requests[-1]["tools"]],
                         ["media.movie_status"])
        self.assertFalse(polite_info["success"])
        self.tool_action.assert_not_called()

        self.remote_reply = {"type": "tool", "tool": "media.movie_status",
                             "arguments": {}}
        result = await self.run_turn("Is it ready?")
        self.assertEqual(result["response"], "Still downloading.")
        self.assertEqual(result["processing_source"], "ai_tool")
        self.status_action.assert_called_once()
        self.assertEqual(self.legacy_calls, 0)

    async def test_fast_reply_is_available_in_following_llm_history(self):
        first = await self.run_turn("lights on")
        second = await self.run_turn("How are you?")
        self.assertEqual(self.requests[-1]["history"], [
            {"role": "user", "content": "lights on"},
            {"role": "assistant", "content": first["response"]},
        ])
        self.assertEqual(second["type"], "conversation")
        self.assertEqual(self.legacy_calls, 1)

    async def test_normal_legacy_fallback_is_unchanged(self):
        self.remote_reply = None
        result = await self.run_turn("download Dune")
        self.assertEqual(result["processing_source"], "legacy")
        self.assertEqual(self.legacy_calls, 1)
        self.assertEqual(result["type"], "unhandled")
