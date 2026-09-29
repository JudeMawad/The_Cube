"""Outbound telemetry must not mutate local playback, volume or gain."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from audio.soloist import PlaybackState
from audio.spotify_reporter import ReceiverReporter, receiver_packet, configured_reporter


class ReporterTests(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.token = Path(temporary.name) / "events.token"
        self.token.write_text("a" * 32)
        self.state = PlaybackState(connected=True, logged_in=True, active=True, status="playing", spotify_volume=10)
        self.reporter = ReceiverReporter("http://backend:8765", "cube", read=lambda: self.state, token_path=self.token)
        self.http = Mock()
        self.http.post.side_effect = [Mock(status_code=200, json=lambda: {"registered": True}),
                                     Mock(status_code=200, json=lambda: {"accepted": True})]

    def test_registration_exact_contract_no_audio_commands(self):
        self.reporter.send(self.http)
        calls = self.http.post.call_args_list
        self.assertEqual([c.args[0] for c in calls], ["http://backend:8765/music/receiver/session", "http://backend:8765/music/receiver/state"])
        packet = calls[1].kwargs["json"]
        self.assertEqual(set(packet), {"version", "session_id", "sequence", "connected", "logged_in", "active", "status", "spotify_volume"})
        self.assertEqual(packet["session_id"], calls[0].kwargs["json"]["session_id"])
        self.assertEqual(packet["spotify_volume"], 10)
        self.assertFalse(calls[1].kwargs["allow_redirects"])
        self.assertEqual(self.http.method_calls[0][0], "post")
        self.assertEqual(len(self.http.method_calls), 2)

    def test_no_backend_configuration_is_noop(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(configured_reporter())

    def test_backend_restart_requires_fresh_session(self):
        self.reporter.send(self.http)
        previous = self.reporter.session
        self.http.post.side_effect = [Mock(status_code=409)]
        with self.assertRaises(ValueError):
            self.reporter.send(self.http)
        self.assertIsNone(self.reporter.session)
        self.http.post.side_effect = [Mock(status_code=200, json=lambda: {"registered": True}),
                                     Mock(status_code=200, json=lambda: {"accepted": True})]
        self.reporter.send(self.http)
        self.assertNotEqual(previous, self.reporter.session)

    def test_paused_and_phone_volume_unchanged_by_telemetry(self):
        state = PlaybackState(connected=True, logged_in=True, active=True, status="paused", spotify_volume=37)
        packet = receiver_packet(state, "a" * 32, 4)
        self.assertEqual(packet["status"], "paused")
        self.assertEqual(packet["spotify_volume"], 37)
        self.assertEqual(state.status, "paused")

    def test_invalid_url_and_token_fail_without_request(self):
        for url in ("file:///secret", "http://user:secret@backend", "http://backend/path", "http://backend/?token=secret"):
            with self.assertRaises(ValueError):
                ReceiverReporter(url, "cube")
        self.token.write_text("short")
        with self.assertRaises(ValueError):
            self.reporter.send(self.http)
        self.http.post.assert_not_called()
