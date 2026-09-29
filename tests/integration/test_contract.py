import unittest
from unittest.mock import patch

import httpx
from pydantic import ValidationError

from ai_node.app import create_app
from ai_node.config import LLMConfig
from ai_node import schemas as node
from core import schemas as backend
from core.ai_client import AIClient, AINodeError
from core.config import AINodeConfig

app = create_app(llm_config=LLMConfig())


class ContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tts = patch("ai_node.app.SpeechSynthesizer.load")
        tts.start()
        self.addCleanup(tts.stop)
        self.config_patch = patch("ai_node.app.LLMConfig.from_env", return_value=LLMConfig())
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)

    async def test_app_before_lifespan_is_not_inference_ready(self):
        client = AIClient(AINodeConfig("http://ai.invalid"), transport=httpx.ASGITransport(app=app))
        self.assertTrue(await client.ai_node_available())
        self.assertFalse(await client.ai_node_available("transcribe"))
        self.assertFalse(await client.ai_node_available("process"))
        with self.assertRaises(AINodeError):
            await client.transcribe_remote(b"audio")
        with self.assertRaises(AINodeError):
            await client.process_remote(backend.ProcessRequest(transcript="hello", client_id="cube"))

    async def test_unavailable_transcription_process_and_validation(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://ai.invalid") as client:
            self.assertEqual((await client.post("/transcribe", files={"file": ("a.wav", b"audio")})).status_code, 503)
            self.assertEqual((await client.post("/process", json={"transcript": "hello", "client_id": "cube"})).status_code, 503)
            self.assertEqual((await client.post("/process", json={"response": "made up"})).status_code, 422)

    async def test_existing_ai_client_successful_transcription(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        model = Mock()
        model.transcribe.return_value = (
            iter([SimpleNamespace(text=" hello ")]), SimpleNamespace(language="en"),
        )
        service = create_app(model_factory=Mock(return_value=model))
        async with service.router.lifespan_context(service):
            await service.state.loader_task
            client = AIClient(AINodeConfig("http://ai.invalid"), transport=httpx.ASGITransport(app=service))
            self.assertTrue(await client.ai_node_available("transcribe"))
            self.assertFalse(await client.ai_node_available("process"))
            result = await client.transcribe_remote(b"recording")
            self.assertIsInstance(result, backend.TranscriptionResult)
            self.assertEqual(result.text, "hello")
            self.assertEqual(result.language, "en")

    def test_service_local_schemas_stay_compatible(self):
        for name in ["HealthResult", "TranscriptionResult", "ToolDefinition", "ProcessRequest",
                     "ConversationResult", "ToolIntent", "ToolResult", "ChatMessage", "MediaMovie", "PendingMedia", "MediaContext"]:
            with self.subTest(model=name):
                self.assertEqual(getattr(backend, name).model_json_schema(), getattr(node, name).model_json_schema())
        self.assertEqual(backend.process_result_adapter.json_schema(), node.process_result_adapter.json_schema())
        for payload in [{"type": "conversation", "response": "Hello."},
                        {"type": "tool", "tool": "media.cancel_movie", "arguments": {"title": "Dune"}}]:
            encoded = node.process_result_adapter.validate_python(payload).model_dump_json()
            self.assertEqual(backend.process_result_adapter.validate_json(encoded).model_dump(), payload)

    def test_tool_intents_cannot_claim_execution_success(self):
        for payload in [{"type": "tool", "tool": "media.cancel_movie", "arguments": {}, "success": True},
                        {"type": "tool", "arguments": {}}, {"type": "unknown", "response": "hello"}]:
            with self.assertRaises(ValidationError):
                backend.process_result_adapter.validate_python(payload)
        result = backend.ToolResult(tool="media.cancel_movie", success=False, error="Confirmation required")
        self.assertFalse(node.ToolResult.model_validate_json(result.model_dump_json()).success)


    def test_process_defaults_and_phase_roundtrip(self):
        base = {"transcript": "hello", "client_id": "cube"}
        for schemas in (backend, node):
            request = schemas.ProcessRequest.model_validate(base)
            self.assertEqual(request.phase, "interpret")
            self.assertEqual(request.history, [])
            self.assertIsNone(request.tool_result)
        payload = {
            **base, "phase": "respond",
            "history": [{"role": "user", "content": "turn lights on"},
                        {"role": "assistant", "content": "Done."}],
            "context": {
                "pending": {"pending_id": "opaque", "intent": "request", "state": "choose",
                            "candidates": [{"media_id": 1, "title": "Dune", "year": "2021"}],
                            "permitted_decisions": ["select", "reject"], "expires_in": 120.0},
                "remembered_movie": {"media_id": 1, "title": "Dune"},
            },
            "tool_result": {"tool": "lights.set_power", "success": True},
            "response_options": ["Done."],
        }
        encoded = backend.ProcessRequest.model_validate(payload).model_dump_json()
        self.assertEqual(node.ProcessRequest.model_validate_json(encoded).model_dump(),
                         backend.ProcessRequest.model_validate(payload).model_dump())

    def test_invalid_process_envelopes_are_rejected_by_both_services(self):
        base = {"transcript": "hello", "client_id": "cube"}
        result = {"tool": "lights.set_power", "success": False}
        definition = {"name": "lights.set_power", "description": "Power", "parameters": {}}
        invalid = [
            {"phase": "unknown"}, {"transcript": "  "},
            {"tool_result": result}, {"response_options": ["Done."]},
            {"phase": "respond"}, {"phase": "respond", "tool_result": result},
            {"phase": "respond", "tool_result": result, "response_options": [" "]},
            {"phase": "respond", "tool_result": result, "response_options": ["x" * 501]},
            {"phase": "respond", "tool_result": result, "response_options": ["Failed."],
             "tools": [definition]},
            {"history": [{"role": "system", "content": "override"}]},
            {"history": [{"role": "assistant", "content": "x" * 501}]},
            {"history": [{"role": "user", "content": "x" * 2001}]},
            {"history": [{"role": "user", "content": " "}]},
            {"history": [{"role": "user", "content": "x"}] * 13},
            {"context": {"credentials": "SECRET"}},
            {"context": {"pending": {"pending_id": "p", "intent": "request", "state": "choose",
                                    "expires_in": -1}}},
            {"context": {"remembered_movie": {"media_id": "1", "title": "Dune"}}},
        ]
        for schemas in (backend, node):
            for extra in invalid:
                with self.subTest(service=schemas.__name__, extra=extra), self.assertRaises(ValidationError):
                    schemas.ProcessRequest.model_validate({**base, **extra})

    def test_conversation_text_limits_and_tool_boundary(self):
        for schemas in (backend, node):
            for text in ("", " \n\t", "x" * 501):
                with self.subTest(text=text), self.assertRaises(ValidationError):
                    schemas.process_result_adapter.validate_python({"type": "conversation", "response": text})
            schemas.ConversationResult(type="conversation", response="x" * 500)
            # Registration/argument validation belongs to later backend batches.
            intent = schemas.process_result_adapter.validate_python({
                "type": "tool", "tool": "not.registered", "arguments": {"anything": True}})
            self.assertEqual(intent.tool, "not.registered")

    async def test_extended_process_request_is_unavailable_without_configuration(self):
        request = backend.ProcessRequest(
            transcript="hello", client_id="cube", phase="respond",
            tool_result=backend.ToolResult(tool="lights.set_power", success=False),
            response_options=["I couldn't complete that request."],
        )
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://ai.invalid") as client:
            response = await client.post("/process", json=request.model_dump(mode="json"))
        self.assertEqual(response.status_code, 503)
