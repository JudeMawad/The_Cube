"""Optional display-only worker. No playback commands or audio lifecycle ownership."""
import asyncio
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import socket
from threading import Event, Thread
import time
from urllib.parse import urlsplit

import httpx

from audio.soloist import read_state
from . import transport

TOKEN_PATH = Path.home() / ".config/cube/events.token"
FRAME_BYTES = 64 * 64 * 3
IDENTITY = re.compile(r"[0-9a-f]{64}\Z")
ZERO = "0" * 64


@dataclass(frozen=True)
class Snapshot:
    track: str
    artwork: str
    playing: bool
    duration: int
    progress: int
    age: int
    received: float
    expires: float


def parse_state(data, started, now):
    keys = {"version", "available", "playing", "track_id", "artwork_id", "duration_ms",
            "progress_ms", "snapshot_age_ms", "valid_for_ms"}
    if (type(data) is not dict or set(data) != keys or type(data["version"]) is not int
            or data["version"] != 1 or type(data["available"]) is not bool
            or type(data["playing"]) is not bool):
        raise ValueError("invalid music display state")
    for key in ("snapshot_age_ms", "valid_for_ms"):
        if type(data[key]) is not int or not 0 <= data[key] <= 15000:
            raise ValueError("invalid music display age")
    for key in ("duration_ms", "progress_ms"):
        if data[key] is not None and (type(data[key]) is not int or not 0 <= data[key] <= 2**53):
            raise ValueError("invalid music display position")
    for key in ("track_id", "artwork_id"):
        if data[key] is not None and (not isinstance(data[key], str) or not IDENTITY.fullmatch(data[key])):
            raise ValueError("invalid music display identity")
    if not data["available"]:
        return None
    if not data["track_id"] or data["snapshot_age_ms"] + data["valid_for_ms"] > 15000:
        raise ValueError("invalid music display lease")
    expires = started + data["valid_for_ms"] / 1000
    if expires <= now:
        return None
    return Snapshot(data["track_id"], data["artwork_id"] or ZERO, data["playing"],
                    data["duration_ms"] if data["duration_ms"] is not None else -1,
                    data["progress_ms"] if data["progress_ms"] is not None else -1,
                    data["snapshot_age_ms"], now, expires)


def metadata(epoch, sequence, expires_ms, status, track, artwork, duration, progress):
    return (f"music v1 {epoch} {sequence} {expires_ms} {status} "
            f"{track} {artwork} {duration} {progress}").encode("ascii")


def artwork_packet(epoch, sequence, track, artwork, frame):
    if len(frame) != FRAME_BYTES:
        raise ValueError("invalid music frame")
    return f"music-art v1 {epoch} {sequence} {track} {artwork}\n".encode("ascii") + frame


