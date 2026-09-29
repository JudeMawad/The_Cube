import asyncio
import json
import unittest
from unittest.mock import Mock, patch

import httpx

from core.ai_client import AIClient, AINodeError
from core.config import AINodeConfig
from core.schemas import ProcessRequest, ToolResult

HEALTH = {"status": "ok", "capabilities": {"transcribe": True, "process": False}}


class AIClientTests(unittest.IsolatedAsyncioTestCase):
    def client(self, handler):
        return AIClient(AINodeConfig("http://ai.invalid/prefix"), transport=httpx.MockTransport(handler))

    async def test_disabled_client_never_constructs_http_transport(self):
        client = AIClient(AINodeConfig())
        with patch("core.ai_client.httpx.AsyncClient") as http:
            self.assertFalse(await client.ai_node_available())
            with self.assertRaises(AINodeError):
                await client.transcribe_remote(b"audio")
            with self.assertRaises(AINodeError):
                await client.process_remote(ProcessRequest(transcript="hello", client_id="cube"))
            http.assert_not_called()

    async def test_health_distinguishes_liveness_from_capability(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, json=HEALTH)
        client = self.client(handler)
        self.assertTrue(await client.ai_node_available())
        self.assertTrue(await client.ai_node_available("transcribe"))
        self.assertFalse(await client.ai_node_available("process"))
        self.assertEqual(str(requests[0].url), "http://ai.invalid/prefix/health")
        with self.assertRaises(ValueError):
            await client.ai_node_available("unknown")

    async def test_offline_http_errors_and_bad_health_are_unavailable(self):
        responses = [httpx.Response(503), httpx.Response(302, headers={"Location": "http://elsewhere"}),
                     httpx.Response(200, text="not json"), httpx.Response(200, json={"status": "ok"}),
                     httpx.Response(200, json={"status": "ok", "capabilities": {"transcribe": "yes", "process": False}})]
        for response in responses:
            handler = Mock(return_value=response)
            self.assertFalse(await self.client(handler).ai_node_available())
            handler.assert_called_once()
        for error in [httpx.ConnectError("SECRET"), httpx.ReadTimeout("SECRET")]:
            handler = Mock(side_effect=error)
            self.assertFalse(await self.client(handler).ai_node_available())
            handler.assert_called_once()

    async def test_overall_deadlines_cancel_slow_transport_without_retry(self):
        started, cancelled = [], []
        async def slow(request):
            started.append(request.url.path)
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.append(True)
        client = AIClient(AINodeConfig("http://ai.invalid", 0.01, 0.01), transport=httpx.MockTransport(slow))
        with patch.object(client, "HEALTH_TIMEOUT", 0.01):
            self.assertFalse(await asyncio.wait_for(client.ai_node_available(), 1))
            with self.assertRaises(AINodeError):
                await asyncio.wait_for(client.transcribe_remote(b"audio"), 1)
            with self.assertRaises(AINodeError):
                await asyncio.wait_for(client.process_remote(ProcessRequest(transcript="hi", client_id="cube")), 1)
        self.assertEqual(len(started), 3)
        self.assertEqual(len(cancelled), 3)

    async def test_transcription_upload_and_typed_result(self):
        def handler(request):
            self.assertEqual(request.method, "POST")
            self.assertTrue(request.url.path.endswith("/transcribe"))
            self.assertIn(b'name="file"', request.content)
            self.assertIn(b"command.wav", request.content)
            self.assertIn(b"test audio", request.content)
            return httpx.Response(200, json={"text": "hello", "language": "en", "processing_time": 0.1})
        self.assertEqual((await self.client(handler).transcribe_remote(b"test audio")).text, "hello")

    async def test_process_roundtrip_for_conversation_and_tool_intent(self):
        for result in [{"type": "conversation", "response": "Hello."},
                       {"type": "tool", "tool": "lights.set_power", "arguments": {"on": True}}]:
            def handler(request):
                self.assertEqual(json.loads(request.content), ProcessRequest(transcript="hello", client_id="cube").model_dump(mode="json"))
                return httpx.Response(200, json=result)
            actual = await self.client(handler).process_remote(ProcessRequest(transcript="hello", client_id="cube"))
            self.assertEqual(actual.model_dump(), result)

    async def test_inference_errors_are_typed_and_do_not_expose_upstream_details(self):
        for response in [httpx.Response(501, text="SECRET"), httpx.Response(200, text="SECRET"),
                         httpx.Response(200, json={"unknown": "SECRET"})]:
            client = self.client(lambda request: response)
            for operation in [lambda: client.transcribe_remote(b"audio"),
                              lambda: client.process_remote(ProcessRequest(transcript="hello", client_id="cube"))]:
                with self.assertRaises(AINodeError) as error:
                    await operation()
                self.assertNotIn("SECRET", str(error.exception))

    async def test_transcription_phase_deadlines_and_other_capabilities(self):
        for timeout in [1.0, 0.1, 2.0]:
            requests = []
            def handler(request):
                requests.append(request)
                if request.url.path == "/health":
                    return httpx.Response(200, json=HEALTH)
                if request.url.path == "/process":
                    return httpx.Response(200, json={"type": "conversation", "response": "hello"})
                return httpx.Response(200, json={"text": "", "language": "en", "processing_time": 0.1})
            client = AIClient(AINodeConfig("http://ai.invalid", timeout),
                              transport=httpx.MockTransport(handler))
            await client.transcribe_remote(b"audio")
            self.assertEqual(requests[-1].extensions["timeout"], {
                "connect": min(0.25, timeout), "read": timeout, "write": timeout, "pool": timeout,
            })
            await client.ai_node_available()
            self.assertTrue(all(t == 1.0 for t in requests[-1].extensions["timeout"].values()))
            await client.process_remote(ProcessRequest(transcript="hello", client_id="cube"))
            self.assertEqual(requests[-1].extensions["timeout"], {
                "connect": 0.25, "read": 10.0, "write": 10.0, "pool": 10.0,
            })

    async def test_safe_failure_categories(self):
        cases = [
            (httpx.ReadTimeout("SECRET"), "timeout", None),
            (httpx.ConnectError("SECRET"), "transport", None),
            (httpx.Response(503, text="SECRET"), "http_status", 503),
            (httpx.Response(200, text="SECRET"), "invalid_response", None),
            (httpx.Response(200, json={"unknown": "SECRET"}), "invalid_response", None),
        ]
        for response, reason, status in cases:
            handler = Mock(side_effect=response) if isinstance(response, Exception) else Mock(return_value=response)
            with self.subTest(reason=reason), self.assertRaises(AINodeError) as caught:
                await self.client(handler).transcribe_remote(b"audio")
            self.assertEqual(caught.exception.reason, reason)
            self.assertEqual(caught.exception.status_code, status)
            self.assertNotIn("SECRET", str(caught.exception))
            handler.assert_called_once()

    async def test_process_timeout_configuration_and_response_phase_serialization(self):
        for timeout in [0.05, 3.0]:
            envelope = ProcessRequest(
                transcript="hello", client_id="cube", phase="respond",
                tool_result=ToolResult(tool="lights.set_power", success=False),
                response_options=["Unable to complete that request."],
            )
            def handler(request):
                self.assertEqual(request.url.path, "/process")
                self.assertEqual(request.extensions["timeout"], {
                    "connect": min(0.25, timeout), "read": timeout, "write": timeout, "pool": timeout})
                self.assertEqual(json.loads(request.content), envelope.model_dump(mode="json"))
                return httpx.Response(200, json={"type": "conversation", "response": envelope.response_options[0]})
            client = AIClient(AINodeConfig("http://ai.invalid", process_timeout=timeout),
                              transport=httpx.MockTransport(handler))
            await client.process_remote(envelope)

    async def test_process_deadline_and_cancellation_close_transport(self):
        for external in (False, True):
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
            client = AIClient(
                AINodeConfig("http://ai.invalid", process_timeout=10 if external else 0.01),
                transport=Transport(),
            )
            task = asyncio.create_task(client.process_remote(ProcessRequest(transcript="hi", client_id="cube")))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                if external:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                else:
                    with self.assertRaises(AINodeError) as caught:
                        await asyncio.wait_for(task, 1)
                    self.assertEqual(caught.exception.reason, "timeout")
                self.assertEqual(events, ["finished", "closed"])
            finally:
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
