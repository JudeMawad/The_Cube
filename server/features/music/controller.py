"""One serialized owner for cloud playback, separate from local interaction gain."""
from collections import OrderedDict
from dataclasses import asdict, dataclass, replace
from threading import RLock
from time import monotonic

from integrations.spotify.errors import SpotifyError
from .protocol import command, identifier, receiver
from .resolution import MusicResolver


@dataclass(frozen=True)
class MusicState:
    linked: bool = False
    stale: bool = True
    playing: bool = False
    device_id: str | None = None
    device_name: str | None = None
    on_cube: bool = False
    track_uri: str | None = None
    track_title: str | None = None
    artists: tuple[str, ...] = ()
    duration_ms: int | None = None
    progress_ms: int | None = None
    spotify_volume: int | None = None
    error: str | None = None
    artwork_url: str | None = None  # Internal only; /music/state v1 is unchanged.


def snapshot(data, device_name):
    if data is None:
        return MusicState(linked=True, stale=False)
    try:
        device = data["device"]
        item = data.get("item") or {}
        if (type(data["is_playing"]) is not bool or not isinstance(device, dict)
                or not isinstance(device.get("name"), str)
                or not (device.get("id") is None or isinstance(device["id"], str))):
            raise ValueError()
        for obj, key, upper in ((data, "progress_ms", 2**53), (item, "duration_ms", 2**53), (device, "volume_percent", 100)):
            if obj.get(key) is not None and (type(obj[key]) is not int or not 0 <= obj[key] <= upper):
                raise ValueError()
        artists = tuple(artist["name"] for artist in item.get("artists", []))
        if (any(not isinstance(a, str) for a in artists)
                or any(item.get(k) is not None and not isinstance(item[k], str) for k in ("uri", "name"))):
            raise ValueError()
        album = item.get("album")
        images = album.get("images", []) if isinstance(album, dict) else []
        artwork = next((image["url"] for image in images if isinstance(image, dict)
                        and isinstance(image.get("url"), str)), None) if isinstance(images, list) else None
        return MusicState(linked=True, stale=False, playing=data["is_playing"],
            device_id=device.get("id"), device_name=device["name"], on_cube=device["name"] == device_name,
            track_uri=item.get("uri"), track_title=item.get("name"), artists=artists,
            duration_ms=item.get("duration_ms"), progress_ms=data.get("progress_ms"),
            spotify_volume=device.get("volume_percent"), artwork_url=artwork)
    except (KeyError, TypeError, AttributeError, ValueError):
        raise SpotifyError("response_invalid") from None


class ReceiverState:
    """Pi monotonic clocks never cross hosts; expiry uses backend receipt time."""
    def __init__(self, *, clock=monotonic):
        self.clock, self.lock = clock, RLock()
        self.session, self.sequence, self.received, self.value = None, -1, 0, None

    def register(self, session):
        if not identifier(session):
            raise SpotifyError("invalid_request")
        with self.lock:
            if session != self.session:
                self.session, self.sequence, self.value = session, -1, None

    def accept(self, data):
        data = receiver(data)
        with self.lock:
            if data["session_id"] != self.session or data["sequence"] <= self.sequence:
                raise SpotifyError("receiver_session_expired")
            self.sequence, self.received, self.value = data["sequence"], self.clock(), data

    def read(self):
        with self.lock:
            if self.value is None or self.clock() - self.received >= 15:
                return {"stale": True, "connected": False, "logged_in": False,
                        "active": False, "status": "unknown", "spotify_volume": None}
            return {"stale": False, **{k: v for k, v in self.value.items()
                    if k not in {"version", "session_id", "sequence"}}}


