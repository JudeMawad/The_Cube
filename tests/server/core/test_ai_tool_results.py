"""Backend replies and durable outcomes after one AI interpretation call."""
import asyncio
import json
from pathlib import Path
import tempfile
import threading
from time import perf_counter
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx

from core.ai_client import AIClient
from core.ai_turn import process_turn
from core.client_state import ClientStateManager
from core.config import AINodeConfig
from core.schemas import ToolResult
from core.tool_registry import ToolRegistry
from features.lights import LightsCommands
from features.lights.tools import register_tools
from features.media import EventStore, MovieCommands
from features.media.tools import MediaTools
from integrations.overseerr_client import Movie, OverseerrClient, RequestUncertain


class AIResultTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = EventStore(Path(self.tmp.name) / "state.sqlite3")
        self.api = Mock(spec=OverseerrClient)
        self.movie = Movie(1, "Dune", "2021")
        self.api.search_movies.return_value = [self.movie]
        self.api.get_movie.return_value = self.movie
        self.api.request_movie.return_value = 44
        self.flow = MovieCommands(self.api, store=self.store)
        self.media = MediaTools(self.flow)
        self.control = Mock()
        self.registry = ToolRegistry()
        self.media.register(self.registry)
        register_tools(self.registry, control_lights=self.control,
                       power_commands=LightsCommands(control_govee=self.control, plugs=Mock()),
                       continuation=self.flow.continuation)
        self.states = ClientStateManager()
        self.legacy = AsyncMock(side_effect=AssertionError("No parser fallback after a valid intent"))
        guard = patch("socket.socket.connect", side_effect=AssertionError("Network forbidden"))
        guard.start()
        self.addCleanup(guard.stop)

    def client(self, handler, timeout=1):
        return AIClient(AINodeConfig("http://ai.invalid", process_timeout=timeout),
                        transport=httpx.MockTransport(handler))

    async def process(self, client):
        return await process_turn({"text": "user transcript", "processing_time": 0.1}, "pi",
                                  voice_started=perf_counter(), ai_client=client,
                                  client_states=self.states, legacy=self.legacy,
                                  tool_registry=self.registry, media_context=self.media.context)

    def intent(self, name="lights.set_power", arguments=None):
        return httpx.Response(200, json={"type": "tool", "tool": name,
                                       "arguments": {"target": "govee", "state": "on"} if arguments is None else arguments})





    async def test_invalid_option_metadata_cannot_break_canonical_fallback(self):
        cases = [
            ({"response": "Canonical.", "response_options": [None, "", " \t", "x" * 501,
                                                               "Alternative.", "Canonical.", "Alternative."]},
             ["Canonical.", "Alternative."]),
            ({"response": "Canonical.", "response_options": "Not a list"}, ["Canonical."]),
            ({"response": "Canonical."}, ["Canonical."]),
            ({"response": "x" * 501, "response_options": ["Unsupported summary."]},
             ["I couldn't summarize that result. Please check its status."]),
        ]
        for reply, expected in cases:
            with self.subTest(reply=reply):
                outcome = ToolResult(tool="lights.set_power", success=True, data={"reply": reply})
                def handler(request):
                    data = json.loads(request.content)
                    if data["phase"] == "interpret":
                        return self.intent()
                    self.assertEqual(data["response_options"], expected)
                    return httpx.Response(200, json={"type": "conversation", "response": "Unapproved."})
                with patch("core.ai_turn.execute_tool", new=AsyncMock(return_value=outcome)) as execute:
                    result = await self.process(self.client(handler))
                execute.assert_awaited_once()
                self.assertEqual(result["response"], expected[0])
                self.assertEqual(result["processing_response_source"], "backend")
                self.legacy.assert_not_awaited()


    async def test_response_cannot_claim_failure_was_successful(self):
        self.control.side_effect = RuntimeError("SECRET")
        def handler(request):
            data = json.loads(request.content)
            if data["phase"] == "interpret":
                return self.intent()
            self.assertFalse(data["tool_result"]["success"])
            return httpx.Response(200, json={"type": "conversation", "response": "Done."})
        result = await self.process(self.client(handler))
        self.assertFalse(result["success"])
        self.assertEqual(result["response"], "I couldn't reach the Govee lights.")
        self.control.assert_called_once()
        self.legacy.assert_not_awaited()



    async def test_cancelled_media_execution_finalizes_sqlite_before_unlock(self):
        entered = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        def request_movie(identity):
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(5):
                raise RuntimeError("Worker not released")
            return 44
        self.api.request_movie.side_effect = request_movie
        handler = Mock(return_value=self.intent("media.request_movie", {"title": "Dune"}))
        task = asyncio.create_task(self.process(self.client(handler)))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
            handler.assert_called_once()  # No final AI call after cancellation.
            self.api.request_movie.assert_called_once_with(1)
            async with self.states.turn("pi") as turn:
                self.assertTrue(self.store.request_record("pi", 1)["confirmed"])
                self.assertEqual(self.flow.submitted[1][0], "accepted")
                self.assertEqual(turn.history, [])
            self.legacy.assert_not_awaited()
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    async def test_light_command_keeps_pending_movie_choice(self):
        self.flow.handle("I want to watch Dune", "pi")
        before = self.media.context("pi").pending
        def handler(request):
            data = json.loads(request.content)
            if data["phase"] == "interpret":
                self.assertEqual(data["context"]["pending"]["pending_id"], before.pending_id)
                return self.intent()
            return httpx.Response(200, json={"type": "conversation", "response": "Done."})
        result = await self.process(self.client(handler))
        self.assertEqual(self.media.context("pi").pending.pending_id, before.pending_id)
        self.assertEqual(result["listen_for_seconds"], 10)
        self.assertGreater(result["context_expires_in"], 0)
        self.api.request_movie.assert_not_called()
        self.control.assert_called_once_with("on")

    async def test_backend_reply_and_history_require_only_one_interpretation(self):
        for name, arguments, action, response in (
            ("lights.set_power", {"target": "govee", "state": "on"}, "lights_on", "Done."),
            ("media.check_availability", {"title": "Dune"}, "movie_confirmation",
             "Dune from 2021 isn't available. Shall I request it?"),
        ):
            self.states = ClientStateManager()
            def handler(request):
                data = json.loads(request.content)
                self.assertEqual(data["phase"], "interpret")
                self.assertIsNone(data["tool_result"])
                self.assertEqual(data["response_options"], [])
                return self.intent(name, arguments)
            request = Mock(side_effect=handler)
            result = await self.process(self.client(request))
            request.assert_called_once()
            self.assertEqual(result["processing_response_source"], "backend")
            self.assertEqual(result["response"], response)
            self.assertEqual(result["action"], action)
            self.assertEqual(result["processing_timings"]["response_generation"], 0)
            async with self.states.turn("pi") as turn:
                self.assertEqual([message.content for message in turn.history], ["user transcript", response])
            self.legacy.assert_not_awaited()
        self.control.assert_called_once_with("on")
        self.api.request_movie.assert_not_called()
        self.assertEqual(self.media.context("pi").pending.state, "offer")

    async def test_uncertain_submission_keeps_backend_failure_without_retry(self):
        message = "I couldn't verify the request. Please check Overseerr before trying again."
        self.api.request_movie.side_effect = RequestUncertain(message)
        request = Mock(return_value=self.intent("media.request_movie", {"title": "Dune"}))
        result = await self.process(self.client(request))
        request.assert_called_once()
        self.assertFalse(result["success"])
        self.assertEqual(result["response"], message)
        self.assertEqual(result["processing_response_source"], "backend")
        self.api.request_movie.assert_called_once_with(1)
        self.assertFalse(self.store.request_record("pi", 1)["confirmed"])
        self.legacy.assert_not_awaited()
