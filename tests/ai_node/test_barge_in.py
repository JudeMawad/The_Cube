"""AI-node cancellation with native models and transports stubbed."""
import asyncio
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import httpx
import numpy as np

from ai_node.app import create_app
from ai_node.config import STTConfig, LLMConfig
from ai_node import cancellation as cancel


class NodeCancellationTests(unittest.IsolatedAsyncioTestCase):
    def make_app(self, **kwargs):
        app = create_app(STTConfig(device='cpu'), **kwargs)
        # No lifespan: load only the explicitly supplied fake STT model.
        app.state.stt.load()
        return app

    async def test_cancel_llm_closes_transport_without_retry(self):
        entered, closed, calls = asyncio.Event(), asyncio.Event(), []
        async def llm(request):
            calls.append(request)
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()
        app = self.make_app(model_factory=Mock(), llm_config=LLMConfig('http://llm.invalid/v1', 'same-model'),
                            llm_transport=httpx.MockTransport(llm))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://ai') as client:
            task = asyncio.create_task(client.post('/process', json={'transcript': 'hello', 'client_id': 'pi'},
                                                   headers={'X-Cube-Interaction-ID': 'e'*32}))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(closed.is_set())
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].headers['X-Cube-Interaction-ID'], 'e'*32)
        import json
        payload = json.loads(calls[0].content)
        self.assertFalse(payload['stream'])
        self.assertEqual(payload['model'], 'same-model')

    async def test_native_stt_stops_after_running_segment(self):
        entered, release, segments = Event(), Event(), []
        def transcribe(*args, **kwargs):
            def generate():
                entered.set()
                release.wait(3)
                segments.append(1)
                yield SimpleNamespace(text='old')
                segments.append(2)
                yield SimpleNamespace(text='never')
            return generate(), SimpleNamespace(language='en')
        app = self.make_app(model_factory=lambda *args, **kw: SimpleNamespace(transcribe=transcribe))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://ai') as client:
            task = asyncio.create_task(client.post('/transcribe', files={'file': ('audio.wav', b'audio')}))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                task.cancel()
                await asyncio.sleep(.02)
                self.assertFalse(task.done())
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(segments, [1])

    async def test_native_tts_finishes_safely_but_does_not_encode_cancelled_audio(self):
        entered, release = Event(), Event()
        def synthesize(*args, **kwargs):
            entered.set()
            release.wait(3)
            return np.zeros(10), 24000
        app = self.make_app(model_factory=Mock())
        app.state.tts._kokoro = SimpleNamespace(create=synthesize)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://ai') as client:
            with patch('ai_node.tts.sf.write') as encode:
                task = asyncio.create_task(client.post('/tts', json={'text': 'old'}))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                    task.cancel()
                    await asyncio.sleep(.02)
                    self.assertFalse(task.done())
                finally:
                    release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                encode.assert_not_called()
