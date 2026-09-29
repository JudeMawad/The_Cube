"""Active async speech, fallback, notification and playback receipt regressions."""
import asyncio
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

import httpx

from audio.async_speech import Speech
from audio.playback import PlaybackOutcome, PlaybackReceipt
from interaction import Interaction
from tests.client.runtime_helpers import runtime_for, complete_reply


class SpeechTests(IsolatedAsyncioTestCase):
    async def test_kokoro_and_piper_publish_speech_only_during_playback(self):
        for status in (200, 503):
            states, calls = [], []
            speech = Speech(lambda owner, state: states.append(state),
                            transport=httpx.MockTransport(lambda _: httpx.Response(status, content=b'wav')))
            async def process(owner, args, **kwargs):
                calls.append(args)
                if args[0] == '/usr/bin/pw-play':
                    self.assertEqual(states[-1], 'speech 0.35')
                else:
                    self.assertEqual(states, [])
            with patch('audio.async_speech.process', side_effect=process):
                self.assertTrue(await speech.say(Interaction(1), 'Hello'))
            self.assertEqual(len(calls), 1 if status == 200 else 2)
            self.assertEqual(states, ['speech 0.35', 'thinking'])

    async def test_failed_remote_playback_cannot_upgrade_power_receipt(self):
        receipt = PlaybackReceipt('session')
        release = Mock()
        receipt.arm('shutdown', release)
        result = {'success': True, 'action': 'cube_shutdown', 'response': 'Shutting down.',
                  '_cube_playback': receipt}
        runtime = runtime_for(result)
        runtime.speech.transport = httpx.MockTransport(lambda _: httpx.Response(200, content=b'wav'))
        with patch('audio.async_speech.process', side_effect=[OSError('player failed'), None, None]) as process:
            played, _, _ = await complete_reply(result, runtime=runtime)
        self.assertTrue(played)  # Piper succeeded, but the failed first player is terminal.
        self.assertEqual(process.await_count, 3)
        self.assertIs(receipt.outcome, PlaybackOutcome.FAILED)
        release.assert_not_called()

    async def test_synthesis_fallback_before_playback_can_release_power(self):
        receipt = PlaybackReceipt('session')
        release = Mock()
        receipt.arm('shutdown', release)
        result = {'success': True, 'action': 'cube_shutdown', 'response': 'Shutting down.',
                  '_cube_playback': receipt}
        runtime = runtime_for(result)
        runtime.speech.transport = httpx.MockTransport(lambda _: httpx.Response(503))
        with patch('audio.async_speech.process', new_callable=AsyncMock):
            played, _, _ = await complete_reply(result, runtime=runtime)
        self.assertTrue(played)
        self.assertIs(receipt.outcome, PlaybackOutcome.COMPLETED)
        release.assert_called_once()

    async def test_cancelled_player_never_starts_fallback(self):
        speech = Speech(Mock(), transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b'wav')))
        owner = Interaction(1)
        owner.receipt = PlaybackReceipt('session')
        async def interrupted(*args, **kwargs):
            owner.cancel()
            raise asyncio.CancelledError()
        with patch('audio.async_speech.process', side_effect=interrupted) as process:
            with self.assertRaises(asyncio.CancelledError):
                await speech.say(owner, 'Hello')
        process.assert_awaited_once()
        self.assertIs(owner.receipt.outcome, PlaybackOutcome.CANCELLED)

    async def test_failed_synthesis_or_playback_and_empty_text(self):
        for status, text, expected_calls in ((503, 'Hello', 1), (200, 'Hello', 2), (200, ' ', 0)):
            states = []
            speech = Speech(lambda owner, state: states.append(state),
                            transport=httpx.MockTransport(lambda _: httpx.Response(status, content=b'wav')))
            with patch('audio.async_speech.process', side_effect=OSError('unavailable')) as process:
                self.assertFalse(await speech.say(Interaction(1), text))
            self.assertEqual(process.await_count, expected_calls)
            self.assertTrue(not states or states[-1] == 'thinking')


