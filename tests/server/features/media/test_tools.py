"""Typed media transitions use only mocked services and temporary SQLite state."""
from contextlib import ExitStack
from dataclasses import replace
import ast
import inspect
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from core.schemas import ToolIntent
from core.tool_registry import ToolContext, ToolRegistry, ToolValidationError
from features.media import EventStore, MovieCommands
from features.media.tools import MediaTools
from integrations.overseerr_client import Movie, OverseerrClient, OverseerrError, RequestUncertain

DUNE = Movie(1, "Dune", "2021")
OLD = Movie(2, "Dune", "1984")
ALIEN = Movie(3, "Alien", "1979")


class MediaToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = 1000
        self.path = Path(self.tmp.name) / "events.sqlite3"
        self.store = EventStore(self.path, clock=lambda: self.now)
        self.api = Mock(spec=OverseerrClient)
        self.api.search_movies.side_effect = lambda title: [ALIEN] if title == "alien" else [DUNE]
        self.api.get_movie.side_effect = lambda identity: {1: DUNE, 2: OLD, 3: ALIEN}[identity]
        self.api.request_movie.return_value = 44
        self.flow = MovieCommands(self.api, store=self.store, clock=lambda: self.now)
        self.tools = MediaTools(self.flow)
        self.registry = ToolRegistry()
        self.tools.register(self.registry)
        guard = patch("socket.socket.connect", side_effect=AssertionError("Network forbidden"))
        guard.start()
        self.addCleanup(guard.stop)

    def run_tool(self, name, client="pi", **arguments):
        bound = self.registry.validate_intent(ToolIntent(type="tool", tool="media." + name, arguments=arguments))
        # A parser exception might be caught as a feature failure: assert every
        # guard stayed untouched as well as making forbidden calls fail.
        with ExitStack() as stack:
            guards = [stack.enter_context(patch.object(self.flow, name, side_effect=AssertionError(name)))
                      for name in ("handle", "_handle", "_dispatch_legacy", "_resolve",
                                   "_unsafe_answer", "_parse_title_year")]
            guards += [stack.enter_context(patch("features.media.movie_commands." + name, side_effect=AssertionError(name)))
                       for name in ("parse_intent", "polite")]
            result = bound.handler(bound.arguments, ToolContext(client)).data["reply"]
            for guard in guards:
                guard.assert_not_called()
            return result

    def answer(self, decision="confirm", client="pi", **arguments):
        token = self.tools.context(client).pending.pending_id
        return self.run_tool("respond_to_pending", client, pending_id=token, decision=decision, **arguments)

    def assert_options(self, result, expected):
        self.assertEqual(result["response_options"], expected)
        self.assertEqual(expected[0], result["response"])
        self.assertGreater(len(expected), 1)
        for text in result["response_options"]:
            self.assertTrue(text.strip())
            self.assertLessEqual(len(text), 500)
            self.assertNotIn("\n", text)

    def test_request_and_existing_outcome_wording_uses_catalog_facts(self):
        result = self.run_tool("request_movie", title="dune", year="2021")
        self.assert_options(result, [
            "Requested Dune from 2021. I'll let you know when it's ready.",
            "I've requested Dune from 2021. I'll let you know when it's ready.",
        ])
        self.assertEqual(result["media_id"], DUNE.media_id)
        self.api.request_movie.assert_called_once_with(DUNE.media_id)
        for state, expected in (
            ("requested", ["Dune from 2021 has already been requested.", "There's already a request for Dune from 2021."]),
            ("available", ["Dune from 2021 is already available.", "Dune from 2021 is already ready to watch."]),
        ):
            with self.subTest(state=state):
                self.api.get_movie.side_effect = lambda _: replace(DUNE, state=state)
                result = self.run_tool("check_availability", title="dune")
                self.assert_options(result, expected)
                self.api.request_movie.assert_called_once()  # No new submission.

    def test_clarification_choices_and_correction_preserve_required_decisions(self):
        self.assert_options(self.run_tool("request_movie"), [
            "Which movie?", "What movie did you have in mind?", "What's the movie title?",
        ])
        self.api.search_movies.side_effect = None
        self.api.search_movies.return_value = [DUNE, OLD]
        result = self.answer("title", title="Dune")
        self.assert_options(result, [
            "Which one do you mean — the 1984 one or the 2021 one?",
            "Did you mean Dune from 1984 or Dune from 2021?",
        ])
        self.assertEqual(result["candidates"], [{"title": "Dune", "year": "1984"}, {"title": "Dune", "year": "2021"}])
        self.api.request_movie.assert_not_called()
        self.answer("select", media_id=1)
        self.api.search_movies.return_value = [ALIEN]
        result = self.run_tool("correct_movie", title="alien")
        self.assert_options(result, [
            "Earlier requests are unchanged. Shall I also request Alien from 1979?",
            "Your earlier requests are unchanged. Would you also like me to request Alien from 1979?",
        ])
        self.api.request_movie.assert_called_once_with(1)
        self.assertIn("confirm", self.tools.context("pi").pending.permitted_decisions)

    def test_availability_offer_never_claims_a_request_was_made(self):
        result = self.run_tool("check_availability", title="Dune")
        self.assert_options(result, [
            "Dune from 2021 isn't available. Shall I request it?",
            "Dune from 2021 isn't available yet. Would you like me to request it?",
        ])
        self.assertEqual(result["action"], "movie_confirmation")
        self.assertTrue(result["follow_up"])
        self.api.request_movie.assert_not_called()

    def test_untracked_request_options_do_not_promise_notifications(self):
        self.flow.store = None
        result = self.run_tool("request_movie", title="Dune")
        self.assert_options(result, ["Requested Dune from 2021.", "I've requested Dune from 2021."])
        self.api.request_movie.assert_called_once_with(1)

    def test_changed_clarification_prompts_replace_old_options_and_can_end_dialogue(self):
        self.api.search_movies.side_effect = None
        self.api.search_movies.return_value = []
        result = self.run_tool("request_movie", title="missing")
        self.assert_options(result, ["I couldn't find a close match. What's the full title and year?",
                                     "I couldn't find a close match. Could you give me the full title and year?"])
        self.answer("title", title="missing again")
        self.assert_options(self.answer("title", title="still missing"), [
            "Let's try again later. Say Hey Cube and the movie request when you're ready.",
            "We can try again later. When you're ready, say Hey Cube and ask for the movie.",
        ])
        self.assertIsNone(self.tools.context("pi").pending)
        self.api.search_movies.return_value = [Movie(i, "Dune", str(1980 + i)) for i in range(1, 5)]
        result = self.run_tool("request_movie", title="Dune")
        self.assert_options(result, ["I found several matches. What's the full title and year?",
                                     "There are several matches. Could you give me the full title and year?"])
        self.api.request_movie.assert_not_called()

    def test_every_status_option_preserves_current_and_historical_distinctions(self):
        cases = [
            ("available", None, "Dune from 2021 is ready to watch.", "You can watch Dune from 2021 now."),
            ("failed", None, "The request for Dune from 2021 needs attention in Overseerr.",
             "The request for Dune from 2021 needs checking in Overseerr."),
            ("requested", "ready", "Dune from 2021 was imported earlier, but it isn't currently marked available.",
             "Dune from 2021 was imported before, but it isn't showing as available now."),
            ("requested", "started", "Dune from 2021 started downloading. It isn't marked ready yet.",
             "The download for Dune from 2021 started, but it isn't marked ready yet."),
            ("requested", None, "Dune from 2021 has been requested, but I haven't received a download update yet.",
             "Dune from 2021 has been requested. I haven't had a download update yet."),
            ("unknown", None, "Dune from 2021 isn't available and I can't see an active request.",
             "Dune from 2021 isn't available, and I can't find an active request for it."),
        ]
        for state, milestone, canonical, alternative in cases:
            with self.subTest(state=state, milestone=milestone), patch.object(self.store, "movie_milestone", return_value=milestone):
                self.api.get_movie.side_effect = lambda _: replace(DUNE, state=state)
                result = self.run_tool("movie_status", title="Dune")
                self.assert_options(result, [canonical, alternative])
                self.assertEqual(result["action"], "movie_status")
        self.api.request_movie.assert_not_called()

    def test_status_unavailable_never_promotes_last_known_state_to_current(self):
        self.api.get_movie.side_effect = OverseerrError("Unavailable")
        for milestone, prefix in [
            (None, "I don't have a download update for Dune from 2021."),
            ("ready", "The last update for Dune from 2021 says it was imported and marked ready."),
            ("started", "The last update for Dune from 2021 says it started downloading."),
        ]:
            with self.subTest(milestone=milestone), patch.object(self.store, "movie_milestone", return_value=milestone):
                result = self.run_tool("movie_status", title="Dune")
                self.assert_options(result, [prefix + " I can't check its current status right now.",
                                             prefix + " I can't verify its current status at the moment."])
        self.api.request_movie.assert_not_called()

    def test_uncertain_request_and_refused_cancellation_retain_full_explanations(self):
        message = "I couldn't verify the request. Please check Overseerr before trying again."
        self.api.request_movie.side_effect = RequestUncertain(message)
        result = self.run_tool("request_movie", title="Dune")
        self.assert_options(result, [message, "Sorry. " + message])
        self.assertFalse(result["success"])
        self.api.request_movie.assert_called_once()
        self.store.remember_movie("other", DUNE)
        result = self.run_tool("cancel_movie", client="other")
        self.assert_options(result, ["I can only cancel movies requested through this Cube.",
                                     "Sorry. I can only cancel movies requested through this Cube."])
        self.assertFalse(result["success"])
        self.api.delete_request.assert_not_called()

    def test_end_and_forget_options_do_not_claim_download_cancellation(self):
        self.assert_options(self.run_tool("end_conversation"), [
            "Okay, cancelled this conversation.",
            "Okay, we'll leave this conversation here. Your movie requests are unchanged.",
        ])
        self.assert_options(self.run_tool("end_conversation", forget=True), [
            "I've forgotten that movie. Existing requests are unchanged.",
            "I've cleared that movie from memory. Your existing requests are unchanged.",
        ])
        self.api.delete_request.assert_not_called()
        self.api.request_movie.assert_not_called()

    def test_request_preserves_durable_outcome_and_duplicate_suppression(self):
        result = self.run_tool("request_movie", title="Dune")
        self.assertEqual(result["action"], "movie_requested")
        self.assertEqual(result["listen_for_seconds"], 0)
        self.assertTrue(self.store.request_record("pi", 1)["confirmed"])
        context = self.tools.context("pi")
        self.assertEqual(context.remembered_movie.media_id, 1)
        self.assertEqual(context.last_requested_movie.media_id, 1)
        self.run_tool("request_movie", title="Dune")
        self.api.request_movie.assert_called_once_with(1)
        fresh = MediaTools(MovieCommands(self.api, store=EventStore(self.path)))
        self.assertEqual(fresh.context("pi").last_requested_movie.media_id, 1)
        self.assertIsNone(fresh.context("other").remembered_movie)

    def test_availability_only_offers_and_confirmation_submits_once(self):
        result = self.run_tool("check_availability", title="Dune")
        self.assertEqual(result["action"], "movie_confirmation")
        token = self.tools.context("pi").pending.pending_id
        self.api.request_movie.assert_not_called()
        self.assertEqual(self.answer()["action"], "movie_requested")
        stale = self.run_tool("respond_to_pending", pending_id=token, decision="confirm")
        self.assertFalse(stale["success"])
        self.api.request_movie.assert_called_once_with(1)

    def test_available_movie_does_not_submit(self):
        self.api.get_movie.side_effect = lambda _: replace(DUNE, state="available")
        self.assertEqual(self.run_tool("check_availability", title="Dune")["action"], "movie_available")
        self.api.request_movie.assert_not_called()
        self.assertIsNone(self.tools.context("pi").pending)

    def test_missing_title_and_ambiguous_candidate_selection(self):
        self.assertEqual(self.run_tool("request_movie")["action"], "movie_title_needed")
        self.api.search_movies.side_effect = None
        self.api.search_movies.return_value = [DUNE, OLD]
        self.answer("title", title="Dune")
        context = self.tools.context("pi").pending
        self.assertEqual([movie.media_id for movie in context.candidates], [2, 1])
        self.assertNotIn("confirm", context.permitted_decisions)
        self.assertFalse(self.run_tool("respond_to_pending", pending_id=context.pending_id,
                                       decision="select", media_id=999)["success"])
        self.api.request_movie.assert_not_called()
        self.answer("select", media_id=2)
        self.api.request_movie.assert_called_once_with(2)

    def test_status_choices_and_corrections_stay_read_only(self):
        self.api.search_movies.side_effect = None
        self.api.search_movies.return_value = [DUNE, OLD]
        self.run_tool("movie_status", title="Dune")
        self.answer("select", media_id=1)
        self.api.search_movies.return_value = [ALIEN]
        self.run_tool("correct_movie", title="Alien")
        self.run_tool("movie_status")
        self.api.request_movie.assert_not_called()
        self.assertEqual(self.store.remembered_movie("pi"), ALIEN)

    def test_correction_after_request_requires_new_confirmation(self):
        self.run_tool("request_movie", title="Dune")
        result = self.run_tool("correct_movie", title="Alien")
        self.assertEqual(result["action"], "movie_confirmation")
        self.assertIn("Earlier requests are unchanged", result["response"])
        self.api.request_movie.assert_called_once_with(1)
        self.answer()
        self.assertEqual([call.args[0] for call in self.api.request_movie.call_args_list], [1, 3])

    def test_rejected_offer_and_title_correction_have_fresh_tokens(self):
        self.run_tool("check_availability", title="Dune")
        token = self.tools.context("pi").pending.pending_id
        self.answer("reject")
        self.assertNotEqual(self.tools.context("pi").pending.pending_id, token)
        self.answer("title", title="Alien")
        self.assertEqual(self.tools.context("pi").pending.candidates[0].media_id, 3)
        self.api.request_movie.assert_not_called()

    def test_stale_expired_and_cross_client_tokens_cannot_act(self):
        self.run_tool("check_availability", title="Dune")
        token = self.tools.context("pi").pending.pending_id
        self.run_tool("check_availability", client="other", title="Dune")
        self.assertFalse(self.run_tool("respond_to_pending", client="other", pending_id=token, decision="confirm")["success"])
        self.assertFalse(self.run_tool("correct_movie", pending_id="wrong", title="Alien")["success"])
        self.now += 120
        self.assertIsNone(self.tools.context("pi").pending)
        self.assertFalse(self.run_tool("respond_to_pending", pending_id=token, decision="confirm")["success"])
        self.assertFalse(self.run_tool("correct_movie", pending_id=token, title="Alien")["success"])
        self.api.request_movie.assert_not_called()

    def test_legacy_pending_can_be_resumed_without_parser(self):
        self.flow.handle("I want to watch Dune", "pi")
        self.answer()
        self.api.request_movie.assert_called_once_with(1)

    def test_typed_pending_can_be_resumed_by_degraded_legacy(self):
        self.run_tool("check_availability", title="Dune")
        self.assertEqual(self.flow.handle("yes", "pi")["action"], "movie_requested")
        self.api.request_movie.assert_called_once_with(1)

    def test_conversation_end_and_forget_do_not_cancel_downloads(self):
        self.run_tool("request_movie", title="Dune")
        self.run_tool("check_availability", title="Alien")
        self.run_tool("end_conversation")
        self.assertIsNone(self.tools.context("pi").pending)
        self.assertEqual(self.store.remembered_movie("pi"), ALIEN)
        self.run_tool("end_conversation", forget=True)
        self.assertIsNone(self.store.remembered_movie("pi"))
        self.assertEqual(self.run_tool("movie_status", reference="last_requested")["action"], "movie_status")
        self.assertTrue(self.store.request_record("pi", 1)["confirmed"])
        self.api.delete_request.assert_not_called()

    def test_cancel_still_requires_owned_preview_and_separate_confirmation(self):
        self.run_tool("request_movie", title="Dune")
        target = {"media_id": 1, "request_id": 44}
        self.flow.cancellations = Mock()
        self.flow.cancellations.preview.return_value = target
        self.flow.cancellations.execute.return_value = {"action": "movie_download_cancelled", "response": "Cancelled.", "success": True}
        result = self.run_tool("cancel_movie", reference="last_requested")
        self.assertEqual(result["action"], "movie_cancellation_confirmation")
        self.flow.cancellations.preview.assert_called_once_with(DUNE, "pi")
        self.flow.cancellations.execute.assert_not_called()
        snapshot = self.tools.context("pi").pending
        self.assertNotIn("request_id", snapshot.model_dump())
        self.assertFalse(self.run_tool("respond_to_pending", pending_id=snapshot.pending_id,
                                       decision="select", media_id=1)["success"])
        self.answer()
        self.flow.cancellations.execute.assert_called_once_with(target, "pi")
        self.assertNotIn(1, self.flow.submitted)
        self.assertEqual(self.flow.recent_intent["pi"], "status")
        self.assertFalse(self.run_tool("respond_to_pending", pending_id=snapshot.pending_id, decision="confirm")["success"])
        self.flow.cancellations.execute.assert_called_once()

    def test_unowned_cancellation_is_refused_without_mutation(self):
        self.store.remember_movie("other", DUNE)
        self.assertEqual(self.run_tool("cancel_movie", client="other")["action"], "movie_cancellation_refused")
        self.api.delete_request.assert_not_called()
        self.api.request_movie.assert_not_called()

    def test_uncertain_request_is_not_repeated(self):
        self.api.request_movie.side_effect = RequestUncertain("Uncertain request")
        self.assertFalse(self.run_tool("request_movie", title="Dune")["success"])
        self.assertFalse(self.run_tool("request_movie", title="Dune")["success"])
        self.api.request_movie.assert_called_once()
        self.assertFalse(self.store.request_record("pi", 1)["confirmed"])

    def test_models_reject_injected_targets_and_inconsistent_decisions(self):
        for name, arguments in [
            ("request_movie", {"title": " "}), ("movie_status", {"media_id": 1}),
            ("cancel_movie", {"title": "Dune", "request_id": 44}),
            ("cancel_movie", {"title": "Dune", "reference": "last_requested"}),
            ("respond_to_pending", {"pending_id": "x", "decision": "confirm", "media_id": 1}),
            ("respond_to_pending", {"pending_id": "x", "decision": "select"}),
            ("respond_to_pending", {"pending_id": "x", "decision": "title"}),
            ("respond_to_pending", {"pending_id": "x", "decision": "select", "media_id": True}),
        ]:
            with self.subTest(name=name, arguments=arguments), self.assertRaises(ToolValidationError):
                self.registry.validate_intent(ToolIntent(type="tool", tool="media." + name, arguments=arguments))
        self.api.request_movie.assert_not_called()

    def test_two_missing_title_misses_close_dialogue_without_extending_expiry(self):
        self.api.search_movies.side_effect = lambda _: []
        self.run_tool("request_movie")
        self.now += 30
        first = self.answer("title", title="Missing movie")
        self.assertEqual(first["action"], "movie_not_found")
        self.assertEqual(self.tools.context("pi").pending.expires_in, 90)
        second = self.answer("title", title="Still missing")
        self.assertEqual(second["action"], "movie_conversation_ended")
        self.assertEqual(second["listen_for_seconds"], 0)
        self.assertIsNone(self.tools.context("pi").pending)
        self.api.request_movie.assert_not_called()

    def test_adapter_source_has_no_parser_or_synthetic_utterance_escape_hatch(self):
        import features.media.tools as module
        tree = ast.parse(inspect.getsource(module))
        forbidden = {"handle", "_handle", "parse_intent", "_resolve", "polite", "normalize",
                     "_dispatch_legacy", "_unsafe_answer", "_parse_title_year", "Intent"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                self.assertNotIn(node.attr, forbidden)
            if isinstance(node, ast.Name):
                self.assertNotIn(node.id, forbidden)
            if isinstance(node, ast.ImportFrom):
                self.assertNotEqual(node.module, "features.media.movie_commands")
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                self.assertNotIn(node.value, forbidden | {"yes", "do it", "download it", "that's the one",
                                                         "the first one", "the second one", "the third one"})

    def test_inventory_matches_approved_contract_without_aliases(self):
        self.assertEqual({tool.name for tool in self.registry.list_executable_tools()}, {
            "media.request_movie", "media.check_availability", "media.movie_status", "media.cancel_movie",
            "media.respond_to_pending", "media.correct_movie", "media.end_conversation"})
        for old in ("movie_availability", "respond_pending", "forget_movie", "last_requested_movie"):
            with self.assertRaises(ToolValidationError):
                self.registry.validate_intent(ToolIntent(type="tool", tool="media." + old, arguments={}))

    def test_adapters_forward_only_structured_fields_and_backend_identity(self):
        for name, method, arguments in [
            ("request_movie", "request_movie", {"title": "The First One", "year": "2021"}),
            ("check_availability", "check_availability", {}),
            ("movie_status", "movie_status", {"title": "Dune"}),
            ("cancel_movie", "begin_movie_cancellation", {"reference": "last_requested"}),
            ("respond_to_pending", "respond_to_pending", {"pending_id": "token", "decision": "select", "media_id": 2}),
            ("correct_movie", "correct_movie", {"title": "Alien", "year": "1979", "pending_id": "token"}),
            ("end_conversation", "end_conversation", {"forget": True}),
        ]:
            with self.subTest(name=name), patch.object(self.flow, method, return_value={
                    "success": True, "action": "test", "response": "Backend reply."}) as operation:
                self.run_tool(name, client="trusted-client", **arguments)
                operation.assert_called_once_with("trusted-client", **arguments)

    def test_target_default_is_resolved_after_wire_validation(self):
        self.store.confirm_request(self.store.prepare_request(DUNE, "pi"), 44)
        self.flow.cancellations = Mock()
        self.flow.cancellations.preview.return_value = {"media_id": 1}
        for arguments, expected in [
            ({}, ALIEN), ({"reference": "remembered"}, ALIEN),
            ({"reference": "last_requested"}, DUNE), ({"title": "Dune"}, DUNE),
            ({"title": "Dune", "year": "2021"}, DUNE),
        ]:
            for name in ("movie_status", "cancel_movie"):
                with self.subTest(name=name, arguments=arguments):
                    self.store.remember_movie("pi", ALIEN)
                    self.api.get_movie.reset_mock()
                    self.flow.cancellations.preview.reset_mock()
                    self.flow.cancellations.status.return_value = None
                    result = self.run_tool(name, **arguments)
                    self.assertTrue(result["success"])
                    if name == "movie_status":
                        self.api.get_movie.assert_called_once_with(expected.media_id)
                    else:
                        self.flow.cancellations.preview.assert_called_once_with(expected, "pi")
        self.api.request_movie.assert_not_called()
        self.flow.cancellations.execute.assert_not_called()

    def test_explicit_reference_conflicts_and_year_without_title_are_rejected(self):
        for name in ("movie_status", "cancel_movie"):
            for arguments in [
                {"title": "Dune", "reference": reference} for reference in (None, "remembered", "last_requested")
            ] + [{"year": "2021"}, {"year": "2021", "reference": "remembered"}]:
                with self.subTest(name=name, arguments=arguments), self.assertRaises(ToolValidationError):
                    self.registry.validate_intent(ToolIntent(type="tool", tool="media." + name, arguments=arguments))
        self.api.search_movies.assert_not_called()
        self.api.request_movie.assert_not_called()

    def test_years_are_strict_strings_for_every_title_accepting_tool(self):
        for name in ("request_movie", "check_availability", "movie_status", "cancel_movie",
                     "correct_movie", "respond_to_pending"):
            extra = {"pending_id": "token", "decision": "title"} if name == "respond_to_pending" else {}
            for year in (2021, 2021.0, True, "021", "20211", "2021\n", " 2021", "２０２１", "1799", "2100"):
                with self.subTest(name=name, year=year), self.assertRaises(ToolValidationError):
                    self.registry.validate_intent(ToolIntent(type="tool", tool="media." + name,
                                                            arguments={"title": "Dune", "year": year, **extra}))
            valid = self.registry.validate_intent(ToolIntent(type="tool", tool="media." + name,
                                                             arguments={"title": "Dune", "year": "2021", **extra}))
            self.assertIs(type(valid.arguments.year), str)
            self.assertEqual(valid.arguments.year, "2021")

    def test_title_year_filters_candidates_without_parsing_title_text(self):
        self.api.search_movies.side_effect = None
        self.api.search_movies.return_value = [DUNE, OLD]
        result = self.run_tool("request_movie", title="Dune", year="1984")
        self.assertEqual(result["media_id"], OLD.media_id)
        self.api.request_movie.assert_called_once_with(OLD.media_id)
        self.api.search_movies.assert_called_once_with("dune")

    def test_number_in_structured_title_is_not_extracted_as_year(self):
        movie = Movie(4, "Blade Runner 2049", "2017")
        self.api.search_movies.side_effect = None
        self.api.search_movies.return_value = [movie]
        self.api.get_movie.side_effect = None
        self.api.get_movie.return_value = movie
        self.run_tool("check_availability", title=movie.title)
        self.api.search_movies.assert_called_once_with("blade runner 2049")
        self.assertEqual(self.tools.context("pi").pending.candidates[0].year, "2017")
        self.api.request_movie.assert_not_called()

    def test_year_correction_and_title_reply_preserve_the_pending_intent(self):
        self.api.search_movies.side_effect = None
        self.api.search_movies.return_value = [DUNE, OLD]
        self.run_tool("movie_status")
        self.answer("title", title="Dune", year="1984")
        self.api.get_movie.assert_called_with(OLD.media_id)
        self.run_tool("movie_status", title="Dune")
        token = self.tools.context("pi").pending.pending_id
        self.run_tool("correct_movie", pending_id=token, title="Dune", year="2021")
        self.api.get_movie.assert_called_with(DUNE.media_id)
        self.api.request_movie.assert_not_called()

    def test_legacy_dispatch_calls_public_operations_with_parsed_fields(self):
        with patch.object(self.flow, "request_movie", wraps=self.flow.request_movie) as operation:
            self.flow.handle("Get Dune from 2021", "pi")
            operation.assert_called_once_with("pi", title="dune", year="2021")
        self.api.search_movies.side_effect = None
        self.api.search_movies.return_value = [DUNE, OLD]
        self.flow.handle("Is Dune ready?", "pi")
        token = self.tools.context("pi").pending.pending_id
        with patch.object(self.flow, "select_pending_movie", wraps=self.flow.select_pending_movie) as operation:
            self.flow.handle("the second one", "pi")
            operation.assert_called_once_with("pi", pending_id=token, media_id=DUNE.media_id)

    def test_legacy_download_affirmation_cannot_confirm_cancellation(self):
        self.flow.cancellations = Mock()
        self.flow.cancellations.preview.return_value = {"media_id": 1}
        self.flow.cancellations.execute.return_value = {
            "success": True, "action": "movie_download_cancelled", "response": "Cancelled."}
        self.store.remember_movie("pi", DUNE)
        self.run_tool("cancel_movie")
        token = self.tools.context("pi").pending.pending_id
        self.flow.handle("download it", "pi")
        self.assertEqual(self.tools.context("pi").pending.pending_id, token)
        self.flow.cancellations.execute.assert_not_called()
        self.flow.handle("yes", "pi")
        self.flow.cancellations.execute.assert_called_once_with({"media_id": 1}, "pi")

    def test_operation_scope_finalizes_once_and_resets_after_failure(self):
        from integrations.overseerr_client import upstream_seconds
        def get_movie(identity):
            upstream_seconds.set(upstream_seconds.get() + 0.25)
            return DUNE
        self.api.get_movie.side_effect = get_movie
        result = self.flow.handle("Get Dune", "pi")
        self.assertEqual(result["timings"]["upstream"], 0.25)
        self.assertIsNone(self.flow._operation_owner.active)
        self.api.search_movies.side_effect = RuntimeError("offline")
        self.assertFalse(self.flow.request_movie("pi", title="Alien")["success"])
        self.assertIsNone(self.flow._operation_owner.active)
        # The same thread can acquire the client lock again after the exception.
        self.assertEqual(self.flow.end_conversation("pi")["action"], "movie_cancelled")

    def test_snapshot_uses_public_boundary_and_is_detached(self):
        self.run_tool("check_availability", title="Dune")
        with patch.object(self.flow, "media_context", wraps=self.flow.media_context) as operation:
            snapshot = self.tools.context("pi")
            operation.assert_called_once_with("pi")
        snapshot.pending.candidates[0].title = "Changed snapshot"
        snapshot.pending.permitted_decisions.clear()
        fresh = self.tools.context("pi")
        self.assertEqual(fresh.pending.candidates[0].title, "Dune")
        self.assertIn("confirm", fresh.pending.permitted_decisions)
