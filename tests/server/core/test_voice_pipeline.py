import os
from threading import Lock
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from core.voice_pipeline import process_transcription, route_command
from speech.stt import transcribe_audio


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_diagnostics_hide_transcript_and_upstream_errors(self):
        sentinel = "private-sentinel-do-not-log"
        with patch.dict(os.environ, {"CUBE_LOG_TRANSCRIPTS": ""}), self.assertLogs("core.voice_pipeline") as logs:
            result = await process_transcription(
                {"text": sentinel, "processing_time": 0}, "cube", voice_started=0,
                handle_command=Mock(side_effect=RuntimeError(sentinel)), continuation=Mock())
        self.assertNotIn(sentinel, " ".join(logs.output))
        self.assertFalse(result["success"])
        self.assertEqual(result["transcript"], sentinel)  # HTTP compatibility is retained.

    async def test_transcript_diagnostics_require_explicit_opt_in(self):
        with patch.dict(os.environ, {"CUBE_LOG_TRANSCRIPTS": "1"}), self.assertLogs("core.voice_pipeline", "INFO") as logs:
            await process_transcription(
                {"text": "diagnostic example", "processing_time": 0}, "cube", voice_started=0,
                handle_command=Mock(return_value=None), continuation=Mock(return_value={}))
        self.assertIn("diagnostic example", " ".join(logs.output))

    async def test_command_result_preserves_failure_and_feature_fields(self):
        command = {"action": "movie_error", "success": False, "response": "Unavailable.",
                   "listen_for_seconds": 0, "context_expires_in": 0, "timings": {"movie": 0.2}}
        continuation = Mock()
        with patch("core.voice_pipeline.time.perf_counter", side_effect=[2, 3, 4]):
            result = await process_transcription(
                {"text": "get Dune", "processing_time": 0.1}, "cube", voice_started=0,
                handle_command=Mock(return_value=command), continuation=continuation)
        self.assertFalse(result["success"])
        self.assertEqual(result["type"], "command")
        self.assertEqual(result["timings"], {"transcription": 0.1, "command": 1, "voice_total": 4, "movie": 0.2})
        self.assertEqual(result["processing_time"], 0.1)
        self.assertEqual(result["context_expires_in"], 0)
        continuation.assert_not_called()

    async def test_exception_keeps_exact_legacy_response_shape(self):
        result = await process_transcription(
            {"text": "lights on", "processing_time": 0.1}, "cube", voice_started=0,
            handle_command=Mock(side_effect=RuntimeError("offline")), continuation=Mock())
        self.assertEqual(result, {"transcript": "lights on", "type": "command", "success": False,
                                 "response": "I couldn't complete that command. Please try again.",
                                 "processing_time": 0.1})

    async def test_unhandled_continuation_and_silence(self):
        for text in ["hello", " "]:
            continuation = Mock(return_value={"listen_for_seconds": 10, "context_expires_in": 20})
            result = await process_transcription(
                {"text": text, "processing_time": 0.1}, "cube", voice_started=0,
                handle_command=Mock(return_value=None), continuation=continuation)
            self.assertEqual(result["type"], "unhandled")
            self.assertTrue(result["success"])
            self.assertIsNone(result["response"])
            self.assertEqual(result["listen_for_seconds"], 10 if text.strip() else 0)
            if text.strip():
                continuation.assert_called_once_with("cube")
            else:
                continuation.assert_not_called()
                self.assertNotIn("context_expires_in", result)

    def test_primary_precedes_fallback_and_keeps_its_own_continuation(self):
        primary = Mock(return_value={"action": "movie_requested", "listen_for_seconds": 0})
        fallback, continuation = Mock(), Mock()
        self.assertEqual(route_command("get Dune", "cube", primary=primary, fallback=fallback,
                                       continuation=continuation), primary.return_value)
        fallback.assert_not_called()
        continuation.assert_not_called()

    def test_fallback_receives_continuation(self):
        continuation = Mock(return_value={"listen_for_seconds": 10})
        result = route_command("lights on", "cube", primary=Mock(return_value=None),
                               fallback=Mock(return_value={"action": "lights_on"}), continuation=continuation)
        self.assertEqual(result, {"action": "lights_on", "listen_for_seconds": 10})

    def test_local_transcription_holds_lock_while_consuming_segments(self):
        lock = Lock()
        def segments():
            self.assertTrue(lock.locked())
            yield SimpleNamespace(text=" hello ")
            yield SimpleNamespace(text=" Cube ")
        model = Mock()
        model.transcribe.return_value = (segments(), SimpleNamespace(language="en"))
        result = transcribe_audio("recording.wav", model=model, transcription_lock=lock)
        model.transcribe.assert_called_once_with("recording.wav", beam_size=5, vad_filter=True)
        self.assertEqual(result["text"], "hello Cube")
        self.assertEqual(result["language"], "en")
        self.assertFalse(lock.locked())