class RuntimeSpeechTests(IsolatedAsyncioTestCase):
    async def test_volume_refresh_precedes_reply_and_followup(self):
        result = {'success': True, 'action': 'cube_set_volume', 'response': 'Volume 50 percent.',
                  'listen_for_seconds': 10}
        runtime = runtime_for(result)
        order = []
        runtime.controls.refresh_volume_display.side_effect = lambda: order.append('refresh')
        def say(text):
            order.append('playback_complete')
            return True
        _, _, coordinator = await complete_reply(result, say, runtime=runtime)
        self.assertEqual(order, ['refresh', 'playback_complete'])
        self.assertEqual(coordinator.state, 'followup')

    async def test_mute_never_speaks_or_reopens_followup(self):
        say = Mock(return_value=True)
        _, _, coordinator = await complete_reply({'success': True, 'action': 'cube_mute',
                                                  'response': 'Muted.', 'listen_for_seconds': 10}, say)
        say.assert_not_called()
        self.assertEqual(coordinator.state, 'idle')

    async def test_notification_revalidates_before_playback_and_acknowledges_after(self):
        runtime = runtime_for({})
        notifications = Mock()
        notifications.validate_async = AsyncMock(return_value=True)
        owner, item = Interaction(1), Mock(payload={'response': 'Ready to watch.'})
        order = []
        async def say(owner, text, guard):
            await guard()
            order.append('played_and_reaped')
            return True
        runtime.speech.say = AsyncMock(side_effect=say)
        notifications.complete.side_effect = lambda item, played: order.append(('ack', played))
        await runtime._notification(owner, notifications, item)
        self.assertEqual(notifications.validate_async.await_count, 2)
        self.assertEqual(order, ['played_and_reaped', ('ack', True)])

    async def test_obsolete_notification_never_plays(self):
        runtime = runtime_for({})
        notifications = Mock()
        notifications.validate_async = AsyncMock(return_value=False)
        runtime.speech.say = AsyncMock()
        item = Mock(payload={'response': 'Old event.'})
        await runtime._notification(Interaction(1), notifications, item)
        runtime.speech.say.assert_not_called()
        notifications.complete.assert_called_once_with(item, False)

    async def test_notification_revalidation_failure_prevents_playback(self):
        runtime = runtime_for({})
        notifications = Mock()
        notifications.validate_async = AsyncMock(side_effect=[True, False])
        runtime.speech.transport = httpx.MockTransport(lambda _: httpx.Response(200, content=b'wav'))
        item = Mock(payload={'response': 'Old event.'})
        with patch('audio.async_speech.process', new_callable=AsyncMock) as process:
            await runtime._notification(Interaction(1), notifications, item)
        process.assert_not_called()
        notifications.complete.assert_called_once_with(item, False)

    async def test_notification_playback_failure_is_not_acknowledged_as_played(self):
        runtime = runtime_for({})
        notifications = Mock()
        notifications.validate_async = AsyncMock(return_value=True)
        runtime.speech.say = AsyncMock(return_value=False)
        item = Mock(payload={'response': 'Ready.'})
        await runtime._notification(Interaction(1), notifications, item)
        notifications.complete.assert_called_once_with(item, False)

    async def test_speaker_transcript_uses_local_adapter(self):
        for text, action in [('Connect to speaker.', 'speaker_connect'),
                             ('Disconnect from speaker.', 'speaker_disconnect')]:
            runtime = runtime_for({'transcript': text, 'response': None})
            runtime.speech.say = AsyncMock(return_value=True)
            with patch('voice_runtime.handle_speaker_command', return_value={
                    'success': True, 'action': action, 'response': 'Done.'}) as adapter:
                await runtime._voice(Interaction(1), b'audio', {})
            adapter.assert_called_once_with(text)
            self.assertEqual(runtime.speech.say.await_args.args[1], 'Done.')

    async def test_voice_transport_failure_speaks_recovery_and_returns_idle(self):
        runtime = runtime_for({})
        runtime.transport = httpx.MockTransport(lambda _: httpx.Response(503))
        said = []
        _, _, coordinator = await complete_reply({}, lambda text: said.append(text) or True, runtime=runtime)
        self.assertIn("can't reach the server", said[0])
        self.assertEqual(coordinator.state, 'idle')
