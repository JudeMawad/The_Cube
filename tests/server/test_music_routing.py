"""Backend voice/registry integration, with fake Spotify; music never reaches AI interpretation."""
import asyncio
import importlib.util
import json
from time import perf_counter
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, skipUnless
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

AVAILABLE = all(importlib.util.find_spec(name) for name in ("pydantic", "starlette"))
if AVAILABLE:
    from core.ai_turn import process_turn
    from core.client_state import ClientStateManager
    from core.local_commands import match, RULES, music_decision
    from core.schemas import ConversationResult, ToolIntent
    from core.tool_registry import ToolContext, ToolRegistry, ToolValidationError
    from features.music.commands import MusicCommands
    from features.music.controller import MusicController
    from features.music.tools import register_tools
    from core.cancellation import Scope, current_scope


@skipUnless(AVAILABLE, "Requires the backend Pydantic/Starlette environment")
class MusicRoutingTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.api = Mock()
        self.api.devices.return_value = [{"id": "cube", "name": "Cube", "is_restricted": False}]
        self.api.playback.return_value = None
        self.api.search.return_value = [{"uri": "spotify:track:" + "a" * 22,
            "name": "Don't Stop Me Now", "artists": ["Queen"]}]
        from core.cancellation import check
        self.controller = MusicController(self.api, check=check)
        self.service = Mock()
        self.service.settings.return_value = SimpleNamespace(cube_id="cube")
        self.service.get.return_value = self.controller
        self.registry = ToolRegistry()
        register_tools(self.registry, commands=MusicCommands(self.service, check=check))
        self.states = ClientStateManager()
        self.legacy = AsyncMock(return_value={"response": None, "success": True, "type": "unhandled"})
        self.context = ToolContext("cube", "b" * 32, "c" * 32)

    async def turn(self, text, *, ai=None, context=None):
        return await process_turn({"text": text, "processing_time": .01}, "cube", voice_started=perf_counter(),
            ai_client=ai, client_states=self.states, legacy=self.legacy, tool_registry=self.registry,
            tool_context=context or self.context, local_intent=match)

    def ai(self, result):
        return SimpleNamespace(config=SimpleNamespace(url="http://unused.invalid"), process_remote=AsyncMock(return_value=result))

    async def test_exact_controls_work_offline_without_ai(self):
        phrases = ("play music", "resume music", "resume", "pause music", "pause", "stop music", "next", "previous", "Spotify volume 20", "music volume 40")
        ai = self.ai(None)
        ai.process_remote.side_effect = AssertionError("Fast path must not reach AI")
        for text in phrases:
            self.context = ToolContext("cube", "b" * 32, uuid4().hex)
            result = await self.turn(text, ai=ai)
            self.assertTrue(result["success"], text)
            self.assertEqual(result["processing_source"], "local_fast")
        self.api.control.assert_called_with("volume", "cube", 40)
        self.legacy.assert_not_called()

    async def test_volume_routes_and_numeric_clarification(self):
        self.assertEqual(match("volume 40").tool, "cube.set_volume")
        self.assertEqual(match("music volume 40").tool, "music.set_volume")
        for text in ("music volume -1", "spotify volume 101", "music volume 4.5", "music volume 40 and 50"):
            result = await self.turn(text)
            self.assertEqual(result["processing_source"], "local_clarification")
            self.assertIn("0 to 100", result["response"])
        self.api.control.assert_not_called()

    async def test_all_music_catalog_fast_examples_execute(self):
        for (feature, operation), rule in RULES.items():
            if feature != "music":
                continue
            phrases = list(rule.get("exact", [])) if rule.get("fast_path_exact") else []
            phrases += [example for pattern in rule.get("patterns", []) if pattern.get("fast_path")
                        for example in pattern.get("examples", [])]
            for phrase in phrases:
                with self.subTest(phrase=phrase):
                    self.context = ToolContext("cube", "b" * 32, uuid4().hex)
                    self.assertEqual(match(phrase).tool, "music." + operation)
                    candidate = match(phrase)
                    if operation == "play_request":
                        args = candidate.arguments
                        kind = args["kind"]
                        entry = {"uri": "spotify:" + kind + ":" + "a" * 22,
                                 "name": args["query"], "artists": [args.get("artist") or "Artist"]}
                        self.api.search.return_value = [entry]
                        self.api.playlists.return_value = [entry]
                    result = await self.turn(phrase)
                    self.assertTrue(result["success"])
                    self.assertEqual(result["listen_for_seconds"], 0)

    async def test_structured_tracks_work_disabled_and_unreachable(self):
        offline = self.ai(None)
        offline.process_remote.side_effect = AssertionError("Music called AI")
        for ai in (None, offline):
            for title, artist in (("Blinding Lights", "The Weeknd"), ("Don't Stop Me Now", "Queen"),
                                  ("Mad About You", "Hooverphonic")):
                with self.subTest(title=title, ai=ai is not None):
                    self.context = ToolContext("cube", "b" * 32, uuid4().hex)
                    self.api.search.return_value = [{"name": title, "artists": [artist],
                        "uri": "spotify:track:" + "a" * 22}]
                    result = await self.turn(f'Please play "{title}" by {artist}.', ai=ai)
                    self.assertTrue(result["success"])
                    self.assertEqual(result["response"], "Playing.")
                    self.assertEqual(result["processing_source"], "local_fast")
                    self.assertEqual(result["processing_response_source"], "backend")
                    self.api.play.assert_called_with("cube", "spotify:track:" + "a" * 22)
        offline.process_remote.assert_not_called()
        self.legacy.assert_not_called()

    async def test_artist_playlist_and_personal_work_disabled_and_unreachable(self):
        offline = self.ai(None)
        offline.process_remote.side_effect = AssertionError("Music called AI")
        for ai in (None, offline):
            for phrase, kind, name in (("play artist Pink Floyd", "artist", "Pink Floyd"),
                                      ("play playlist Evening Chill", "playlist", "Evening Chill"),
                                      ("play my Discover Weekly", "playlist", "Discover Weekly")):
                with self.subTest(phrase=phrase, ai=ai is not None):
                    self.context = ToolContext("cube", "b" * 32, uuid4().hex)
                    uri = "spotify:" + kind + ":" + "a" * 22
                    self.api.search.return_value = [{"name": name, "uri": uri}]
                    self.api.playlists.return_value = [{"name": name, "uri": uri}]
                    result = await self.turn(phrase, ai=ai)
                    self.assertEqual(result["response"], "Playing.")
                    self.assertEqual(result["processing_source"], "local_fast")
                    self.api.play.assert_called_with("cube", uri)
        offline.process_remote.assert_not_called()
        self.legacy.assert_not_called()

    async def test_guards_decline_even_hallucinated_music_tools(self):
        ai = self.ai(ToolIntent(type="tool", tool="music.play_request", arguments={"query": "Queen", "kind": "artist"}))
        for text in ("don't play Queen", "why play Queen", "can you not play Queen", 'what does "play Queen" mean?',
                     "play Queen but don't start it", "don't go next", "don't set music volume 40"):
            result = await self.turn(text, ai=ai)
            self.assertFalse(result["success"], text)
        self.api.play.assert_not_called()
        self.api.control.assert_not_called()

    async def test_ai_cannot_execute_any_local_music_tool(self):
        for tool, args in (("music.set_volume", {"percent": 100}),
                           ("music.play_request", {"query": "Queen", "kind": "artist"}),
                           ("music.next", {})):
            ai = self.ai(ToolIntent(type="tool", tool=tool, arguments=args))
            result = await self.turn("hello", ai=ai)
            self.assertFalse(result["success"])
            self.assertEqual(ai.process_remote.call_args.args[0].tools, [])
        self.api.control.assert_not_called()
        self.api.play.assert_not_called()
        self.api.search.assert_not_called()

    async def test_nonmusic_discussion_does_not_advertise_music_tools(self):
        ai = self.ai(ConversationResult(type="conversation", response="Hello."))
        await self.turn("hello", ai=ai)
        self.assertFalse(any(tool.name.startswith("music.") for tool in ai.process_remote.call_args.args[0].tools))

    async def test_search_results_not_in_history_or_ai_request(self):
        # Result data never travels through model context/history. Only the
        # user's own query and the backend's generic reply may be retained.
        self.api.search.return_value[0]["album"] = "METADATA_SENTINEL"
        ai = self.ai(ToolIntent(type="tool", tool="music.play_request",
                    arguments={"query": "Don't Stop Me Now", "kind": "track"}))
        await self.turn("play track Don't Stop Me Now", ai=ai)
        ai.process_remote.return_value = ConversationResult(type="conversation", response="Hello.")
        await self.turn("hello", ai=ai)
        serialized = ai.process_remote.call_args.args[0].model_dump_json()
        self.assertNotIn("METADATA_SENTINEL", serialized)
        self.assertNotIn("spotify:track:", serialized)
        self.assertEqual(ai.process_remote.call_args.args[0].history[-1].content, "Playing.")

    async def test_unauthenticated_context_is_rejected(self):
        result = await self.turn("next", context=ToolContext("cube"))
        self.assertFalse(result["success"])
        self.service.get.assert_not_called()

    async def test_strict_tool_schema_forbids_model_ids_and_boolean_volume(self):
        for name, args in (("music.set_volume", {"percent": True}),
                           ("music.play_request", {"query": "x", "kind": "track", "uri": "spotify:track:x"}),
                           ("music.play_request", {"query": "x", "kind": "artist", "personal": True}),
                           ("music.next", {"request_id": "a" * 32})):
            with self.assertRaises(ToolValidationError):
                self.registry.validate_intent(ToolIntent(type="tool", tool=name, arguments=args))

    async def test_unsupported_music_is_backend_owned_without_ai_or_false_history(self):
        ai = self.ai(ConversationResult(type="conversation", response="Starting it now."))
        for phrase in ("play something relaxing", "play something from the 80s",
                       "put on something calm", "play something similar to this",
                       "play some music from the 80s", "put on some music"):
            result = await self.turn(phrase, ai=ai)
            self.assertEqual(result["processing_source"], "local_clarification")
            self.assertFalse(result["success"])
            self.assertIn("play track", result["response"])
        ai.process_remote.assert_not_called()
        async with self.states.turn("cube") as turn:
            self.assertNotIn("Starting it now.", str(turn.history))
        self.legacy.assert_not_called()
        self.api.play.assert_not_called()
        self.api.search.assert_not_called()

    async def test_review_refusals_never_search_mutate_or_call_ai(self):
        ai = self.ai(ConversationResult(type="conversation", response="Playing."))
        for phrase in ("play track Blinding Lights, actually don't",
                       "play track Blinding Lights only if I ask later",
                       "play track Blinding Lights but do not start it",
                       "play artist Queen unless I change my mind"):
            with self.subTest(phrase=phrase):
                self.assertIsNone(match(phrase))
                result = await self.turn(phrase, ai=ai)
                self.assertFalse(result["success"])
                self.assertEqual(result["processing_source"], "local_clarification")
                self.assertEqual(result["processing_response_source"], "backend")
        ai.process_remote.assert_not_called()
        self.api.search.assert_not_called()
        self.api.playlists.assert_not_called()
        self.api.play.assert_not_called()
        self.api.control.assert_not_called()

    async def test_music_decline_history_is_truthful_on_followup(self):
        ai = self.ai(ConversationResult(type="conversation", response="Starting it now."))
        result = await self.turn("play something relaxing, not too loud", ai=ai)
        ai.process_remote.assert_not_called()
        ai.process_remote.return_value = ConversationResult(type="conversation", response="Please give a complete command.")
        await self.turn("Yeah", ai=ai)
        history = ai.process_remote.call_args.args[0].history
        self.assertEqual(history[-1].content, result["response"])
        self.assertNotIn("Starting it now.", str(history))
        self.api.play.assert_not_called()

    async def test_nonmusic_and_ambiguous_names_keep_normal_ai_routing(self):
        ai = self.ai(ConversationResult(type="conversation", response="Tell me more."))
        for phrase in ("put on the lights", "play chess with me", "play Queen",
                       "play chess with me, but don't let me win", "play chess only if you can",
                       "play The Weeknd", "Why do people like music?", "hello"):
            with self.subTest(phrase=phrase):
                ai.process_remote.reset_mock()
                result = await self.turn(phrase, ai=ai)
                self.assertEqual(result["processing_source"], "ai_conversation")
                ai.process_remote.assert_awaited_once()
        self.api.search.assert_not_called()
        self.api.play.assert_not_called()

    async def test_my_title_precedes_implicit_personal_playlist(self):
        self.api.search.return_value = [{"name": "My Way", "artists": ["Frank Sinatra"],
                                        "uri": "spotify:track:" + "a" * 22}]
        ai = self.ai(None)
        result = await self.turn("play My Way by Frank Sinatra", ai=ai)
        self.assertEqual(result["response"], "Playing.")
        self.assertEqual(match("play My Way by Frank Sinatra").arguments,
                         {"query": "my way", "artist": "frank sinatra", "kind": "track",
                          "personal": False, "selection": "exact"})
        self.api.play.assert_called_once_with("cube", "spotify:track:" + "a" * 22)
        self.api.playlists.assert_not_called()
        ai.process_remote.assert_not_called()
        for phrase, query, personal in (("play my Discover Weekly", "discover weekly", True),
                ("play playlist Songs by Friends", "songs by friends", False)):
            args = match(phrase).arguments
            self.assertEqual((args["kind"], args["query"], args["personal"]), ("playlist", query, personal))

    async def test_refusals_conditions_and_hypotheses_stop_before_all_external_calls(self):
        ai = self.ai(ConversationResult(type="conversation", response="Playing."))
        phrases = (
            "play track Blinding Lights, not now",
            "play track Blinding Lights, just do not start it",
            "play track Blinding Lights hypothetically",
            "play track Blinding Lights if the lights are on",
            "play Blinding Lights by The Weeknd, not now",
            "play artist Queen, wait",
            "play playlist Chill and then pause",
            "pause music, not now", "next only if I ask later",
            "don't play artist Queen", '"play artist Queen"',
            "what if you play artist Queen", "I said play artist Queen",
            "why play artist Queen?", "don't set music volume 40",
            "play something relaxing, not too loud",
        )
        for phrase in phrases:
            for variant in (phrase, phrase.upper(), "  " + phrase + "  "):
                with self.subTest(phrase=variant):
                    self.assertEqual(music_decision(variant).state, "blocked")
                    self.assertIsNone(match(variant))
                    result = await self.turn(variant, ai=ai)
                    self.assertFalse(result["success"])
                    self.assertEqual(result["processing_source"], "local_clarification")
                    self.assertEqual(result["processing_response_source"], "backend")
                    self.assertNotEqual(result["response"], "Playing.")
        ai.process_remote.assert_not_called()
        self.legacy.assert_not_called()
        self.service.get.assert_not_called()
        self.api.search.assert_not_called()
        self.api.playlists.assert_not_called()
        self.api.play.assert_not_called()
        self.api.control.assert_not_called()
        async with self.states.turn("cube") as turn:
            self.assertNotIn("Playing.", str(turn.history))

    async def test_positive_negative_word_titles_still_execute(self):
        ai = self.ai(None)
        ai.process_remote.side_effect = AssertionError("Music called AI")
        for title in ("Don't Stop Me Now", "Never Gonna Give You Up", "Imagine",
                      "I Can't Get No Satisfaction"):
            for form in ("Please play track {}.", 'play track "{}"'):
                with self.subTest(title=title, form=form):
                    self.context = ToolContext("cube", "b" * 32, uuid4().hex)
                    result = await self.turn(form.format(title), ai=ai)
                    self.assertEqual(result["response"], "Playing.")
                    self.assertEqual(result["processing_source"], "local_fast")
        self.assertEqual(self.api.play.call_count, 8)
        ai.process_remote.assert_not_called()

    async def test_command_suffix_matrix_never_reaches_search_or_ai(self):
        ai = self.ai(ConversationResult(type="conversation", response="Starting it now."))
        prefixes = ("play track Blinding Lights", "play artist Queen", "play playlist Chill",
                    "play Blinding Lights by The Weeknd")
        suffixes = (", not now", ", just do not start it", " hypothetically", " if the lights are on",
                    ", no thanks", ", just kidding", " after I ask you", " but wait", " and reboot",
                    " unless I ask", "; do not play it", " — don't", " provided that I ask", " as an example",
                    " actually no", " but maybe later")
        for prefix in prefixes:
            for suffix in suffixes:
                with self.subTest(prefix=prefix, suffix=suffix):
                    result = await self.turn(prefix + suffix, ai=ai)
                    self.assertFalse(result["success"])
                    self.assertEqual(result["processing_source"], "local_clarification")
        ai.process_remote.assert_not_called()
        self.service.get.assert_not_called()

    async def test_quoted_name_punctuation_is_literal(self):
        result = await self.turn('play track "Bye, Bye, Bye"')
        self.assertEqual(result["response"], "Playing.")
        self.api.search.assert_called_once_with("bye, bye, bye", "track", details=True)

    async def test_explicit_personal_playlist_containing_by_keeps_private_lookup(self):
        uri = "spotify:playlist:" + "a" * 22
        self.api.playlists.return_value = [{"name": "Songs by Friends", "uri": uri}]
        result = await self.turn("play playlist my Songs by Friends")
        self.assertEqual(result["response"], "Playing.")
        self.api.play.assert_called_once_with("cube", uri)
        self.api.search.assert_not_called()
        self.assertEqual(match("play my Songs by Friends").arguments["kind"], "track")

    async def test_cancelled_scope_issues_zero_music_commands(self):
        scope = Scope("a" * 32)
        scope.cancelled.set()
        token = current_scope.set(scope)
        try:
            with self.assertRaises(asyncio.CancelledError):
                await self.turn("next")
        finally:
            current_scope.reset(token)
        self.api.control.assert_not_called()

    async def test_top_track_is_first_not_exact_and_replay_does_not_search_or_mutate(self):
        self.api.search.return_value = [
            {"name": "Blinding Lights Live", "uri": "spotify:track:" + "a" * 22},
            {"name": "Blinding Lights", "uri": "spotify:track:" + "b" * 22}]
        ai = self.ai(None)
        ai.process_remote.side_effect = AssertionError("Music called AI")
        for _ in range(2):
            result = await self.turn("play track Blinding Lights", ai=ai)
            self.assertEqual(result["response"], "Playing.")
            self.assertEqual(result["processing_source"], "local_fast")
        self.api.search.assert_called_once_with("blinding lights", "track", details=True)
        self.api.play.assert_called_once_with("cube", "spotify:track:" + "a" * 22)
        ai.process_remote.assert_not_called()

    async def test_no_results_wrong_artist_and_ambiguous_playlist_never_play(self):
        for phrase, items in (("play track Missing", []),
                ("play Song by Queen", [{"name": "Song", "artists": ["Other"], "uri": "spotify:track:" + "a" * 22}]),
                ("play playlist Chill", [{"name": "Chill", "uri": "spotify:playlist:" + c * 22} for c in "ab"])):
            self.api.search.return_value = items
            self.context = ToolContext("cube", "b" * 32, uuid4().hex)
            result = await self.turn(phrase)
            self.assertFalse(result["success"])
            self.assertNotEqual(result["response"], "Playing.")
            self.assertEqual(result["processing_source"], "local_fast")
        self.api.play.assert_not_called()

    async def test_safety_stops_search_as_well_as_mutation(self):
        for phrase in ("don't play Queen", "don't play artist Queen", "why play Queen?",
                       "play Queen but don't start it", "play track Queen but don't start it",
                       "don't set music volume 40", '"play artist Queen"',
                       "what if you play artist Queen", "I said play artist Queen"):
            with self.subTest(phrase=phrase):
                self.assertIsNone(match(phrase))
                await self.turn(phrase)
        self.api.search.assert_not_called()
        self.api.playlists.assert_not_called()
        self.api.play.assert_not_called()
        self.api.control.assert_not_called()

    async def test_qualified_delimiter_ambiguity_clarifies_without_search(self):
        result = await self.turn("play Stand by Me by Ben E King")
        self.assertEqual(result["processing_source"], "local_clarification")
        self.api.search.assert_not_called()

    async def test_alias_and_personal_scope_failure_use_existing_resolver(self):
        from integrations.spotify.errors import SpotifyError
        uri = "spotify:playlist:" + "a" * 22
        with patch("features.music.resolution.read_aliases", return_value={"evening chill": uri}):
            result = await self.turn("play playlist Evening Chill")
        self.assertEqual(result["response"], "Playing.")
        self.api.play.assert_called_once_with("cube", uri)
        self.api.search.assert_not_called()
        self.api.play.reset_mock()
        self.api.playlists.side_effect = SpotifyError("playlist_scope_required")
        self.context = ToolContext("cube", "b" * 32, uuid4().hex)
        result = await self.turn("play my Discover Weekly")
        self.assertFalse(result["success"])
        self.assertIn("playlist access", result["response"])
        self.api.play.assert_not_called()
        self.api.search.assert_not_called()

    async def test_failed_named_playback_replay_never_mutates_twice(self):
        from integrations.spotify.errors import SpotifyError
        self.api.play.side_effect = SpotifyError("restricted")
        for _ in range(2):
            result = await self.turn("play track Don't Stop Me Now")
            self.assertFalse(result["success"])
            self.assertNotEqual(result["response"], "Playing.")
        self.api.search.assert_called_once()
        self.api.play.assert_called_once()

    async def test_named_music_requires_trusted_context_before_search(self):
        for context in (ToolContext("cube"), ToolContext("other", "b" * 32, "c" * 32)):
            result = await self.turn("play artist Pink Floyd", context=context)
            self.assertFalse(result["success"])
        self.service.get.assert_not_called()
        self.api.search.assert_not_called()
