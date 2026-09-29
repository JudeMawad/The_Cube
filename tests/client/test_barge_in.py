"""Deterministic frame/ownership tests: no microphone, speaker, or services."""
import unittest
from unittest.mock import Mock

from interaction import Coordinator, Interaction, Recorder, WakeLatch
from audio.playback import PlaybackReceipt, PlaybackOutcome


class InteractionTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.submit, self.publish_display_state = Mock(), Mock()
        self.loop = Coordinator(self.submit, self.publish_display_state, clock=lambda: self.now)

    def frame(self, active=False, level=0, data=b"s"):
        self.now += .08
        self.loop.frame(data, level, active, .004)

    def test_repeated_scores_do_not_interrupt_new_command(self):
        self.frame(True, .1)
        first = self.loop.current
        for _ in range(15):
            self.frame(True, .1)
        self.assertIs(self.loop.current, first)
        for _ in range(3):
            self.frame()
        self.frame(True, .1)
        self.assertTrue(first.cancelled.is_set())
        self.assertIsNot(self.loop.current, first)

    def test_detection_frame_and_early_command_are_preserved(self):
        for data in (b"wake-tail", b"turn", b"the"):
            self.frame(data=data)
        self.frame(True, .1, b"lights")
        self.frame(False, .1, b"red")
        for _ in range(11):
            self.frame()
        self.assertIn(b"turnthelightsred", self.submit.call_args.args[1])

    def test_barge_in_owns_all_states_and_stale_completion_is_ignored(self):
        for state in ("listening", "thinking", "speech 0.35", "followup"):
            old = self.loop.begin(.004, wake=True)
            old.receipt = PlaybackReceipt("session")
            self.loop.set_state(state)
            self.loop.latch = WakeLatch()
            self.frame(True)
            new = self.loop.current
            self.assertTrue(old.cancelled.is_set())
            self.assertIs(old.receipt.outcome, PlaybackOutcome.CANCELLED)
            self.loop.complete(old, 10, .004)
            self.assertIs(self.loop.current, new)
            self.assertEqual(self.loop.state, "listening")

    def test_no_speech_returns_idle_without_request(self):
        self.frame(True)
        for _ in range(65):
            self.frame()
        self.assertEqual(self.loop.state, "idle")
        self.submit.assert_not_called()

    def test_wake_energy_alone_does_not_end_listening_before_command(self):
        self.frame(True, .1, b"wake")
        for _ in range(30):
            self.frame()
        self.submit.assert_not_called()
        self.assertEqual(self.loop.state, "listening")
        self.frame(level=.1, data=b"command")
        for _ in range(11):
            self.frame()
        self.assertIn(b"command", self.submit.call_args.args[1])

    def test_old_cancel_cannot_touch_new_receipt(self):
        old, new = Interaction(1), Interaction(2)
        old.receipt, new.receipt = PlaybackReceipt("s"), PlaybackReceipt("s")
        old.cancel()
        self.assertIsNone(new.receipt.outcome)

    def test_speech_just_before_deadline_can_finish(self):
        recorder = Recorder(.004, 0, 1)
        recorder.feed(b"a", .1, .99)
        self.assertFalse(recorder.done)
        recorder.feed(b"b", .1, 1.5)
        self.assertEqual(recorder.feed(b"c", 0, 2.3), b"abc")


import asyncio
import sys
from unittest.mock import patch, AsyncMock
import httpx
from audio.async_speech import Speech, process
from audio.playback import PlaybackInterrupted


class AsyncSpeechTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_http_never_starts_piper_or_player(self):
        entered, closed = asyncio.Event(), asyncio.Event()
        async def handle(request):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()
        owner = Interaction(1)
        speaker = Speech(Mock(), transport=httpx.MockTransport(handle))
        with patch('audio.async_speech.process', new_callable=AsyncMock) as player:
            task = asyncio.create_task(speaker.say(owner, 'hello'))
            await entered.wait()
            owner.cancel()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            player.assert_not_called()
        self.assertTrue(closed.is_set())

    async def test_real_owned_child_is_reaped_on_cancel(self):
        # Harmless child, never an audio player or an installed service.
        import os
        owner = Interaction(1)
        real_spawn = asyncio.create_subprocess_exec
        children = []
        async def spawn(*args, **kwargs):
            child = await real_spawn(*args, **kwargs)
            children.append(child)
            return child
        with patch('audio.async_speech.asyncio.create_subprocess_exec', side_effect=spawn):
            task = asyncio.create_task(process(owner, [sys.executable, '-c', 'import time; time.sleep(60)']))
            while not children:
                await asyncio.sleep(.001)
            owner.cancel()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertIsNotNone(children[0].returncode)
        with self.assertRaises(ProcessLookupError):
            os.kill(children[0].pid, 0)

    async def test_cancellation_during_spawn_does_not_orphan_child(self):
        entered, release = asyncio.Event(), asyncio.Event()
        child = Mock(returncode=None)
        child.wait = AsyncMock()
        async def spawn(*args, **kwargs):
            entered.set()
            await release.wait()
            return child
        with patch('audio.async_speech.asyncio.create_subprocess_exec', side_effect=spawn):
            task = asyncio.create_task(process(Interaction(1), ['fake']))
            await entered.wait()
            task.cancel()
            await asyncio.sleep(0)
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        child.terminate.assert_called_once()
        child.wait.assert_awaited_once()

    async def test_ordinary_http_failure_keeps_piper_and_receipt(self):
        owner = Interaction(1)
        owner.receipt = PlaybackReceipt('session')
        speaker = Speech(Mock(), transport=httpx.MockTransport(lambda request: httpx.Response(503)))
        with patch('audio.async_speech.process', new_callable=AsyncMock) as player:
            self.assertTrue(await speaker.say(owner, 'hello'))
        self.assertEqual(player.await_count, 2)
        self.assertIsNone(owner.receipt.outcome)

    async def test_cancelled_waiter_never_starts_audio(self):
        owner = Interaction(1)
        speaker = Speech(Mock())
        await speaker.output_lock.acquire()
        with patch('audio.async_speech.process', new_callable=AsyncMock) as player:
            task = asyncio.create_task(speaker.play(owner, 'fake.wav'))
            await asyncio.sleep(0)
            owner.cancel()
            speaker.output_lock.release()
            with self.assertRaises(PlaybackInterrupted):
                await task
            player.assert_not_called()


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    def runtime(self, transport):
        from voice_runtime import VoiceRuntime
        from queue import SimpleQueue
        runtime = VoiceRuntime.__new__(VoiceRuntime)
        runtime.endpoint = 'http://backend/voice'
        runtime.client_id = 'pi'
        runtime.controls = Mock()
        runtime.make_wav = lambda data: data
        runtime.listening_seconds = lambda result: result.get('listen_for_seconds', 0)
        runtime.transport = transport
        runtime.events = SimpleQueue()
        runtime.speech = Speech(lambda owner, state: runtime.events.put((owner, 'state', state)),
                                transport=transport)
        return runtime

    async def test_late_http_completion_cannot_trigger_speaker_or_tts(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def remote(request):
            entered.set()
            await release.wait()
            return httpx.Response(200, json={'transcript': 'connect to the speaker', 'response': 'old'})
        runtime = self.runtime(httpx.MockTransport(remote))
        owner = Interaction(1)
        with patch('voice_runtime.handle_speaker_command') as speaker, \
             patch.object(runtime.speech, 'say', new_callable=AsyncMock) as say:
            task = asyncio.create_task(runtime._voice(owner, b'audio', {}))
            await entered.wait()
            owner.cancel()  # Deliberately leave HTTP alive to test stale delivery.
            release.set()
            await task
            speaker.assert_not_called()
            say.assert_not_called()
        self.assertTrue(runtime.events.empty())

    async def test_progress_cue_is_cancelled_before_reply_playback(self):
        runtime = self.runtime(httpx.MockTransport(lambda req: httpx.Response(200, json={
            'transcript': 'hello', 'response': 'new'})))
        owner = Interaction(1)
        entered, stopped = asyncio.Event(), asyncio.Event()
        async def cue(owner):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        async def say(owner, text, guard):
            await entered.wait()
            await guard()
            self.assertTrue(stopped.is_set())
            return True
        with patch.object(runtime, '_cue', side_effect=cue), \
             patch.object(runtime.speech, 'say', side_effect=say), \
             patch('voice_runtime.handle_speaker_command', return_value=None):
            await runtime._voice(owner, b'audio', {})
        self.assertEqual(runtime.events.get_nowait(), (owner, 'done', (True, 0)))

    async def test_notification_interruption_is_not_acknowledged_as_played(self):
        runtime = self.runtime(None)
        notifications = Mock()
        notifications.validate_async = AsyncMock(return_value=True)
        item = Mock(payload={'response': 'notification'})
        entered = asyncio.Event()
        async def say(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        owner = Interaction(1)
        with patch.object(runtime.speech, 'say', side_effect=say):
            task = asyncio.create_task(runtime._notification(owner, notifications, item))
            await entered.wait()
            owner.cancel()
            task.cancel()
            await task
        notifications.complete.assert_called_once_with(item, False)
        self.assertTrue(runtime.events.empty())

    async def test_old_led_and_completion_events_cannot_change_new_listening(self):
        runtime = self.runtime(None)
        coordinator = Coordinator(Mock(), Mock())
        old = coordinator.begin(.004, wake=True)
        new = coordinator.begin(.004, wake=True)
        runtime.events.put((old, 'state', 'idle'))
        runtime.events.put((old, 'done', (True, 10)))
        runtime.drain(coordinator, .004)
        self.assertIs(coordinator.current, new)
        self.assertEqual(coordinator.state, 'listening')

    async def test_power_release_is_scheduled_off_frame_loop(self):
        owner = Interaction(1)
        receipt = PlaybackReceipt('session')
        release, scheduled = Mock(), []
        receipt.arm('shutdown', release)
        receipt.authorize({'success': True, 'action': 'cube_shutdown'})
        receipt.finish(PlaybackOutcome.COMPLETED, release_scheduler=scheduled.append)
        release.assert_not_called()
        self.assertEqual(scheduled, [release])
        receipt.finish(PlaybackOutcome.CANCELLED)
        self.assertIs(receipt.outcome, PlaybackOutcome.COMPLETED)


class RuntimeThreadTests(unittest.TestCase):
    def test_repeated_interruption_closes_requests_and_runtime_thread(self):
        from threading import Event
        from voice_runtime import VoiceRuntime
        entered = Event()
        closed = []
        async def remote(request):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                closed.append(request.headers['X-Cube-Interaction-ID'])
        controls = Mock()
        controls.begin_request.return_value = (None, {'X-Cube-Client-ID': 'pi'})
        runtime = VoiceRuntime('http://test/voice', 'pi', controls, lambda audio: audio,
                               lambda result: 0, transport=httpx.MockTransport(remote))
        try:
            for generation in range(20):
                entered.clear()
                owner = Interaction(generation)
                runtime.submit(owner, b'audio')
                self.assertTrue(entered.wait(2))
                owner.cancel()
        finally:
            runtime.close()
        self.assertEqual(len(closed), 20)
        self.assertEqual(len(set(closed)), 20)
        self.assertFalse(runtime.thread.is_alive())
        self.assertFalse(runtime.tasks)
