import asyncio
from dataclasses import replace
import json
from pathlib import Path
from threading import Event
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, Mock, patch

import httpx

from audio.soloist import PlaybackState
from display.music import MusicProvider, parse_state, FRAME_BYTES, ZERO


def wire(**changes):
    return {"version": 1, "available": True, "playing": True, "track_id": "a" * 64,
            "artwork_id": "b" * 64, "duration_ms": 100000, "progress_ms": 20000,
            "snapshot_age_ms": 0, "valid_for_ms": 15000, **changes}


class ProviderTests(TestCase):
    def setUp(self):
        self.now = 100
        self.local = PlaybackState(connected=True, logged_in=True, active=True, status="playing")
        self.sent = []
        self.provider = MusicProvider("http://backend:8765", "cube", clock=lambda: self.now,
            read=lambda: self.local, send=lambda channel, data: self.sent.append(data))
        self.provider.snapshot = parse_state(wire(), 100, 100)

    def publish(self):
        self.provider.publish(None)
        return self.sent[-1].decode("ascii").split()

    def test_freshness_progress_pause_resume_and_disconnect(self):
        self.assertEqual(self.publish()[-1], "20000")
        self.now = 101
        self.assertEqual(self.publish()[-1], "21000")
        self.local = replace(self.local, status="paused")
        self.now = 102
        self.assertEqual(self.publish()[5], "2")
        self.now = 104
        # Old cloud 'playing' samples cannot move progress during a local pause.
        self.provider.snapshot = parse_state(wire(progress_ms=24000), 104, 104)
        self.assertEqual(self.publish()[-1], "22000")
        self.local = replace(self.local, status="playing")
        self.now = 105
        self.publish()
        self.now = 106
        self.assertEqual(self.publish()[-1], "23000")
        self.local = replace(self.local, status="buffering")
        self.publish()
        self.now = 107
        self.assertEqual(self.publish()[-1], "23000")
        self.local = replace(self.local, connected=False)
        self.assertEqual(self.publish()[5], "0")
        self.local = replace(self.local, connected=True)
        self.now = 119
        self.assertEqual(self.publish()[5], "0")

    def test_seek_and_unknown_position(self):
        self.publish()
        self.now += 5
        self.provider.snapshot = parse_state(wire(progress_ms=1000), 105, 105)
        self.assertEqual(self.publish()[-1], "1000")
        self.provider.snapshot = parse_state(wire(progress_ms=None, duration_ms=None), 105, 105)
        self.assertEqual(self.publish()[-2:], ["-1", "-1"])

    def test_artwork_periodic_resend_and_send_failure(self):
        self.provider.frame_id = "b" * 64
        self.provider.frame = b"\0" * FRAME_BYTES
        self.provider.publish(None)
        self.assertEqual(len(self.sent), 2)
        self.assertTrue(self.sent[1].startswith(b"music-art v1 "))
        self.provider.publish(None)
        self.assertEqual(len(self.sent), 3)
        self.provider.snapshot = parse_state(wire(track_id="c" * 64), 100, 100)
        self.provider.publish(None)
        self.assertEqual(len(self.sent), 5)  # same album is resent immediately for a new track
        self.now += 5
        self.provider.publish(None)
        self.assertEqual(len(self.sent), 7)
        self.provider.send = Mock(side_effect=BlockingIOError())
        self.provider.publish(None)  # missing renderer never breaks voice

    def test_strict_schema_and_conservative_network_lease(self):
        for changes in ({"version": True}, {"playing": 1}, {"track_id": "url"},
                        {"progress_ms": True}, {"valid_for_ms": 15001},
                        {"snapshot_age_ms": 1}, {"extra": "field"}):
            with self.assertRaises(ValueError):
                parse_state(wire(**changes), 100, 100)
        self.assertIsNone(parse_state(wire(), 100, 115))
        snapshot = parse_state(wire(), 100, 105)
        self.assertEqual(snapshot.expires, 115)
        self.assertIsNone(parse_state(wire(available=False), 100, 100))

    def test_configuration_rejects_credentials_and_non_backend_paths(self):
        for url in ("http://user:pass@backend", "http://backend/music", "ftp://backend", "http://backend?token=x"):
            with self.assertRaises(ValueError):
                MusicProvider(url, "cube")

    def test_thread_start_close_and_close_before_start(self):
        entered = Event()
        async def blocked(*args):
            entered.set()
            await asyncio.Future()
        self.provider.fetch = blocked
        self.provider.start()
        worker = self.provider.thread
        self.provider.start()
        self.assertIs(self.provider.thread, worker)
        try:
            self.assertTrue(entered.wait(2))
        finally:
            self.provider.close()
        self.assertFalse(worker.is_alive())
        self.provider.close()
        stopped = MusicProvider("http://backend", "cube")
        stopped.close()
        stopped.start()
        self.assertIsNone(stopped.thread)


