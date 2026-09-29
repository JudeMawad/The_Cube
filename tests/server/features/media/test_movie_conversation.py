"""Conversation safety, persistent references, and concurrent movie requests."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import sqlite3
import tempfile
from threading import Event
import unittest
from unittest.mock import Mock

from features.media.media_events import EventStore, MediaEvent
from features.media.movie_commands import MovieCommands
from integrations.overseerr_client import Movie, OverseerrClient, OverseerrError, RequestUncertain

DUNE = Movie(1, "Dune", "2021")
OLD_DUNE = Movie(2, "Dune", "1984")
ALIEN = Movie(3, "Alien", "1979")


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "media.sqlite3"
        self.now = 1000
        self.store = EventStore(self.path, clock=lambda: self.now)
        self.api = Mock(spec=OverseerrClient)
        self.api.request_movie.return_value = 101
        self.api.search_movies.side_effect = lambda title: [ALIEN] if title == "alien" else [DUNE]
        self.api.get_movie.side_effect = lambda identity: {1: DUNE, 2: OLD_DUNE, 3: ALIEN}[identity]
        self.flow = self.fresh()

    def fresh(self):
        return MovieCommands(self.api, store=EventStore(self.path, clock=lambda: self.now), clock=lambda: self.now)

    def say(self, text, client="pi"):
        return self.flow.handle(text, client)

    def test_natural_request_phrases(self):
        for index, phrase in enumerate(["Can you get Dune?", "Could you download Dune, please?",
                                        "Please request Dune", "Would you please get Dune, thanks?"]):
            with self.subTest(phrase=phrase):
                self.flow = self.fresh()
                result = self.say(phrase, f"pi-{index}")
                self.assertEqual(result["action"], "movie_requested")
                self.assertEqual(result["listen_for_seconds"], 0)
                self.assertIn("Dune from 2021", result["response"])
                self.assertIn("let you know", result["response"])

    def test_missing_title_accepts_bare_title(self):
        result = self.say("download")
        self.assertEqual(result["response"], "Which movie?")
        self.assertTrue(result["follow_up"])
        self.assertEqual(self.say("Dune")["action"], "movie_requested")

    def test_ordinal_order_matches_spoken_years(self):
        self.api.search_movies.side_effect = None
        self.api.search_movies.return_value = [DUNE, OLD_DUNE]
        result = self.say("get Dune")
        self.assertEqual([m["year"] for m in result["candidates"]], ["1984", "2021"])
        self.say("the first one please")
        self.api.request_movie.assert_called_once_with(OLD_DUNE.media_id)

    def test_status_candidate_and_yes_are_read_only(self):
        self.api.search_movies.side_effect = None
        self.api.search_movies.return_value = [DUNE, OLD_DUNE]
        self.say("Is Dune ready?")
        self.assertEqual(self.say("the newer one")["action"], "movie_status")
        self.say("yes please")
        self.api.request_movie.assert_not_called()

    def test_status_correction_stays_read_only(self):
        self.say("Is Dune ready?")
        self.assertEqual(self.say("No, I meant Alien")["action"], "movie_status")
        self.api.request_movie.assert_not_called()
        self.assertEqual(self.store.remembered_movie("pi"), ALIEN)

    def test_watching_is_an_offer_not_a_request(self):
        result = self.say("I want to watch Dune")
        self.assertIn("Shall I request", result["response"])
        self.api.request_movie.assert_not_called()
        self.assertEqual(self.say("yes please")["action"], "movie_requested")

    def test_rejection_and_correction_before_submission(self):
        self.say("get Duen")
        self.assertEqual(self.say("no")["action"], "movie_title_needed")
        self.say("Alien")
        self.api.request_movie.assert_called_once_with(ALIEN.media_id)

    def test_correction_after_acceptance_requires_new_confirmation(self):
        self.say("get Dune")
        result = self.say("Actually, I meant Alien")
        self.assertEqual(result["action"], "movie_confirmation")
        self.assertIn("Earlier requests are unchanged", result["response"])
        self.api.request_movie.assert_called_once_with(DUNE.media_id)
        self.say("yes please")
        self.assertEqual([c.args[0] for c in self.api.request_movie.call_args_list], [1, 3])

    def test_two_unknown_answers_end_listening_without_extending_expiry(self):
        self.say("get Duen")
        self.now += 30
        self.assertEqual(self.say("hmm what")["context_expires_in"], 90)
        self.assertEqual(self.say("I don't know")["listen_for_seconds"], 0)
        self.say("yes")
        self.api.request_movie.assert_not_called()

    def test_two_unknown_titles_end_listening(self):
        self.api.search_movies.side_effect = lambda title: []
        self.say("get")
        self.say("unfindable")
        self.assertEqual(self.say("still unfindable")["listen_for_seconds"], 0)

    def test_two_unknown_settled_followups_end_listening(self):
        self.say("get Dune")
        self.assertEqual(self.say("hmm what")["listen_for_seconds"], 10)
        self.assertEqual(self.say("hmm what")["listen_for_seconds"], 0)

    def test_status_after_ten_minutes_hours_and_restart(self):
        self.say("get Dune")
        self.api.get_movie.side_effect = lambda _: replace(DUNE, state="requested")
        for elapsed in [600, 36000]:
            self.now += elapsed
            self.flow = self.fresh()
            result = self.say("Has it started yet?")
            self.assertIn("Dune from 2021", result["response"])
            self.assertIn("haven't received a download update", result["response"])
            self.assertEqual(result["listen_for_seconds"], 10)
        self.api.request_movie.assert_called_once()

    def test_expired_choices_never_reactivate_through_durable_memory(self):
        self.say("get Dune")
        self.say("get Alie")
        self.now += 120
        result = self.say("yes please")
        self.assertIn("expired", result["response"])
        self.flow = self.fresh()
        self.say("yes")
        self.api.request_movie.assert_called_once()
        self.assertEqual(self.store.remembered_movie("pi"), DUNE)

    def test_forget_survives_restart_and_disables_history_fallback(self):
        self.say("get Dune")
        self.say("forget that movie")
        self.flow = self.fresh()
        self.assertEqual(self.say("any update")["action"], "movie_title_needed")
        self.assertIsNone(self.store.remembered_movie("pi"))
        self.assertIn("Dune", self.say("What was the last movie I requested?")["response"])
        self.api.request_movie.assert_called_once()

    def test_existing_history_fallback_is_client_scoped(self):
        identity = self.store.prepare_request(DUNE, "pi")
        self.store.confirm_request(identity)
        self.assertEqual(self.say("Any update?")["action"], "movie_status")
        self.assertEqual(self.say("Any update?", "other-pi")["action"], "movie_title_needed")
        self.api.request_movie.assert_not_called()

    def test_device_commands_and_cancel_preserve_durable_memory(self):
        self.say("get Dune")
        self.say("get Alie")
        for text in ["lights on", "set the volume to 50 percent", "connect to the speaker"]:
            self.assertIsNone(self.say(text))
            self.assertEqual(self.flow.continuation("pi")["listen_for_seconds"], 10)
        self.say("cancel")
        self.assertEqual(self.store.remembered_movie("pi"), DUNE)
        self.say("yes")
        self.api.request_movie.assert_called_once()

    def test_status_uses_undelivered_milestone_and_current_availability(self):
        self.say("get Dune")
        self.store.accept([MediaEvent("movie", 1, "", "started", "download-1")])
        self.assertIn("started downloading", self.say("has it started yet")["response"])
        self.store.accept([MediaEvent("movie", 1, "", "ready", "file-1")])
        self.api.get_movie.side_effect = lambda _: replace(DUNE, state="available")
        self.assertIn("ready to watch", self.say("is it ready")["response"])
        self.assertIsNotNone(self.store.next_event("pi", 0))  # Status never ACKs events.

    def test_status_outage_labels_historical_information(self):
        self.say("get Dune")
        self.store.accept([MediaEvent("movie", 1, "", "started", "download-1")])
        self.api.get_movie.side_effect = OverseerrError("offline")
        result = self.say("any update")
        self.assertIn("last update", result["response"])
        self.assertIn("can't check its current status", result["response"])

    def test_ambiguous_search_does_not_replace_reference(self):
        self.say("get Alien")
        self.api.search_movies.side_effect = lambda _: [DUNE, OLD_DUNE]
        self.say("get Dune")
        self.assertEqual(self.store.remembered_movie("pi"), ALIEN)

    def test_uncertain_request_does_not_retry_or_claim_tracking(self):
        self.api.request_movie.side_effect = RequestUncertain("unverified")
        self.assertFalse(self.say("get Dune")["success"])
        self.say("yes")
        self.say("get Dune")
        self.api.request_movie.assert_called_once()
        self.assertIsNone(self.store.last_requested_movie("pi"))

    def test_concurrent_clients_submit_same_movie_once(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda client: self.say("get Dune", client), ["pi", "other-pi"]))
        self.assertEqual(sum(r["action"] == "movie_requested" for r in results), 1)
        self.api.request_movie.assert_called_once_with(1)

    def test_slow_search_does_not_block_other_client(self):
        started, release = Event(), Event()

        def search(title):
            if title == "dune":
                started.set()
                release.wait(2)
                return [DUNE]
            return [ALIEN]

        self.api.search_movies.side_effect = search
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.say, "is Dune ready", "pi")
            try:
                self.assertTrue(started.wait(1))
                second = pool.submit(self.say, "is Alien ready", "other-pi")
                self.assertEqual(second.result(timeout=1)["action"], "movie_status")
            finally:
                release.set()
            first.result(timeout=1)

    def test_additive_migration_preserves_old_rows(self):
        self.say("get Dune")
        self.store.accept([MediaEvent("movie", 1, "", "started", "download-1")])
        with sqlite3.connect(self.path) as db:
            db.execute("DROP TABLE client_movie_context")
            before = db.execute("SELECT * FROM requests").fetchall()
            events = db.execute("SELECT * FROM notifications").fetchall()
        fresh = EventStore(self.path)
        self.assertEqual(fresh.remembered_movie("pi"), DUNE)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT * FROM requests").fetchall(), before)
            self.assertEqual(db.execute("SELECT * FROM notifications").fetchall(), events)

    def test_last_request_question_clears_old_confirmation(self):
        self.say("get Dune")
        self.say("get Alie")
        self.assertIn("Dune", self.say("What was the last movie I requested?")["response"])
        self.assertEqual(self.say("yes")["action"], "movie_no_pending")
        self.assertEqual(self.say("No, I meant Alien")["action"], "movie_status")
        self.api.request_movie.assert_called_once_with(DUNE.media_id)

    def test_last_request_without_history_also_clears_confirmation(self):
        self.say("get Duen")
        self.say("What was the last movie I requested?")
        self.assertEqual(self.say("yes")["action"], "movie_no_pending")
        self.api.request_movie.assert_not_called()

    def test_availability_offer_correction_preserves_read_only_intent(self):
        self.say("I want to watch Dune")
        result = self.say("No, I meant Alien")
        self.assertIn("Shall I request", result["response"])
        self.api.request_movie.assert_not_called()
        self.say("yes please")
        self.api.request_movie.assert_called_once_with(ALIEN.media_id)

    def test_rejected_availability_offer_accepts_replacement_without_requesting(self):
        self.say("I want to watch Dune")
        self.say("no")
        result = self.say("Alien")
        self.assertIn("Shall I request", result["response"])
        self.api.request_movie.assert_not_called()
