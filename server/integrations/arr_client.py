"""Small transport shared by the two Arr clients (API v3)."""

from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx


class ArrError(Exception):
    """Safe diagnostic; never includes upstream bodies, URLs, or credentials."""


@dataclass(frozen=True)
class Settings:
    url: str = field(repr=False)
    api_key: str = field(repr=False)

    @classmethod
    def load(cls, path, service):
        try:
            values = {}
            for line in path.read_text().splitlines():
                key, sep, value = line.strip().partition("=")
                if sep and key.strip() in {service + "_URL", service + "_API_KEY"}:
                    value = value.strip()
                    if value[:1] in {"'", '"'}:
                        if len(value) < 2 or value[-1] != value[0]:
                            raise ValueError()
                        value = value[1:-1]
                    values[key.strip()] = value
            url = values[service + "_URL"].rstrip("/")
            key = values[service + "_API_KEY"]
            parsed = urlsplit(url)
            if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                    or parsed.username or parsed.password or parsed.query or parsed.fragment
                    or not key or any(c.isspace() for c in key)):
                raise ValueError()
            return cls(url, key)
        except (OSError, UnicodeError, KeyError, ValueError):
            raise ArrError(f"{service} configuration is missing or invalid.") from None


class ArrClient:
    def _get(self, path):
        return self._request("GET", path)

    def _request(self, method, path, *, array=False, allow_not_found=False, **kwargs):
        settings = Settings.load(self.config_path, self.service)
        base = settings.url
        if not base.endswith("/api/v3"):
            base += "/api/v3"
        try:
            with httpx.Client(timeout=10, trust_env=False, follow_redirects=False) as client:
                response = client.request(method, base + path, headers={
                    "X-Api-Key": settings.api_key, "Accept": "application/json",
                }, **kwargs)
            if allow_not_found and response.status_code == 404:
                return None
            response.raise_for_status()
            if method == "DELETE" and response.status_code in {200, 204}:
                return None
            data = response.json()
            if not isinstance(data, list if array else dict):
                raise ValueError()
            return data
        except (httpx.HTTPError, ValueError):
            raise ArrError(f"{self.service} API operation could not be verified.") from None

    def system_status(self):
        data = self._get("/system/status")
        if not isinstance(data.get("version"), str):
            raise ArrError(f"{self.service} returned an unexpected status.")
        return data

    def get_media(self, media_id):
        if type(media_id) is not int or media_id <= 0:
            raise ArrError("Invalid media identity.")
        data = self._get(f"/{self.resource}/{media_id}")
        if data.get("id") != media_id:
            raise ArrError(f"{self.service} returned an unexpected identity.")
        return data
