"""Check actual playback call sites without HTTP, audio hardware, or Spotify."""
import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx

from audio import speech
from audio.async_speech import Speech
from interaction import Interaction


def assert_speech_stream(test, args):
    test.assertEqual(args[0], "/usr/bin/pw-play")
    role = args[args.index("--media-role") + 1]
    test.assertEqual(role, "Communication")
    props = json.loads(args[args.index("--properties") + 1])
    test.assertEqual(props["application.name"], "Cube Speech")
    test.assertEqual(props["application.id"], "cube.speech")
    test.assertEqual(props["node.name"], "cube.speech")
    test.assertIs(props["state.restore-props"], False)
    # No property may override the CLI role back to Music. WirePlumber uses
    # role before application.id/name when forming its saved stream-state key.
    test.assertNotEqual(props.get("media.role", role), "Music")
    test.assertEqual(args[args.index("--target") + 1], "cube.assistant")
    test.assertIs(props["node.dont-fallback"], True)
    test.assertIs(props["node.dont-move"], True)
    test.assertIs(props["state.restore-target"], False)
    test.assertNotIn("target.object", props)
    test.assertNotIn("--volume", args)


class AsyncStreamIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_kokoro_and_piper_playback(self):
        for status in (200, 503):
            with self.subTest(status=status):
                player = Speech(Mock(), transport=httpx.MockTransport(
                    lambda request: httpx.Response(status, content=b"wav")))
                owner = Interaction(1)
                with patch("audio.async_speech.process", new_callable=AsyncMock) as process:
                    self.assertTrue(await player.say(owner, "Hello"))
                self.assertEqual(process.await_count, 1 if status == 200 else 2)
                call = process.await_args_list[-1]
                self.assertIs(call.args[0], owner)
                assert_speech_stream(self, call.args[1])
                if status == 503:
                    self.assertEqual(process.await_args_list[0].args[1][0], speech.PIPER)

    async def test_cue_uses_same_identity(self):
        player = Speech(Mock())
        with patch("audio.async_speech.process", new_callable=AsyncMock) as process:
            await player.cue(Interaction(1))
        process.assert_awaited_once()
        assert_speech_stream(self, process.await_args.args[1])
        player.post_state.assert_not_called()