class AsyncProviderTests(IsolatedAsyncioTestCase):
    async def test_fetch_auth_redirect_bounds_and_timeout(self):
        with TemporaryDirectory() as directory:
            token = Path(directory) / "token"
            token.write_text("x" * 32)
            provider = MusicProvider("http://backend", "cube", token_path=token)
            requests = []
            response = httpx.Response(200, json=wire())
            async def handle(request):
                requests.append(request)
                return httpx.Response(response.status_code, headers=response.headers,
                                      stream=httpx.ByteStream(response.content))
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle), follow_redirects=False) as http:
                self.assertEqual(json.loads(await provider.fetch(http, "/music/display/state", 4096)), wire())
                self.assertEqual(requests[0].headers["X-Cube-Token"], "x" * 32)
                self.assertEqual(requests[0].headers["X-Cube-Client-ID"], "cube")
                for response in (httpx.Response(302, headers={"Location": "http://other"}),
                                 httpx.Response(401), httpx.Response(200, content=b"x" * 4097)):
                    with self.assertRaises(ValueError):
                        await provider.fetch(http, "/music/display/state", 4096)
                self.assertEqual(len(requests), 4)

    async def test_delayed_artwork_cannot_replace_new_track(self):
        provider = MusicProvider("http://backend", "cube", clock=lambda: 100)
        provider.snapshot = parse_state(wire(), 100, 100)
        async def fetch(*args):
            provider.snapshot = parse_state(wire(track_id="c" * 64, artwork_id="d" * 64), 100, 100)
            provider.stopped.set()
            return b"\x00" * FRAME_BYTES
        provider.fetch = fetch
        with patch("display.music.asyncio.sleep", new=AsyncMock()):
            await provider.artwork(None)
        self.assertIsNone(provider.frame)
        self.assertEqual(provider.frame_id, ZERO)

    async def test_failed_poll_never_extends_snapshot(self):
        provider = MusicProvider("http://backend", "cube", clock=lambda: 100, read=PlaybackState)
        old = provider.snapshot = parse_state(wire(), 100, 100)
        async def fetch(*args):
            provider.stopped.set()
            raise httpx.ReadTimeout("offline")
        provider.fetch = fetch
        with patch("display.music.asyncio.sleep", new=AsyncMock()) as sleep:
            await provider.poll(None)
        self.assertIs(provider.snapshot, old)
        sleep.assert_awaited_once_with(15)

    async def test_local_publish_continues_during_slow_network_and_cancels(self):
        provider = MusicProvider("http://backend", "cube", read=PlaybackState, send=Mock())
        entered = asyncio.Event()
        async def blocked(*args):
            entered.set()
            await asyncio.Future()
        provider.fetch = blocked
        with patch("display.music.socket.socket") as socket_factory:
            task = asyncio.create_task(provider._run())
            await asyncio.wait_for(entered.wait(), 1)
            self.assertGreaterEqual(provider.send.call_count, 1)
            await asyncio.sleep(1.05)
            self.assertGreaterEqual(provider.send.call_count, 2)
            provider.stopped.set()
            task.cancel()
            await asyncio.wait_for(task, 1)
            socket_factory.return_value.__enter__.return_value.setblocking.assert_called_once_with(False)
