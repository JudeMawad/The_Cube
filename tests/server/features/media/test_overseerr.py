"""Offline tests: all HTTP, Whisper and physical light calls are mocked."""
import importlib
import io
import json
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from fastapi.testclient import TestClient

from features.media.movie_commands import MovieCommands, YES, NO
from integrations.overseerr_client import (
    CONFIG_PATH, Movie, OverseerrClient, OverseerrError, RequestUncertain,
    Settings, parse_movie,
)

MOVIE = Movie(157336, "Interstellar", "2014")
API_MOVIE = {"id": 157336, "title": "Interstellar", "releaseDate": "2014-11-05",
             "mediaType": "movie"}


class DialogueTests(unittest.TestCase):
    def setUp(self):
        self.api = Mock(spec=OverseerrClient)
        self.api.search_movies.return_value = [MOVIE]
        self.api.get_movie.return_value = MOVIE
        self.now = 100.0
        self.flow = MovieCommands(self.api, clock=lambda: self.now)

    def search(self):
        return self.flow.handle("download Interstella")

    def test_fuzzy_match_stores_confirmation(self):
        result = self.search()
        self.assertEqual(result["response"], "Did you mean Interstellar from 2014?")
        self.assertTrue(result["follow_up"])
        self.assertEqual(result["expires_in"], 120)
        self.api.request_movie.assert_not_called()
        self.api.get_movie.assert_not_called()

    def test_each_confirmation_submits_stored_id_only_once(self):
        for phrase in YES | {"Yes!", "That’s the one."}:
            with self.subTest(phrase=phrase):
                self.api.reset_mock()
                self.flow = MovieCommands(self.api, clock=lambda: self.now)
                self.search()
                self.assertEqual(self.flow.handle(phrase)["action"], "movie_requested")
                self.api.get_movie.assert_called_once_with(157336)
                self.api.request_movie.assert_called_once_with(157336)
                self.assertEqual(self.flow.handle("yes")["action"], "movie_no_pending")
                self.api.request_movie.assert_called_once()

    def test_each_cancellation_clears_state(self):
        for phrase in NO:
            with self.subTest(phrase=phrase):
                self.search()
                self.assertEqual(self.flow.handle(phrase)["action"], "movie_cancelled")
                self.flow.handle("yes")
        self.api.request_movie.assert_not_called()

    def test_confirmation_without_pending_never_uses_network(self):
        for phrase in YES:
            self.assertEqual(self.flow.handle(phrase)["action"], "movie_no_pending")
        self.assertEqual(self.api.mock_calls, [])

    def test_expiration_boundary(self):
        self.search()
        self.now += 120
        self.assertIn("expired", self.flow.handle("yes")["response"])
        self.api.request_movie.assert_not_called()

    def test_before_expiry(self):
        self.search()
        self.now += 119
        self.assertEqual(self.flow.handle("okay")["action"], "movie_requested")

    def test_clients_are_isolated(self):
        self.flow.handle("download Interstella", "cube-a")
        self.assertEqual(self.flow.handle("yes", "cube-b")["action"], "movie_no_pending")
        self.assertEqual(self.flow.handle("yes", "cube-a")["action"], "movie_requested")

    def test_concurrent_yes_submits_once(self):
        self.search()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(self.flow.handle, ["yes", "yes"]))
        self.assertEqual(sum(r["action"] == "movie_requested" for r in results), 1)
        self.api.request_movie.assert_called_once_with(157336)

    def test_restart_loses_pending(self):
        self.search()
        fresh = MovieCommands(self.api)
        self.assertEqual(fresh.handle("yes")["action"], "movie_no_pending")
        self.api.request_movie.assert_not_called()

    def test_intervening_command_preserves_pending(self):
        self.search()
        self.assertIsNone(self.flow.handle("lights on"))
        self.flow.handle("yes")
        self.api.request_movie.assert_called_once_with(MOVIE.media_id)

    def test_confirmation_must_be_whole_utterance(self):
        for phrase in ["yes but not now", "do it tomorrow", "don't download it", "okay no"]:
            self.search()
            self.assertTrue(self.flow.handle(phrase)["follow_up"])
        self.api.request_movie.assert_not_called()

    def test_best_match_beats_api_order(self):
        self.api.search_movies.return_value = [Movie(1, "Something Else", "2020"), MOVIE]
        self.assertEqual(self.flow.handle("download Interstellar")["media_id"], 157336)

    def test_ambiguous_accepts_new_command_with_year(self):
        self.api.search_movies.return_value = [
            Movie(1, "Dune", "1984"), Movie(2, "Dune", "2021")]
        result = self.flow.handle("download Dune")
        self.assertEqual(result["action"], "movie_ambiguous")
        self.assertIn("1984", result["response"])
        self.flow.handle("yes")
        self.api.request_movie.assert_not_called()
        result = self.flow.handle("download Dune from 2021")
        self.assertEqual(result["media_id"], 2)
        self.api.search_movies.assert_called_with("dune")

    def test_numeric_title_is_not_year(self):
        self.api.search_movies.return_value = [Movie(1, "1917", "2019")]
        self.flow.handle("download 1917")
        self.api.search_movies.assert_called_once_with("1917")

    def test_no_results_replaces_old_pending(self):
        self.search()
        self.api.search_movies.return_value = []
        self.assertEqual(self.flow.handle("download missing")["action"], "movie_not_found")
        self.flow.handle("yes")
        self.api.request_movie.assert_not_called()

    def test_bad_match_waits_for_a_better_title(self):
        self.api.search_movies.return_value = [Movie(1, "Dune", "2021")]
        self.assertEqual(self.search()["action"], "movie_not_found")
        self.assertEqual(self.flow.pending["cube"].state, "title")

    def test_empty_title(self):
        self.assertEqual(self.flow.handle("download")["action"], "movie_title_needed")
        self.api.search_movies.assert_not_called()

    def test_available_requested_and_failed_do_not_store(self):
        for state, action in [("available", "movie_available"),
                              ("requested", "movie_already_requested"), ("failed", "movie_error")]:
            self.api.search_movies.return_value = [replace(MOVIE, state=state)]
            self.assertEqual(self.flow.handle("download Interstellar")["action"], action)
            self.assertFalse(self.flow.pending)
        self.api.request_movie.assert_not_called()

    def test_status_rechecked_at_confirmation(self):
        for state in ["available", "requested", "failed"]:
            self.search()
            self.api.get_movie.return_value = replace(MOVIE, state=state)
            self.flow.handle("yes")
        self.api.request_movie.assert_not_called()

    def test_failed_search_clears_old_pending(self):
        self.search()
        self.api.search_movies.side_effect = OverseerrError("Overseerr unavailable.")
        self.assertFalse(self.search()["success"])
        self.flow.handle("yes")
        self.api.request_movie.assert_not_called()

    def test_unexpected_errors_do_not_expose_secrets(self):
        self.api.search_movies.side_effect = RuntimeError("PRIVATE_KEY_SENTINEL")
        output = io.StringIO()
        with redirect_stdout(output):
            result = self.search()
        self.assertFalse(result["success"])
        self.assertNotIn("PRIVATE_KEY_SENTINEL", str(result) + output.getvalue())

    def test_failed_confirmation_consumes_pending(self):
        self.search()
        self.api.get_movie.side_effect = OverseerrError("Overseerr unavailable.")
        self.assertFalse(self.flow.handle("yes")["success"])
        self.assertEqual(self.flow.handle("yes")["action"], "movie_no_pending")
        self.api.request_movie.assert_not_called()

    def test_uncertain_post_rechecks_without_retrying(self):
        self.search()
        self.api.request_movie.side_effect = RequestUncertain("Check Overseerr.")
        self.api.get_movie.side_effect = [MOVIE, replace(MOVIE, state="requested")]
        self.assertEqual(self.flow.handle("yes")["action"], "movie_already_requested")
        self.api.request_movie.assert_called_once_with(157336)

    def test_unresolved_post_is_not_retried(self):
        self.search()
        self.api.request_movie.side_effect = RequestUncertain("Check Overseerr.")
        self.assertFalse(self.flow.handle("yes")["success"])
        self.flow.handle("yes")
        self.api.request_movie.assert_called_once()


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.settings = patch("integrations.overseerr_client.Settings.load",
                              return_value=Settings("http://overseerr.invalid", "TEST_ONLY"))
        self.settings.start()
        self.addCleanup(self.settings.stop)
        self.transport = patch("integrations.overseerr_client.httpx.Client")
        self.factory = self.transport.start()
        self.addCleanup(self.transport.stop)
        self.http = self.factory.return_value
        self.api = OverseerrClient()

    def respond(self, code=200, data=None):
        self.http.request.return_value = httpx.Response(code, json=data)

    def test_search_request_filters_tv_and_people(self):
        self.respond(data={"results": [API_MOVIE, {"id": 2, "mediaType": "tv"},
                                      {"id": 3, "mediaType": "person"}]})
        self.assertEqual(self.api.search_movies("Interstellar"), [MOVIE])
        args, kwargs = self.http.request.call_args
        request = httpx.Request(*args, **kwargs)
        self.assertEqual(request.method, "GET")
        self.assertEqual(str(request.url.copy_with(query=None)),
                         "http://overseerr.invalid/api/v1/search")
        self.assertEqual(kwargs["headers"]["X-Api-Key"], "TEST_ONLY")
        self.assertEqual(dict(request.url.params), {"query": "Interstellar", "page": "1"})
        self.factory.assert_called_once_with(timeout=10, follow_redirects=False, trust_env=False)

    def test_post_payload_uses_tmdb_id(self):
        self.respond(201, {"id": 17, "status": 1})
        self.api.request_movie(157336)
        args, kwargs = self.http.request.call_args
        self.assertEqual(args[0], "POST")
        self.assertEqual(kwargs["json"], {"mediaType": "movie", "mediaId": 157336})

    def test_api_suffix_not_duplicated(self):
        Settings.load.return_value = Settings("http://overseerr.invalid/api/v1", "TEST_ONLY")
        self.respond(data=API_MOVIE)
        self.api.get_movie(157336)
        self.assertEqual(self.http.request.call_args.args[1],
                         "http://overseerr.invalid/api/v1/movie/157336")

    def test_wrong_detail_id_rejected(self):
        self.respond(data={**API_MOVIE, "id": 9})
        with self.assertRaises(OverseerrError):
            self.api.get_movie(157336)

    def test_http_errors_do_not_expose_body(self):
        for code in [401, 403, 404, 429, 500, 503, 302]:
            with self.subTest(code=code):
                self.respond(code, {"message": "PRIVATE_KEY_SENTINEL"})
                with self.assertRaises(OverseerrError) as caught:
                    self.api.search_movies("Interstellar")
                self.assertNotIn("PRIVATE_KEY_SENTINEL", str(caught.exception))

    def test_timeout_sanitized(self):
        self.http.request.side_effect = httpx.ReadTimeout("PRIVATE_KEY_SENTINEL")
        with self.assertRaises(OverseerrError) as caught:
            self.api.search_movies("Interstellar")
        self.assertNotIn("PRIVATE_KEY_SENTINEL", str(caught.exception))

    def test_post_timeout_and_conflict_are_uncertain(self):
        self.http.request.side_effect = httpx.ReadTimeout("private")
        with self.assertRaises(RequestUncertain):
            self.api.request_movie(157336)
        self.http.request.side_effect = None
        self.respond(409, {"message": "already requested"})
        with self.assertRaises(RequestUncertain):
            self.api.request_movie(157336)

    def test_invalid_json_and_schema(self):
        responses = [httpx.Response(200, text="not json"),
                     httpx.Response(200, json=[]),
                     httpx.Response(200, json={"results": None}),
                     httpx.Response(200, json={"results": [{"mediaType": "movie", "id": "bad"}]})]
        for response in responses:
            self.http.request.return_value = response
            with self.assertRaises(OverseerrError):
                self.api.search_movies("Interstellar")

    def test_status_mapping(self):
        for status, state in [(1, "unknown"), (2, "requested"), (3, "requested"),
                              (4, "available"), (5, "available"), (6, "unknown")]:
            self.assertEqual(parse_movie({**API_MOVIE, "mediaInfo": {"status": status}}).state, state)

    def test_active_requests_and_failed_requests(self):
        for status, state in [(1, "requested"), (2, "requested"), (3, "unknown"), (4, "failed")]:
            info = {"status": 1, "requests": [{"status": status, "is4k": False}]}
            self.assertEqual(parse_movie({**API_MOVIE, "mediaInfo": info}).state, state)

    def test_malformed_post_result_is_uncertain(self):
        self.respond(201, {})
        with self.assertRaises(RequestUncertain):
            self.api.request_movie(157336)


