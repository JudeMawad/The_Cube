"""Bounded Spotify HTTP calls. Mutations are never retried after ambiguous failure."""
import math
import re
from threading import RLock
from time import monotonic

import httpx

from .errors import SpotifyError

BASE = "https://api.spotify.com/v1"
URI = re.compile(r"spotify:(track|artist|album|playlist):[A-Za-z0-9]{22}\Z")


def valid_uri(value, *, track_only=False):
    match = URI.fullmatch(value) if isinstance(value, str) else None
    if not match or track_only and match[1] != "track":
        raise SpotifyError("invalid_request")
    return value


class SpotifyWebApi:
    def __init__(self, tokens, http, *, clock=monotonic, check=lambda: None):
        self.tokens, self.http, self.clock = tokens, http, clock
        self.check = check
        self.lock, self.blocked_until, self.failure_count = RLock(), 0, 0
        self.blocked_code = "rate_limited"

    def request(self, method, path, *, params=None, body=None, mutation=False):
        with self.lock:
            self.check()
            remaining = self.blocked_until - self.clock()
            if remaining > 0:
                raise SpotifyError(self.blocked_code, retry_after=math.ceil(remaining))
            access = self.tokens.access()
            for attempt in range(2):
                self.check()
                try:
                    response = self.http.request(method, BASE + path, params=params,
                        **({"json": body} if body is not None else {}),
                        headers={"Authorization": "Bearer " + access})
                except httpx.HTTPError:
                    self._backoff("network_unavailable")
                    raise SpotifyError("network_unavailable") from None
                if response.status_code == 401 and attempt == 0:
                    access = self.tokens.access(rejected=access)
                    continue
                break
            if response.status_code == 401:
                raise SpotifyError("reauthorize")
            if response.status_code == 429:
                try:
                    data = response.json()
                    quota = data.get("error", {}).get("reason") == "QUOTA_EXCEEDED"
                except (ValueError, AttributeError):
                    quota = False
                code = "quota_exceeded" if quota else "rate_limited"
                try:
                    delay = float(response.headers.get("Retry-After", ""))
                    if not math.isfinite(delay) or delay < 0:
                        raise ValueError()
                except ValueError:
                    delay = 3600 if quota else 30
                delay = max(1, delay)
                self.blocked_until, self.blocked_code = self.clock() + delay, code
                raise SpotifyError(code, retry_after=math.ceil(delay))
            if response.status_code == 403:
                raise SpotifyError("restricted")
            if response.status_code == 404:
                raise SpotifyError("device_unavailable")
            if response.status_code >= 500:
                self._backoff("service_unavailable")
                raise SpotifyError("service_unavailable")
            if response.status_code not in (200, 202, 204):
                raise SpotifyError("request_rejected")
            self.failure_count = 0
            # A successful player mutation is acknowledged by status alone.
            # Spotify acknowledgment bodies are not necessarily JSON.
            # Reads retain strict parsing (and their existing 204 semantics).
            if mutation or response.status_code == 204:
                return None
            try:
                data = response.json()
                if not isinstance(data, dict):
                    raise ValueError()
                return data
            except (ValueError, UnicodeError):
                raise SpotifyError("response_invalid") from None

    def _backoff(self, code):
        self.failure_count = min(self.failure_count + 1, 6)
        self.blocked_until = self.clock() + min(60, 2 ** self.failure_count)
        self.blocked_code = code

    def devices(self):
        data = self.request("GET", "/me/player/devices")
        if not isinstance(data, dict) or not isinstance(data.get("devices"), list):
            raise SpotifyError("response_invalid")
        devices = data["devices"]
        for device in devices:
            if (not isinstance(device, dict) or not isinstance(device.get("name"), str)
                    or not (device.get("id") is None or isinstance(device["id"], str))
                    or type(device.get("is_restricted")) is not bool
                    or type(device.get("is_active")) is not bool):
                raise SpotifyError("response_invalid")
        return devices

    def playback(self):
        return self.request("GET", "/me/player")

    def currently_playing(self):
        return self.request("GET", "/me/player/currently-playing")

    def queue(self):
        return self.request("GET", "/me/player/queue")

    def search(self, query, kind, *, details=False):
        if not isinstance(kind, str) or kind not in {"track", "artist", "playlist"} or not isinstance(query, str) or not 1 <= len(query.strip()) <= 256:
            raise SpotifyError("invalid_request")
        data = self.request("GET", "/search", params={"q": query.strip(), "type": kind, "limit": 5})
        try:
            items = data[kind + "s"]["items"]
            if not isinstance(items, list):
                raise ValueError()
            result = []
            for item in items:
                if item is None:
                    continue
                if not isinstance(item["name"], str) or not isinstance(item["uri"], str):
                    raise ValueError()
                if not item["uri"].startswith("spotify:" + kind + ":") or item.get("is_playable") is False:
                    continue
                entry = {"uri": valid_uri(item["uri"]), "name": item["name"]}
                if details and kind == "track":
                    artists = item.get("artists", [])
                    if not isinstance(artists, list) or any(not isinstance(a, dict) or not isinstance(a.get("name"), str) for a in artists):
                        raise ValueError()
                    entry["artists"] = [a["name"] for a in artists]
                result.append(entry)
            return result
        except (KeyError, TypeError, AttributeError, ValueError):
            raise SpotifyError("response_invalid") from None

    def playlists(self):
        """Bounded owned/followed playlist lookup; never follow upstream URLs."""
        self.tokens.require_scopes({"playlist-read-private"})
        result = []
        for offset in range(0, 200, 50):
            data = self.request("GET", "/me/playlists", params={"limit": 50, "offset": offset})
            try:
                items, total = data["items"], data["total"]
                if not isinstance(items, list) or len(items) > 50 or type(total) is not int or total < 0:
                    raise ValueError()
                if total > 200:
                    raise SpotifyError("playlist_library_large")
                for item in items:
                    if item is None:
                        continue
                    uri = valid_uri(item["uri"])
                    if not uri.startswith("spotify:playlist:") or not isinstance(item["name"], str):
                        raise ValueError()
                    result.append({"uri": uri, "name": item["name"]})
                if not data.get("next"):
                    return result
                if not items:
                    raise ValueError()
            except (KeyError, TypeError, AttributeError, ValueError):
                raise SpotifyError("response_invalid") from None
        raise SpotifyError("playlist_library_large")

    def play(self, device, uri=None):
        body = None
        if uri is not None:
            valid_uri(uri)
            body = {"uris": [uri]} if uri.startswith("spotify:track:") else {"context_uri": uri}
        return self.request("PUT", "/me/player/play", params={"device_id": device}, body=body, mutation=True)

    def transfer(self, device, *, play=True):
        return self.request("PUT", "/me/player", body={"device_ids": [device], "play": play}, mutation=True)

    def control(self, operation, device, value=None):
        params = {"device_id": device}
        if operation == "volume":
            if type(value) is not int or not 0 <= value <= 100:
                raise SpotifyError("invalid_request")
            params["volume_percent"] = value
        elif operation == "seek":
            if type(value) is not int or not 0 <= value <= 2147483647:
                raise SpotifyError("invalid_request")
            params["position_ms"] = value
        elif operation == "queue":
            params["uri"] = valid_uri(value, track_only=True)
        elif operation not in {"pause", "next", "previous"}:
            raise SpotifyError("invalid_request")
        return self.request("POST" if operation in {"next", "previous", "queue"} else "PUT",
                            "/me/player/" + operation, params=params, mutation=True)
