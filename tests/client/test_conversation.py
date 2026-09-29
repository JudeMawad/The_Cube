"""Follow-up timing belongs to the frame coordinator, after async playback."""
import importlib
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from interaction import Coordinator, Recorder
from tests.client.runtime_helpers import complete_reply


class ConversationTests(unittest.IsolatedAsyncioTestCase):
    async def test_next_listening_window_starts_after_playback(self):
        _, _, coordinator = await complete_reply({'response': 'Which movie?', 'listen_for_seconds': 10},
                                                 lambda text: True)
        self.assertEqual(coordinator.state, 'followup')
        self.assertIsNotNone(coordinator.recorder)
        self.assertAlmostEqual(coordinator.recorder.deadline - coordinator.clock(), 10, delta=.1)

    async def test_success_without_followup_returns_idle(self):
        _, _, coordinator = await complete_reply({'response': 'Cancelled.', 'listen_for_seconds': 0},
                                                 lambda text: True)
        self.assertEqual(coordinator.state, 'idle')
        self.assertIsNone(coordinator.recorder)

    def test_silence_ends_one_followup_window_without_submission(self):
        now = [0.0]
        submit = Mock()
        coordinator = Coordinator(submit, Mock(), clock=lambda: now[0])
        coordinator.begin(.004, waiting_seconds=10)
        now[0] = 10.1
        coordinator.frame(b'\0\0', 0, False, .004)
        self.assertEqual(coordinator.state, 'idle')
        submit.assert_not_called()

    def test_speech_started_before_deadline_can_finish(self):
        recorder = Recorder(.004, 0, 1)
        recorder.feed(b'command', .1, .99)
        self.assertFalse(recorder.done)
        recorder.feed(b'more', .1, 1.5)
        self.assertEqual(recorder.feed(b'end', 0, 2.3), b'commandmoreend')

    def test_wire_listening_contract_and_compatibility(self):
        with patch.dict(sys.modules, {'openwakeword.model': SimpleNamespace(Model=Mock())}):
            cube = importlib.import_module('cube')
        for reply, seconds in (({'listen_for_seconds': 20}, 10), ({'listen_for_seconds': -1}, 0),
                               ({'listen_for_seconds': 0, 'follow_up': True}, 0),
                               ({'follow_up': True, 'expires_in': 120}, 10), ({}, 0)):
            self.assertEqual(cube.listening_seconds(reply), seconds)
