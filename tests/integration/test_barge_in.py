"""Real TCP disconnect propagation on ephemeral loopback ports, fake inference."""
import asyncio
from contextlib import asynccontextmanager
import json
import socket
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import httpx
from fastapi import FastAPI, Request, Response
import uvicorn

from ai_node.app import create_app
from ai_node.config import STTConfig, LLMConfig
from core.ai_client import AIClient
from core.config import AINodeConfig
from tests.server.core.test_barge_in import load_backend


@asynccontextmanager
async def serve(app):
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port,
                            lifespan='off', log_level='critical', access_log=False))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(3):
            while not server.started:
                if task.done():
                    task.result()
                    raise RuntimeError('Test server did not start')
                await asyncio.sleep(.005)
        yield f'http://127.0.0.1:{port}'
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5)
        sock.close()


class DisconnectChainTests(unittest.IsolatedAsyncioTestCase):
    async def test_pi_disconnect_reaches_backend_node_and_llm_socket(self):
        llm = FastAPI()
        entered, disconnected = asyncio.Event(), asyncio.Event()
        received = []
        @llm.post('/v1/chat/completions')
        async def chat(request: Request):
            received.append((dict(request.headers), await request.json()))
            if len(received) == 1:
                entered.set()
                while (await request.receive())['type'] != 'http.disconnect':
                    pass
                disconnected.set()
                return Response(status_code=499)
            return {'choices': [{'finish_reason': 'stop', 'message': {
                'role': 'assistant', 'content': json.dumps({'type': 'conversation', 'response': 'New answer.'})}}]}

        backend = load_backend()
        backend.media_tools.context = Mock(return_value=None)
        self.addAsyncCleanup(backend.remote_tts_client.aclose)
        self.addCleanup(backend.plug_commands.close)
        self.addCleanup(backend.control_channel.close)
        async with serve(llm) as llm_url:
            node = create_app(STTConfig(device='cpu'),
                model_factory=lambda *args, **kwargs: SimpleNamespace(transcribe=lambda *args, **kwargs: (
                    iter([SimpleNamespace(text='Tell me a joke')]), SimpleNamespace(language='en'))),
                llm_config=LLMConfig(llm_url + '/v1', 'unchanged-test-model', timeout=10))
            node.state.stt.load()
            async with serve(node) as node_url:
                backend.ai_client = AIClient(AINodeConfig(node_url, transcription_timeout=10, process_timeout=10))
                async with serve(backend.app) as backend_url:
                    async with httpx.AsyncClient(timeout=5) as client:
                        old = asyncio.create_task(client.post(backend_url + '/voice',
                            files={'file': ('command.wav', b'fake')}, headers={
                                'X-Cube-Client-ID': 'test-pi', 'X-Cube-Interaction-ID': 'f'*32}))
                        await asyncio.wait_for(entered.wait(), 3)
                        old.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await old
                        await asyncio.wait_for(disconnected.wait(), 3)
                        # A newer turn remains usable and receives no old text.
                        response = await client.post(backend_url + '/voice',
                            files={'file': ('command.wav', b'fake')}, headers={
                                'X-Cube-Client-ID': 'test-pi', 'X-Cube-Interaction-ID': 'a'*32})
                        self.assertEqual(response.status_code, 200, response.text)
                        self.assertEqual(response.json()['response'], 'New answer.')
        self.assertEqual(len(received), 2)
        self.assertEqual(received[0][0]['x-cube-interaction-id'], 'f'*32)
        self.assertEqual(received[1][0]['x-cube-interaction-id'], 'a'*32)
        self.assertEqual(received[0][1]['model'], 'unchanged-test-model')
        self.assertFalse(received[0][1]['stream'])
        self.assertEqual(len(received[1][1]['messages']), 2)  # system + new user; no cancelled history
