"""TTS HTTP and startup regressions without loading models or using hardware."""
import importlib.util
import io
from pathlib import Path
from tests.paths import ROOT
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import wave

from fastapi.testclient import TestClient
import numpy as np


def load_server(factory=None, missing=None):
    modules = {"faster_whisper": SimpleNamespace(WhisperModel=Mock())}
    modules["tts.kokoro_tts"] = SimpleNamespace(KokoroTTS=factory or Mock())
    if missing:
        modules[missing] = None
    spec = importlib.util.spec_from_file_location(
        "tts_test_server", ROOT / "server/server.py"
    )
    server = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(server)
    return server


class TTSTests(unittest.TestCase):
    def setUp(self):
        self.engine = Mock()
        self.engine.synthesize.return_value = (np.zeros(2400, dtype=np.float32), 24000)
        self.server = load_server(Mock(return_value=self.engine))
        self.web = TestClient(self.server.app)
        self.addCleanup(self.web.close)

    def test_wav_and_settings(self):
        response = self.web.post("/tts", json={"text": " Hello Cube. "})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "audio/wav")
        self.assertEqual(response.headers["x-tts-voice"], "bm_lewis")
        self.assertGreaterEqual(float(response.headers["x-tts-time"]), 0)
        self.engine.synthesize.assert_called_once_with("Hello Cube.")
        with wave.open(io.BytesIO(response.content)) as audio:
            self.assertEqual(audio.getframerate(), 24000)
            self.assertEqual(audio.getnframes(), 2400)

    def test_input_limits(self):
        for text in ["", "  ", "x" * 501]:
            with self.subTest(text_length=len(text)):
                self.assertEqual(self.web.post("/tts", json={"text": text}).status_code, 400)
        self.engine.synthesize.assert_not_called()
        self.assertEqual(self.web.post("/tts", json={"text": "x" * 500}).status_code, 200)

    def test_synthesis_failure_is_500_and_backend_survives(self):
        self.engine.synthesize.side_effect = RuntimeError("test synthesis failure")
        self.assertEqual(self.web.post("/tts", json={"text": "Hello"}).status_code, 500)
        self.assertEqual(self.web.get("/health").status_code, 200)

    def test_startup_failures_leave_other_routes_available(self):
        cases = [
            {"factory": Mock(side_effect=FileNotFoundError("test missing model"))},
            {"missing": "tts.kokoro_tts"},
            {"missing": "soundfile"},
        ]
        for case in cases:
            with self.subTest(case=case):
                server = load_server(**case)
                with TestClient(server.app) as web:
                    self.assertEqual(web.get("/health").json()["tts"], "unavailable")
                    self.assertEqual(web.post("/tts", json={"text": "Hello"}).status_code, 503)
                    with patch.object(server, "transcribe_audio", return_value={
                        "text": "hello", "language": "en", "processing_time": 0.01,
                    }):
                        for endpoint in ["/transcribe", "/voice"]:
                            self.assertEqual(web.post(endpoint, files={"file": ("test.wav", b"test")}).status_code, 200)

    def test_cache_reuses_wav_and_distinguishes_text(self):
        first = self.web.post("/tts", json={"text": "Hello Cube."})
        second = self.web.post("/tts", json={"text": "Hello Cube."})
        self.assertEqual(first.content, second.content)
        self.engine.synthesize.assert_called_once()
        self.web.post("/tts", json={"text": "Different reply."})
        self.assertEqual(self.engine.synthesize.call_count, 2)

    def test_cache_is_bounded_and_failed_synthesis_is_not_cached(self):
        self.engine.synthesize.side_effect = RuntimeError("offline")
        self.web.post("/tts", json={"text": "retry"})
        self.engine.synthesize.side_effect = None
        self.assertEqual(self.web.post("/tts", json={"text": "retry"}).status_code, 200)
        for index in range(65):
            self.web.post("/tts", json={"text": f"Reply {index}"})
        self.assertEqual(len(self.server.tts_cache), 64)
        calls = self.engine.synthesize.call_count
        self.web.post("/tts", json={"text": "Reply 0"})
        self.assertEqual(self.engine.synthesize.call_count, calls + 1)

    def test_slow_transcription_leaves_event_loop_responsive(self):
        import asyncio
        from threading import Event
        import httpx
        entered, release = Event(), Event()

        def transcribe(path):
            entered.set()
            release.wait(3)
            return {"text": "", "language": "en", "processing_time": 0.1}

        async def exercise():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app),
                                         base_url="http://test") as client:
                task = asyncio.create_task(client.post("/voice", files={"file": ("a.wav", b"audio")}))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                    response = await asyncio.wait_for(client.get("/health"), 1)
                    self.assertEqual(response.status_code, 200)
                finally:
                    release.set()
                response = await task
                self.assertIn("voice_total", response.json()["timings"])
                self.assertEqual(response.json()["listen_for_seconds"], 0)

        with patch.object(self.server, "transcribe_audio", side_effect=transcribe):
            asyncio.run(exercise())
