"""Real HTTP composition with stubbed models, temporary state, and no network."""
import asyncio
from email.parser import BytesParser
from email.policy import default
import importlib.util
import json
from pathlib import Path
from tests.paths import ROOT
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi.testclient import TestClient
import numpy as np  # Load native modules before patch.dict restores sys.modules.

from core.ai_client import AIClient
from core.config import AINodeConfig
from core.client_identity import client_identity
from features.media.event_routes import client_identity as legacy_identity
from features.media import EventStore


class VoiceHTTPTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.paths = []
        self.no_network = patch("socket.socket.connect", side_effect=AssertionError("Unexpected network access"))
        self.no_network.start()
        self.addCleanup(self.no_network.stop)
        self.remote = Mock()
        self.environment = patch.dict("os.environ", {"CUBE_AI_NODE_URL": "",
                                                     "CUBE_AI_TRANSCRIPTION_TIMEOUT": "",
                                                     "CUBE_WHISPER_MODEL": ""})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.whisper, self.kokoro = Mock(), Mock()
        spec = importlib.util.spec_from_file_location(
            "cube_http_regression", ROOT / "server/server.py")
        self.server = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {
            "faster_whisper": SimpleNamespace(WhisperModel=self.whisper),
            "tts.kokoro_tts": SimpleNamespace(KokoroTTS=self.kokoro),
        }), patch("features.media.EventStore", return_value=EventStore(Path(self.directory.name) / "state.sqlite3")):
            with patch.object(AIClient, "__init__", side_effect=AssertionError("Disabled client constructed")):
                spec.loader.exec_module(self.server)
        self.server.movie_commands = Mock()
        self.server.movie_commands.handle.return_value = None
        self.server.movie_commands.continuation.return_value = {"listen_for_seconds": 10, "context_expires_in": 20}
        self.server.control_lights = Mock()
        tuya = patch("integrations.tuya.TuyaClient")
        self.tuya = tuya.start().return_value
        self.addCleanup(tuya.stop)
        self.addCleanup(self.server.plug_commands.close)
        self.tuya.get_device_info.return_value = SimpleNamespace(online=True)
        self.tuya.set_power.return_value = None
        self.tuya.is_on.side_effect = AssertionError("No power confirmation reads")
        self.web = TestClient(self.server.app, raise_server_exceptions=False)
        self.addCleanup(self.web.close)
        save = self.server.save_audio
        async def record_path(file):
            path = await save(file)
            self.paths.append(Path(path))
            return path
        self.server.save_audio = record_path

    def voice(self, text, headers=None):
        def transcribe(path):
            self.assertTrue(Path(path).exists())
            return {"text": text, "language": "en", "processing_time": 0.01}
        with patch.object(self.server, "transcribe_audio", side_effect=transcribe):
            response = self.web.post("/voice", headers=headers or {}, files={"file": ("voice.wav", b"audio")})
        self.assertTrue(self.paths)
        self.assertTrue(all(not path.exists() for path in self.paths))
        self.remote.assert_not_called()
        return response

    def test_startup_settings_and_local_command_with_ai_disabled(self):
        self.whisper.assert_called_once_with("base.en", device="cpu", compute_type="int8",
                                             download_root=self.server.backend_config.whisper_cache_dir)
        self.assertEqual(self.kokoro.call_args.kwargs["speed"], 1.3)
        self.assertEqual(self.kokoro.call_args.kwargs["voice"], "bm_lewis")
        def lights(*args):
            self.assertTrue(all(not path.exists() for path in self.paths))
        self.server.control_lights.side_effect = lights
        response = self.voice("lights on", {"X-Cube-Client-ID": "kitchen"})
        self.assertEqual(response.status_code, 200)
        result = response.json()
        for field in ["transcript", "type", "success", "response", "listen_for_seconds",
                      "context_expires_in", "timings", "processing_time"]:
            self.assertIn(field, result)
        self.assertEqual(result["transcription_source"], "local_cpu")
        self.assertEqual(result["transcription_timings"]["remote_attempt"], 0)
        self.assertEqual(result["action"], "lights_on")
        self.assertEqual(result["response"], "Done.")
        self.server.movie_commands.handle.assert_called_once_with("lights on", "kitchen")
        self.server.control_lights.assert_called_once_with("on")

    def test_local_cube_control_still_requires_pi_control_context(self):
        with patch.object(self.server.control_channel, "request") as hardware:
            result = self.voice("Reboot Cube").json()
        self.assertEqual(result["processing_source"], "local_fast")
        self.assertEqual(result["action"], "cube_reboot")
        self.assertFalse(result["success"])
        self.assertEqual(result["response"], "I couldn't reach the Cube.")
        hardware.assert_not_called()
        self.server.movie_commands.handle.assert_not_called()

    def test_local_named_light_and_plug_status_with_ai_disabled(self):
        result = self.voice("Turn off the mirror").json()
        self.assertEqual(result["processing_source"], "local_fast")
        self.assertTrue(result["success"])
        self.tuya.set_power.assert_called_once()
        self.server.control_lights.assert_not_called()
        self.server.movie_commands.handle.assert_not_called()
        self.tuya.is_on.side_effect = None
        self.tuya.is_on.return_value = False
        result = self.voice("Is the mirror light on?").json()
        self.assertEqual(result["processing_source"], "local_fast")
        self.assertTrue(result["success"])
        self.assertEqual(result["response"], "The mirror plug reports that it's off.")
        self.tuya.set_power.assert_called_once()

    def test_new_local_command_after_remote_transcription_failure(self):
        self.enable_remote(lambda request: (_ for _ in ()).throw(httpx.ConnectError("offline")),
                           process_handler=lambda request: (_ for _ in ()).throw(
                               AssertionError("Local control reached AI interpretation")))
        with patch.object(self.server, "transcribe_audio", return_value={
                "text": "Turn off your display", "language": "en", "processing_time": 0.01}), \
                patch.object(self.server.cube_commands, "execute", return_value={"success": True}) as execute:
            result = self.post_voice().json()
        self.assertTrue(result["success"])
        self.assertEqual(result["action"], "cube_set_display")
        self.assertEqual(result["transcription_source"], "local_cpu")
        self.assertEqual(result["processing_source"], "local_fast")
        self.assertEqual(execute.call_count, 1)
        self.assertEqual(execute.call_args.args[1], "set_display")
        self.assertEqual(execute.call_args.kwargs, {"state": "off"})
        self.remote.assert_called_once()
        self.processing_remote.assert_not_called()
        self.server.movie_commands.handle.assert_not_called()

    def test_unhandled_silence_and_command_failure_remain_local(self):
        self.assertIsNone(self.voice("weather").json()["response"])
        self.assertEqual(self.voice(" ").json()["listen_for_seconds"], 0)
        self.server.control_lights.side_effect = RuntimeError("offline")
        result = self.voice("lights on").json()
        self.assertFalse(result["success"])
        self.assertIn("timings", result)
        self.assertTrue(result["partial"])

    def test_transcription_failure_cleans_upload_for_both_endpoints(self):
        for endpoint in ["/voice", "/transcribe"]:
            with patch.object(self.server, "transcribe_audio", side_effect=RuntimeError("bad audio")):
                response = self.web.post(endpoint, files={"file": ("voice.wav", b"audio")})
            self.assertEqual(response.status_code, 500)
            self.assertTrue(all(not path.exists() for path in self.paths))
        self.remote.assert_not_called()

    def test_identity_validation_and_legacy_export(self):
        self.assertIs(client_identity, legacy_identity)
        self.assertEqual(self.voice("lights on", {"X-Cube-Client-ID": "invalid identity"}).status_code, 400)
        self.server.control_lights.assert_not_called()

    def enable_remote(self, handler, *, timeout=1.0, process_handler=None):
        self.remote = Mock(side_effect=handler)
        self.processing_remote = Mock(side_effect=process_handler or (lambda request: httpx.Response(501)))
        def dispatch(request):
            if request.url.path == "/process":
                return self.processing_remote(request)
            return self.remote(request)
        self.server.ai_client = AIClient(
            AINodeConfig("http://ai.invalid", timeout),
            transport=httpx.MockTransport(dispatch),
        )

    def post_voice(self):
        return self.web.post("/voice", headers={"X-Cube-Client-ID": "kitchen"},
                             files={"file": ("voice.wav", b"audio")})

    def test_configured_startup_constructs_client_without_requests(self):
        spec = importlib.util.spec_from_file_location(
            "configured_voice_test", ROOT / "server/server.py")
        module = importlib.util.module_from_spec(spec)
        with patch.dict("os.environ", {"CUBE_AI_NODE_URL": "http://ai.invalid",
                                      "CUBE_AI_TRANSCRIPTION_TIMEOUT": "0.75"}), \
                patch.dict(sys.modules, {
                    "faster_whisper": SimpleNamespace(WhisperModel=self.whisper),
                    "tts.kokoro_tts": SimpleNamespace(KokoroTTS=self.kokoro),
                }), patch("features.media.EventStore", return_value=EventStore(
                    Path(self.directory.name) / "configured.sqlite3")), \
                patch.object(AIClient, "_request", new_callable=AsyncMock) as request:
            spec.loader.exec_module(module)
        self.assertIsInstance(module.ai_client, AIClient)
        self.assertEqual(module.ai_client.config, AINodeConfig("http://ai.invalid", 0.75))
        request.assert_not_called()

    def test_remote_success_uses_same_audio_and_pipeline_once_after_cleanup(self):
        def remote(request):
            self.assertEqual(request.method, "POST")
            self.assertEqual(request.url.path, "/transcribe")
            self.assertEqual(len(self.paths), 1)
            self.assertEqual(self.paths[0].read_bytes(), b"audio")
            message = BytesParser(policy=default).parsebytes(
                b"Content-Type: " + request.headers["content-type"].encode() +
                b"\r\nMIME-Version: 1.0\r\n\r\n" + request.content)
            parts = list(message.iter_parts())
            self.assertEqual(len(parts), 1)
            self.assertEqual(parts[0].get_param("name", header="content-disposition"), "file")
            self.assertEqual(parts[0].get_payload(decode=True), b"audio")
            return httpx.Response(200, json={"text": "lights on", "language": "en", "processing_time": 0.261})
        self.enable_remote(remote)
        def lights(*args):
            self.assertTrue(all(not path.exists() for path in self.paths))
        self.server.control_lights.side_effect = lights
        with patch.object(self.server, "transcribe_audio") as local, \
                patch.object(self.server, "process_transcription",
                             wraps=self.server.process_transcription) as pipeline:
            response = self.post_voice()
        local.assert_not_called()
        self.remote.assert_called_once()
        pipeline.assert_awaited_once()
        self.assertEqual(pipeline.await_args.args[0]["text"], "lights on")
        self.server.movie_commands.handle.assert_called_once_with("lights on", "kitchen")
        self.server.control_lights.assert_called_once_with("on")
        result = response.json()
        self.assertEqual(result["response"], "Done.")
        self.assertEqual(result["transcription_source"], "remote_ai")
        self.assertEqual(result["processing_time"], 0.261)
        self.assertEqual(result["timings"]["transcription"], 0.261)
        self.assertEqual(result["transcription_timings"]["local_attempt"], 0)
        self.assertTrue(all(not path.exists() for path in self.paths))

    def test_remote_failures_fall_back_and_cleanup(self):
        valid = {"text": "unused", "language": "en", "processing_time": 0.1}
        failures = [
            httpx.ConnectError("SECRET"), httpx.ReadTimeout("SECRET"),
            *[httpx.Response(status, text="SECRET") for status in [302, 400, 503, 500, 502]],
            httpx.Response(200, text="SECRET"),
            *[httpx.Response(200, json=data) for data in [
                {}, [], {**valid, "text": None}, {**valid, "language": 42},
                {**valid, "processing_time": -1}, {**valid, "processing_time": "0.1"},
                {**valid, "unexpected": "SECRET"},
            ]],
            httpx.Response(200, text='{"text":"x","language":"en","processing_time":NaN}'),
            httpx.Response(200, text='{"text":"x","language":"en","processing_time":Infinity}'),
        ]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                def remote(request):
                    self.assertEqual(request.url.path, "/transcribe")
                    if isinstance(failure, Exception):
                        raise failure
                    return failure
                self.enable_remote(remote)
                self.server.movie_commands.handle.reset_mock()
                self.server.control_lights.reset_mock()
                def transcribe(path):
                    self.assertEqual(Path(path).read_bytes(), b"audio")
                    return {"text": "lights on", "language": "en", "processing_time": 1.352}
                with patch.object(self.server, "transcribe_audio", side_effect=transcribe) as local, \
                        self.assertLogs("uvicorn.error.transcription", level="WARNING") as logs:
                    response = self.post_voice()
                self.assertEqual(response.status_code, 200)
                result = response.json()
                self.assertEqual(result["transcription_source"], "local_cpu")
                self.assertEqual(result["response"], "Done.")
                self.assertEqual(result["processing_time"], 1.352)
                self.assertEqual(result["timings"]["transcription"], 1.352)
                self.assertNotIn("SECRET", response.text + " ".join(logs.output))
                local.assert_called_once()
                self.remote.assert_called_once()
                self.server.movie_commands.handle.assert_called_once_with("lights on", "kitchen")
                self.server.control_lights.assert_called_once_with("on")
                self.assertTrue(all(not path.exists() for path in self.paths))

    def test_failed_fallback_preserves_500_and_does_not_run_commands(self):
        self.enable_remote(lambda request: httpx.Response(503))
        with patch.object(self.server, "transcribe_audio", side_effect=RuntimeError("bad audio")) as local:
            response = self.post_voice()
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.text, "Internal Server Error")
        local.assert_called_once()
        self.remote.assert_called_once()
        self.server.movie_commands.handle.assert_not_called()
        self.assertTrue(all(not path.exists() for path in self.paths))

    def test_raw_transcribe_stays_local_with_remote_configured(self):
        self.enable_remote(lambda request: (_ for _ in ()).throw(AssertionError("Remote called")))
        transcription = {"text": "raw", "language": "en", "processing_time": 0.1}
        with patch.object(self.server, "transcribe_audio", return_value=transcription) as local:
            response = self.web.post("/transcribe", files={"file": ("a.wav", b"audio")})
        self.assertEqual(response.json(), transcription)
        self.remote.assert_not_called()
        local.assert_called_once()
        self.assertTrue(all(not path.exists() for path in self.paths))

    def test_remote_cancellation_cleans_upload_without_commands_or_fallback(self):
        async def exercise():
            entered = asyncio.Event()
            events = []
            class Transport(httpx.AsyncBaseTransport):
                async def handle_async_request(self, request):
                    entered.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        events.append("finished")
                async def aclose(self):
                    events.append("closed")
            self.server.ai_client = AIClient(AINodeConfig("http://ai.invalid"), transport=Transport())
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app),
                                         base_url="http://test") as client:
                with patch.object(self.server, "transcribe_audio") as local:
                    task = asyncio.create_task(client.post("/voice", files={"file": ("a.wav", b"audio")}))
                    try:
                        await asyncio.wait_for(entered.wait(), 1)
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                    finally:
                        if not task.done():
                            task.cancel()
                            try:
                                await task
                            except asyncio.CancelledError:
                                pass
                    local.assert_not_called()
            self.assertEqual(events, ["finished", "closed"])
            self.assertTrue(self.paths)
            self.assertTrue(all(not path.exists() for path in self.paths))
            self.server.movie_commands.handle.assert_not_called()
        asyncio.run(exercise())

    def test_remote_command_failure_keeps_spoken_error_and_adds_metadata(self):
        self.enable_remote(lambda request: httpx.Response(
            200, json={"text": "lights on", "language": "en", "processing_time": 0.261}))
        self.server.control_lights.side_effect = RuntimeError("offline")
        with patch.object(self.server, "transcribe_audio") as local:
            response = self.post_voice()
        local.assert_not_called()
        self.remote.assert_called_once()
        result = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(result["success"])
        self.assertEqual(result["response"], "Done, but I couldn't reach the Govee lights.")
        self.assertEqual(result["transcription_source"], "remote_ai")
        self.assertEqual(result["transcription_timings"]["local_attempt"], 0)
        self.assertIn("timings", result)
        self.assertTrue(result["partial"])
        self.assertTrue(all(not path.exists() for path in self.paths))

    def test_clear_light_command_skips_ai_process_after_remote_stt(self):
        self.enable_remote(lambda request: httpx.Response(200, json={
            "text": "Could you turn on the lights?", "language": "en", "processing_time": 0.1}),
            process_handler=Mock(side_effect=AssertionError("Fast path must not call AI")))
        response = self.post_voice()
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result["processing_source"], "local_fast")
        self.assertEqual(result["transcription_source"], "remote_ai")
        self.assertEqual(result["action"], "lights_on")
        self.assertEqual(result["type"], "command")
        self.assertTrue(result["response"].startswith("Done"))
        self.assertIn("timings", result)
        self.assertIn("listen_for_seconds", result)
        self.server.control_lights.assert_called_once_with("on")
        self.server.movie_commands.handle.assert_called_once_with("Could you turn on the lights?", "kitchen")
        self.processing_remote.assert_not_called()

    def test_ai_conversation_runs_after_cleanup_and_skips_legacy(self):
        def process(request):
            self.assertTrue(self.paths)
            self.assertTrue(all(not path.exists() for path in self.paths))
            payload = json.loads(request.content)
            self.assertEqual(payload["transcript"], "Hello Cube")
            self.assertEqual(payload["client_id"], "kitchen")
            self.assertIn("media.respond_to_pending", [tool["name"] for tool in payload["tools"]])
            return httpx.Response(200, json={"type": "conversation", "response": "Hello there."})
        self.enable_remote(lambda request: httpx.Response(200, json={
            "text": "Hello Cube", "language": "en", "processing_time": 0.261}),
            process_handler=process)
        with patch.object(self.server, "transcribe_audio") as local, \
                patch.object(self.server, "process_transcription", new_callable=AsyncMock) as legacy:
            response = self.post_voice()
        self.assertEqual(response.status_code, 200)
        local.assert_not_called()
        legacy.assert_not_awaited()
        self.server.movie_commands.handle.assert_not_called()
        self.server.control_lights.assert_not_called()
        self.remote.assert_called_once()
        self.processing_remote.assert_called_once()
        result = response.json()
        self.assertEqual(result["processing_source"], "ai_conversation")
        self.assertEqual(result["transcription_source"], "remote_ai")
        self.assertEqual(result["response"], "Hello there.")
        self.assertEqual(result["listen_for_seconds"], 10)

    def test_invalid_ai_tool_intent_cannot_execute_legacy_command(self):
        self.enable_remote(lambda request: httpx.Response(200, json={
            "text": "make the lights blue now", "language": "en", "processing_time": 0.1}),
            process_handler=lambda request: httpx.Response(200, json={
                "type": "tool", "tool": "lights.set_power", "arguments": {"on": "invalid"}}))
        response = self.post_voice()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["success"])
        self.assertEqual(response.json()["processing_source"], "ai_tool")
        self.server.movie_commands.handle.assert_not_called()
        self.server.control_lights.assert_not_called()
        self.processing_remote.assert_called_once()
        self.assertTrue(all(not path.exists() for path in self.paths))

    def test_process_cancellation_cleans_history_and_never_runs_legacy(self):
        async def exercise():
            entered = asyncio.Event()
            events = []
            class Transport(httpx.AsyncBaseTransport):
                async def handle_async_request(inner, request):
                    if request.url.path == "/transcribe":
                        return httpx.Response(200, json={
                            "text": "How are you?", "language": "en", "processing_time": 0.1})
                    self.assertEqual(request.url.path, "/process")
                    self.assertTrue(all(not path.exists() for path in self.paths))
                    entered.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        events.append("finished")
                async def aclose(inner):
                    events.append("closed")
            self.server.ai_client = AIClient(AINodeConfig("http://ai.invalid"), transport=Transport())
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app),
                                         base_url="http://test") as client:
                task = asyncio.create_task(client.post("/voice", headers={"X-Cube-Client-ID": "kitchen"},
                                                      files={"file": ("a.wav", b"audio")}))
                try:
                    await asyncio.wait_for(entered.wait(), 1)
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                finally:
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
            self.assertEqual(events, ["closed", "finished", "closed"])
            self.server.movie_commands.handle.assert_not_called()
            self.server.control_lights.assert_not_called()
            async with self.server.client_states.turn("kitchen") as turn:
                self.assertEqual(turn.history, [])
        asyncio.run(exercise())

    def test_bound_light_tool_and_response_run_after_audio_cleanup(self):
        def control(*args):
            self.assertTrue(all(not path.exists() for path in self.paths))
        self.server.control_lights.side_effect = control
        def process(request):
            payload = json.loads(request.content)
            if payload["phase"] == "interpret":
                return httpx.Response(200, json={"type": "tool", "tool": "lights.set_power", "arguments": {"target": "all", "state": "on"}})
            self.assertEqual(payload["tools"], [])
            self.assertTrue(payload["tool_result"]["success"])
            return httpx.Response(200, json={"type": "conversation", "response": payload["response_options"][0]})
        self.enable_remote(lambda request: httpx.Response(200, json={
            "text": "Could you illuminate the room?", "language": "en", "processing_time": 0.1}), process_handler=process)
        response = self.post_voice()
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result["action"], "lights_on")
        self.assertEqual(result["response"], "Done.")
        self.assertEqual(result["listen_for_seconds"], 10)
        self.assertEqual(result["processing_response_source"], "backend")
        self.server.control_lights.assert_called_once_with("on")
        self.server.movie_commands.handle.assert_not_called()
        self.assertEqual(self.processing_remote.call_count, 1)
        self.assertTrue(all(not path.exists() for path in self.paths))

    def test_local_named_music_http_reply_with_real_adapter_and_duplicate_delivery(self):
        from core.http_auth import authenticate
        from features.music.controller import MusicController
        from integrations.spotify.api import SpotifyWebApi
        calls = []
        def spotify(request):
            calls.append((request.method, request.url.path))
            if request.url.path.endswith("/search"):
                return httpx.Response(200, json={"tracks": {"items": [{"name": "Blinding Lights",
                    "uri": "spotify:track:" + "a" * 22, "artists": [{"name": "The Weeknd"}]}]}})
            if request.url.path.endswith("/devices"):
                return httpx.Response(200, json={"devices": [{"name": "Cube", "id": "cube",
                    "is_restricted": False, "is_active": True}]})
            self.assertEqual((request.method, request.url.path), ("PUT", "/v1/me/player/play"))
            return httpx.Response(200, content=b"Mutation accepted by server")
        token_path = Path(self.directory.name) / "events.token"
        token_path.write_text("test-token-" + "a" * 32)
        headers = {"X-Cube-Client-ID": "cube", "X-Cube-Control-Session": "b" * 32,
                   "X-Cube-Request-ID": "c" * 32, "X-Cube-Token": token_path.read_text()}
        with httpx.Client(transport=httpx.MockTransport(spotify)) as http:
            controller = MusicController(SpotifyWebApi(SimpleNamespace(access=lambda: "test-access"), http))
            with patch.object(self.server.music_service, "settings", return_value=SimpleNamespace(cube_id="cube")), \
                    patch.object(self.server.music_service, "get", return_value=controller), \
                    patch.object(self.server, "authenticate_control", side_effect=lambda request: authenticate(request, token_path)):
                for _ in range(2):
                    response = self.voice("play Blinding Lights by The Weeknd", headers)
                    self.assertEqual(response.status_code, 200)
                    result = response.json()
                    self.assertTrue(result["success"])
                    self.assertEqual(result["response"], "Playing.")
                    self.assertEqual(result["listen_for_seconds"], 0)
                    self.assertEqual(result["processing_source"], "local_fast")
                    self.assertEqual(result["processing_response_source"], "backend")
                denied = self.voice("play track Blinding Lights", {**headers, "X-Cube-Token": "wrong"})
                self.assertEqual(denied.status_code, 401)
        self.assertEqual(calls, [("GET", "/v1/search"), ("GET", "/v1/me/player/devices"), ("PUT", "/v1/me/player/play")])
        self.server.movie_commands.handle.assert_not_called()
