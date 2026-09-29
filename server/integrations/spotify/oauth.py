"""PKCE and refresh tokens. No implicit grant, password or client secret."""
import base64
import hashlib
import math
import secrets
from threading import RLock
from time import time, monotonic
from urllib.parse import urlencode, urlsplit, parse_qs

import httpx

from .config import REDIRECT_URI, SCOPES
from .errors import SpotifyError

TOKEN_URL = "https://accounts.spotify.com/api/token"


def challenge(verifier):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()


class Authorization:
    def __init__(self, client_id, *, clock=monotonic, scopes=SCOPES):
        self.client_id, self.clock = client_id, clock
        self.scopes = scopes
        self.state = secrets.token_urlsafe(32)
        self.verifier = secrets.token_urlsafe(64)
        self.deadline, self.used = clock() + 300, False

    def url(self):
        return "https://accounts.spotify.com/authorize?" + urlencode({
            "client_id": self.client_id, "response_type": "code", "redirect_uri": REDIRECT_URI,
            "scope": " ".join(self.scopes), "state": self.state,
            "code_challenge_method": "S256", "code_challenge": challenge(self.verifier)})

    def callback(self, target):
        parsed = urlsplit(target)
        query = parse_qs(parsed.query)
        if (self.used or self.clock() >= self.deadline or parsed.path != "/callback"
                or len(target) > 8192 or len(query.get("state", [])) != 1
                or not secrets.compare_digest(query["state"][0], self.state)):
            raise SpotifyError("authorization_invalid")
        self.used = True
        if "error" in query:
            raise SpotifyError("authorization_denied")
        if len(query.get("code", [])) != 1 or not query["code"][0]:
            raise SpotifyError("authorization_invalid")
        return query["code"][0]


def token_document(data, client_id, now, previous=None):
    if not isinstance(data, dict):
        raise SpotifyError("response_invalid")
    refresh = data.get("refresh_token", (previous or {}).get("refresh_token"))
    access = data.get("access_token")
    expires = data.get("expires_in")
    scope = data.get("scope", (previous or {}).get("scope", ""))
    if (any(not isinstance(v, str) or not v or len(v) > 8192 for v in (access, refresh))
            or data.get("token_type", "").lower() != "bearer"
            or type(expires) is not int or not 0 < expires <= 86400
            or not isinstance(scope, str) or not set(SCOPES) <= set(scope.split())):
        raise SpotifyError("response_invalid")
    return {"client_id": client_id, "access_token": access, "refresh_token": refresh,
            "expires_at": now + expires, "scope": scope}


class Tokens:
    def __init__(self, settings, store, http, *, clock=time, timer=monotonic):
        self.settings, self.store, self.http = settings, store, http
        self.clock, self.timer, self.lock = clock, timer, RLock()
        self.blocked_until = 0

    def _exchange(self, data, previous=None):
        if self.timer() < self.blocked_until:
            raise SpotifyError("auth_unavailable")
        try:
            response = self.http.post(TOKEN_URL, data={"client_id": self.settings.client_id, **data})
        except httpx.HTTPError:
            self.blocked_until = self.timer() + 5
            raise SpotifyError("network_unavailable") from None
        if response.status_code in (400, 401):
            # Do not echo Spotify's response (it may contain sensitive context).
            raise SpotifyError("reauthorize")
        if response.status_code != 200:
            try:
                delay = float(response.headers.get("Retry-After", "60"))
                if not math.isfinite(delay) or delay < 0:
                    raise ValueError()
            except ValueError:
                delay = 60
            self.blocked_until = self.timer() + max(1, delay)
            raise SpotifyError("auth_unavailable", retry_after=math.ceil(max(1, delay)))
        try:
            return token_document(response.json(), self.settings.client_id, self.clock(), previous)
        except (ValueError, TypeError, AttributeError):
            raise SpotifyError("response_invalid") from None

    def authorize(self, code, verifier):
        with self.lock, self.store.locked():
            value = self._exchange({"grant_type": "authorization_code", "code": code,
                                    "redirect_uri": REDIRECT_URI, "code_verifier": verifier})
            self.store.write(value)

    def access(self, rejected=None):
        with self.lock, self.store.locked():
            value = self.store.read()
            if (value.get("client_id") != self.settings.client_id
                    or any(not isinstance(value.get(k), str) or not value[k]
                           for k in ("access_token", "refresh_token", "scope"))
                    or not set(SCOPES) <= set(value["scope"].split())
                    or type(value.get("expires_at")) not in (int, float)
                    or not math.isfinite(value["expires_at"])):
                raise SpotifyError("storage_invalid")
            if value["expires_at"] > self.clock() + 60 and value["access_token"] != rejected:
                return value["access_token"]
            try:
                value = self._exchange({"grant_type": "refresh_token", "refresh_token": value["refresh_token"]}, value)
            except SpotifyError as error:
                if error.code == "reauthorize":
                    self.store.delete()
                raise
            self.store.write(value)
            return value["access_token"]

    def logout(self):
        with self.lock, self.store.locked():
            self.store.delete()

    def require_scopes(self, required):
        with self.lock, self.store.locked():
            value = self.store.read()
            if not isinstance(value.get("scope"), str) or not set(required) <= set(value["scope"].split()):
                raise SpotifyError("playlist_scope_required")
