"""Wire-only compatibility; production services never import each other."""
import json
import sys
from unittest import TestCase
from unittest.mock import Mock

from tests.paths import ROOT
sys.path.insert(0, str(ROOT / "client/app"))
from audio.soloist import PlaybackState
from audio.spotify_reporter import receiver_packet
from features.music.controller import ReceiverState
from features.music.protocol import receiver


class SpotifyContractTests(TestCase):
    def test_backend_display_projection_consumed_by_pi_without_shared_clock(self):
        from features.music.controller import MusicController
        from features.music.display import MusicDisplay
        from display.music import MusicProvider, parse_state
        api = Mock()
        api.playback.return_value = {"device": {"name": "Cube", "id": "cube"},
            "is_playing": True, "progress_ms": 1000,
            "item": {"uri": "spotify:track:" + "a" * 22, "duration_ms": 200000}}
        controller = MusicController(api, clock=lambda: 987654)
        backend = MusicDisplay(controller, Mock())
        value = json.loads(json.dumps(backend.state()))
        pi = MusicProvider("http://backend", "cube", clock=lambda: 101,
            read=lambda: PlaybackState(connected=True, logged_in=True, active=True, status="playing"),
            send=Mock())
        pi.snapshot = parse_state(value, 100, 101)
        pi.publish(None)
        packet = pi.send.call_args.args[1].decode().split()
        self.assertEqual(packet[4:6], ["115000", "1"])
        self.assertEqual(packet[-2:], ["200000", "1000"])
        self.assertNotIn("987654", packet)
        self.assertNotIn("artwork_url", controller.wire_state())
        self.assertEqual(controller.completed, {})
        api.control.assert_not_called()
        api.play.assert_not_called()

    def test_real_pi_packet_accepted_by_backend_without_clock_or_audio_control(self):
        packet = json.loads(json.dumps(receiver_packet(PlaybackState(
            connected=True, logged_in=True, active=True, status="paused", spotify_volume=40,
            observed=999999, updated=999999), "a" * 32, 1)))
        self.assertEqual(receiver(packet), packet)
        backend = ReceiverState(clock=lambda: 10)
        backend.register(packet["session_id"])
        backend.accept(packet)
        self.assertEqual(backend.read(), {"stale": False, "connected": True, "logged_in": True,
                                         "active": True, "status": "paused", "spotify_volume": 40})
