"""Offline event, persistence, transport and natural movie-flow regressions."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx

from integrations.arr_client import ArrError, Settings
from features.media.event_routes import event_router
from features.media.media_events import EventStore, parse_events
from features.media.media_wording import request_ack, download_started, ready_to_watch
from features.media.movie_commands import MovieCommands
from integrations.overseerr_client import Movie, OverseerrClient, RequestUncertain
from integrations.radarr_client import RadarrClient
from integrations.sonarr_client import SonarrClient

DUNE = Movie(438631, "Dune", "2021")
OLD_DUNE = Movie(841, "Dune", "1984")


def radarr(kind="Grab", tmdb=438631):
    payload = {"eventType": kind, "movie": {"id": 7, "tmdbId": tmdb, "title": "Dune", "year": 2021},
               "downloadId": "test-download"}
    if kind == "Download":
        payload["movieFile"] = {"id": 9}
    return payload


def sonarr(kind="Grab", grouped=False):
    payload = {"eventType": kind, "series": {"id": 8, "tvdbId": 123},
               "episodes": [{"seasonNumber": 1, "episodeNumber": 2}], "downloadId": "test-tv"}
    if kind == "Download":
        payload["episodeFiles" if grouped else "episodeFile"] = [{"id": 5}] if grouped else {"id": 5}
    return payload


class StoreFixture(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "state.sqlite3"
        self.store = EventStore(self.path, clock=lambda: 1000)

    def register(self):
        request_id = self.store.prepare_request(DUNE, "pi")
        self.store.confirm_request(request_id)
        return request_id


class StoreTests(StoreFixture):
    def test_persistence_identity_and_ownership(self):
        self.register()
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT media_type, external_id, title, year, requested_at, client_id, confirmed FROM requests").fetchone(),
                             ("movie", 438631, "Dune", "2021", 1000, "pi", 1))
        self.assertEqual(self.store.accept(parse_events("radarr", radarr(tmdb=1))), 0)
        self.assertEqual(self.store.accept(parse_events("radarr", radarr())), 1)
        self.assertIsNone(self.store.next_event("different-pi", 0))

    def test_start_ready_dedup_and_restart(self):
        self.register()
        for kind in ["Grab", "Download"]:
            self.assertEqual(self.store.accept(parse_events("radarr", radarr(kind))), 1)
            self.assertEqual(self.store.accept(parse_events("radarr", radarr(kind))), 0)
            different_release = {**radarr(kind), "downloadId": "retry-or-upgrade"}
            self.assertEqual(self.store.accept(parse_events("radarr", different_release)), 0)
        first = self.store.next_event("pi", 0)
        self.store = EventStore(self.path)
        self.assertEqual(self.store.next_event("pi", 0), first)
        self.assertFalse(self.store.acknowledge("other", first["id"], first["ack_token"]))
        self.assertFalse(self.store.acknowledge("pi", first["id"], "wrong"))
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT started_announced, ready_announced FROM requests").fetchone(), (0, 0))
        for _ in range(2):  # Lost ACK response can be retried safely.
            self.assertTrue(self.store.acknowledge("pi", first["id"], first["ack_token"]))
        second = self.store.next_event("pi", 0)
        self.assertEqual(second["response"], ready_to_watch("Dune"))
        self.store.acknowledge("pi", second["id"], second["ack_token"])
        self.store = EventStore(self.path)
        self.assertIsNone(self.store.next_event("pi", 0))
        self.assertEqual(self.store.accept(parse_events("radarr", radarr("Download"))), 0)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT started_announced, ready_announced FROM requests").fetchone(), (1, 1))

    def test_early_event_waits_for_verified_post(self):
        request_id = self.store.prepare_request(DUNE, "pi")
        self.store.accept(parse_events("radarr", radarr()))
        self.assertIsNone(self.store.next_event("pi", 0))
        self.store.confirm_request(request_id)
        self.assertIsNotNone(self.store.next_event("pi", 0))

    def test_retried_unverified_intent_discards_old_events(self):
        self.store.prepare_request(DUNE, "pi")
        self.store.accept(parse_events("radarr", radarr()))
        self.register()
        self.assertIsNone(self.store.next_event("pi", 0))

    def test_ready_before_grab_suppresses_stale_start(self):
        self.register()
        self.store.accept(parse_events("radarr", radarr("Download")))
        self.assertEqual(self.store.accept(parse_events("radarr", radarr())), 0)

    def test_long_poll_wakes_on_event_without_busy_polling(self):
        self.register()
        with ThreadPoolExecutor() as pool:
            result = pool.submit(self.store.next_event, "pi", 2)
            time.sleep(0.03)
            self.assertFalse(result.done())
            self.store.accept(parse_events("radarr", radarr()))
            self.assertIsNotNone(result.result(timeout=1))
        self.assertIsNone(self.store.next_event("other", 0.01))

    def test_concurrent_webhook_delivery_creates_one(self):
        self.register()
        with ThreadPoolExecutor() as pool:
            counts = list(pool.map(self.store.accept, [parse_events("radarr", radarr())] * 8))
        self.assertEqual(sum(counts), 1)

    def test_sonarr_episode_scoping_and_both_import_shapes(self):
        self.assertEqual(self.store.accept(parse_events("sonarr", sonarr())), 0)
        request_id = self.store.prepare("episode", 123, "Example", "2020", "pi", "S01E02")
        self.store.confirm_request(request_id)
        self.assertEqual(self.store.accept(parse_events("sonarr", sonarr())), 1)
        self.assertEqual(self.store.accept(parse_events("sonarr", sonarr("Download"))), 1)
        self.assertEqual(self.store.accept(parse_events("sonarr", sonarr("Download", True))), 0)
        other = sonarr()
        other["episodes"][0]["episodeNumber"] = 3
        self.assertEqual(self.store.accept(parse_events("sonarr", other)), 0)

    def test_unknown_test_and_malformed_events(self):
        for kind in ["Test", "MovieAdded", "Rename", "Health", "Import"]:
            self.assertEqual(parse_events("radarr", {"eventType": kind}), [])
        for payload in [{}, {"eventType": "Grab"}, radarr(tmdb=True),
                        {**radarr("Download"), "movieFile": None}]:
            with self.assertRaises(ValueError):
                parse_events("radarr", payload)


class MovieFlowTests(StoreFixture):
    def setUp(self):
        super().setUp()
        self.api = Mock(spec=OverseerrClient)
        self.api.request_movie.return_value = 101
        self.api.search_movies.return_value = [DUNE]
        self.api.get_movie.side_effect = lambda identity: DUNE if identity == DUNE.media_id else OLD_DUNE
        self.flow = MovieCommands(self.api, store=self.store)

    def test_unique_explicit_request_persists_and_acknowledges(self):
        result = self.flow.handle("download Dune", "pi")
        self.assertEqual(result["response"], request_ack(DUNE.label))
        self.assertEqual(result["action"], "movie_requested")
        self.assertNotIn("follow_up", result)
        self.api.request_movie.assert_called_once_with(DUNE.media_id)
        self.store.accept(parse_events("radarr", radarr()))
        self.assertIsNotNone(self.store.next_event("pi", 0))

    def test_year_newer_original_resolve_without_download_prefix(self):
        for phrase, selected in [("2021", DUNE), ("The 2021 one.", DUNE),
                                 ("The newer one.", DUNE), ("The original.", OLD_DUNE),
                                 ("The 1984 one.", OLD_DUNE)]:
            self.api.reset_mock()
            self.flow = MovieCommands(self.api, store=self.store)
            self.api.search_movies.return_value = [OLD_DUNE, DUNE]
            result = self.flow.handle("download Dune", "pi")
            self.assertEqual(result["response"], "Which one do you mean — the 1984 one or the 2021 one?")
            self.assertTrue(result["follow_up"])
            self.api.request_movie.assert_not_called()
            result = self.flow.handle(phrase, "pi")
            self.assertEqual(result["response"], request_ack(selected.label))
            self.api.request_movie.assert_called_once_with(selected.media_id)

    def test_the_thing_year_resolution(self):
        movies = [Movie(1091, "The Thing", "1982"), Movie(60935, "The Thing", "2011")]
        self.api.search_movies.return_value = movies
        self.api.get_movie.side_effect = lambda identity: next(m for m in movies if m.media_id == identity)
        self.assertTrue(self.flow.handle("download The Thing", "pi")["follow_up"])
        self.assertEqual(self.flow.handle("1982", "pi")["response"], request_ack(movies[0].label))
        self.api.request_movie.assert_called_once_with(1091)

    def test_yes_and_unknown_year_do_not_choose_ambiguous_movie(self):
        self.api.search_movies.return_value = [OLD_DUNE, DUNE]
        self.flow.handle("download Dune", "pi")
        for phrase in ["yes", "1999"]:
            self.assertTrue(self.flow.handle(phrase, "pi")["follow_up"])
        self.api.request_movie.assert_not_called()

    def test_uncertain_post_does_not_claim_ownership(self):
        def uncertain(identity):
            self.store.accept(parse_events("radarr", radarr()))
            raise RequestUncertain("Check Overseerr.")
        self.api.request_movie.side_effect = uncertain
        self.assertFalse(self.flow.handle("download Dune", "pi")["success"])
        self.assertIsNone(self.store.next_event("pi", 0))

    def test_storage_failure_prevents_untracked_post(self):
        with patch.object(self.store, "prepare_request", side_effect=OSError("private")):
            self.assertFalse(self.flow.handle("download Dune", "pi")["success"])
        self.api.request_movie.assert_not_called()


class WordingTests(unittest.TestCase):
    def test_recent_delayed_and_ready(self):
        self.assertEqual(download_started("Dune", 100, 219), "Dune just started downloading.")
        self.assertEqual(download_started("Dune", 100, 220),
                         "Hey, the movie you requested earlier, Dune, just started downloading.")
        self.assertEqual(ready_to_watch("Dune"), "Hey, quick update — Dune is ready to watch.")


class RouteTests(StoreFixture):
    def setUp(self):
        super().setUp()
        app = FastAPI()
        app.include_router(event_router(self.store))
        self.web = TestClient(app)
        self.addCleanup(self.web.close)
        token_path = patch("features.media.event_routes.TOKEN_PATH")
        token_path.start().read_text.return_value = "x" * 32
        self.addCleanup(token_path.stop)
        self.headers = {"X-Cube-Token": "x" * 32, "X-Cube-Client-ID": "pi"}

    def test_webhook_get_and_explicit_ack(self):
        self.register()
        self.assertEqual(self.web.post("/webhooks/radarr", headers=self.headers, json=radarr()).json(), {"queued": 1})
        url = "/events/next?wait=0"
        event = self.web.get(url, headers=self.headers).json()
        self.assertEqual(self.web.get(url, headers=self.headers).json(), event)
        response = self.web.post(f"/events/{event['id']}/ack", headers=self.headers, json={"ack_token": event["ack_token"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.web.get(url, headers=self.headers).status_code, 204)

    def test_auth_test_and_failures(self):
        self.assertEqual(self.web.post("/webhooks/radarr", json=radarr()).status_code, 401)
        for service in ["radarr", "sonarr"]:
            self.assertEqual(self.web.post(f"/webhooks/{service}", headers=self.headers, json={"eventType": "Test"}).json(), {"queued": 0})
        self.assertEqual(self.web.post("/webhooks/radarr", headers=self.headers, json={}).status_code, 400)
        with patch.object(self.store, "accept", side_effect=OSError("PRIVATE_KEY_SENTINEL")):
            result = self.web.post("/webhooks/radarr", headers=self.headers, json=radarr())
        self.assertEqual(result.status_code, 503)
        self.assertNotIn("PRIVATE_KEY_SENTINEL", result.text)
        with patch("features.media.event_routes.TOKEN_PATH") as path:
            path.read_text.side_effect = FileNotFoundError()
            self.assertEqual(self.web.get("/events/next", headers=self.headers).status_code, 503)


class ArrClientTests(unittest.TestCase):
    def test_each_client_reads_only_its_own_file(self):
        for cls, service in [(RadarrClient, "RADARR"), (SonarrClient, "SONARR")]:
            with patch("pathlib.Path.read_text", autospec=True, return_value=f'{service}_URL=http://arr.invalid\n{service}_API_KEY=TEST_ONLY'), patch("integrations.arr_client.httpx.Client") as factory:
                factory.return_value.__enter__.return_value.request.return_value = httpx.Response(
                    200, json={"version": "test"}, request=httpx.Request("GET", "http://arr.invalid"))
                self.assertEqual(cls().system_status()["version"], "test")
                Path.read_text.assert_called_once_with(Path.home() / f".config/cube/{service.lower()}.env")
                factory.assert_called_once_with(timeout=10, trust_env=False, follow_redirects=False)
                self.assertEqual(factory.return_value.__enter__.return_value.request.call_args.args[1], "http://arr.invalid/api/v3/system/status")

    def test_safe_errors_and_private_repr(self):
        self.assertNotIn("SECRET", repr(Settings("http://arr.invalid", "SECRET")))
        with patch("integrations.arr_client.Settings.load", return_value=Settings("http://arr.invalid", "SECRET")), patch("integrations.arr_client.httpx.Client", side_effect=httpx.ConnectError("SECRET")):
            with self.assertRaises(ArrError) as caught:
                RadarrClient().system_status()
        self.assertNotIn("SECRET", str(caught.exception))
