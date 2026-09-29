"""Soloist protocol and reconnect tests without a receiver or Spotify account."""
import asyncio
from dataclasses import FrozenInstanceError, asdict
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock

from audio.activity import atomic_json
from audio.soloist import PlaybackState, SoloistObserver, endpoint, normalize, parse_trace, read_state


class NormalizationTests(unittest.TestCase):
    def test_crashed_bridge_snapshot_expires(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.json"
            atomic_json(path, asdict(PlaybackState(connected=True, observed=10)))
            self.assertTrue(read_state(path, now=11).connected)
            self.assertFalse(read_state(path, now=12).connected)
            self.assertFalse(read_state(path, now=1).connected)

    def test_auth_playback_phone_pause_volume_logout(self):
        state = normalize(PlaybackState(), {"type": "auth_state", "logged_in": True,
                                            "is_active": True}, 1)
        state = normalize(state, {"type": "playback_state", "status": "playing", "volume": 10,
                                  "item": {"decorations": {"identity": {"name": "discard"}}}}, 2)
        self.assertTrue(state.connected and state.logged_in and state.active)
        self.assertFalse(hasattr(state, "item"))
        state = normalize(state, {"type": "playback_changed", "status": "paused"}, 3)
        state = normalize(state, {"type": "volume_changed", "volume": 80}, 4)
        self.assertEqual((state.status, state.spotify_volume), ("paused", 80))
        with self.assertRaises(FrozenInstanceError):
            state.status = "playing"
        state = normalize(state, {"type": "auth_state", "logged_in": False}, 5)
        self.assertFalse(state.logged_in or state.active)
        self.assertIsNone(state.spotify_volume)

    def test_malformed_and_unknown_events(self):
        for event in ([], {"type": "auth_state", "logged_in": 1},
                      {"type": "playback_changed", "status": "garbage"},
                      {"type": "volume_changed", "volume": True},
                      {"type": "volume_changed", "volume": float("nan")},
                      {"type": "device_changed", "is_active": 1}):
            with self.subTest(event=event), self.assertRaises(ValueError):
                normalize(PlaybackState(), event, 1)
        state = PlaybackState(connected=True)
        self.assertIs(normalize(state, {"type": "future_event"}, 1), state)

    def test_trace_contract(self):
        self.assertEqual(parse_trace(b'123 {"type":"auth_state","logged_in":false}'),
                         {"type": "auth_state", "logged_in": False})
        for line in (b"invalid", b"stamp {}", b"123 not JSON"):
            with self.assertRaises(ValueError):
                parse_trace(line)

    def test_discovery_is_loopback_only_and_reread_on_reconnect(self):
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp)
            (data / "ws.addr").write_text("127.0.0.1")
            (data / "ws.port").write_text("9090")
            self.assertEqual(endpoint(data), "127.0.0.1:9090")
            (data / "ws.port").write_text("9091")
            self.assertEqual(endpoint(data), "127.0.0.1:9091")
            for address, port in (("0.0.0.0", "9090"), ("192.168.1.2", "9090"),
                                  ("localhost", "9090"), ("127.0.0.1", "0"),
                                  ("127.0.0.1", "65536")):
                (data / "ws.addr").write_text(address)
                (data / "ws.port").write_text(port)
                with self.assertRaises(ValueError):
                    endpoint(data)


class ObserverTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_read_loop_normalizes_and_reaps_only_trace_child(self):
        events = [{"type": "auth_state", "logged_in": True},
                  {"type": "playback_state", "status": "playing", "volume": 10},
                  {"type": "playback_changed", "status": "paused"}]
        reader = asyncio.StreamReader()
        for event in events:
            reader.feed_data(b"123 " + json.dumps(event).encode() + b"\n")
        reader.feed_eof()
        child = Mock(stdout=reader, returncode=None, wait=AsyncMock())
        spawn, publish = AsyncMock(return_value=child), Mock()
        observer = SoloistObserver(publish, spawn=spawn, discover=lambda: "127.0.0.1:9000")
        await observer.session()
        self.assertEqual(observer.state.status, "paused")
        self.assertEqual(spawn.call_args.args[1:], ("ctl", "trace", "--ws", "127.0.0.1:9000"))
        child.terminate.assert_called_once()
        child.wait.assert_awaited_once()

    async def test_reconnect_backoff_and_disconnected_state(self):
        delays = []
        async def sleep(delay):
            delays.append(delay)
            if len(delays) == 7:
                raise asyncio.CancelledError()
        publish = Mock()
        observer = SoloistObserver(publish, sleep=sleep, clock=lambda: 10)
        observer.session = AsyncMock(side_effect=OSError("offline"))
        with self.assertRaises(asyncio.CancelledError):
            await observer.run()
        self.assertEqual(delays, [.5, 1, 2, 4, 8, 10, 10])
        self.assertTrue(all(not call.args[0].connected for call in publish.call_args_list))

    async def test_cancel_during_spawn_retains_child_ownership(self):
        entered, released = asyncio.Event(), asyncio.Event()
        child = Mock(returncode=None, wait=AsyncMock())
        async def spawn(*args, **kwargs):
            entered.set()
            await released.wait()
            return child
        observer = SoloistObserver(Mock(), spawn=spawn, discover=lambda: "127.0.0.1:9000")
        task = asyncio.create_task(observer.session())
        await entered.wait()
        task.cancel()
        released.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        child.terminate.assert_called_once()
        child.wait.assert_awaited_once()
