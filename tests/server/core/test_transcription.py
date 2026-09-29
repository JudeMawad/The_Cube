"""Offline selector tests, including HTTP cancellation before local fallback."""
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import httpx

from core.ai_client import AIClient
from core.config import AINodeConfig
from core.transcription import select_transcription


LOCAL = {"text": "local", "language": "en", "processing_time": 0.4}
REMOTE = {"text": "remote", "language": "en", "processing_time": 0.1}
LOGGER = "uvicorn.error.transcription"


class SlowTransport(httpx.AsyncBaseTransport):
    def __init__(self):
        self.entered = asyncio.Event()
        self.events = []

    async def handle_async_request(self, request):
        self.events.append("request")
        self.entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.events.append("request_finished")

    async def aclose(self):
        self.events.append("closed")


class SelectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "audio.wav"
        self.path.write_bytes(b"same recording")
        self.local = Mock(return_value=LOCAL)
        no_network = patch("socket.socket.connect", side_effect=AssertionError("Network forbidden"))
        no_network.start()
        self.addCleanup(no_network.stop)

    def client(self, handler, timeout=1.0):
        return AIClient(AINodeConfig("http://ai.invalid", timeout),
                        transport=httpx.MockTransport(handler))

    async def select(self, client=None):
        return await select_transcription(
            str(self.path), local_transcribe=self.local, ai_client=client,
        )

    async def test_disabled_does_not_read_for_upload_or_construct_http(self):
        with patch("pathlib.Path.read_bytes", side_effect=AssertionError("Remote read")), \
                patch("core.ai_client.httpx.AsyncClient") as http, \
                patch("core.transcription.perf_counter", side_effect=[0, 1, 3, 4]):
            selected = await self.select()
        http.assert_not_called()
        self.local.assert_called_once_with(str(self.path))
        self.assertIs(selected.transcription, LOCAL)
        self.assertEqual(selected.source, "local_cpu")
        self.assertEqual(selected.timings, {"remote_attempt": 0, "local_attempt": 2, "total": 4})

    async def test_remote_success_and_silence(self):
        for text in ["remote", "", "  "]:
            self.local.reset_mock()
            handler = Mock(return_value=httpx.Response(200, json={**REMOTE, "text": text}))
            with self.subTest(text=text), self.assertLogs(LOGGER, level="INFO") as logs, \
                    patch("core.transcription.perf_counter", side_effect=[0, 1, 3, 4]):
                selected = await self.select(self.client(handler))
            self.local.assert_not_called()
            handler.assert_called_once()
            self.assertEqual(selected.transcription, {**REMOTE, "text": text})
            self.assertEqual(selected.source, "remote_ai")
            self.assertEqual(selected.timings, {"remote_attempt": 2, "local_attempt": 0, "total": 4})
            self.assertIn("source=remote_ai", " ".join(logs.output))

    async def test_failure_timings_and_logs_preserve_local_processing_time(self):
        handler = Mock(side_effect=httpx.ConnectError("SECRET"))
        with self.assertLogs(LOGGER, level="INFO") as logs, \
                patch("core.transcription.perf_counter", side_effect=[0, 1, 2, 3, 4, 6, 7]):
            selected = await self.select(self.client(handler))
        self.assertEqual(selected.timings, {"remote_attempt": 2, "local_attempt": 2, "total": 7})
        self.assertEqual(selected.source, "local_cpu")
        self.assertIs(selected.transcription, LOCAL)
        self.local.assert_called_once_with(str(self.path))
        output = " ".join(logs.output)
        self.assertIn("reason=transport", output)
        self.assertIn("source=local_cpu", output)
        self.assertNotIn("SECRET", output)

    async def test_deadline_cancels_and_closes_http_before_fallback(self):
        transport = SlowTransport()
        client = AIClient(AINodeConfig("http://ai.invalid", 0.01), transport=transport)
        def local(path):
            self.assertEqual(transport.events, ["request", "request_finished", "closed"])
            transport.events.append("local")
            return LOCAL
        self.local.side_effect = local
        before = set(asyncio.all_tasks())
        with self.assertLogs(LOGGER, level="WARNING") as logs:
            selected = await asyncio.wait_for(self.select(client), 1)
        self.assertEqual(selected.source, "local_cpu")
        self.local.assert_called_once()
        self.assertEqual(transport.events, ["request", "request_finished", "closed", "local"])
        self.assertEqual(set(asyncio.all_tasks()), before)
        self.assertIn("reason=timeout", " ".join(logs.output))

    async def test_external_cancellation_closes_http_without_fallback(self):
        transport = SlowTransport()
        client = AIClient(AINodeConfig("http://ai.invalid"), transport=transport)
        task = asyncio.create_task(self.select(client))
        try:
            await asyncio.wait_for(transport.entered.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.local.assert_not_called()
            self.assertEqual(transport.events, ["request", "request_finished", "closed"])
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    async def test_failed_remote_preparation_uses_local_path(self):
        client = self.client(Mock(side_effect=AssertionError("HTTP must not start")))
        with patch("pathlib.Path.read_bytes", side_effect=OSError("SECRET")), \
                self.assertLogs(LOGGER, level="WARNING") as logs:
            selected = await self.select(client)
        self.local.assert_called_once_with(str(self.path))
        self.assertEqual(selected.source, "local_cpu")
        self.assertIn("reason=audio_read", " ".join(logs.output))
        self.assertNotIn("SECRET", " ".join(logs.output))

    async def test_local_error_is_not_wrapped(self):
        error = RuntimeError("local failure")
        self.local.side_effect = error
        with self.assertRaises(RuntimeError) as caught:
            await self.select()
        self.assertIs(caught.exception, error)

    async def test_deadline_also_bounds_response_body_and_closes_stream(self):
        events = []
        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                try:
                    yield b'{"text":'
                    await asyncio.Event().wait()
                finally:
                    events.append("body_finished")
            async def aclose(self):
                events.append("body_closed")
        class Transport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                return httpx.Response(200, stream=Body())
            async def aclose(self):
                events.append("client_closed")
        client = AIClient(AINodeConfig("http://ai.invalid", 0.01), transport=Transport())
        def local(path):
            self.assertEqual(events, ["body_finished", "body_closed", "client_closed"])
            return LOCAL
        self.local.side_effect = local
        with self.assertLogs(LOGGER, level="WARNING"):
            result = await asyncio.wait_for(self.select(client), 1)
        self.local.assert_called_once()
        self.assertEqual(result.source, "local_cpu")
