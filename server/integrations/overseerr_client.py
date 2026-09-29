"""Server-only Overseerr API access; no credential or upstream body logging."""

from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import Lock
import time
from pathlib import Path
import re
from urllib.parse import quote, urlsplit

import httpx


upstream_seconds = ContextVar("movie_upstream_seconds", default=0.0)

CONFIG_PATH = Path.home() / ".config/cube/overseerr.env"


class OverseerrError(Exception):
    """An error whose message is safe to return to the voice client."""


class RequestUncertain(OverseerrError):
    pass


@dataclass(frozen=True)
class Settings:
    url: str = field(repr=False)
    api_key: str = field(repr=False)

    @classmethod
    def load(cls):
        # Existing file is authoritative for manual and systemd launches.
        # Parse assignments as data; never execute/source the file.
        try:
            values = {}
            for line in CONFIG_PATH.read_text().splitlines():
                key, sep, value = line.strip().partition("=")
                if sep and key.strip() in {"OVERSEERR_URL", "OVERSEERR_API_KEY"}:
                    value = value.strip()
                    if value[:1] in {"'", '"'}:
                        if len(value) < 2 or value[-1] != value[0]:
                            raise ValueError()
                        value = value[1:-1]
                    values[key.strip()] = value
            url = values["OVERSEERR_URL"].rstrip("/")
            api_key = values["OVERSEERR_API_KEY"]
            parsed = urlsplit(url)
            if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                    or parsed.username or parsed.password or parsed.query
                    or parsed.fragment or not api_key
                    or any(c.isspace() for c in api_key)):
                raise ValueError()
            return cls(url, api_key)
        except (OSError, UnicodeError, KeyError, ValueError):
            raise OverseerrError("The Overseerr configuration is missing or invalid.") from None


@dataclass(frozen=True)
class Movie:
    media_id: int  # TMDB ID, not Overseerr's internal media database ID.
    title: str
    year: str | None
    state: str = "unknown"

    @property
    def label(self):
        return f"{self.title} from {self.year}" if self.year else self.title


def movie_state(data):
    info = data.get("mediaInfo") or {}
    if not isinstance(info, dict):
        raise OverseerrError("Overseerr returned an unexpected response.")
    status = info.get("status", 1)
    if status in {4, 5}:
        return "available"
    requests = info.get("requests") or []
    if not isinstance(requests, list):
        raise OverseerrError("Overseerr returned an unexpected response.")
    if status in {2, 3} or any(
        isinstance(r, dict) and not r.get("is4k") and r.get("status") in {1, 2}
        for r in requests
    ):
        return "requested"
    if any(isinstance(r, dict) and not r.get("is4k") and r.get("status") == 4 for r in requests):
        return "failed"
    return "unknown"


def parse_movie(data):
    if (not isinstance(data, dict) or type(data.get("id")) is not int
            or data["id"] <= 0 or not isinstance(data.get("title"), str)
            or not data["title"].strip()):
        raise OverseerrError("Overseerr returned an unexpected response.")
    date = data.get("releaseDate") or data.get("release_date") or ""
    year = date[:4] if isinstance(date, str) and re.match(r"^\d{4}", date) else None
    return Movie(data["id"], data["title"].strip(), year, movie_state(data))


class OverseerrClient:
    def __init__(self):
        self._http = None
        self._lock = Lock()

    def _transport(self):
        with self._lock:
            if self._http is None:
                self._http = httpx.Client(timeout=10, follow_redirects=False, trust_env=False)
            return self._http

    def close(self):
        with self._lock:
            if self._http is not None:
                self._http.close()
                self._http = None

    def _request(self, method, path, *, allow_not_found=False, array=False, **kwargs):
        settings = Settings.load()
        base = settings.url
        if not base.endswith("/api/v1"):
            base += "/api/v1"
        started = time.perf_counter()
        try:
            # Credentials are still read per call; the pool stores no API key.
            response = self._transport().request(
                method, base + path,
                headers={"X-Api-Key": settings.api_key, "Accept": "application/json"},
                **kwargs,
            )
        except (httpx.HTTPError, ValueError):
            if method in {"POST", "DELETE"}:
                raise RequestUncertain(
                    "I couldn't verify whether Overseerr accepted the request. "
                    "Please check Overseerr before trying again."
                ) from None
            raise OverseerrError("I can't reach Overseerr right now. Please try again later.") from None
        finally:
            upstream_seconds.set(upstream_seconds.get() + time.perf_counter() - started)
        if allow_not_found and response.status_code == 404:
            return None
        if method == "DELETE" and response.status_code == 204:
            return None
        if response.status_code in {401, 403}:
            raise OverseerrError("Overseerr rejected the API key or its permissions.")
        if not 200 <= response.status_code < 300:
            if method in {"POST", "DELETE"}:
                raise RequestUncertain(
                    "Overseerr didn't confirm the request. Please check Overseerr before trying again."
                )
            raise OverseerrError("Overseerr is unavailable or could not complete the search.")
        try:
            data = response.json()
            if not isinstance(data, list if array else dict):
                raise ValueError()
            return data
        except ValueError:
            if method in {"POST", "DELETE"}:
                raise RequestUncertain(
                    "I couldn't verify the request. Please check Overseerr before trying again."
                ) from None
            raise OverseerrError("Overseerr returned an unexpected response.") from None

    def search_movies(self, query):
        encoded_query = quote(query, safe="")
        data = self._request(
            "GET",
            f"/search?query={encoded_query}&page=1",
        )
        results = data.get("results")
        if not isinstance(results, list):
            raise OverseerrError("Overseerr returned an unexpected response.")
        return [parse_movie(item) for item in results
                if isinstance(item, dict) and item.get("mediaType") == "movie"]

    def get_movie(self, media_id):
        movie = parse_movie(self._request("GET", f"/movie/{media_id}"))
        if movie.media_id != media_id:
            raise OverseerrError("Overseerr returned an unexpected movie.")
        return movie

    def request_movie(self, media_id):
        result = self._request("POST", "/request", json={"mediaType": "movie", "mediaId": media_id})
        if type(result.get("id")) is not int or result["id"] <= 0 or result.get("status") not in {1, 2}:
            raise RequestUncertain("I couldn't verify the request. Please check Overseerr before trying again.")
        return result["id"]

    def movie_details(self, media_id):
        data = self._request("GET", f"/movie/{media_id}")
        if parse_movie(data).media_id != media_id:
            raise OverseerrError("Overseerr returned an unexpected movie.")
        return data

    def get_request(self, request_id):
        data = self._request("GET", f"/request/{request_id}", allow_not_found=True)
        if data is not None and data.get("id") != request_id:
            raise OverseerrError("Overseerr returned an unexpected request.")
        return data

    def delete_request(self, request_id):
        if type(request_id) is not int or request_id <= 0:
            raise ValueError("Invalid request identity")
        self._request("DELETE", f"/request/{request_id}", allow_not_found=True)

    def requesting_user_id(self):
        data = self._request("GET", "/auth/me")
        if type(data.get("id")) is not int or data["id"] <= 0:
            raise OverseerrError("I couldn't verify the requesting user.")
        return data["id"]

    def radarr_servers(self):
        return self._request("GET", "/settings/radarr", array=True)