class MusicController:
    def __init__(self, api, *, device_name="Cube", clock=monotonic, check=lambda: None, aliases_path=None):
        self.api, self.device_name, self.clock = api, device_name, clock
        self.check = check
        self.resolver = MusicResolver(api, aliases_path=aliases_path, clock=clock)
        self.lock, self.state, self.fetched, self.retry_at = RLock(), MusicState(), float("-inf"), 0
        self.completed = OrderedDict()
        self.resolved = OrderedDict()

    def read(self, *, max_age=15):
        with self.lock:
            if self.clock() < self.retry_at or not self.state.stale and self.clock() - self.fetched < max_age:
                return self.state
            try:
                self.state = snapshot(self.api.playback(), self.device_name)
                self.fetched = self.clock()
                self.retry_at = 0
            except SpotifyError as error:
                if error.code in {"not_linked", "reauthorize", "storage_invalid"}:
                    self.state = MusicState(error=error.code)
                else:
                    self.state = replace(self.state, stale=True, error=error.code)
                self.retry_at = self.clock() + max(15, error.retry_after or 0)
            return self.state

    def devices(self):
        with self.lock:
            return [{k: d.get(k) for k in ("id", "name", "is_active", "is_restricted", "supports_volume")}
                    for d in self.api.devices()]

    def cube(self):
        # Resolve before every command. Never fall back to the account's active
        # device; multiple exact names need human resolution, not a guess.
        devices = [d for d in self.api.devices() if d["name"] == self.device_name]
        if len(devices) > 1:
            raise SpotifyError("device_ambiguous")
        if not devices or not devices[0].get("id"):
            raise SpotifyError("device_unavailable")
        if devices[0]["is_restricted"]:
            raise SpotifyError("restricted")
        return devices[0]

    def search(self, query, kind):
        with self.lock:
            return self.api.search(query, kind)

    def execute(self, data):
        data = command(data)
        with self.lock:
            self.check()
            previous = self.completed.get(data["request_id"])
            if previous:
                original, error = previous
                if original != data:
                    raise SpotifyError("request_conflict")
                if error:
                    raise SpotifyError(error[0], retry_after=error[1])
                return {"accepted": True}
            error = ("service_unavailable", None)
            try:
                self._execute(data)
                error = None
            except SpotifyError as failure:
                error = (failure.code, failure.retry_after)
                raise
            finally:
                self.completed[data["request_id"]] = (data, error)
                if len(self.completed) > 128:
                    self.completed.popitem(last=False)
                # A 204 acknowledges a command, not its eventual playback state.
                self.state = replace(self.state, stale=True)
                self.retry_at = 0
            return {"accepted": True}

    def play_request(self, request_id, **arguments):
        """Resolve once per trusted request; retries cannot select a new result."""
        if not identifier(request_id):
            raise SpotifyError("invalid_request")
        with self.lock:
            self.check()
            previous = self.resolved.get(request_id)
            if previous:
                original, uri = previous
                if original != arguments:
                    raise SpotifyError("request_conflict")
            else:
                uri = self.resolver.resolve(**arguments)
                self.check()
                self.resolved[request_id] = (dict(arguments), uri)
                if len(self.resolved) > 128:
                    self.resolved.popitem(last=False)
            return self.execute({"request_id": request_id, "operation": "play_uri", "uri": uri})

    def _execute(self, data):
        device = self.cube()
        self.check()
        device_id, operation = device["id"], data["operation"]
        if operation == "play_uri":
            self.api.play(device_id, data["uri"])
        elif operation in {"play_music", "resume"}:
            current = self.api.playback()
            self.check()
            state = snapshot(current, self.device_name)
            if state.device_id and state.device_id != device_id:
                # One transfer with play=true avoids unordered transfer+play calls.
                self.api.transfer(device_id, play=True)
            elif state.device_id == device_id and state.playing:
                return
            elif current is None:
                # Spotify may retain a context even when GET returns 204.
                try:
                    self.api.play(device_id)
                except SpotifyError as failure:
                    if failure.code in {"device_unavailable", "request_rejected"}:
                        raise SpotifyError("nothing_to_resume") from None
                    raise
            else:
                self.api.play(device_id)
        elif operation == "transfer":
            self.api.transfer(device_id, play=True)
        else:
            if operation == "volume" and device.get("supports_volume") is False:
                raise SpotifyError("restricted")
            self.api.control(operation, device_id, data.get("uri", data.get("value")))

    def wire_state(self):
        state = asdict(self.read())
        state.pop("artwork_url")
        state["artists"] = list(state["artists"])
        return state
