"""Run on the backend environment; no server.py inference/model startup."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
except ImportError:
    FastAPI = None

from features.music.controller import MusicController, ReceiverState
from integrations.spotify.config import Settings


@unittest.skipIf(FastAPI is None, "Backend FastAPI environment is not installed")
class MusicRouteTests(unittest.TestCase):
    def setUp(self):
        from features.music.routes import music_router
        from core.http_auth import authenticate
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        token = Path(temporary.name) / "events.token"
        token.write_text("a" * 32)
        auth = patch("features.music.routes.authenticate", side_effect=lambda request: authenticate(request, token))
        auth.start()
        self.addCleanup(auth.stop)
        self.api = Mock()
        self.api.playback.return_value = None
        self.api.devices.return_value = [{"name": "Cube", "id": "cube", "is_active": True, "is_restricted": False}]
        self.controller = MusicController(self.api)
        self.service = Mock()
        self.service.settings.return_value = Settings("a" * 32, "cube")
        self.service.get.return_value = self.controller
        self.service.receiver = ReceiverState()
        app = FastAPI()
        app.include_router(music_router(self.service))
        self.web = TestClient(app)
        self.addCleanup(self.web.close)
        self.headers = {"X-Cube-Token": "a" * 32, "X-Cube-Client-ID": "cube"}

    def test_authentication_and_identity_before_cloud(self):
        self.assertEqual(self.web.get("/music/state").status_code, 401)
        self.assertEqual(self.web.get("/music/state", headers={**self.headers, "X-Cube-Client-ID": "other"}).status_code, 403)
        self.service.get.assert_not_called()

    def test_authenticated_state_and_explicit_command(self):
        response = self.web.get("/music/state", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["receiver"]["stale"])
        response = self.web.post("/music/commands", headers=self.headers,
                                 json={"request_id": uuid4().hex, "operation": "pause"})
        self.assertEqual(response.json(), {"accepted": True})
        self.api.control.assert_called_once_with("pause", "cube", None)

    def test_strict_input_errors(self):
        for body in ({"request_id": uuid4().hex, "operation": "duck"},
                     {"request_id": uuid4().hex, "operation": "pause", "gain": .1}):
            self.assertEqual(self.web.post("/music/commands", headers=self.headers, json=body).status_code, 422)
        self.assertEqual(self.web.post("/music/receiver/state", headers=self.headers, json={}).status_code, 422)
        self.api.control.assert_not_called()

    def test_receiver_session_expiry(self):
        body = {"version": 1, "session_id": "a" * 32, "sequence": 1, "connected": False,
                "logged_in": False, "active": False, "status": "unknown", "spotify_volume": None}
        self.assertEqual(self.web.post("/music/receiver/state", headers=self.headers, json=body).status_code, 409)
        self.assertEqual(self.web.post("/music/receiver/session", headers=self.headers, json={"session_id": "a" * 32}).status_code, 200)
        self.assertEqual(self.web.post("/music/receiver/state", headers=self.headers, json=body).status_code, 200)
        self.assertEqual(self.web.post("/music/receiver/state", headers=self.headers, json=body).status_code, 409)
        self.api.playback.assert_not_called()

    def test_display_authentication_identity_and_binary_contract(self):
        for path in ("/music/display/state", "/music/display/artwork/" + "a" * 64):
            self.assertEqual(self.web.get(path).status_code, 401)
            self.assertEqual(self.web.get(path, headers={**self.headers, "X-Cube-Client-ID": "other"}).status_code, 403)
        self.service.get_display.assert_not_called()
        from features.music.display import MusicDisplay
        self.service.get_display.return_value = MusicDisplay(self.controller, Mock())
        response = self.web.get("/music/display/state", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["available"], False)
        self.assertEqual(response.headers["cache-control"], "no-store")
        display = self.service.get_display.return_value
        display.frames["a" * 64] = b"\x00" * 12288
        response = self.web.get("/music/display/artwork/" + "a" * 64, headers=self.headers)
        self.assertEqual(response.content, b"\x00" * 12288)
        self.assertEqual(response.headers["content-type"], "application/octet-stream")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.web.get("/music/display/artwork/invalid", headers=self.headers).status_code, 404)
