"""Offline OAuth/storage/Web API regressions. No account, socket or backend models."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import httpx

from integrations.spotify.api import SpotifyWebApi
from integrations.spotify.config import SCOPES, PLAYLIST_SCOPES, REDIRECT_URI, Settings
from integrations.spotify.errors import SpotifyError
from integrations.spotify.oauth import Authorization, Tokens, challenge
from integrations.spotify.storage import TokenStore, read_private, write_private


class SpotifyTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = TokenStore(Path(self.directory.name))
        self.settings = Settings("a" * 32, "cube")
        self.now = 1000
        self.calls, self.responses = [], []

        def handle(request):
            self.calls.append(request)
            result = self.responses.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        self.http = httpx.Client(transport=httpx.MockTransport(handle), trust_env=False)
        self.addCleanup(self.http.close)
        self.tokens = Tokens(self.settings, self.store, self.http, clock=lambda: self.now, timer=lambda: self.now)
        self.api = SpotifyWebApi(self.tokens, self.http, clock=lambda: self.now)
        self.store.write(self.saved())

    def saved(self, **updates):
        return {"client_id": self.settings.client_id, "access_token": "test-access", "refresh_token": "test-refresh",
                "scope": " ".join(SCOPES), "expires_at": 5000, **updates}

    def token_response(self, **updates):
        return httpx.Response(200, json={"access_token": "new-access", "token_type": "Bearer",
            "expires_in": 3600, "scope": " ".join(SCOPES), **updates})

    def test_pkce_rfc_vector_and_loopback(self):
        self.assertEqual(challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"),
                         "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM")
        flow = Authorization(self.settings.client_id, clock=lambda: self.now)
        query = parse_qs(urlsplit(flow.url()).query)
        self.assertEqual(query["redirect_uri"], [REDIRECT_URI])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertNotIn(flow.verifier, flow.url())
        self.assertEqual(set(query["scope"][0].split()), set(SCOPES))
        self.assertEqual(flow.callback("/callback?code=test-code&state=" + flow.state), "test-code")
        with self.assertRaisesRegex(SpotifyError, "authorization_invalid"):
            flow.callback("/callback?code=test-code&state=" + flow.state)

    def test_callback_csrf_duplicate_timeout_denial(self):
        for suffix in ("?code=x&state=wrong", "?code=x", "?code=x&state=S&state=S", "?code=x&code=y&state=S"):
            flow = Authorization(self.settings.client_id, clock=lambda: self.now)
            with self.assertRaises(SpotifyError):
                flow.callback("/callback" + suffix.replace("S", flow.state))
        flow = Authorization(self.settings.client_id, clock=lambda: self.now)
        self.now += 301
        with self.assertRaisesRegex(SpotifyError, "authorization_invalid"):
            flow.callback("/callback?code=x&state=" + flow.state)
        flow = Authorization(self.settings.client_id)
        with self.assertRaisesRegex(SpotifyError, "authorization_denied"):
            flow.callback("/callback?error=access_denied&state=" + flow.state)

    def test_optional_playlist_scope_preserves_existing_tokens(self):
        original = self.store.read()
        flow = Authorization(self.settings.client_id, scopes=SCOPES + PLAYLIST_SCOPES)
        scopes = parse_qs(urlsplit(flow.url()).query)["scope"][0].split()
        self.assertEqual(set(scopes), set(SCOPES + PLAYLIST_SCOPES))
        with self.assertRaisesRegex(SpotifyError, "playlist_scope_required"):
            self.tokens.require_scopes(set(PLAYLIST_SCOPES))
        self.assertEqual(self.store.read(), original)
        self.assertEqual(self.tokens.access(), "test-access")
        self.assertEqual(self.calls, [])

    def test_playlist_scope_survives_refresh_omitting_scope(self):
        self.store.write(self.saved(expires_at=1000, scope=" ".join(SCOPES + PLAYLIST_SCOPES)))
        self.responses = [httpx.Response(200, json={"access_token": "new-access", "token_type": "Bearer", "expires_in": 3600})]
        self.assertEqual(self.tokens.access(), "new-access")
        self.tokens.require_scopes(set(PLAYLIST_SCOPES))

    def test_authorize_no_secret_and_atomic_private_tokens(self):
        self.responses = [self.token_response(refresh_token="new-refresh")]
        self.tokens.authorize("test-code", "test-verifier")
        data = parse_qs(self.calls[0].content.decode())
        self.assertEqual(data["code_verifier"], ["test-verifier"])
        self.assertEqual(data["redirect_uri"], [REDIRECT_URI])
        self.assertNotIn("client_secret", data)
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.store.read()["refresh_token"], "new-refresh")
        self.assertFalse(list(Path(self.directory.name).glob(".spotify-*")))

    def test_expiry_refresh_preserves_refresh_token(self):
        self.store.write(self.saved(expires_at=1000))
        self.responses = [self.token_response()]
        self.assertEqual(self.tokens.access(), "new-access")
        self.assertEqual(self.store.read()["refresh_token"], "test-refresh")
        self.assertEqual(parse_qs(self.calls[0].content.decode())["grant_type"], ["refresh_token"])

    def test_refresh_rotation_and_concurrent_single_exchange(self):
        self.store.write(self.saved(expires_at=1000))
        self.responses = [self.token_response(refresh_token="rotated")]
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(list(pool.map(lambda _: self.tokens.access(), range(4))), ["new-access"] * 4)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.store.read()["refresh_token"], "rotated")

    def test_401_refresh_only_once_and_replay_rejected_request(self):
        self.responses = [httpx.Response(401), self.token_response(), httpx.Response(204)]
        self.api.control("next", "cube-id")
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.calls[2].headers["Authorization"], "Bearer new-access")
        self.responses = [httpx.Response(401), self.token_response(), httpx.Response(401)]
        with self.assertRaisesRegex(SpotifyError, "reauthorize"):
            self.api.playback()

    def test_invalid_refresh_revokes_local_credentials(self):
        self.store.write(self.saved(expires_at=1000))
        self.responses = [httpx.Response(400, json={"error": "invalid_grant"})]
        with self.assertRaisesRegex(SpotifyError, "reauthorize"):
            self.tokens.access()
        self.assertFalse(self.store.path.exists())
        with self.assertRaisesRegex(SpotifyError, "not_linked"):
            self.tokens.access()
        self.assertEqual(len(self.calls), 1)

    def test_logout_missing_and_corruption(self):
        self.tokens.logout()
        with self.assertRaisesRegex(SpotifyError, "not_linked"):
            self.tokens.access()
        self.store.path.write_text("{broken")
        os.chmod(self.store.path, 0o600)
        with self.assertRaisesRegex(SpotifyError, "storage_invalid"):
            self.tokens.access()
        self.assertEqual(self.calls, [])

    def test_reject_unsafe_modes_symlink_fifo_size_owner(self):
        for mode in (0o644, 0o640, 0o666):
            os.chmod(self.store.path, mode)
            with self.assertRaisesRegex(SpotifyError, "storage_invalid"):
                self.store.read()
        self.store.path.unlink()
        target = Path(self.directory.name) / "other"
        write_private(target, self.saved())
        self.store.path.symlink_to(target)
        with self.assertRaisesRegex(SpotifyError, "storage_invalid"):
            self.store.read()
        self.store.path.unlink()
        os.mkfifo(self.store.path, 0o600)
        with self.assertRaisesRegex(SpotifyError, "storage_invalid"):
            self.store.read()
        self.store.path.unlink()
        self.store.path.write_text("x" * 32769)
        os.chmod(self.store.path, 0o600)
        with self.assertRaisesRegex(SpotifyError, "storage_invalid"):
            self.store.read()

    def test_wrong_client_and_missing_scope(self):
        for updates in ({"client_id": "b" * 32}, {"scope": ""}, {"expires_at": float("inf")}):
            # External corruption can contain nonstandard JSON.
            self.store.path.write_text(json.dumps(self.saved(**updates)))
            with self.assertRaisesRegex(SpotifyError, "storage_invalid"):
                self.tokens.access()
        self.assertEqual(self.calls, [])

    def test_bad_token_payload_does_not_overwrite_valid_store(self):
        original = self.store.read()
        self.responses = [self.token_response(expires_in="3600")]
        with self.assertRaisesRegex(SpotifyError, "response_invalid"):
            self.tokens.access(rejected="test-access")
        self.assertEqual(self.store.read(), original)

    def test_429_retry_after_and_quota_no_busy_loop(self):
        for reason, code, fallback in (("RATE_LIMITED", "rate_limited", 30), ("QUOTA_EXCEEDED", "quota_exceeded", 3600)):
            self.api.blocked_until = 0
            self.responses = [httpx.Response(429, json={"error": {"reason": reason}})]
            with self.assertRaises(SpotifyError) as error:
                self.api.playback()
            self.assertEqual(error.exception.code, code)
            self.assertEqual(error.exception.retry_after, fallback)
            count = len(self.calls)
            with self.assertRaisesRegex(SpotifyError, code):
                self.api.control("next", "cube")
            self.assertEqual(len(self.calls), count)
        self.api.blocked_until = 0
        self.responses = [httpx.Response(429, headers={"Retry-After": "91"})]
        with self.assertRaises(SpotifyError) as error:
            self.api.playback()
        self.assertEqual(error.exception.retry_after, 91)
        self.now += 92
        self.responses = [httpx.Response(204)]
        self.assertIsNone(self.api.playback())

    def test_http_failures_and_network_do_not_replay_mutations(self):
        for response, code in ((httpx.Response(400), "request_rejected"),
                (httpx.Response(403), "restricted"), (httpx.Response(404), "device_unavailable"),
                (httpx.Response(500), "service_unavailable"), (httpx.Response(502), "service_unavailable"),
                (httpx.ReadTimeout("test"), "network_unavailable")):
            self.api.blocked_until = 0
            self.responses = [response]
            before = len(self.calls)
            with self.assertRaisesRegex(SpotifyError, code):
                self.api.control("next", "cube")
            self.assertEqual(len(self.calls), before + 1)

    def test_mutation_success_ignores_acknowledgment_body_once(self):
        track = "spotify:track:" + "a" * 22
        actions = [
            ("resume", lambda: self.api.play("cube")),
            ("play_uri", lambda: self.api.play("cube", track)),
            ("transfer", lambda: self.api.transfer("cube")),
        ]
        actions += [(operation, lambda op=operation, val=value: self.api.control(op, "cube", val))
                    for operation, value in (("pause", None), ("next", None), ("previous", None),
                                             ("volume", 40), ("seek", 123), ("queue", track))]
        acknowledgment = b"Mutation accepted by server"  # Synthetic, 27-byte non-JSON body.
        self.assertEqual(len(acknowledgment), 27)
        for name, action in actions:
            for status in (200, 202, 204):
                for body in (b"", b" \n\t", b"{}", b"[]", b"null", b'{"accepted":true}',
                             acknowledgment, b"\xff\x00\xfe"):
                    with self.subTest(operation=name, status=status, body=body):
                        response = httpx.Response(status, content=body)
                        response.json = Mock(side_effect=AssertionError("Mutation acknowledgment must not be parsed"))
                        self.responses = [response]
                        before = len(self.calls)
                        self.assertIsNone(action())
                        response.json.assert_not_called()
                        self.assertEqual(len(self.calls), before + 1)

    def test_reads_still_require_json_dictionaries(self):
        reads = [self.api.playback, self.api.currently_playing, self.api.queue,
                 self.api.devices, lambda: self.api.search("Song", "track")]
        for read in reads:
            for status in (200, 202):
                for body in (b"", b"Mutation accepted by server", b'{"broken":', b"[]", b"null"):
                    with self.subTest(read=read.__name__, status=status, body=body):
                        self.responses = [httpx.Response(status, content=body)]
                        before = len(self.calls)
                        with self.assertRaisesRegex(SpotifyError, "response_invalid"):
                            read()
                        self.assertEqual(len(self.calls), before + 1)

    def test_endpoint_methods_and_payloads(self):
        track = "spotify:track:" + "a" * 22
        artist = "spotify:artist:" + "b" * 22
        actions = [
            (lambda: self.api.play("cube", track), "PUT", "/me/player/play", {"uris": [track]}),
            (lambda: self.api.play("cube", artist), "PUT", "/me/player/play", {"context_uri": artist}),
            (lambda: self.api.play("cube"), "PUT", "/me/player/play", None),
            (lambda: self.api.transfer("cube"), "PUT", "/me/player", {"device_ids": ["cube"], "play": True}),
        ]
        for action, method, path, body in actions:
            self.responses = [httpx.Response(204)]
            action()
            request = self.calls[-1]
            self.assertEqual((request.method, request.url.path), (method, "/v1" + path))
            self.assertEqual(json.loads(request.content) if request.content else None, body)
        for operation, value, parameter in (("pause", None, None), ("next", None, None), ("previous", None, None),
                                          ("seek", 123, "position_ms"), ("volume", 40, "volume_percent"), ("queue", track, "uri")):
            self.responses = [httpx.Response(204)]
            self.api.control(operation, "cube", value)
            request = self.calls[-1]
            self.assertEqual(request.method, "POST" if operation in {"next", "previous", "queue"} else "PUT")
            self.assertEqual(request.url.params["device_id"], "cube")
            if parameter:
                self.assertEqual(request.url.params[parameter], str(value))
        for method, path in ((self.api.currently_playing, "currently-playing"), (self.api.queue, "queue")):
            self.responses = [httpx.Response(200, json={})]
            method()
            self.assertEqual(self.calls[-1].url.path, "/v1/me/player/" + path)

    def test_search_minimal_data_and_no_result(self):
        uri = "spotify:track:" + "a" * 22
        self.responses = [httpx.Response(200, json={"tracks": {"items": [None, {"uri": uri, "name": "Song", "album": {"images": ["discard"]}}]}})]
        self.assertEqual(self.api.search("Song", "track"), [{"uri": uri, "name": "Song"}])
        self.assertEqual(self.calls[-1].url.params["limit"], "5")
        self.responses = [httpx.Response(200, json={"playlists": {"items": []}})]
        self.assertEqual(self.api.search("absent", "playlist"), [])

    def test_device_schema_and_bad_inputs(self):
        for data in ({}, {"devices": {}}, {"devices": [None]}, {"devices": [{"name": "Cube"}]}):
            self.responses = [httpx.Response(200, json=data)]
            with self.assertRaisesRegex(SpotifyError, "response_invalid"):
                self.api.devices()
        for operation, value in (("volume", True), ("volume", 101), ("seek", -1), ("queue", "https://example.com")):
            with self.assertRaisesRegex(SpotifyError, "invalid_request"):
                self.api.control(operation, "cube", value)

    def test_oauth_rate_limit_respected_without_repeated_refresh(self):
        self.store.write(self.saved(expires_at=1000))
        self.responses = [httpx.Response(429, headers={"Retry-After": "120"})]
        with self.assertRaises(SpotifyError) as error:
            self.tokens.access()
        self.assertEqual(error.exception.retry_after, 120)
        self.now += 61
        with self.assertRaisesRegex(SpotifyError, "auth_unavailable"):
            self.tokens.access()
        self.assertEqual(len(self.calls), 1)

    def test_error_does_not_contain_upstream_secret(self):
        self.responses = [httpx.Response(403, json={"error": {"message": "sensitive upstream message"}})]
        with self.assertRaises(SpotifyError) as error:
            self.api.playback()
        self.assertEqual(str(error.exception), "restricted")

    def test_callback_listener_binds_loopback_and_closes(self):
        from integrations.spotify.setup import authorize
        from unittest.mock import patch
        flow = Authorization(self.settings.client_id)
        server = Mock()
        server.__enter__ = Mock(return_value=server)
        server.__exit__ = Mock(return_value=False)
        server_factory = Mock(return_value=server)
        output = Mock()
        with patch("integrations.spotify.setup.Authorization", return_value=flow), \
                patch("integrations.spotify.setup.monotonic", return_value=flow.deadline + 1):
            with self.assertRaisesRegex(SpotifyError, "authorization_timeout"):
                authorize(self.tokens, self.settings.client_id, server_class=server_factory, output=output)
        self.assertEqual(server_factory.call_args.args[0], ("127.0.0.1", 8768))
        server.__exit__.assert_called_once()
        self.assertEqual(self.calls, [])

    def test_two_token_managers_share_refresh_lock(self):
        second = Tokens(self.settings, TokenStore(Path(self.directory.name)), self.http,
                        clock=lambda: self.now, timer=lambda: self.now)
        self.store.write(self.saved(expires_at=1000))
        self.responses = [self.token_response(refresh_token="rotated")]
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(tokens.access) for tokens in (self.tokens, second)]
            self.assertEqual([future.result(2) for future in futures], ["new-access", "new-access"])
        self.assertEqual(len(self.calls), 1)

    def test_callback_success_denial_closes_and_never_logs_codes(self):
        import io
        from integrations.spotify.setup import authorize
        from unittest.mock import patch
        for denied in (False, True):
            flow = Authorization(self.settings.client_id)
            code = "error=access_denied" if denied else "code=test-code"
            request = ("GET /callback?" + code + "&state=" + flow.state + " HTTP/1.1\r\nHost: 127.0.0.1:8768\r\n\r\n").encode()
            sock = Mock()
            sock.makefile.return_value = io.BytesIO(request)
            closed = []
            class Server:
                def __init__(self, address, handler):
                    self.handler = handler
                def __enter__(self):
                    return self
                def __exit__(self, *_):
                    closed.append(True)
                def handle_request(self):
                    self.handler(sock, ("127.0.0.1", 12345), self)
            tokens, output = Mock(), Mock()
            with patch("integrations.spotify.setup.Authorization", return_value=flow):
                if denied:
                    with self.assertRaisesRegex(SpotifyError, "authorization_denied"):
                        authorize(tokens, self.settings.client_id, server_class=Server, output=output)
                    tokens.authorize.assert_not_called()
                else:
                    authorize(tokens, self.settings.client_id, server_class=Server, output=output)
                    tokens.authorize.assert_called_once_with("test-code", flow.verifier)
            self.assertEqual(closed, [True])
            printed = str(output.call_args_list)
            self.assertNotIn("test-code", printed)
            self.assertNotIn(flow.verifier, printed)

    def test_search_rejects_arbitrary_types_without_http(self):
        for query, kind in (("test", []), (None, "track"), ("x", {}), ("", "track")):
            with self.assertRaisesRegex(SpotifyError, "invalid_request"):
                self.api.search(query, kind)
        self.assertEqual(self.calls, [])