class ConfigTests(unittest.TestCase):
    def test_exact_existing_path(self):
        from pathlib import Path
        self.assertEqual(CONFIG_PATH, Path.home() / ".config/cube/overseerr.env")

    def test_existing_file_is_authoritative_and_repr_is_private(self):
        # In-memory fixture: no second env file is created.
        content = '# comment\nOVERSEERR_URL="http://overseerr.invalid/"\nOVERSEERR_API_KEY=TEST_ONLY\n'
        with patch("pathlib.Path.read_text", return_value=content), patch.dict(
                "os.environ", {"OVERSEERR_API_KEY": "WRONG"}):
            settings = Settings.load()
        self.assertEqual(settings.url, "http://overseerr.invalid")
        self.assertEqual(settings.api_key, "TEST_ONLY")
        self.assertNotIn("TEST_ONLY", repr(settings))

    def test_bad_or_missing_file_is_safe(self):
        for content in ["", "OVERSEERR_URL=nope\nOVERSEERR_API_KEY=TEST_ONLY",
                        "OVERSEERR_URL=http://overseerr.invalid\nOVERSEERR_API_KEY="]:
            with patch("pathlib.Path.read_text", return_value=content):
                with self.assertRaises(OverseerrError) as caught:
                    Settings.load()
                self.assertNotIn("TEST_ONLY", str(caught.exception))
        with patch("pathlib.Path.read_text", side_effect=PermissionError("private")):
            with self.assertRaises(OverseerrError):
                Settings.load()


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # No model download, microphone, or physical Govee calls.
        with patch.dict(sys.modules, {
            "faster_whisper": SimpleNamespace(WhisperModel=Mock()),
            "tts.kokoro_tts": SimpleNamespace(KokoroTTS=Mock()),
        }):
            cls.server = importlib.import_module("server")

    def setUp(self):
        self.api = Mock(spec=OverseerrClient)
        self.api.search_movies.return_value = [MOVIE]
        self.api.get_movie.return_value = MOVIE
        self.flow = MovieCommands(self.api)
        self.flow_patch = patch.object(self.server, "movie_commands", self.flow)
        self.flow_patch.start()
        self.addCleanup(self.flow_patch.stop)
        self.lights_patch = patch.object(self.server, "control_lights")
        self.lights = self.lights_patch.start()
        self.addCleanup(self.lights_patch.stop)
        tuya = patch("integrations.tuya.TuyaClient")
        self.tuya = tuya.start().return_value
        self.addCleanup(tuya.stop)
        self.addCleanup(self.server.plug_commands.close)
        self.tuya.get_device_info.return_value = SimpleNamespace(online=True)
        self.tuya.set_power.return_value = None
        self.tuya.is_on.side_effect = AssertionError("No power confirmation reads")
        self.web = TestClient(self.server.app)

    def voice(self, text):
        with patch.object(self.server, "transcribe_audio", return_value={
                "text": text, "language": "en", "processing_time": 0.01}):
            result = self.web.post("/voice", files={"file": ("voice.wav", b"mock audio", "audio/wav")})
        self.assertEqual(result.status_code, 200)
        return result.json()

    def test_voice_clear_request_has_no_redundant_confirmation(self):
        result = self.voice("download Interstellar")
        self.assertEqual(result["type"], "command")
        self.assertTrue(result["success"])
        self.assertEqual(result["response"], "Requested Interstellar from 2014.")
        self.assertEqual(result["action"], "movie_requested")
        self.assertNotIn("follow_up", result)
        self.api.request_movie.assert_called_once_with(157336)
        self.lights.assert_not_called()

    def test_voice_ambiguity_and_year_followup_contract(self):
        movies = [Movie(1, "Dune", "1984"), Movie(2, "Dune", "2021")]
        self.api.search_movies.return_value = movies
        self.api.get_movie.side_effect = lambda identity: movies[identity - 1]
        self.assertTrue(self.voice("download Dune")["follow_up"])
        self.api.request_movie.assert_not_called()
        result = self.voice("The 2021 one.")
        self.assertEqual(result["response"], "Requested Dune from 2021.")
        self.assertEqual(result["action"], "movie_requested")
        self.api.request_movie.assert_called_once_with(2)

    def test_overseerr_failure_preserves_voice_contract(self):
        self.api.search_movies.side_effect = OverseerrError("Overseerr rejected the API key.")
        result = self.voice("download Interstellar")
        self.assertFalse(result["success"])
        self.assertEqual(result["type"], "command")
        self.assertEqual(result["action"], "movie_error")

    def test_existing_govee_commands_remain_deterministic(self):
        for text, action, args in [
            ("lights on", "lights_on", ("on",)),
            ("switch the lights off", "lights_off", ("off",)),
            ("set lights to 50 percent", "lights_brightness", ("brightness", 50)),
            ("make the lights blue", "lights_color", ("color", "blue")),
        ]:
            with self.subTest(text=text):
                self.lights.reset_mock()
                self.assertEqual(self.voice(text)["action"], action)
                self.lights.assert_called_once_with(*args)
        self.assertEqual(self.api.mock_calls, [])

    def test_unhandled_and_raw_transcribe_unchanged(self):
        self.assertEqual(self.voice("what is the weather")["type"], "unhandled")
        with patch.object(self.server, "transcribe_audio", return_value={"text": "download Interstellar"}):
            result = self.web.post("/transcribe", files={"file": ("voice.wav", b"mock")})
        self.assertEqual(result.json(), {"text": "download Interstellar"})
        self.assertEqual(self.api.mock_calls, [])

    def test_health_includes_tts(self):
        self.assertEqual(self.web.get("/health").json(),
                         {"status": "ok", "whisper": "ready", "govee": "ready",
                          "tts": "ready"})


if __name__ == "__main__":
    unittest.main()
