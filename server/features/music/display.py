"""Read-only display projection and bounded artwork cache, never playback control."""
from collections import OrderedDict
from hashlib import sha256
import re
import struct
from threading import Lock
from time import monotonic
from urllib.parse import urlsplit

import av
import httpx
import numpy as np

MAX_BYTES = 2 * 1024 * 1024
FRAME_BYTES = 64 * 64 * 3
IDENTITY = re.compile(r"[0-9a-f]{64}\Z")


def artwork_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        return None
    try:
        url = urlsplit(value)
        if (url.scheme == "https" and url.hostname in {"i.scdn.co", "mosaic.scdn.co"}
                and url.port in (None, 443) and not url.username and not url.password
                and not url.query and not url.fragment and url.path.startswith("/")
                and not any(ord(c) < 33 or ord(c) > 126 for c in value)):
            return value
    except ValueError:
        pass
    return None


def dimensions(body):
    """Bound image allocation before invoking a decoder; only PNG/JPEG allowed."""
    if body.startswith(b"\x89PNG\r\n\x1a\n") and len(body) >= 33 and body[8:16] == b"\x00\x00\x00\rIHDR":
        width, height = struct.unpack(">II", body[16:24])
        codec = "png"
    elif body.startswith(b"\xff\xd8"):
        index, width, height, codec = 2, 0, 0, "mjpeg"
        while index + 4 <= len(body):
            if body[index] != 255:
                break
            while index < len(body) and body[index] == 255:
                index += 1
            if index >= len(body):
                break
            marker = body[index]
            index += 1
            if marker in (0xD9, 0xDA):
                break
            length = int.from_bytes(body[index:index + 2], "big")
            if length < 2 or index + length > len(body):
                break
            if marker in {0xC0, 0xC1, 0xC2} and length >= 8:
                height, width = struct.unpack(">HH", body[index + 3:index + 7])
                break
            index += length
    else:
        raise ValueError("unsupported artwork")
    if not (1 <= width <= 2048 and 1 <= height <= 2048 and width * height <= 4_194_304):
        raise ValueError("artwork dimensions invalid")
    return codec, width, height


def prepare_artwork(body):
    if len(body) > MAX_BYTES:
        raise ValueError("artwork too large")
    codec, width, height = dimensions(body)
    decoder = av.CodecContext.create(codec, "r")
    decoder.thread_count = 1
    decoder.options = {"max_pixels": "4194304"}
    frames = decoder.decode(av.Packet(body))
    if len(frames) != 1 or (frames[0].width, frames[0].height) != (width, height):
        raise ValueError("artwork frame invalid")
    scale = min(48 / width, 48 / height)
    w, h = max(1, round(width * scale)), max(1, round(height * scale))
    pixels = frames[0].reformat(width=w, height=h, format="rgba").to_ndarray()
    rgb = (pixels[:, :, :3].astype(np.uint16) * pixels[:, :, 3:4] // 255).astype(np.uint8)
    output = np.zeros((64, 64, 3), dtype=np.uint8)
    left, top = (64 - w) // 2, (60 - h) // 2
    output[top:top + h, left:left + w] = rgb
    return output.tobytes()


class MusicDisplay:
    def __init__(self, controller, http, *, clock=monotonic):
        self.controller, self.http, self.clock = controller, http, clock
        self.lock, self.fetch_lock = Lock(), Lock()
        self.urls, self.frames, self.retry = OrderedDict(), OrderedDict(), {}

    def state(self):
        # Capture the state and its timestamp atomically. Decoding/network artwork
        # work is deliberately outside the controller's command lock.
        with self.controller.lock:
            state = self.controller.read(max_age=5)
            age = max(0, self.controller.clock() - self.controller.fetched)
        valid = state.linked and not state.stale and age < 15
        track = state.track_uri if state.track_uri and state.track_uri.startswith("spotify:track:") else None
        available = bool(valid and state.on_cube and track)
        art = None
        url = artwork_url(state.artwork_url) if available else None
        if url:
            art = sha256(url.encode()).hexdigest()
            with self.lock:
                self.urls[art] = url
                self.urls.move_to_end(art)
                while len(self.urls) > 8:
                    key, _ = self.urls.popitem(last=False)
                    self.frames.pop(key, None)
                    self.retry.pop(key, None)
        return {"version": 1, "available": available, "playing": bool(available and state.playing),
                "track_id": sha256(track.encode()).hexdigest() if available else None,
                "artwork_id": art, "duration_ms": state.duration_ms if available else None,
                "progress_ms": state.progress_ms if available else None,
                "snapshot_age_ms": min(15000, int(age * 1000)) if age < 15 else 15000,
                "valid_for_ms": max(0, int((15 - age) * 1000)) if available else 0}

    def artwork(self, identity):
        if not IDENTITY.fullmatch(identity):
            return None
        with self.lock:
            if identity in self.frames:
                return self.frames[identity]
            url = self.urls.get(identity)
            if not url or self.clock() < self.retry.get(identity, 0):
                return None
        # One bounded fetch/decode at a time; concurrent callers get a placeholder.
        if not self.fetch_lock.acquire(blocking=False):
            return None
        try:
            with self.lock:
                if identity in self.frames:
                    return self.frames[identity]
                if self.urls.get(identity) != url or self.clock() < self.retry.get(identity, 0):
                    return None
                self.retry[identity] = self.clock() + 30
            body = bytearray()
            deadline = self.clock() + 6
            with self.http.stream("GET", url, headers={"Accept": "image/jpeg, image/png",
                                                      "Accept-Encoding": "identity"},
                                  follow_redirects=False) as response:
                if (response.status_code != 200
                        or response.headers.get("Content-Encoding", "identity").lower() != "identity"):
                    return None
                length = response.headers.get("Content-Length")
                if length is not None and (not length.isdecimal() or int(length) > MAX_BYTES):
                    return None
                for chunk in response.iter_raw():
                    if len(body) + len(chunk) > MAX_BYTES or self.clock() > deadline:
                        return None
                    body.extend(chunk)
            frame = prepare_artwork(bytes(body))
            with self.lock:
                if self.urls.get(identity) == url:
                    self.frames[identity] = frame
                    self.retry.pop(identity, None)
            return frame
        except (httpx.HTTPError, ValueError, av.FFmpegError, OverflowError):
            return None
        finally:
            with self.lock:
                if identity not in self.urls:
                    self.retry.pop(identity, None)
            self.fetch_lock.release()

    def close(self):
        self.http.close()
