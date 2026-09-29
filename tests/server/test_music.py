"""MusicController and wire validation with fake cloud and monotonic time."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from threading import Event
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock
from uuid import uuid4

import httpx

from features.music.controller import MusicController, ReceiverState, snapshot
from features.music.protocol import command, receiver
from integrations.spotify.errors import SpotifyError
from integrations.spotify.api import SpotifyWebApi


class MusicTests(TestCase):
    def setUp(self):
        self.now = 100
        self.api = Mock()
        self.device = {"id": "cube-id", "name": "Cube", "is_active": True, "is_restricted": False, "supports_volume": True}
        self.api.devices.return_value = [self.device]
        self.api.playback.return_value = self.playback()
        self.controller = MusicController(self.api, clock=lambda: self.now)

    def playback(self, *, device="cube-id", playing=False):
        return {"device": {"id": device, "name": "Cube" if device == "cube-id" else "Phone", "volume_percent": 70},
                "is_playing": playing, "progress_ms": 123, "item": {"uri": "spotify:track:" + "a" * 22,
                "name": "Song", "artists": [{"name": "Artist"}], "duration_ms": 1000}}

    def execute(self, operation, **values):
        return self.controller.execute({"request_id": uuid4().hex, "operation": operation, **values})

    def test_resume_cube(self):
        self.execute("play_music")
        self.api.play.assert_called_once_with("cube-id")
        self.api.transfer.assert_not_called()

    def test_transfer_other_session_single_action(self):
        self.api.playback.return_value = self.playback(device="phone", playing=True)
        self.execute("play_music")
        self.api.transfer.assert_called_once_with("cube-id", play=True)
        self.api.play.assert_not_called()

    def test_already_playing_is_noop_and_no_random_fallback(self):
        self.api.playback.return_value = self.playback(playing=True)
        self.execute("play_music")
        self.api.play.assert_not_called()
        self.api.playback.return_value = None
        self.api.play.side_effect = SpotifyError("device_unavailable")
        with self.assertRaisesRegex(SpotifyError, "nothing_to_resume"):
            self.execute("play_music")
        self.api.search.assert_not_called()

    def test_validated_track_artist_playlist_and_controls(self):
        for kind in ("track", "artist", "playlist"):
            uri = "spotify:" + kind + ":" + "a" * 22
            self.execute("play_uri", uri=uri)
            self.api.play.assert_called_with("cube-id", uri)
        for op in ("pause", "next", "previous"):
            self.execute(op)
            self.api.control.assert_called_with(op, "cube-id", None)
        self.execute("volume", value=40)
        self.api.control.assert_called_with("volume", "cube-id", 40)

    def test_device_change_disappearance_restricted_duplicate(self):
        self.execute("pause")
        self.device["id"] = "new-id"
        self.execute("pause")
        self.api.control.assert_called_with("pause", "new-id", None)
        for devices, code in (([], "device_unavailable"), ([{**self.device, "id": None}], "device_unavailable"),
                ([self.device, self.device], "device_ambiguous"), ([{**self.device, "is_restricted": True}], "restricted"),
                ([{**self.device, "name": "Cube bedroom"}], "device_unavailable")):
            self.api.devices.return_value = devices
            with self.assertRaisesRegex(SpotifyError, code):
                self.execute("next")
        self.assertEqual(self.api.control.call_count, 2)

    def test_mutation_failure_no_retry_then_reresolve(self):
        self.api.control.side_effect = SpotifyError("device_unavailable")
        with self.assertRaises(SpotifyError):
            self.execute("next")
        self.api.control.assert_called_once()
        self.api.control.side_effect = None
        self.device["id"] = "changed"
        self.execute("next")
        self.api.control.assert_called_with("next", "changed", None)

    def test_command_idempotency_and_conflict(self):
        data = {"request_id": uuid4().hex, "operation": "next"}
        self.controller.execute(data)
        self.controller.execute(data)
        self.api.control.assert_called_once()
        with self.assertRaisesRegex(SpotifyError, "request_conflict"):
            self.controller.execute({**data, "operation": "previous"})
        self.api.control.side_effect = SpotifyError("network_unavailable")
        data["request_id"] = uuid4().hex
        for _ in range(2):
            with self.assertRaisesRegex(SpotifyError, "network_unavailable"):
                self.controller.execute(data)
        self.assertEqual(self.api.control.call_count, 2)

    def test_http_acknowledgments_preserve_receipts_without_state_verification(self):
        for operation in ("pause", "resume", "next", "previous", "play_music", "volume"):
            for status, body in ((204, b""), (200, b""), (202, b""), (200, b"{}"),
                                 (200, b"Mutation accepted by server"), (202, b"Acknowledged"),
                                 (403, b""), (500, b"")):
                with self.subTest(operation=operation, status=status, body=body):
                    calls = []
                    def handle(request):
                        calls.append((request.method, request.url.path))
                        if request.url.path.endswith("/devices"):
                            return httpx.Response(200, json={"devices": [self.device]})
                        if request.method == "GET":
                            return httpx.Response(200, json=self.playback())
                        return httpx.Response(status, content=body)
                    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
                        api = SpotifyWebApi(SimpleNamespace(access=lambda: "test-access"), http)
                        controller = MusicController(api)
                        data = {"request_id": uuid4().hex, "operation": operation}
                        if operation == "volume":
                            data["value"] = 40
                        error = {403: "restricted", 500: "service_unavailable"}.get(status)
                        for _ in range(2):
                            if error:
                                with self.assertRaisesRegex(SpotifyError, error):
                                    controller.execute(data)
                            else:
                                self.assertEqual(controller.execute(data), {"accepted": True})
                        self.assertEqual(controller.completed[data["request_id"]],
                                         (data, (error, None) if error else None))
                        self.assertEqual(sum(method != "GET" for method, _ in calls), 1)
                        expected_reads = [("GET", "/v1/me/player/devices")]
                        if operation in {"resume", "play_music"}:
                            expected_reads.append(("GET", "/v1/me/player"))
                        self.assertEqual(calls[:-1], expected_reads)
                        self.assertTrue(controller.state.stale)

    def test_command_serialization_without_sleeps(self):
        entered, release = Event(), Event()
        def control(*args):
            entered.set()
            self.assertTrue(release.wait(3))
        self.api.control.side_effect = control
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.execute, "next")
            self.assertTrue(entered.wait(3))
            second = pool.submit(self.execute, "previous")
            self.assertEqual(self.api.control.call_count, 1)
            release.set()
            first.result(3)
            second.result(3)
        self.assertEqual(self.api.control.call_count, 2)

    def test_cache_stale_backoff_no_automatic_playback(self):
        state = self.controller.read()
        with self.assertRaises(FrozenInstanceError):
            state.playing = True
        for _ in range(5):
            self.controller.read()
        self.api.playback.assert_called_once()
        self.now += 16
        self.api.playback.side_effect = SpotifyError("quota_exceeded", retry_after=3600)
        self.assertTrue(self.controller.read().stale)
        self.now += 30
        self.assertEqual(self.controller.read().error, "quota_exceeded")
        self.assertEqual(self.api.playback.call_count, 2)
        self.api.play.assert_not_called()
        self.api.control.assert_not_called()
        self.api.transfer.assert_not_called()

    def test_malformed_playback_is_error_not_voice_failure(self):
        for data in ([], {"device": {}, "is_playing": False}, {**self.playback(), "is_playing": 1},
                     {**self.playback(), "progress_ms": "10"}):
            with self.assertRaisesRegex(SpotifyError, "response_invalid"):
                snapshot(data, "Cube")

    def test_search_does_not_play(self):
        self.api.search.return_value = []
        self.assertEqual(self.controller.search("missing", "track"), [])
        self.api.play.assert_not_called()

    def test_strict_command_rejects_audio_and_extra_fields(self):
        for data in ({"operation": "pause"}, {"request_id": "a" * 32, "operation": "duck"},
                {"request_id": "a" * 32, "operation": "pause", "gain": .1},
                {"request_id": "a" * 32, "operation": "volume", "value": True},
                {"request_id": "a" * 32, "operation": "play_uri", "uri": "file:///tmp/audio"}):
            with self.assertRaisesRegex(SpotifyError, "invalid_request"):
                command(data)


class ReceiverTests(TestCase):
    def setUp(self):
        self.now = 10
        self.state = ReceiverState(clock=lambda: self.now)
        self.packet = {"version": 1, "session_id": "a" * 32, "sequence": 1, "connected": True,
                       "logged_in": True, "active": True, "status": "playing", "spotify_volume": 10.0}
        self.state.register(self.packet["session_id"])

    def test_receipt_expiry_and_no_clock_transfer(self):
        self.state.accept(self.packet)
        self.assertFalse(self.state.read()["stale"])
        self.now += 15
        self.assertTrue(self.state.read()["stale"])
        self.assertFalse(self.state.read()["connected"])

    def test_reconnect_and_stale_session_sequence(self):
        self.state.accept(self.packet)
        with self.assertRaisesRegex(SpotifyError, "receiver_session_expired"):
            self.state.accept(self.packet)
        self.state.register("b" * 32)
        with self.assertRaises(SpotifyError):
            self.state.accept({**self.packet, "sequence": 2})
        self.state.accept({**self.packet, "session_id": "b" * 32})
        self.assertFalse(self.state.read()["stale"])

    def test_reject_metadata_secrets_ducking_and_malformed(self):
        for updates in ({"track": "secret"}, {"access_token": "secret"}, {"gain": .1},
                        {"connected": 1}, {"spotify_volume": float("nan")}, {"version": True}, {"status": []}):
            with self.assertRaisesRegex(SpotifyError, "invalid_request"):
                receiver({**self.packet, **updates})


class MusicIsolationTests(TestCase):
    def test_missing_config_never_initializes_http(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        from unittest.mock import patch
        from features.music.service import MusicService
        with TemporaryDirectory() as directory, patch("features.music.service.httpx.Client") as http:
            service = MusicService(Path(directory) / "absent")
            http.assert_not_called()
            with self.assertRaisesRegex(SpotifyError, "not_linked"):
                service.get()
            service.close()
            http.assert_not_called()

    def test_logout_clears_cached_metadata_on_refresh(self):
        now = [0]
        api = Mock()
        api.playback.return_value = {"device": {"id": "cube", "name": "Cube"},
                                     "is_playing": True, "item": {"name": "Song"}}
        controller = MusicController(api, clock=lambda: now[0])
        self.assertEqual(controller.read().track_title, "Song")
        now[0] = 16
        api.playback.side_effect = SpotifyError("not_linked")
        self.assertFalse(controller.read().linked)
        self.assertIsNone(controller.read().track_title)
