import asyncio
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import httpx

from ai_node.app import create_app, transcribe_upload
from ai_node.config import LLMConfig, STTConfig
from ai_node.stt import SpeechToText, STTUnavailable, load_model


def model_with_text(text=" hello "):
    model = Mock()
    model.transcribe.side_effect = lambda *a, **kw: (
        iter([SimpleNamespace(text=text), SimpleNamespace(text=" world ")]),
        SimpleNamespace(language="en"),
    )
    return model


async def event_set(event):
    if not await asyncio.to_thread(event.wait, 3):
        raise AssertionError("Worker did not reach expected point")


class ConfigAndSTTTests(unittest.TestCase):
    def test_defaults_blank_and_independent_overrides(self):
        self.assertEqual(STTConfig.from_env({}), STTConfig())
        self.assertEqual(STTConfig.from_env({
            "CUBE_AI_WHISPER_MODEL": " ", "CUBE_AI_WHISPER_DEVICE": "",
            "CUBE_AI_WHISPER_COMPUTE_TYPE": "\t", "CUBE_WHISPER_MODEL": "large",
        }), STTConfig())
        self.assertEqual(STTConfig.from_env({
            "CUBE_AI_WHISPER_MODEL": " small.en ", "CUBE_AI_WHISPER_DEVICE": " cuda ",
            "CUBE_AI_WHISPER_COMPUTE_TYPE": " int8_float16 ",
        }), STTConfig("small.en", "cuda", "int8_float16"))

    def test_loader_import_and_exact_arguments(self):
        factory = Mock()
        with patch.dict("sys.modules", {"faster_whisper": SimpleNamespace(WhisperModel=factory)}):
            self.assertIs(load_model("base.en", device="cuda", compute_type="float16"), factory.return_value)
        factory.assert_called_once_with("base.en", device="cuda", compute_type="float16",
                                        download_root=STTConfig().cache_dir)

    def test_load_once_reuse_no_warmup_and_timing(self):
        model = model_with_text()
        factory = Mock(return_value=model)
        stt = SpeechToText(STTConfig(), model_factory=factory)
        self.assertFalse(stt.ready)
        factory.assert_not_called()
        with self.assertRaises(STTUnavailable):
            stt.transcribe("audio")
        stt.load()
        stt.load()
        self.assertTrue(stt.ready)
        model.transcribe.assert_not_called()
        factory.assert_called_once_with("base.en", device="cuda", compute_type="float16",
                                        download_root=STTConfig().cache_dir)
        with patch("ai_node.stt.time.perf_counter", side_effect=[10, 10.1236]):
            self.assertEqual(stt.transcribe("audio").model_dump(), {
                "text": "hello world", "language": "en", "processing_time": 0.124,
            })
        stt.transcribe("second")
        self.assertEqual(model.transcribe.call_count, 2)
        model.transcribe.assert_any_call("audio", beam_size=5, vad_filter=True)

    def test_loading_failure_never_retries_or_falls_back(self):
        factory = Mock(side_effect=RuntimeError("unsupported compute/device"))
        stt = SpeechToText(STTConfig(), model_factory=factory)
        with self.assertLogs("ai_node.stt", level="ERROR") as logs:
            stt.load()
        self.assertIn("unsupported compute/device", "".join(logs.output))
        stt.load()
        with self.assertRaises(STTUnavailable):
            stt.transcribe("audio")
        self.assertFalse(stt.ready)
        factory.assert_called_once_with("base.en", device="cuda", compute_type="float16",
                                        download_root=STTConfig().cache_dir)

    def test_auto_device_is_rejected_without_cpu_selection(self):
        factory = Mock()
        stt = SpeechToText(STTConfig(device="auto"), model_factory=factory)
        with self.assertLogs("ai_node.stt", level="ERROR"):
            stt.load()
        self.assertFalse(stt.ready)
        factory.assert_not_called()

    def test_lock_covers_generator_and_timer_starts_before_wait(self):
        entered = threading.Event()
        release = threading.Event()
        second_waiting = threading.Event()
        calls = []
        lock = threading.Lock()
        timed = threading.local()

        class ObservedLock:
            def __enter__(self):
                self_test.assertTrue(timed.started)
                if lock.locked():
                    second_waiting.set()
                lock.acquire()

            def __exit__(self, *args):
                lock.release()

        def timer():
            timed.started = True
            return 1.0

        def transcribe(path, **kwargs):
            calls.append(path)
            def segments():
                self.assertTrue(lock.locked())
                if path == "first":
                    entered.set()
                    self.assertTrue(release.wait(3))
                yield SimpleNamespace(text=path)
                self.assertTrue(lock.locked())
            return segments(), SimpleNamespace(language="en")

        self_test = self
        stt = SpeechToText(STTConfig(), model_factory=lambda *a, **kw: SimpleNamespace(transcribe=transcribe))
        stt.load()
        stt._inference_lock = ObservedLock()
        with patch("ai_node.stt.time.perf_counter", side_effect=timer), ThreadPoolExecutor(2) as pool:
            first = pool.submit(stt.transcribe, "first")
            try:
                self.assertTrue(entered.wait(3))
                second = pool.submit(stt.transcribe, "second")
                self.assertTrue(second_waiting.wait(3))
                self.assertEqual(calls, ["first"])
                self.assertTrue(stt.ready)
            finally:
                release.set()
            self.assertEqual(first.result(3).text, "first")
            self.assertEqual(second.result(3).text, "second")

    def test_copy_error_cleans_private_directory(self):
        paths = []
        def fail_copy(source, destination, **kwargs):
            paths.append(Path(destination.name))
            destination.write(b"partial")
            raise OSError("copy failed")
        with patch("ai_node.app.shutil.copyfileobj", side_effect=fail_copy):
            with self.assertRaises(OSError):
                transcribe_upload(Mock(), BytesIO(b"audio"))
        self.assertFalse(paths[0].parent.exists())


class HTTPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tts = patch("ai_node.app.SpeechSynthesizer.load")
        tts.start()
        self.addCleanup(tts.stop)
        config_patch = patch("ai_node.app.LLMConfig.from_env", return_value=LLMConfig())
        config_patch.start()
        self.addCleanup(config_patch.stop)

    def client(self, app):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://ai.invalid")

    async def test_loading_health_and_shutdown_wait_without_warmup(self):
        entered, release = threading.Event(), threading.Event()
        model = model_with_text()
        def factory(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise AssertionError("Loader was abandoned")
            return model
        app = create_app(model_factory=factory)
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        try:
            await event_set(entered)
            async with self.client(app) as client:
                health = await asyncio.wait_for(client.get("/health"), 1)
                self.assertEqual(health.status_code, 200)
                self.assertFalse(health.json()["capabilities"]["transcribe"])
                self.assertEqual((await client.post("/transcribe", files={"file": ("a", b"x")})).status_code, 503)
            shutdown = asyncio.create_task(lifespan.__aexit__(None, None, None))
            await asyncio.sleep(0)
            shutdown.cancel()
            await asyncio.sleep(0)
            self.assertFalse(shutdown.done())
            self.assertFalse(app.state.loader_task.done())
        finally:
            release.set()
        with self.assertRaises(asyncio.CancelledError):
            await shutdown
        self.assertTrue(app.state.loader_task.done())
        self.assertTrue(app.state.stt.ready)
        model.transcribe.assert_not_called()

    async def test_ready_health_results_validation_and_cleanup(self):
        model = model_with_text()
        paths = []
        def transcribe(path, **kwargs):
            path = Path(path)
            paths.append(path)
            self.assertEqual(path.read_bytes(), b"identical audio")
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            return iter([SimpleNamespace(text=" Hello ")]), SimpleNamespace(language="en")
        model.transcribe.side_effect = transcribe
        factory = Mock(return_value=model)
        app = create_app(model_factory=factory)
        async with app.router.lifespan_context(app):
            await app.state.loader_task
            model.transcribe.assert_not_called()
            async with self.client(app) as client:
                self.assertEqual((await client.get("/health")).json(), {
                    "status": "ok", "capabilities": {"transcribe": True, "process": False},
                })
                for _ in range(2):
                    response = await client.post("/transcribe", files={"file": ("../../escape.wav", b"identical audio")})
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(set(response.json()), {"text", "language", "processing_time"})
                    self.assertEqual(response.json()["text"], "Hello")
                    self.assertEqual(response.json()["language"], "en")
                    self.assertFalse(paths[-1].parent.exists())
                self.assertNotEqual(paths[0], paths[1])
                self.assertNotEqual(paths[0].name, "escape.wav")
                self.assertEqual((await client.post("/transcribe")).status_code, 422)
                with patch("ai_node.app.tempfile.TemporaryDirectory", wraps=__import__("tempfile").TemporaryDirectory) as temp:
                    response = await client.post("/transcribe", files={"file": ("a", b"")})
                    self.assertEqual(response.status_code, 400)
                    temp.assert_called_once()
                self.assertEqual(model.transcribe.call_count, 2)
        # Re-entering this application's lifespan does not create another model.
        async with app.router.lifespan_context(app):
            await app.state.loader_task
        factory.assert_called_once()

    async def test_failure_safe_and_next_upload_succeeds(self):
        paths = []
        def transcribe(path, **kwargs):
            paths.append(Path(path))
            def segments():
                if len(paths) == 1:
                    raise RuntimeError("private decoder detail")
                yield SimpleNamespace(text="recovered")
            return segments(), SimpleNamespace(language="en")
        app = create_app(model_factory=lambda *a, **kw: SimpleNamespace(transcribe=transcribe))
        async with app.router.lifespan_context(app):
            await app.state.loader_task
            async with self.client(app) as client:
                with self.assertLogs("ai_node.app", level="ERROR"):
                    result = await client.post("/transcribe", files={"file": ("a", b"broken")})
                self.assertEqual(result.status_code, 500)
                self.assertEqual(result.json(), {"detail": "Transcription failed."})
                self.assertFalse(paths[-1].parent.exists())
                self.assertTrue((await client.get("/health")).json()["capabilities"]["transcribe"])
                result = await client.post("/transcribe", files={"file": ("a", b"ok")})
                self.assertEqual(result.json()["text"], "recovered")
                self.assertFalse(paths[-1].parent.exists())

    async def test_failed_loading_http_unavailable_and_no_retry(self):
        factory = Mock(side_effect=RuntimeError("no CUDA"))
        app = create_app(model_factory=factory)
        with self.assertLogs("ai_node.stt", level="ERROR"):
            async with app.router.lifespan_context(app):
                await app.state.loader_task
                async with self.client(app) as client:
                    health = await client.get("/health")
                    self.assertEqual(health.status_code, 200)
                    self.assertFalse(health.json()["capabilities"]["transcribe"])
                    for _ in range(2):
                        self.assertEqual((await client.post("/transcribe", files={"file": ("a", b"x")})).status_code, 503)
        factory.assert_called_once()

    async def test_inference_health_and_repeated_cancellation_preserve_file(self):
        entered, release = threading.Event(), threading.Event()
        paths = []
        def transcribe(path, **kwargs):
            paths.append(Path(path))
            def segments():
                entered.set()
                if not release.wait(3):
                    raise AssertionError("Worker abandoned")
                self.assertEqual(paths[0].read_bytes(), b"keep me")
                yield SimpleNamespace(text="done")
            return segments(), SimpleNamespace(language="en")
        app = create_app(model_factory=lambda *a, **kw: SimpleNamespace(transcribe=transcribe))
        async with app.router.lifespan_context(app):
            await app.state.loader_task
            async with self.client(app) as client:
                request = asyncio.create_task(client.post("/transcribe", files={"file": ("a", b"keep me")}))
                try:
                    await event_set(entered)
                    health = await asyncio.wait_for(client.get("/health"), 1)
                    self.assertTrue(health.json()["capabilities"]["transcribe"])
                    for _ in range(2):
                        request.cancel()
                        await asyncio.sleep(0)
                    self.assertFalse(request.done())
                    self.assertTrue(paths[0].exists())
                finally:
                    release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await request
                self.assertFalse(paths[0].parent.exists())
