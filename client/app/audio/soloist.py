"""Read-only Soloist local events via upstream's ctl trace WebSocket client."""
import asyncio
from dataclasses import asdict, dataclass, replace
import json
from pathlib import Path
from time import monotonic

from .activity import atomic_json, read_json, runtime_dir
from .async_speech import finish_cleanup

SOLOIST = "/opt/cube/soloist/current/soloist"
DATA = Path.home() / ".local/share/cube/soloist"


@dataclass(frozen=True)
class PlaybackState:
    connected: bool = False
    logged_in: bool = False
    active: bool = False
    status: str = "unknown"
    spotify_volume: float | None = None
    updated: float = 0.0
    observed: float = 0.0


def read_state(path=None, *, now=None):
    """A crashed observer must never leave a permanently connected snapshot."""
    now = monotonic() if now is None else now
    try:
        state = PlaybackState(**read_json(path or runtime_dir() / "soloist.json"))
        if (not 0 <= now - state.observed < 2
                or any(type(v) is not bool for v in (state.connected, state.logged_in, state.active))
                or state.status not in {"unknown", "idle", "playing", "paused", "buffering"}
                or (state.spotify_volume is not None and (
                    type(state.spotify_volume) not in (int, float) or not 0 <= state.spotify_volume <= 100))):
            raise ValueError("Stale Soloist snapshot")
        return state
    except (OSError, ValueError, TypeError):
        return PlaybackState()


def normalize(state, event, now):
    """Keep only local availability/play state/volume; discard content metadata."""
    if not isinstance(event, dict):
        raise ValueError("Invalid Soloist event")
    kind = event.get("type")
    changes = {}
    if kind == "auth_state":
        if type(event.get("logged_in")) is not bool:
            raise ValueError("Invalid auth event")
        changes["logged_in"] = event["logged_in"]
        if not event["logged_in"]:
            return PlaybackState(connected=True, updated=now, observed=now)
    if kind in {"auth_state", "device_changed", "playback_state"} and "is_active" in event:
        if type(event["is_active"]) is not bool:
            raise ValueError("Invalid device event")
        changes["active"] = event["is_active"]
    if kind in {"playback_state", "playback_changed"}:
        if event.get("status") not in {"idle", "playing", "paused", "buffering"}:
            raise ValueError("Invalid playback event")
        changes["status"] = event["status"]
    if kind in {"playback_state", "volume_changed"} and "volume" in event:
        value = event["volume"]
        if type(value) not in (int, float) or not 0 <= value <= 100:
            raise ValueError("Invalid volume event")
        changes["spotify_volume"] = value
    if not changes:
        return state
    return replace(state, connected=True, updated=now, observed=now, **changes)


def endpoint(data=DATA):
    # Never allow an edited discovery file to turn this observer into a LAN
    # client. Pin the validated address explicitly, avoiding a discovery race.
    with (data / "ws.addr").open() as source:
        address = source.read(64).strip()
    with (data / "ws.port").open() as source:
        port = source.read(16).strip()
    if address != "127.0.0.1" or not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise ValueError("Soloist loopback endpoint unavailable")
    return f"127.0.0.1:{int(port)}"


def parse_trace(line):
    # Upstream trace format: <unix_epoch_ms> <json_event>.
    stamp, data = line.split(b" ", 1)
    if not stamp.isdigit() or len(line) > 262144:
        raise ValueError("Invalid Soloist trace")
    return json.loads(data)


class SoloistObserver:
    def __init__(self, publish, *, clock=monotonic, discover=endpoint,
                 spawn=asyncio.create_subprocess_exec, sleep=asyncio.sleep):
        self.publish, self.clock, self.discover = publish, clock, discover
        self.spawn, self.sleep = spawn, sleep
        self.state = PlaybackState()

    def update(self, state):
        self.state = state
        self.publish(state)

    async def session(self):
        creation = asyncio.create_task(self.spawn(SOLOIST, "ctl", "trace", "--ws", self.discover(),
                                 stdout=asyncio.subprocess.PIPE,
                                 stderr=asyncio.subprocess.DEVNULL, limit=262144))
        try:
            child = await asyncio.shield(creation)
            # The daemon sends auth_state on connect; ensure a hung ctl cannot
            # advertise a usable connection without completing that handshake.
            first = await asyncio.wait_for(child.stdout.readline(), 5)
            event = parse_trace(first)
            if event.get("type") != "auth_state":
                raise ValueError("Missing Soloist handshake")
            self.update(normalize(PlaybackState(), event, self.clock()))
            while True:
                try:
                    line = await asyncio.wait_for(child.stdout.readline(), 1)
                except TimeoutError:
                    line = None
                if line == b"":
                    break
                if self.clock() - self.state.observed >= 1:
                    self.update(replace(self.state, observed=self.clock()))
                if line is None:
                    continue
                try:
                    state = normalize(self.state, parse_trace(line), self.clock())
                except (ValueError, TypeError, KeyError):
                    continue
                if state != self.state:
                    self.update(state)
        finally:
            async def reap():
                child = await creation
                if child.returncode is None:
                    try:
                        child.terminate()
                    except ProcessLookupError:
                        pass
                    try:
                        await asyncio.wait_for(child.wait(), 1)
                    except TimeoutError:
                        child.kill()
                        await child.wait()
            await finish_cleanup(asyncio.create_task(reap()))

    async def run(self):
        delay = .5
        while True:
            started = self.clock()
            try:
                await self.session()
            except (OSError, ValueError, TypeError, TimeoutError):
                pass
            finally:
                self.update(PlaybackState(updated=self.clock(), observed=self.clock()))
            if self.clock() - started > 10:
                delay = .5
            await self.sleep(delay)
            delay = min(10, delay * 2)


def main():
    path = runtime_dir() / "soloist.json"
    def publish(state):
        atomic_json(path, asdict(state))
    try:
        asyncio.run(SoloistObserver(publish).run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
