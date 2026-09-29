"""Version 1 personal-Cube contract. No Spotify tokens or local gain fields."""
import re
from integrations.spotify.api import valid_uri
from integrations.spotify.errors import SpotifyError

ID = re.compile(r"[0-9a-f]{32}\Z")


def identifier(value):
    return isinstance(value, str) and ID.fullmatch(value) is not None


def command(data):
    if type(data) is not dict or not identifier(data.get("request_id")):
        raise SpotifyError("invalid_request")
    operation = data.get("operation")
    if not isinstance(operation, str) or operation not in {
            "play_music", "resume", "play_uri", "pause", "next", "previous", "seek", "volume", "transfer", "queue"}:
        raise SpotifyError("invalid_request")
    extra = {"uri"} if operation in {"play_uri", "queue"} else {"value"} if operation in {"volume", "seek"} else set()
    if set(data) != {"request_id", "operation"} | extra:
        raise SpotifyError("invalid_request")
    if "uri" in extra:
        valid_uri(data["uri"], track_only=operation == "queue")
    if "value" in extra and (type(data["value"]) is not int or not 0 <= data["value"] <= (100 if operation == "volume" else 2147483647)):
        raise SpotifyError("invalid_request")
    return dict(data)


def receiver(data):
    if (type(data) is not dict or set(data) != {"version", "session_id", "sequence", "connected", "logged_in", "active", "status", "spotify_volume"}
            or type(data["version"]) is not int or data["version"] != 1
            or not identifier(data["session_id"])
            or type(data["sequence"]) is not int or not 0 <= data["sequence"] <= 2**63 - 1
            or any(type(data[k]) is not bool for k in ("connected", "logged_in", "active"))
            or not isinstance(data["status"], str) or data["status"] not in {"unknown", "idle", "playing", "paused", "buffering"}
            or (data["spotify_volume"] is not None and (type(data["spotify_volume"]) not in (int, float) or not 0 <= data["spotify_volume"] <= 100))):
        raise SpotifyError("invalid_request")
    return dict(data)
