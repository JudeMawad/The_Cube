"""Cancellation checkpoints and backend routes, with all external effects stubbed."""
import asyncio
from contextlib import redirect_stdout
import importlib.util
import io
from pathlib import Path
import tempfile
from threading import Event, Lock
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import Mock, AsyncMock, patch

import httpx
import numpy  # Load native extensions before patch.dict restores sys.modules.
import soundfile
from fastapi import Request, UploadFile

from core import cancellation as cancel
from core.config import AINodeConfig
from core.ai_client import AIClient
from speech.stt import transcribe_audio
from tests.paths import ROOT


def request(disconnected, *, path='/voice', headers=()):
    async def receive():
        await disconnected.wait()
        return {'type': 'http.disconnect'}
    return Request({'type': 'http', 'method': 'POST', 'scheme': 'http',
                    'path': path, 'query_string': b'', 'headers': list(headers),
                    'server': ('test', 80), 'client': ('test', 1)}, receive)


def load_backend():
    spec = importlib.util.spec_from_file_location('barge_backend', ROOT / 'server/server.py')
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {'faster_whisper': SimpleNamespace(WhisperModel=Mock()),
                                 'tts.kokoro_tts': SimpleNamespace(KokoroTTS=Mock())}), \
         patch('features.media.EventStore'), patch.dict('os.environ', {'CUBE_AI_NODE_URL': ''}), \
         redirect_stdout(io.StringIO()):
        spec.loader.exec_module(module)
    return module


class CancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_disconnect_cancels_wait_and_preserves_identifier(self):
        disconnected, started, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        seen = []
        @cancel.disconnect_aware
        async def route(http_request: Request):
            seen.append(cancel.headers())
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()
        task = asyncio.create_task(route(request(disconnected, headers=[(b'x-cube-interaction-id', b'a'*32)])))
        await started.wait()
        disconnected.set()
        result = await asyncio.wait_for(task, 2)
        self.assertEqual(result.status_code, 499)
        self.assertTrue(closed.is_set())
        self.assertEqual(seen, [{'X-Cube-Interaction-ID': 'a'*32}])
        self.assertIsNone(cancel.current_scope.get())

    async def test_native_worker_retains_file_until_safe_cleanup(self):
        disconnected, started, release = asyncio.Event(), Event(), Event()
        paths = []
        @cancel.disconnect_aware
        async def route(http_request: Request):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'audio'
                path.write_bytes(b'audio')
                paths.append(path)
                def native():
                    started.set()
                    if not release.wait(3):
                        raise AssertionError('test worker timed out')
                    self.assertTrue(path.exists())
                await cancel.worker(native)
        task = asyncio.create_task(route(request(disconnected)))
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 2))
            disconnected.set()
            await asyncio.sleep(.02)
            self.assertFalse(task.done())
            self.assertTrue(paths[0].exists())
        finally:
            release.set()
        self.assertEqual((await task).status_code, 499)
        self.assertFalse(paths[0].exists())

    async def test_cancelled_lock_waiter_never_calls_native_stt(self):
        scope = cancel.Scope('b'*32)
        token = cancel.current_scope.set(scope)
        lock = Lock()
        lock.acquire()
        model = Mock()
        try:
            task = asyncio.create_task(cancel.worker(transcribe_audio, 'audio', model=model,
                                                      transcription_lock=lock))
            await asyncio.sleep(.02)
            scope.cancelled.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
            model.transcribe.assert_not_called()
        finally:
            lock.release()
            cancel.current_scope.reset(token)

    async def test_whisper_stops_between_segments_and_closes_generator(self):
        scope = cancel.Scope('c'*32)
        token = cancel.current_scope.set(scope)
        yielded, closed = [], []
        def segments():
            try:
                yielded.append(1)
                scope.cancelled.set()
                yield SimpleNamespace(text='old')
                yielded.append(2)
            finally:
                closed.append(True)
        model = Mock()
        model.transcribe.return_value = (segments(), SimpleNamespace(language='en'))
        try:
            with self.assertRaises(asyncio.CancelledError):
                await cancel.worker(transcribe_audio, 'audio', model=model, transcription_lock=Lock())
        finally:
            cancel.current_scope.reset(token)
        self.assertEqual(yielded, [1])
        self.assertEqual(closed, [True])


class BackendRoutesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = load_backend()
        self.addAsyncCleanup(self.server.remote_tts_client.aclose)
        self.addCleanup(self.server.plug_commands.close)
        self.addCleanup(self.server.control_channel.close)

    async def test_remote_stt_disconnect_never_falls_back_or_interprets(self):
        entered, disconnected, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def remote(req):
            self.assertEqual(req.headers['X-Cube-Interaction-ID'], 'd'*32)
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()
        self.server.ai_client = AIClient(AINodeConfig('http://ai.invalid', transcription_timeout=10),
                                         transport=httpx.MockTransport(remote))
        with patch.object(self.server, 'transcribe_audio') as local, \
             patch.object(self.server, 'process_turn', new_callable=AsyncMock) as process:
            task = asyncio.create_task(self.server.voice(request(disconnected,
                headers=[(b'x-cube-interaction-id', b'd'*32)]), UploadFile(io.BytesIO(b'audio'))))
            await entered.wait()
            disconnected.set()
            self.assertEqual((await asyncio.wait_for(task, 2)).status_code, 499)
            local.assert_not_called()
            process.assert_not_called()
        self.assertTrue(closed.is_set())

    async def test_remote_tts_disconnect_never_uses_local_or_cache(self):
        entered, disconnected = asyncio.Event(), asyncio.Event()
        async def remote(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        self.server.ai_config = AINodeConfig('http://ai.invalid')
        with patch.object(self.server.remote_tts_client, 'post', side_effect=remote), \
             patch.object(self.server, 'local_tts_wav') as local:
            task = asyncio.create_task(self.server.synthesize_speech(self.server.TTSRequest(text='old'),
                                        request(disconnected, path='/tts')))
            await entered.wait()
            disconnected.set()
            self.assertEqual((await asyncio.wait_for(task, 2)).status_code, 499)
            local.assert_not_called()
        self.assertFalse(self.server.tts_cache)

    async def test_local_stt_disconnect_blocks_tool_start(self):
        entered, release, disconnected = Event(), Event(), asyncio.Event()
        def transcribe(path):
            entered.set()
            release.wait(3)
            self.assertTrue(Path(path).exists())
            return {'text': 'lights on', 'processing_time': .1}
        with patch.object(self.server, 'transcribe_audio', side_effect=transcribe), \
             patch.object(self.server, 'process_turn', new_callable=AsyncMock) as process:
            task = asyncio.create_task(self.server.voice(request(disconnected), UploadFile(io.BytesIO(b'audio'))))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                disconnected.set()
                await asyncio.sleep(.02)
            finally:
                release.set()
            self.assertEqual((await task).status_code, 499)
            process.assert_not_called()

    async def test_local_tts_cancel_discards_native_output(self):
        entered, release, disconnected = Event(), Event(), asyncio.Event()
        def native(text):
            entered.set()
            release.wait(3)
            return b'wav'
        with patch.object(self.server, 'local_tts_wav', side_effect=native):
            task = asyncio.create_task(self.server.synthesize_speech(self.server.TTSRequest(text='old'),
                                        request(disconnected, path='/tts')))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                disconnected.set()
                await asyncio.sleep(.02)
            finally:
                release.set()
            self.assertEqual((await task).status_code, 499)
            self.assertFalse(self.server.tts_cache)

    async def test_disconnect_during_upload_read_cleans_tempfile_before_inference(self):
        entered, release, disconnected = Event(), Event(), asyncio.Event()
        paths = []
        original_temp = tempfile.NamedTemporaryFile
        def temporary(*args, **kwargs):
            file = original_temp(*args, **kwargs)
            paths.append(Path(file.name))
            return file
        class SlowUpload(io.BytesIO):
            def read(self, *args):
                entered.set()
                release.wait(3)
                return super().read(*args)
        with patch.object(self.server.tempfile, 'NamedTemporaryFile', side_effect=temporary), \
             patch.object(self.server, 'transcribe_audio') as transcribe:
            task = asyncio.create_task(self.server.voice(request(disconnected), UploadFile(SlowUpload(b'audio'))))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                disconnected.set()
                await asyncio.sleep(.02)
            finally:
                release.set()
            self.assertEqual((await task).status_code, 499)
            transcribe.assert_not_called()
        self.assertTrue(paths)
        self.assertTrue(all(not path.exists() for path in paths))

    async def test_preroll_prefix_is_removed_only_for_wake_recordings(self):
        for prefix, original, expected in [(True, 'Hey Cube, turn the lights red.', 'turn the lights red.'),
                                          (True, 'Cube, volume up', 'volume up'),
                                          (True, 'Hey Cube.', ''),
                                          (False, 'Hey Cube, tell me about wakes', 'Hey Cube, tell me about wakes')]:
            with self.subTest(prefix=prefix, text=original):
                headers = [(b'x-cube-wake-preroll', b'1')] if prefix else []
                with patch.object(self.server, 'transcribe_audio', return_value={'text': original, 'processing_time': .1}), \
                     patch.object(self.server, 'process_turn', new_callable=AsyncMock, return_value={}) as process:
                    await self.server.voice(request(asyncio.Event(), headers=headers), UploadFile(io.BytesIO(b'audio')))
                self.assertEqual(process.call_args.args[0]['text'], expected)
