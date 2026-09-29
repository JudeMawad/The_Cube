"""Display projection stays read-only and artwork is bounded/untrusted input."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from hashlib import sha256
from fractions import Fraction
import struct
from threading import Event
from unittest import TestCase
from unittest.mock import Mock, patch
import zlib

import httpx
import av
import numpy as np

from features.music.controller import MusicController
from features.music.display import MusicDisplay, MAX_BYTES, artwork_url, prepare_artwork
from integrations.spotify.errors import SpotifyError


def png(width=120, height=60):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress((b"\x00" + b"\xff\x00\x00" * width) * height)) + chunk(b"IEND", b""))


class MusicDisplayTests(TestCase):
    def setUp(self):
        self.now = 100
        self.api = Mock()
        self.api.playback.return_value = {"device": {"name": "Cube", "id": "cube"},
            "is_playing": True, "progress_ms": 5000, "item": {"uri": "spotify:track:" + "a" * 22,
            "name": "Track", "duration_ms": 100000, "artists": [],
            "album": {"images": [{"url": "https://i.scdn.co/image/abc"}]}}}
        self.controller = MusicController(self.api, clock=lambda: self.now)
        self.requests = []
        self.response = httpx.Response(200, content=png())
        def request(req):
            self.requests.append(req)
            return httpx.Response(self.response.status_code, headers=self.response.headers,
                                  stream=httpx.ByteStream(self.response.content))
        http = httpx.Client(transport=httpx.MockTransport(request))
        self.display = MusicDisplay(self.controller, http, clock=lambda: self.now)
        self.addCleanup(self.display.close)

    def test_freshness_old_wire_and_read_only(self):
        old = self.controller.wire_state()
        self.assertNotIn("artwork_url", old)
        state = self.display.state()
        self.assertTrue(state["available"])
        self.assertEqual(state["valid_for_ms"], 15000)
        self.assertEqual(state["track_id"], sha256(self.controller.state.track_uri.encode()).hexdigest())
        self.assertEqual(self.api.playback.call_count, 1)
        self.now += 4
        self.assertEqual(self.display.state()["valid_for_ms"], 11000)
        self.now += 1
        self.display.state()
        self.assertEqual(self.api.playback.call_count, 2)
        self.api.play.assert_not_called()
        self.api.control.assert_not_called()
        self.api.search.assert_not_called()
        self.assertEqual(self.controller.completed, {})

    def test_unavailable_transferred_episode_and_rate_limit(self):
        self.display.state()
        for code in ("rate_limited", "network_unavailable", "reauthorize"):
            self.now += 60
            self.api.playback.side_effect = SpotifyError(code, retry_after=30)
            state = self.display.state()
            self.assertFalse(state["available"])
            self.assertIsNone(state["artwork_id"])
            count = self.api.playback.call_count
            self.now += 5
            self.display.state()
            self.assertEqual(self.api.playback.call_count, count)
        self.api.playback.side_effect = None
        self.now += 60
        self.api.playback.return_value["device"]["name"] = "Phone"
        self.assertFalse(self.display.state()["available"])
        self.now += 5
        self.api.playback.return_value["device"]["name"] = "Cube"
        self.api.playback.return_value["item"]["uri"] = "spotify:episode:x"
        self.assertFalse(self.display.state()["available"])

    def test_fitted_frame_cache_and_no_credentials(self):
        key = self.display.state()["artwork_id"]
        frame = self.display.artwork(key)
        self.assertEqual(len(frame), 12288)
        def pixel(x, y):
            return frame[(y * 64 + x) * 3:(y * 64 + x + 1) * 3]
        self.assertEqual(pixel(8, 18), b"\xff\x00\x00")
        self.assertEqual(pixel(55, 41), b"\xff\x00\x00")
        for x, y in ((7, 18), (56, 18), (8, 17), (8, 42), (8, 63)):
            self.assertEqual(pixel(x, y), b"\x00" * 3)
        self.assertEqual(self.display.artwork(key), frame)
        self.assertEqual(len(self.requests), 1)
        self.assertNotIn("authorization", self.requests[0].headers)
        self.assertNotIn("x-cube-token", self.requests[0].headers)

    def test_only_spotify_image_urls_and_known_id(self):
        for url in ("http://i.scdn.co/image/x", "https://i.scdn.co.evil/x", "https://127.0.0.1/x",
                    "https://user:pass@i.scdn.co/x", "https://i.scdn.co:444/x",
                    "https://i.scdn.co/x?token=secret", "https://i.scdn.co/\nx"):
            self.assertIsNone(artwork_url(url))
        self.assertIsNone(self.display.artwork("../x"))
        self.assertIsNone(self.display.artwork("a" * 64))
        self.assertEqual(self.requests, [])

    def test_prepared_viewport_keeps_existing_position_for_native_layout(self):
        # Native rendering enlarges the x=8..55, y=6..53 viewport to 58x58.
        # Backend placement must not also change with the native layout.
        for width, height, w, h in ((120, 120, 48, 48), (120, 60, 48, 24),
                                    (60, 120, 24, 48), (120, 77, 48, 31)):
            with self.subTest(size=(width, height)):
                pixels = np.frombuffer(prepare_artwork(png(width, height)), dtype=np.uint8).reshape(64, 64, 3)
                expected = np.zeros((64, 64, 3), dtype=np.uint8)
                left, top = (64 - w) // 2, (60 - h) // 2
                expected[top:top + h, left:left + w] = (255, 0, 0)
                np.testing.assert_array_equal(pixels, expected)

    def test_bad_artwork_redirect_status_limits_and_retry_bound(self):
        key = self.display.state()["artwork_id"]
        for response in (httpx.Response(302, headers={"Location": "http://localhost/secret"}),
                         httpx.Response(429), httpx.Response(500),
                         httpx.Response(200, content=b"not an image"),
                         httpx.Response(200, headers={"Content-Length": str(MAX_BYTES + 1)}),
                         httpx.Response(200, content=b"x" * (MAX_BYTES + 1))):
            self.now += 30
            self.response = response
            self.assertIsNone(self.display.artwork(key))
            count = len(self.requests)
            self.assertIsNone(self.display.artwork(key))
            self.assertEqual(len(self.requests), count)

    def test_dimensions_rejected_before_decode(self):
        with patch("features.music.display.av.CodecContext") as decoder:
            for body in (png(2049, 1), png(1, 2049), b"GIF89a", png()[:24]):
                with self.assertRaises(ValueError):
                    prepare_artwork(body)
            decoder.create.assert_not_called()

    def test_jpeg_portrait_fitted_and_malformed_decode(self):
        encoder = av.CodecContext.create("mjpeg", "w")
        encoder.width, encoder.height, encoder.pix_fmt = 60, 120, "yuvj420p"
        encoder.time_base = Fraction(1, 1)
        frame = av.VideoFrame.from_ndarray(np.full((120, 60, 3), 200, dtype=np.uint8), format="rgb24")
        body = b"".join(bytes(packet) for packet in encoder.encode(frame) + encoder.encode(None))
        prepared = prepare_artwork(body)
        pixels = np.frombuffer(prepared, dtype=np.uint8).reshape(64, 64, 3)
        self.assertEqual(pixels[:, :20].max(), 0)
        self.assertGreater(pixels[6:54, 20:44].min(), 190)
        self.assertEqual(pixels[:6].max(), 0)
        self.assertEqual(pixels[54:].max(), 0)
        key = self.display.state()["artwork_id"]
        self.response = httpx.Response(200, content=png()[:33])
        self.assertIsNone(self.display.artwork(key))

    def test_network_timeout_and_expired_download_do_not_mutate(self):
        key = self.display.state()["artwork_id"]
        with patch.object(self.display.http, "stream", side_effect=httpx.ReadTimeout("timeout")):
            self.assertIsNone(self.display.artwork(key))
        self.api.control.assert_not_called()
        self.api.play.assert_not_called()
        self.now += 30
        outer = self
        class SlowStream(httpx.SyncByteStream):
            def __iter__(self):
                outer.now += 7
                yield png()
        with patch.object(self.display.http, "stream") as stream:
            stream.return_value.__enter__.return_value = httpx.Response(200, stream=SlowStream())
            with patch("features.music.display.prepare_artwork") as prepare:
                self.assertIsNone(self.display.artwork(key))
                prepare.assert_not_called()

    def test_cache_is_eight_entries(self):
        first = self.display.state()["artwork_id"]
        self.display.artwork(first)
        for index in range(10):
            self.controller.state = replace(self.controller.state, artwork_url=f"https://i.scdn.co/image/{index}")
            key = self.display.state()["artwork_id"]
            self.response = httpx.Response(200, content=png())
            self.display.artwork(key)
        self.assertEqual(len(self.display.urls), 8)
        self.assertEqual(len(self.display.frames), 8)
        self.assertNotIn(first, self.display.urls)
        self.assertIsNone(self.display.artwork(first))

    def test_slow_artwork_never_holds_controller_or_duplicates_fetch(self):
        entered, release = Event(), Event()
        key = self.display.state()["artwork_id"]
        def decode(body):
            entered.set()
            release.wait(2)
            return b"\x00" * 12288
        with patch("features.music.display.prepare_artwork", side_effect=decode), ThreadPoolExecutor() as pool:
            pending = pool.submit(self.display.artwork, key)
            self.assertTrue(entered.wait(1))
            try:
                self.assertIsNone(self.display.artwork(key))
                self.assertTrue(self.controller.lock.acquire(blocking=False))
                self.controller.lock.release()
                self.assertTrue(self.display.state()["available"])
            finally:
                release.set()
            self.assertEqual(len(pending.result()), 12288)
        self.assertEqual(len(self.requests), 1)
