"""Stateful external fakes exercise cancellation outcomes, identity races and recovery."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx

from integrations.arr_client import ArrError, Settings as ArrSettings
from features.media.event_routes import event_router
from features.media.media_events import EventStore, MediaEvent
from features.media.movie_commands import MovieCommands
from integrations.overseerr_client import Movie, OverseerrClient, OverseerrError, Settings, RequestUncertain, parse_movie
from integrations.radarr_client import RadarrClient

DUNE = Movie(1, "Dune", "2021")


class CancellationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "events.sqlite3"
        self.now = 1000
        self.store = EventStore(self.path, clock=lambda: self.now)
        self.request = {"id": 44, "type": "movie", "is4k": False, "status": 2,
                        "createdAt": datetime.fromtimestamp(1010, timezone.utc).isoformat(),
                        "media": {"tmdbId": 1}, "requestedBy": {"id": 7}, "serverId": 10}
        self.requests = {44: deepcopy(self.request)}
        self.movie = {"id": 8, "tmdbId": 1, "hasFile": False, "monitored": True}
        self.queue = [{"id": 80, "movieId": 8, "downloadId": "download-1"}]
        self.mutations = []
        self.api = Mock(spec=OverseerrClient)
        self.api.movie_details.side_effect = self.details
        self.api.get_movie.side_effect = lambda identity: parse_movie(self.details(identity))
        self.api.get_request.side_effect = lambda identity: deepcopy(self.requests.get(identity))
        self.api.requesting_user_id.return_value = 7
        self.api.radarr_servers.return_value = [{"id": 10, "isDefault": True, "is4k": False}]
        self.api.delete_request.side_effect = self.delete_request
        self.api.request_movie.side_effect = self.request_again
        self.api.search_movies.return_value = [DUNE]
        self.radarr = Mock(spec=RadarrClient)
        self.radarr.find_movie.side_effect = lambda identity: deepcopy(self.movie)
        self.radarr.queue_for_movie.side_effect = lambda identity: deepcopy(self.queue)
        self.radarr.set_monitored.side_effect = self.monitor
        self.radarr.remove_queue_item.side_effect = self.remove_download
        self.radarr.delete_movie.side_effect = self.delete_movie
        self.identity = self.store.prepare_request(DUNE, "pi")
        self.store.confirm_request(self.identity, 44)
        self.store.remember_movie("pi", DUNE)
        self.flow = self.fresh()

    def fresh(self):
        self.store = EventStore(self.path, clock=lambda: self.now)
        return MovieCommands(self.api, store=self.store, clock=lambda: self.now, radarr=self.radarr)

    def details(self, identity):
        return {"id": identity, "title": "Dune", "releaseDate": "2021-01-01", "mediaInfo": {
            "status": 5 if self.movie and self.movie["hasFile"] else (3 if self.requests else 1),
            "requests": [deepcopy(r) for r in self.requests.values()], "serviceId": 10,
            "externalServiceId": self.movie["id"] if self.movie else None}}

    def monitor(self, identity, value):
        self.assertEqual(identity, self.movie["id"])
        self.mutations.append(("monitor", value))
        self.movie["monitored"] = value

    def remove_download(self, identity):
        self.mutations.append(("queue", identity))
        self.queue = [item for item in self.queue if item["id"] != identity]

    def delete_request(self, identity):
        self.mutations.append(("request", identity))
        self.requests.pop(identity, None)

    def delete_movie(self, identity):
        self.assertEqual(identity, self.movie["id"])
        self.assertFalse(self.movie["hasFile"])
        self.assertEqual(self.queue, [])
        self.assertEqual(self.requests, {})
        self.mutations.append(("movie", identity))
        self.movie = None

    def legacy_cancel(self):
        self.say("cancel it")
        self.flow.pending["pi"].cancellation_target.pop("remove_radarr")
        return self.say("yes")

    def request_again(self, identity):
        self.assertEqual(identity, 1)
        if self.movie is None:
            self.movie = {"id": 9, "tmdbId": 1, "hasFile": False, "monitored": True}
        self.requests[45] = {**deepcopy(self.request), "id": 45}
        return 45

    def say(self, phrase, client="pi"):
        return self.flow.handle(phrase, client)

    def cancel(self):
        self.assertEqual(self.say("cancel that movie download")["action"], "movie_cancellation_confirmation")
        return self.say("yes")

    def assert_options(self, outcome, expected):
        self.assertEqual(outcome["response_options"], expected)
        self.assertEqual(expected[0], outcome["response"])
        for option in expected:
            self.assertTrue(option.strip())
            self.assertLessEqual(len(option), 500)

    def test_approved_wording_distinguishes_preview_completion_and_existing_cancellation(self):
        preview = self.say("cancel the download of Dune")
        self.assert_options(preview, ["I found Dune from 2021, Should I delete it now?",
                                      "I found Dune from 2021. Do you want me to cancel it now?"])
        self.assertEqual(self.mutations, [])
        completed = self.say("yes")
        self.assert_options(completed, ["Alright I cancelled it.", "I've cancelled Dune from 2021."])
        self.assertTrue(completed["success"])
        mutations = list(self.mutations)
        status = self.say("did you cancel it")
        self.assert_options(status, ["The request for Dune from 2021 was cancelled.",
                                     "The movie request for Dune from 2021 has been cancelled."])
        already = self.say("cancel it")
        self.assert_options(already, ["Dune from 2021 is already cancelled.",
                                      "No need to cancel it again. Dune from 2021 is already cancelled."])
        self.assertEqual(self.mutations, mutations)

    def test_uncertain_wording_never_claims_completion(self):
        self.radarr.set_monitored.side_effect = ArrError("Unavailable")
        outcome = self.cancel()
        self.assertEqual(outcome["action"], "movie_cancellation_uncertain")
        self.assertFalse(outcome["success"])
        message = "I couldn't verify the full cancellation of Dune from 2021."
        self.assert_options(outcome, [message + " Ask me to try cancelling it again.",
                                      message + " You can ask me to try cancelling it again."])
        self.assertEqual(self.mutations, [])
        self.assert_options(self.say("did you cancel it"), [
            "Cancellation of Dune from 2021 is still unverified. Ask me to try cancelling it again.",
            "I still can't verify the cancellation of Dune from 2021. You can ask me to try cancelling it again.",
        ])

    def test_partial_wording_preserves_verified_stop_and_unverified_removals(self):
        self.api.delete_request.side_effect = OverseerrError("Unavailable")
        outcome = self.cancel()
        self.assertEqual(outcome["action"], "movie_cancellation_partial")
        self.assertFalse(outcome["success"])
        message = "The download for Dune from 2021 stopped, but I couldn't verify removal from Overseerr and Radarr."
        self.assert_options(outcome, [message + " Ask me to try cancelling it again.",
                                      message + " You can ask me to try cancelling it again."])
        self.assertEqual(self.mutations, [("monitor", False), ("queue", 80)])

    def test_partial_wording_does_not_claim_a_stop_before_queue_removal(self):
        self.radarr.remove_queue_item.side_effect = ArrError("Unavailable")
        outcome = self.cancel()
        self.assertEqual(outcome["action"], "movie_cancellation_partial")
        self.assertFalse(outcome["success"])
        message = "I couldn't verify the full cancellation of Dune from 2021."
        self.assert_options(outcome, [message + " Ask me to try cancelling it again.",
                                      message + " You can ask me to try cancelling it again."])
        self.assertEqual(self.mutations, [("monitor", False)])

    def test_confirmed_cancellation_is_ordered_and_verified(self):
        prompt = self.say("cancel the download of Dune")
        self.assertIn("Dune from 2021", prompt["response"])
        self.assertIn("Dune", prompt["response"])
        self.assertIn("delete", prompt["response"])
        self.assertEqual(self.mutations, [])
        self.assertEqual(self.say("yes please")["action"], "movie_download_cancelled")
        self.assertEqual(self.mutations, [("monitor", False), ("queue", 80), ("request", 44), ("movie", 8)])
        self.assertEqual(self.store.owned_request("pi", 1)["cancellation_state"], "cancelled")

    def test_natural_short_commands_always_confirm(self):
        for phrase in ["cancel download", "stop downloading", "retry cancellation", "cancel last request"]:
            self.assertEqual(self.say(phrase)["action"], "movie_cancellation_confirmation")
        self.assertEqual(self.mutations, [])
        self.assertEqual(self.say("yes")["action"], "movie_download_cancelled")
        self.assertIn("was cancelled", self.say("did you cancel it")["response"])

    def test_download_phrase_does_not_confirm_a_cancellation(self):
        self.say("cancel it")
        self.assertEqual(self.say("download it")["action"], "movie_cancellation_confirmation")
        self.assertEqual(self.mutations, [])

    def test_waiting_approval_has_no_radarr_mutations(self):
        self.movie, self.queue = None, []
        self.requests[44]["status"] = 1
        self.assertEqual(self.cancel()["action"], "movie_download_cancelled")
        self.assertEqual(self.mutations, [("request", 44)])

    def test_delayed_reference_survives_restart(self):
        self.now += 36000
        self.flow = self.fresh()
        self.assertEqual(self.cancel()["action"], "movie_download_cancelled")
        self.flow = self.fresh()
        self.assertIn("was cancelled", self.say("has it started yet")["response"])

    def test_cancel_last_request_does_not_use_unowned_reference(self):
        self.store.remember_movie("pi", Movie(99, "Other", "2000"))
        self.assertIn("Dune", self.say("cancel my last movie request")["response"])
        self.assertEqual(self.mutations, [])

    def test_no_and_bare_cancel_only_end_confirmation(self):
        for phrase in ["no", "cancel", "never mind"]:
            self.say("stop downloading it")
            self.say(phrase)
            self.assertEqual(self.say("yes")["action"], "movie_no_pending")
        self.assertEqual(self.mutations, [])

    def test_expired_and_restarted_confirmation_never_executes(self):
        self.say("cancel it")
        self.now += 120
        self.assertEqual(self.say("yes")["action"], "movie_no_pending")
        self.say("cancel it")
        self.flow = self.fresh()
        self.say("yes")
        self.assertEqual(self.mutations, [])

    def test_concurrent_yes_only_cancels_once(self):
        self.say("cancel it")
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(self.say, ["yes", "yes"]))
        self.assertEqual(sum(r["action"] == "movie_download_cancelled" for r in results), 1)
        self.api.delete_request.assert_called_once_with(44)

    def test_unowned_and_other_cube_requests_are_refused(self):
        self.store.remember_movie("other", DUNE)
        self.assertEqual(self.say("cancel it", "other")["action"], "movie_cancellation_refused")
        other = self.store.prepare_request(DUNE, "other")
        self.store.confirm_request(other, 44)
        self.assertEqual(self.say("cancel it")["action"], "movie_cancellation_refused")
        self.assertEqual(self.mutations, [])

    def test_multiple_upstream_requests_are_refused(self):
        self.requests[46] = {**deepcopy(self.request), "id": 46, "requestedBy": {"id": 99}}
        self.assertEqual(self.say("cancel it")["action"], "movie_cancellation_refused")
        self.assertEqual(self.mutations, [])

    def test_legacy_request_needs_time_and_user_match(self):
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE requests SET overseerr_request_id=NULL")
        self.assertEqual(self.say("cancel it")["action"], "movie_cancellation_confirmation")
        self.requests[44]["createdAt"] = datetime.fromtimestamp(2000, timezone.utc).isoformat()
        self.assertEqual(self.say("cancel it")["action"], "movie_cancellation_refused")
        self.requests[44] = deepcopy(self.request)
        self.api.requesting_user_id.return_value = 9
        self.assertEqual(self.say("cancel it")["action"], "movie_cancellation_refused")
        self.assertEqual(self.mutations, [])

    def test_changed_service_identity_is_refused(self):
        self.radarr.verify_server.side_effect = ArrError("wrong instance")
        self.assertEqual(self.say("cancel it")["action"], "movie_cancellation_refused")
        self.assertEqual(self.mutations, [])

    def test_service_changed_after_confirmation_is_refused(self):
        self.say("cancel it")
        self.requests[44]["serverId"] = 11
        self.assertEqual(self.say("yes")["action"], "movie_cancellation_refused")
        self.assertEqual(self.mutations, [])

    def test_failed_reenable_can_be_retried_after_restart(self):
        self.legacy_cancel()
        self.radarr.set_monitored.side_effect = ArrError("offline")
        self.assertFalse(self.say("get Dune")["success"])
        self.api.request_movie.assert_not_called()
        self.flow = self.fresh()
        self.radarr.set_monitored.side_effect = self.monitor
        self.assertEqual(self.say("get Dune")["action"], "movie_requested")
        self.assertEqual(self.store.owned_request("pi", 1)["generation"], 2)
        self.assertTrue(self.movie["monitored"])

    def test_import_before_confirmation_preserves_library(self):
        self.say("cancel it")
        self.movie["hasFile"] = True
        self.assertEqual(self.say("yes")["action"], "movie_cancellation_refused")
        self.assertEqual(self.mutations, [])

    def test_import_during_cancellation_prevents_download_removal(self):
        def monitor(identity, value):
            self.monitor(identity, value)
            self.movie["hasFile"] = True
        self.radarr.set_monitored.side_effect = monitor
        self.assertFalse(self.cancel()["success"])
        self.radarr.remove_queue_item.assert_not_called()
        self.api.delete_request.assert_not_called()
        self.assertIn("ready to watch", self.say("any update")["response"])

    def test_new_or_reused_queue_identity_requires_new_confirmation(self):
        self.say("cancel it")
        self.queue[0]["downloadId"] = "different-download"
        self.assertEqual(self.say("yes")["action"], "movie_cancellation_refused")
        self.assertEqual(self.mutations, [])
        self.assertEqual(self.cancel()["action"], "movie_download_cancelled")

    def test_lost_delete_reply_is_resolved_without_repeating_delete(self):
        def delete(identity):
            self.delete_request(identity)
            raise RequestUncertain("response lost")
        self.api.delete_request.side_effect = delete
        self.assertEqual(self.cancel()["action"], "movie_cancellation_partial")
        self.radarr.delete_movie.assert_not_called()
        self.assertEqual(self.cancel()["action"], "movie_download_cancelled")
        self.api.delete_request.assert_called_once()

    def test_partial_failure_is_persistent_and_retry_only_finishes_remaining_steps(self):
        self.api.delete_request.side_effect = RequestUncertain("offline")
        self.assertEqual(self.cancel()["action"], "movie_cancellation_partial")
        self.assertIn("still unverified", self.say("any update")["response"])
        self.flow = self.fresh()
        self.api.delete_request.side_effect = self.delete_request
        self.say("try cancelling it again")
        self.assertEqual(self.say("yes")["action"], "movie_download_cancelled")
        self.radarr.set_monitored.assert_called_once()
        self.radarr.remove_queue_item.assert_called_once()

    def test_read_only_reconciliation_after_restart(self):
        self.api.delete_request.side_effect = RequestUncertain("offline")
        self.cancel()
        self.requests.clear()  # The remaining work was completed externally.
        self.movie = None
        self.flow = self.fresh()
        before = list(self.mutations)
        self.assertIn("was cancelled", self.say("any update")["response"])
        self.assertEqual(self.mutations, before)
        self.assertEqual(self.store.owned_request("pi", 1)["cancellation_state"], "cancelled")

    def test_uncertain_cancellation_blocks_new_request(self):
        self.radarr.remove_queue_item.side_effect = ArrError("offline")
        self.cancel()
        self.assertFalse(self.say("get Dune")["success"])
        self.api.request_movie.assert_not_called()

    def test_cancelled_movie_can_be_requested_again_with_new_generation(self):
        self.assertEqual(self.cancel()["action"], "movie_download_cancelled")
        self.assertIsNone(self.movie)
        self.assertEqual(self.say("get Dune")["action"], "movie_requested")
        self.assertEqual(self.movie["id"], 9)
        tracked = self.store.owned_request("pi", 1)
        self.assertEqual(tracked["generation"], 2)
        self.assertEqual(tracked["overseerr_request_id"], 45)
        self.assertEqual(tracked["cancellation_state"], "active")
        self.assertTrue(self.movie["monitored"])
        self.assertEqual(self.store.cancellation("pi", 1)["outcome"], "complete")

    def test_radarr_delete_failure_is_partial_and_retry_finishes_only_removal(self):
        self.radarr.delete_movie.side_effect = ArrError("offline")
        self.assertEqual(self.cancel()["action"], "movie_cancellation_partial")
        before = list(self.mutations)
        self.flow = self.fresh()
        self.assertIn("still unverified", self.say("any update")["response"])
        self.assertEqual(self.mutations, before)
        self.assertFalse(self.say("get Dune")["success"])
        self.radarr.delete_movie.side_effect = self.delete_movie
        self.assertEqual(self.cancel()["action"], "movie_download_cancelled")
        self.assertEqual(self.mutations, before + [("movie", 8)])

    def test_lost_radarr_delete_reply_is_verified_without_retry(self):
        def delete(identity):
            self.delete_movie(identity)
            raise ArrError("response lost")
        self.radarr.delete_movie.side_effect = delete
        self.assertEqual(self.cancel()["action"], "movie_download_cancelled")
        self.radarr.delete_movie.assert_called_once_with(8)

    def test_successful_delete_response_requires_absent_movie(self):
        self.radarr.delete_movie.side_effect = None  # Upstream accepts but hasn't removed it.
        self.assertEqual(self.cancel()["action"], "movie_cancellation_partial")
        self.assertIsNotNone(self.movie)
        self.assertEqual(self.store.owned_request("pi", 1)["cancellation_state"], "partial")

    def test_import_just_before_radarr_removal_keeps_entry_and_file(self):
        def delete_request(identity):
            self.delete_request(identity)
            self.movie["hasFile"] = True
        self.api.delete_request.side_effect = delete_request
        self.assertFalse(self.cancel()["success"])
        self.radarr.delete_movie.assert_not_called()
        self.assertTrue(self.movie["hasFile"])

    def test_replaced_radarr_entry_is_not_deleted(self):
        def delete_request(identity):
            self.delete_request(identity)
            self.movie["id"] = 99
        self.api.delete_request.side_effect = delete_request
        self.assertFalse(self.cancel()["success"])
        self.radarr.delete_movie.assert_not_called()

    def test_legacy_cancellation_cleanup_requires_new_confirmation(self):
        self.assertEqual(self.legacy_cancel()["action"], "movie_download_cancelled")
        before = list(self.mutations)
        self.flow = self.fresh()
        self.assertIn("was cancelled", self.say("any update")["response"])
        prompt = self.say("cancel it")
        self.assertIn("Dune", prompt["response"])
        self.assertIn("delete", prompt["response"])
        self.assertEqual(self.mutations, before)
        self.assertEqual(self.say("yes")["action"], "movie_download_cancelled")
        self.assertEqual(self.mutations, before + [("movie", 8)])
        self.assertIsNone(self.movie)

    def test_second_cancellation_targets_new_radarr_entry(self):
        self.cancel()
        self.say("get Dune")
        self.assertEqual(self.cancel()["action"], "movie_download_cancelled")
        self.assertEqual(self.radarr.delete_movie.call_args_list, [unittest.mock.call(8), unittest.mock.call(9)])

    def test_cancelled_and_old_generation_events_cannot_play_or_ack(self):
        self.store.accept([MediaEvent("movie", 1, "", "started", "download-1")])
        event = self.store.next_event("pi", 0)
        self.assertTrue(self.store.event_valid("pi", event["id"], event["ack_token"]))
        self.cancel()
        self.assertFalse(self.store.event_valid("pi", event["id"], event["ack_token"]))
        self.assertFalse(self.store.acknowledge("pi", event["id"], event["ack_token"]))
        self.assertIsNone(self.store.next_event("pi", 0))
        self.say("get Dune")
        self.assertEqual(self.store.accept([MediaEvent("movie", 1, "", "ready", "download-1")]), 0)
        self.assertEqual(self.store.accept([MediaEvent("movie", 1, "", "ready", "unknown-old-file")]), 0)
        self.assertEqual(self.store.accept([MediaEvent("movie", 1, "", "started", "download-2")]), 1)
        self.assertEqual(self.store.accept([MediaEvent("movie", 1, "", "ready", "download-2")]), 1)
        fresh = self.store.next_event("pi", 0)
        self.assertNotEqual(fresh["id"], event["id"])
        self.assertFalse(self.store.event_valid("pi", event["id"], event["ack_token"]))

    def test_validation_endpoint_requires_auth_identity_and_receipt(self):
        self.store.accept([MediaEvent("movie", 1, "", "started", "download-1")])
        event = self.store.next_event("pi", 0)
        app = FastAPI()
        app.include_router(event_router(self.store))
        url = f"/events/{event['id']}/validate"
        body = {"ack_token": event["ack_token"]}
        headers = {"X-Cube-Token": "x" * 32, "X-Cube-Client-ID": "pi"}
        with patch("features.media.event_routes.TOKEN_PATH") as path, TestClient(app) as client:
            path.read_text.return_value = "x" * 32
            self.assertEqual(client.post(url, json=body).status_code, 401)
            self.assertTrue(client.post(url, json=body, headers=headers).json()["valid"])
            self.assertFalse(client.post(url, json=body, headers={**headers, "X-Cube-Client-ID": "other"}).json()["valid"])
            self.cancel()
            self.assertFalse(client.post(url, json=body, headers=headers).json()["valid"])


class MigrationTests(unittest.TestCase):
    def test_upgrade_preserves_receipts_context_and_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old.sqlite3"
            with sqlite3.connect(path) as db:
                db.executescript("""
                    CREATE TABLE requests (id INTEGER PRIMARY KEY, media_type TEXT NOT NULL,
                        external_id INTEGER NOT NULL, episode TEXT NOT NULL DEFAULT '',
                        title TEXT NOT NULL, year TEXT, requested_at REAL NOT NULL,
                        client_id TEXT NOT NULL, confirmed INTEGER NOT NULL DEFAULT 0,
                        started_announced INTEGER NOT NULL DEFAULT 0, ready_announced INTEGER NOT NULL DEFAULT 0,
                        UNIQUE(media_type,external_id,episode,client_id));
                    CREATE TABLE notifications (id INTEGER PRIMARY KEY,
                        request_id INTEGER NOT NULL REFERENCES requests(id), stage TEXT NOT NULL,
                        event_id TEXT NOT NULL, response TEXT NOT NULL, ack_token TEXT NOT NULL,
                        delivered_at REAL, UNIQUE(request_id,stage));
                    CREATE TABLE client_movie_context (client_id TEXT PRIMARY KEY,
                        external_id INTEGER, title TEXT, year TEXT, updated_at REAL NOT NULL);
                    INSERT INTO requests VALUES (3,'movie',1,'','Dune','2021',1000,'pi',1,1,0);
                    INSERT INTO notifications VALUES (27,3,'started','old','Started','receipt-old',1001);
                    INSERT INTO notifications VALUES (28,3,'ready','old','Ready','receipt-new',NULL);
                    INSERT INTO client_movie_context VALUES ('pi',1,'Dune','2021',1000);
                """)
            for _ in range(2):  # A repeat startup is idempotent.
                store = EventStore(path)
                self.assertEqual(store.remembered_movie("pi"), DUNE)
                self.assertEqual(store.next_event("pi", 0)["id"], 28)
                self.assertTrue(store.event_valid("pi", 28, "receipt-new"))
                self.assertEqual(store.owned_request("pi", 1)["generation"], 1)
                with sqlite3.connect(path) as db:
                    self.assertEqual(db.execute("SELECT id,delivered_at FROM notifications ORDER BY id").fetchall(),
                                     [(27, 1001), (28, None)])
                    self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])


class TransportTests(unittest.TestCase):
    def test_delete_accepts_empty_success_and_does_not_retry_timeout(self):
        with patch("integrations.overseerr_client.Settings.load", return_value=Settings("http://example.invalid", "TEST_ONLY")), \
             patch("integrations.overseerr_client.httpx.Client") as factory:
            http = factory.return_value
            api = OverseerrClient()
            http.request.return_value = httpx.Response(204)
            self.assertIsNone(api.delete_request(44))
            http.request.side_effect = httpx.ReadTimeout("PRIVATE_SENTINEL")
            with self.assertRaises(RequestUncertain) as error:
                api.delete_request(44)
            self.assertNotIn("PRIVATE_SENTINEL", str(error.exception))
            self.assertEqual(http.request.call_count, 2)

    def test_radarr_removal_flags_and_monitoring_body(self):
        api = RadarrClient()
        with patch.object(api, "_request") as request:
            api.remove_queue_item(80)
            request.assert_called_with("DELETE", "/queue/80", allow_not_found=True,
                params={"removeFromClient": "true", "blocklist": "false", "skipRedownload": "true"})
            api.set_monitored(8, False)
            request.assert_called_with("PUT", "/movie/editor", array=True,
                                      json={"movieIds": [8], "monitored": False})

    def test_radarr_movie_deletion_keeps_files_and_allows_future_imports(self):
        api = RadarrClient()
        with patch.object(api, "_request") as request:
            api.delete_movie(8)
            request.assert_called_once_with("DELETE", "/movie/8", allow_not_found=True,
                params={"deleteFiles": "false", "addImportExclusion": "false"})

    def test_radarr_queue_pagination_and_identity_validation(self):
        api = RadarrClient()
        with patch.object(api, "_get", side_effect=[
            {"records": [{"id": 1, "movieId": 8}], "totalRecords": 2},
            {"records": [{"id": 2, "movieId": 8}], "totalRecords": 2},
        ]):
            self.assertEqual(len(api.queue_for_movie(8)), 2)
        with patch.object(api, "_get", return_value={"records": [{"id": 1, "movieId": 9}], "totalRecords": 1}):
            with self.assertRaises(ArrError):
                api.queue_for_movie(8)

    def test_radarr_server_key_must_match_without_exposure(self):
        with patch("integrations.radarr_client.Settings.load", return_value=ArrSettings("http://example.invalid", "TEST_ONLY")):
            api = RadarrClient()
            api.verify_server([{"id": 10, "apiKey": "TEST_ONLY", "is4k": False}], 10)
            with self.assertRaises(ArrError):
                api.verify_server([{"id": 10, "apiKey": "DIFFERENT", "is4k": False}], 10)