class MusicProvider:
    def __init__(self, base_url, client_id, *, token_path=TOKEN_PATH,
                 clock=time.monotonic, read=read_state, send=transport.send_state):
        parsed = urlsplit(base_url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
            raise ValueError("invalid music backend URL")
        self.url, self.client_id, self.token_path = base_url.rstrip("/"), client_id, token_path
        self.clock, self.read, self.send = clock, read, send
        self.epoch, self.sequence = time.monotonic_ns(), 0
        self.snapshot = self.applied = None
        self.frame, self.frame_id, self.next_art = None, ZERO, 0
        self.track, self.status, self.progress, self.position_at = ZERO, 0, -1, 0
        self.stopped, self.thread, self.loop, self.task = Event(), None, None, None

    def headers(self):
        token = self.token_path.read_text().strip()
        if len(token) < 32 or not token.isascii() or any(c.isspace() for c in token):
            raise ValueError("Cube authentication unavailable")
        return {"X-Cube-Token": token, "X-Cube-Client-ID": self.client_id}

    async def fetch(self, http, path, limit):
        async with asyncio.timeout(6):
            async with http.stream("GET", self.url + path, headers=self.headers()) as response:
                if response.status_code != 200:
                    raise ValueError("music display unavailable")
                if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    raise ValueError("encoded music display response")
                body = bytearray()
                async for chunk in response.aiter_raw():
                    if len(body) + len(chunk) > limit:
                        raise ValueError("music display response too large")
                    body.extend(chunk)
                return bytes(body)

    async def poll(self, http):
        while not self.stopped.is_set():
            started = self.clock()
            try:
                body = await self.fetch(http, "/music/display/state", 4096)
                self.snapshot = parse_state(json.loads(body), started, self.clock())
            except (httpx.HTTPError, OSError, ValueError, TypeError, KeyError, TimeoutError):
                # An error cannot renew the old snapshot's expiry.
                pass
            local = self.read()
            active = local.connected and local.logged_in and local.active
            await asyncio.sleep(5 if active else 15)

    async def artwork(self, http):
        while not self.stopped.is_set():
            snapshot = self.snapshot
            if (snapshot and snapshot.expires > self.clock() and snapshot.artwork != ZERO
                    and snapshot.artwork != self.frame_id):
                try:
                    frame = await self.fetch(http, "/music/display/artwork/" + snapshot.artwork, FRAME_BYTES)
                    if len(frame) != FRAME_BYTES:
                        raise ValueError("invalid music frame")
                    current = self.snapshot
                    if (current and current.expires > self.clock()
                            and (current.track, current.artwork) == (snapshot.track, snapshot.artwork)):
                        self.frame, self.frame_id, self.next_art = frame, snapshot.artwork, 0
                except (httpx.HTTPError, OSError, ValueError, TimeoutError):
                    pass
            await asyncio.sleep(5)

    def publish(self, channel):
        now, local, snapshot = self.clock(), self.read(), self.snapshot
        available = (snapshot and snapshot.expires > now and local.connected and local.logged_in
                     and local.active and local.status in {"playing", "paused", "buffering"})
        status = {"playing": 1, "paused": 2, "buffering": 3}[local.status] if available else 0
        track = snapshot.track if available else ZERO
        # Advance the previously observed position up to a local pause/buffering
        # edge. Repeated cloud samples that predate that edge cannot unfreeze it.
        if self.status == 1 and self.progress >= 0:
            self.progress += max(0, int((now - self.position_at) * 1000))
        if available and (track != self.track or snapshot is not self.applied):
            if track != self.track:
                self.next_art = 0
            if track != self.track or (snapshot.playing == (status == 1) and status != 3):
                self.progress = snapshot.progress
                if self.progress >= 0 and snapshot.playing:
                    self.progress += snapshot.age + max(0, int((now - snapshot.received) * 1000))
            self.applied = snapshot
        if not available:
            self.progress = -1
        self.track, self.status, self.position_at = track, status, now
        duration = snapshot.duration if available else -1
        if duration >= 0:
            self.progress = min(self.progress, duration)
        self.sequence += 1
        art = snapshot.artwork if available else ZERO
        packet = metadata(self.epoch, self.sequence, int(snapshot.expires * 1000) if available else 0,
                          status, track, art, duration, self.progress)
        try:
            self.send(channel, packet)
            # Periodic resend also repairs datagram loss and renderer restarts.
            if available and self.frame_id == art and self.frame is not None and now >= self.next_art:
                self.send(channel, artwork_packet(self.epoch, self.sequence, track, art, self.frame))
                self.next_art = now + 5
        except OSError:
            pass

    async def _run(self):
        self.loop, self.task = asyncio.get_running_loop(), asyncio.current_task()
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as channel:
                channel.setblocking(False)
                async with httpx.AsyncClient(timeout=httpx.Timeout(3, connect=2),
                                             headers={"Accept-Encoding": "identity"},
                                             follow_redirects=False, trust_env=False) as http:
                    async with asyncio.TaskGroup() as group:
                        group.create_task(self.poll(http))
                        group.create_task(self.artwork(http))
                        while not self.stopped.is_set():
                            self.publish(channel)
                            await asyncio.sleep(1)
                        # Covers close() racing startup before task was installed.
                        raise asyncio.CancelledError()
        except asyncio.CancelledError:
            pass

    def start(self):
        if self.thread is None and not self.stopped.is_set():
            self.thread = Thread(target=lambda: asyncio.run(self._run()), name="cube-music-display", daemon=True)
            self.thread.start()

    def close(self):
        self.stopped.set()
        if self.loop and self.task and self.loop.is_running():
            try:
                self.loop.call_soon_threadsafe(self.task.cancel)
            except RuntimeError:
                pass  # Worker already closed its loop during shutdown.
        if self.thread:
            self.thread.join(timeout=2)


def configured_provider(base_url, client_id):
    return MusicProvider(os.environ.get("CUBE_SPOTIFY_BACKEND_URL", base_url), client_id)
