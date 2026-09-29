"""Offline tests for AI interpretation and tool validation."""
import asyncio
import json
from time import perf_counter
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx

from core.ai_client import AIClient
from core.ai_turn import process_turn
from core.client_state import ClientStateManager
from core.config import AINodeConfig
from core.schemas import ToolDefinition
from core.tool_registry import ToolArguments, ToolRegistry


class AITurnTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.states = ClientStateManager()
        self.legacy = AsyncMock(side_effect=lambda: {
            "transcript": "lights on", "type": "unhandled", "success": True,
            "response": None, "processing_time": 0.261,
        })
        guard = patch("socket.socket.connect", side_effect=AssertionError("Network forbidden"))
        guard.start()
        self.addCleanup(guard.stop)

    def client(self, handler, timeout=10):
        return AIClient(AINodeConfig("http://ai.invalid", process_timeout=timeout),
                        transport=httpx.MockTransport(handler))

    async def process(self, client=None, text="lights on", client_id="cube"):
        return await process_turn(
            {"text": text, "language": "en", "processing_time": 0.261},
            client_id, voice_started=perf_counter(), ai_client=client,
            client_states=self.states, legacy=self.legacy,
        )

    async def test_conversation_bypasses_parser_even_for_known_commands(self):
        for text in ["lights on", "download Dune"]:
            requests = []
            def handler(request):
                requests.append(json.loads(request.content))
                self.assertEqual(request.url.path, "/process")
                return httpx.Response(200, json={"type": "conversation", "response": "Let's talk."})
            result = await self.process(self.client(handler), text=text)
            self.legacy.assert_not_awaited()
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0]["transcript"], text)
            self.assertEqual(requests[0]["tools"], [])
            self.assertEqual(requests[0]["phase"], "interpret")
            self.assertEqual(result["type"], "conversation")
            self.assertEqual(result["processing_source"], "ai_conversation")
            self.assertEqual(result["listen_for_seconds"], 10)
            self.assertEqual(result["processing_time"], 0.261)
            self.assertEqual(result["timings"]["command"], 0)
            self.assertGreaterEqual(result["timings"]["voice_total"], 0)
            self.assertEqual(result["processing_timings"]["execution"], 0)

    async def test_valid_tool_intents_are_declined_without_fallback(self):
        for tool, arguments in [("not.registered", {}), ("lights.set_power", {"on": "invalid"}),
                                ("lights.set_power", {"on": True})]:
            handler = Mock(return_value=httpx.Response(200, json={
                "type": "tool", "tool": tool, "arguments": arguments}))
            result = await self.process(self.client(handler))
            handler.assert_called_once()
            self.legacy.assert_not_awaited()
            self.assertFalse(result["success"])
            self.assertEqual(result["processing_source"], "ai_tool")
            self.assertEqual(result["response"], "I couldn't complete that request.")
            self.assertEqual(result["listen_for_seconds"], 0)

    async def test_only_interpretation_failures_use_legacy(self):
        failures = [
            httpx.ConnectError("SECRET"), httpx.ReadTimeout("SECRET"),
            *[httpx.Response(code, text="SECRET") for code in (302, 400, 501, 503, 500)],
            httpx.Response(200, text="SECRET"),
            *[httpx.Response(200, json=data) for data in [
                {}, {"type": "unknown"}, {"type": "conversation", "response": " "},
                {"type": "conversation", "response": "x" * 501},
                {"type": "tool", "tool": "lights.set_power", "arguments": []},
                {"type": "tool", "tool": "lights.set_power", "arguments": {}, "success": True},
            ]],
        ]
        for failure in failures:
            self.legacy.reset_mock()
            handler = Mock(side_effect=failure) if isinstance(failure, Exception) else Mock(return_value=failure)
            with self.subTest(failure=type(failure).__name__), \
                    self.assertLogs("uvicorn.error.processing", level="WARNING") as logs:
                result = await self.process(self.client(handler))
            self.legacy.assert_awaited_once()
            handler.assert_called_once()
            self.assertEqual(result["processing_source"], "legacy")
            self.assertIsNone(result["response"])
            self.assertNotIn("SECRET", " ".join(logs.output))

    async def test_disabled_and_silence_do_not_request_ai(self):
        for client in [None, AIClient(AINodeConfig())]:
            self.legacy.reset_mock()
            with patch("core.ai_client.httpx.AsyncClient") as http:
                result = await self.process(client)
            http.assert_not_called()
            self.legacy.assert_awaited_once()
            self.assertEqual(result["processing_source"], "legacy")
        handler = Mock(side_effect=AssertionError("Silence reached AI"))
        before = len(self.states)
        await self.process(self.client(handler), text=" \t", client_id="silent")
        handler.assert_not_called()
        self.assertEqual(len(self.states), before)

    async def test_completed_history_flows_from_legacy_to_ai_and_is_isolated(self):
        self.legacy.side_effect = lambda: {"response": "Done.", "success": True}
        await self.process(text="lights on")
        requests = []
        def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={"type": "conversation", "response": "Hello."})
        client = self.client(handler)
        await self.process(client, text="hello")
        self.assertEqual(requests[0]["history"], [
            {"role": "user", "content": "lights on"}, {"role": "assistant", "content": "Done."}])
        await self.process(client, text="hello", client_id="other")
        self.assertEqual(requests[1]["history"], [])
        self.assertEqual(requests[1]["client_id"], "other")

    async def test_deadline_unwinds_before_parser_and_cancellation_never_falls_back(self):
        for cancel in (False, True):
            self.states = ClientStateManager()
            self.legacy.reset_mock()
            events = []
            entered = asyncio.Event()
            class Transport(httpx.AsyncBaseTransport):
                async def handle_async_request(self, request):
                    entered.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        events.append("finished")
                async def aclose(self):
                    events.append("closed")
            client = AIClient(AINodeConfig("http://ai.invalid", process_timeout=10 if cancel else 0.01),
                              transport=Transport())
            def legacy():
                self.assertEqual(events, ["finished", "closed"])
                return {"response": None}
            self.legacy.side_effect = legacy
            task = asyncio.create_task(self.process(client))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                if cancel:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    self.legacy.assert_not_awaited()
                else:
                    result = await asyncio.wait_for(task, 1)
                    self.assertEqual(result["processing_source"], "legacy")
                    self.legacy.assert_awaited_once()
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            self.assertEqual(events, ["finished", "closed"])
            async with self.states.turn("cube") as turn:
                self.assertEqual(turn.history, [])

    async def test_legacy_error_propagates_without_history(self):
        error = RuntimeError("legacy failure")
        self.legacy.side_effect = error
        with self.assertRaises(RuntimeError) as caught:
            await self.process()
        self.assertIs(caught.exception, error)
        async with self.states.turn("cube") as turn:
            self.assertEqual(turn.history, [])

    async def test_registry_validation_is_after_interpretation_without_execution(self):
        class Power(ToolArguments):
            on: bool
        executor = Mock(side_effect=AssertionError("Invalid intent must not execute"))
        registry = ToolRegistry()
        registry.register(ToolDefinition(name="metadata.only", description="Future", parameters={}))
        registry.register(ToolDefinition(name="lights.set_power", description="Power", parameters={}),
                          arguments_model=Power, handler=executor)
        cases = [
            ("unknown.SECRET", {}, "unadvertised_tool"),
            ("metadata.only", {}, "unadvertised_tool"),
            ("lights.set_power", {"on": "SECRET"}, "invalid_arguments"),
            ("lights.set_power", {"on": True, "client_id": "SECRET"}, "invalid_arguments"),
        ]
        for name, arguments, reason in cases:
            requests = []
            def handler(request):
                requests.append(json.loads(request.content))
                return httpx.Response(200, json={"type": "tool", "tool": name, "arguments": arguments})
            with self.subTest(reason=reason), self.assertLogs("uvicorn.error.processing", level="WARNING") as logs:
                result = await process_turn(
                    {"text": "lights on", "language": "en", "processing_time": 0.1},
                    "cube", voice_started=perf_counter(), ai_client=self.client(handler),
                    client_states=self.states, legacy=self.legacy, tool_registry=registry,
                )
            self.assertEqual(len(requests), 1)
            self.assertEqual([t["name"] for t in requests[0]["tools"]], ["lights.set_power"])
            self.assertEqual(requests[0]["tools"][0]["parameters"], Power.model_json_schema())
            self.assertFalse(result["success"])
            self.assertEqual(result["processing_source"], "ai_tool")
            self.assertEqual(result["response"], "I couldn't complete that request.")
            self.assertEqual(result["processing_timings"]["execution"], 0)
            self.assertIn("reason=" + reason, " ".join(logs.output))
            self.assertNotIn("SECRET", " ".join(logs.output) + json.dumps(result))
            self.legacy.assert_not_awaited()
            executor.assert_not_called()
