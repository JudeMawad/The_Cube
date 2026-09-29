"""Offline fixtures exercising the production async voice/completion path."""
import asyncio
from queue import SimpleQueue
from unittest.mock import AsyncMock, Mock, patch

import httpx

from audio.async_speech import Speech
from audio.playback import PlaybackInterrupted
from interaction import Coordinator, Interaction
from voice_runtime import VoiceRuntime


def runtime_for(result, *, controls=None):
    runtime = VoiceRuntime.__new__(VoiceRuntime)
    runtime.endpoint, runtime.client_id = 'http://backend.invalid/voice', 'pi'
    runtime.controls = controls if controls is not None else Mock()
    if controls is None:
        runtime.controls.take_power_failure.return_value = False
    runtime.make_wav = lambda audio: audio
    runtime.listening_seconds = lambda reply: reply.get('listen_for_seconds', 0)
    wire = {key: value for key, value in result.items() if key != '_cube_playback'}
    runtime.transport = httpx.MockTransport(lambda request: httpx.Response(200, json=wire))
    runtime.events = SimpleQueue()
    runtime.speech = Speech(lambda owner, state: runtime.events.put((owner, 'state', state)))
    return runtime


async def complete_reply(result, say=None, *, controls=None, runtime=None):
    runtime = runtime or runtime_for(result, controls=controls)
    owner = Interaction(1)
    owner.receipt = result.get('_cube_playback')
    coordinator = Coordinator(Mock(), Mock())
    coordinator.current = owner
    coordinator.generation = 1
    coordinator.set_state('thinking')
    if say is not None:
        async def speak(owner, text, guard=None):
            if guard:
                await guard()
            try:
                return say(text)
            except PlaybackInterrupted:
                owner.cancel()
                raise
        runtime.speech.say = AsyncMock(side_effect=speak)
    pending = []
    runtime._schedule = lambda owner, coroutine: pending.append(asyncio.create_task(coroutine))
    # Only the hardware speaker adapter is mocked; HTTP, ownership, reply
    # authorization, completion, and power-release scheduling use real code.
    with patch('voice_runtime.handle_speaker_command', return_value=None):
        await runtime._voice(owner, b'audio', {})
        events = []
        while not runtime.events.empty():
            events.append(runtime.events.get_nowait())
        played = next((value[0] for sender, kind, value in events if kind == 'done'), False)
        for event in events:
            runtime.events.put(event)
        runtime.drain(coordinator, .004)
        while pending:
            tasks, pending[:] = list(pending), []
            await asyncio.gather(*tasks)
            runtime.drain(coordinator, .004)
    return played, runtime, coordinator


def run_reply(result, say):
    return asyncio.run(complete_reply(result, say))[0]
