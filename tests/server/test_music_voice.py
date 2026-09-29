"""Offline deterministic search and trusted voice execution; no Spotify account."""
import asyncio
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock

import httpx

from features.music.commands import MusicCommands
from features.music.controller import MusicController
from features.music.resolution import MusicResolver, read_aliases
from integrations.spotify.api import SpotifyWebApi
from integrations.spotify.errors import SpotifyError
from integrations.spotify.storage import write_private


def item(kind="track", name="Don't Stop Me Now", artist="Queen", identifier="a"):
    result = {"name": name, "uri": f"spotify:{kind}:" + identifier * 22}
    if kind == "track":
        result["artists"] = [artist]
    return result


class ResolutionTests(TestCase):
    def setUp(self):
        self.api = Mock()
        self.api.search.return_value = [item()]
        self.now = 100
        self.resolver = MusicResolver(self.api, clock=lambda: self.now)

    def test_top_track_selection_is_explicit_and_validated(self):
        self.api.search.return_value = [item(name="Different top title"), item(identifier="b")]
        self.assertEqual(self.resolver.resolve(query="Imperfect transcription", kind="track", selection="top"), item()["uri"])
        self.api.search.assert_called_once_with("Imperfect transcription", "track", details=True)
        self.api.search.return_value = []
        with self.assertRaisesRegex(SpotifyError, "not_found"):
            self.resolver.resolve(query="Missing", kind="track", selection="top")
        self.api.search.return_value = [item(kind="artist")]
        with self.assertRaisesRegex(SpotifyError, "invalid_request"):
            self.resolver.resolve(query="Wrong URI", kind="track", selection="top")
        for fields in ({"kind": "artist"}, {"kind": "playlist"}, {"kind": "track", "artist": "Queen"},
                       {"kind": "track", "personal": True}):
            with self.assertRaisesRegex(SpotifyError, "invalid_request"):
                self.resolver.resolve(query="x", selection="top", **fields)

    def test_track_and_artist_qualified_query(self):
        uri = self.resolver.resolve(query="Don't Stop Me Now", kind="track", artist="Queen")
        self.assertEqual(uri, item()["uri"])
        self.api.search.assert_called_once_with('track:"Don\'t Stop Me Now" artist:"Queen"', "track", details=True)
        self.api.play.assert_not_called()

    def test_artist_and_playlist_exact_match(self):
        for kind, name in (("artist", "The Weeknd"), ("playlist", "Chill")):
            self.api.search.return_value = [item(kind, name)]
            self.assertEqual(self.resolver.resolve(query=name, kind=kind), item(kind, name)["uri"])

    def test_ambiguity_does_not_pick_first_different_artist(self):
        self.api.search.return_value = [item(), item(artist="Cover Artist", identifier="b")]
        with self.assertRaisesRegex(SpotifyError, "ambiguous"):
            self.resolver.resolve(query="Don't Stop Me Now", kind="track")
        self.assertEqual(self.resolver.resolve(query="Don't Stop Me Now", kind="track", artist="Queen"), item()["uri"])

    def test_same_title_performers_release_duplicates_are_equivalent(self):
        self.api.search.return_value = [item(), item(identifier="b")]
        self.assertEqual(self.resolver.resolve(query="Don't Stop Me Now", kind="track"), item()["uri"])

    def test_auto_disambiguates_type(self):
        self.api.search.side_effect = lambda _, kind, **kw: [item(kind, "Example")]
        with self.assertRaisesRegex(SpotifyError, "ambiguous"):
            self.resolver.resolve(query="Example", kind="auto")
        self.assertEqual(self.api.search.call_count, 2)

    def test_fuzzy_title_wrong_artist_and_empty_do_not_play(self):
        for results, artist in (([], None), ([item(name="Don't Stop Me Now Live")], None), ([item()], "Other")):
            self.api.search.return_value = results
            with self.assertRaisesRegex(SpotifyError, "not_found"):
                self.resolver.resolve(query="Don't Stop Me Now", kind="track", artist=artist)
        self.api.play.assert_not_called()

    def test_private_playlist_no_public_fallback_and_cached(self):
        self.api.playlists.return_value = [item("playlist", "Discover Weekly")]
        for _ in range(2):
            self.assertEqual(self.resolver.resolve(query="my Discover Weekly", kind="auto"), item("playlist")["uri"])
        self.api.playlists.assert_called_once()
        self.api.search.assert_not_called()
        self.now += 121
        self.api.playlists.return_value = []
        with self.assertRaisesRegex(SpotifyError, "playlist_not_found"):
            self.resolver.resolve(query="Discover Weekly", kind="playlist")
        self.api.search.assert_not_called()

    def test_duplicate_playlist_requires_alias(self):
        self.api.search.return_value = [item("playlist", "Chill"), item("playlist", "Chill", identifier="b")]
        with self.assertRaisesRegex(SpotifyError, "ambiguous"):
            self.resolver.resolve(query="Chill", kind="playlist")

    def test_alias_works_without_library_scope_and_never_captures_track(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "playlist-aliases.json"
            write_private(path, {"Evening Chill": item("playlist")["uri"]})
            resolver = MusicResolver(self.api, aliases_path=path)
            self.assertEqual(resolver.resolve(query="my evening chill", kind="auto"), item("playlist")["uri"])
            self.api.search.assert_not_called()
            self.api.playlists.assert_not_called()
            with self.assertRaisesRegex(SpotifyError, "not_found"):
                resolver.resolve(query="Evening Chill", kind="track")
            write_private(path, {"Evening Chill": "https://bad.invalid"})
            with self.assertRaisesRegex(SpotifyError, "aliases_invalid"):
                resolver.resolve(query="Evening Chill", kind="playlist")

    def test_alias_file_permissions_missing_and_normalized_duplicates(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "aliases.json"
            self.assertEqual(read_aliases(path), {})
            for data in ({"Chill": item("playlist")["uri"], "CHILL": item("playlist")["uri"]},
                         {"music": item("playlist")["uri"]}, {" ": item("playlist")["uri"]}):
                write_private(path, data)
                with self.assertRaisesRegex(SpotifyError, "aliases_invalid"):
                    read_aliases(path)
            write_private(path, {"Chill": item("playlist")["uri"]})
            os.chmod(path, 0o644)
            with self.assertRaisesRegex(SpotifyError, "aliases_invalid"):
                read_aliases(path)

    def test_invalid_argument_types(self):
        for fields in ({"query": "x", "kind": []}, {"query": "", "kind": "track"},
                       {"query": "x", "kind": "artist", "personal": True},
                       {"query": "x", "kind": "playlist", "artist": "Queen"}):
            with self.assertRaisesRegex(SpotifyError, "invalid_request"):
                self.resolver.resolve(**fields)


class VoiceCommandTests(TestCase):
    def setUp(self):
        self.api = Mock()
        self.api.devices.return_value = [{"name": "Cube", "id": "cube", "is_restricted": False}]
        self.api.playback.return_value = None
        self.api.search.return_value = [item()]
        self.controller = MusicController(self.api)
        self.service = Mock()
        self.service.settings.return_value = SimpleNamespace(cube_id="cube")
        self.service.get.return_value = self.controller
        self.commands = MusicCommands(self.service)
        self.context = SimpleNamespace(client_id="cube", control_session="a" * 32, request_id="b" * 32)

    def test_authenticated_controls_generic_response(self):
        for index, operation in enumerate(("play_music", "resume", "pause", "next", "previous", "set_volume")):
            self.context.request_id = f"{index:032x}"
            result = self.commands.execute(self.context, operation, {"percent": 40} if operation == "set_volume" else {})
            self.assertTrue(result["success"])
            self.assertEqual(result["listen_for_seconds"], 0)
            self.assertNotIn("title", result)
            self.assertNotIn("uri", result)
        self.api.control.assert_called_with("volume", "cube", 40)

    def test_missing_trusted_context_or_wrong_cube_fails_closed(self):
        for fields in ({"control_session": None}, {"request_id": None}, {"request_id": "model-id"}, {"client_id": "stranger"}):
            context = SimpleNamespace(**{**vars(self.context), **fields})
            result = self.commands.execute(context, "next", {})
            self.assertFalse(result["success"])
            self.assertEqual(result["error"], "voice_unavailable")
        self.service.get.assert_not_called()
        self.api.control.assert_not_called()

    def test_replay_does_not_repeat_control_or_search(self):
        args = {"query": "Don't Stop Me Now", "kind": "track", "artist": "Queen", "personal": False}
        first = self.commands.execute(self.context, "play_request", args)
        second = self.commands.execute(self.context, "play_request", args)
        self.assertEqual(first, second)
        self.assertEqual(first["response"], "Playing.")
        self.assertEqual(first["listen_for_seconds"], 0)
        self.api.search.assert_called_once()
        self.api.play.assert_called_once_with("cube", item()["uri"])
        self.assertEqual(set(first), {"success", "response", "action", "listen_for_seconds"})
        conflict = self.commands.execute(self.context, "play_request", {**args, "query": "Something Else"})
        self.assertEqual(conflict["error"], "request_conflict")

    def test_no_result_clarifies_without_metadata(self):
        self.api.search.return_value = []
        result = self.commands.execute(self.context, "play_request", {"query": "Missing", "kind": "track"})
        self.assertFalse(result["success"])
        self.assertEqual(result["listen_for_seconds"], 10)
        self.assertNotIn("Missing", result["response"])
        self.api.play.assert_not_called()

    def test_no_followup_spam_for_operational_errors(self):
        for code in ("not_linked", "restricted", "quota_exceeded", "network_unavailable", "playlist_scope_required"):
            self.service.get.side_effect = SpotifyError(code)
            result = self.commands.execute(self.context, "next", {})
            self.assertEqual(result["error"], code)
            self.assertEqual(result["listen_for_seconds"], 0)
            self.assertLess(len(result["response"]), 150)

    def test_cancelled_after_search_cannot_start_playback(self):
        cancelled = False
        def check():
            if cancelled:
                raise asyncio.CancelledError()
        def search(*args, **kwargs):
            nonlocal cancelled
            cancelled = True
            return [item()]
        self.controller.check = check
        self.api.search.side_effect = search
        with self.assertRaises(asyncio.CancelledError):
            self.commands.execute(self.context, "play_request", {"query": "Don't Stop Me Now", "kind": "track"})
        self.api.play.assert_not_called()
        self.api.devices.assert_not_called()

    def test_cancelled_after_device_lookup_cannot_mutate(self):
        count = 0
        def check():
            nonlocal count
            count += 1
            if count == 2:
                raise asyncio.CancelledError()
        self.controller.check = check
        with self.assertRaises(asyncio.CancelledError):
            self.controller.execute({"request_id": "a" * 32, "operation": "next"})
        self.api.control.assert_not_called()


class PlaylistApiTests(TestCase):
    def setUp(self):
        self.responses, self.calls = [], []
        def handle(request):
            self.calls.append(request)
            return self.responses.pop(0)
        self.http = httpx.Client(transport=httpx.MockTransport(handle))
        self.addCleanup(self.http.close)
        self.tokens = Mock()
        self.tokens.access.return_value = "fake"
        self.api = SpotifyWebApi(self.tokens, self.http)

    def test_playlist_scope_paging_and_no_external_next_fetch(self):
        self.responses = [httpx.Response(200, json={"items": [item("playlist")], "total": 2, "next": "https://untrusted.invalid"}),
                          httpx.Response(200, json={"items": [item("playlist", identifier="b")], "total": 2, "next": None})]
        self.assertEqual(len(self.api.playlists()), 2)
        self.tokens.require_scopes.assert_called_once_with({"playlist-read-private"})
        self.assertEqual([r.url.host for r in self.calls], ["api.spotify.com"] * 2)
        self.assertEqual([r.url.params["offset"] for r in self.calls], ["0", "50"])

    def test_large_library_and_missing_scope_fail_before_selection(self):
        self.responses = [httpx.Response(200, json={"items": [], "total": 201})]
        with self.assertRaisesRegex(SpotifyError, "playlist_library_large"):
            self.api.playlists()
        self.tokens.require_scopes.side_effect = SpotifyError("playlist_scope_required")
        with self.assertRaisesRegex(SpotifyError, "playlist_scope_required"):
            self.api.playlists()
        self.assertEqual(len(self.calls), 1)

    def test_details_strip_artwork_and_keep_performers(self):
        upstream = {"name": "Song", "uri": item()["uri"], "artists": [{"name": "Queen", "images": ["secret"]}], "album": {"images": ["secret"]}}
        self.responses = [httpx.Response(200, json={"tracks": {"items": [upstream]}})]
        self.assertEqual(self.api.search("Song", "track", details=True), [{"name": "Song", "uri": item()["uri"], "artists": ["Queen"]}])

    def test_cancelled_after_refresh_does_not_send_playback_command(self):
        cancelled = False
        def access():
            nonlocal cancelled
            cancelled = True
            return "fake"
        def check():
            if cancelled:
                raise asyncio.CancelledError()
        self.tokens.access.side_effect = access
        self.api.check = check
        with self.assertRaises(asyncio.CancelledError):
            self.api.control("next", "cube")
        self.assertEqual(self.calls, [])
