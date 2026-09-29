"""Interruptible versions of the existing WAV/Piper path, owned by one turn."""
import asyncio
import tempfile
from time import monotonic

import httpx

from . import speech


async def finish_cleanup(task):
    """Do not lose process ownership on repeated cancellation during teardown."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            pass
    return task.result()


async def process(owner, args, *, text=None):
    owner.check()
    # Shield creation so cancellation cannot orphan a process between fork and
    # handle publication. No process-name kills: only this owned child is stopped.
    creation = asyncio.create_task(asyncio.create_subprocess_exec(
        *map(str, args), stdin=asyncio.subprocess.PIPE if text is not None else None,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL))
    child = None
    try:
        child = await asyncio.shield(creation)
        owner.check()
        await child.communicate(text.encode() if text is not None else None)
        owner.check()
        if child.returncode:
            raise OSError("Cube audio process failed")
    finally:
        async def reap():
            proc = child if child is not None else await creation
            if proc.returncode is None:
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), .1)
                except TimeoutError:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                    await proc.wait()
        await finish_cleanup(asyncio.create_task(reap()))


class Speech:
    def __init__(self, post_state, *, transport=None):
        self.post_state = post_state
        self.transport = transport
        self.output_lock = asyncio.Lock()

    async def play(self, owner, path, *, speaking=True, guard=None):
        owner.check()
        if guard is not None:
            await guard()
            owner.check()
        async with self.output_lock:
            owner.check()
            if speaking:
                self.post_state(owner, "speech 0.35")
                print(f"[BARGE] playback_start at={monotonic():.6f} interaction={owner.identifier}", flush=True)
            try:
                await process(owner, speech.pw_play_args(path))
            except OSError:
                if owner.receipt is not None:
                    from .playback import PlaybackOutcome
                    owner.receipt.finish(PlaybackOutcome.FAILED, "playback_failed")
                raise
            finally:
                if speaking:
                    print(f"[BARGE] playback_stopped at={monotonic():.6f} interaction={owner.identifier}", flush=True)
                    self.post_state(owner, "thinking")

    async def say(self, owner, text, *, guard=None):
        text = str(text).strip()
        if not text:
            return False
        try:
            owner.check()
            async with httpx.AsyncClient(transport=self.transport, follow_redirects=True) as client:
                async with asyncio.timeout(speech.TTS_TIMEOUT):
                    response = await client.post(
                        speech.TTS_ENDPOINT, json={"text": text},
                        headers={"X-Cube-Interaction-ID": owner.identifier},
                        timeout=speech.TTS_TIMEOUT)
                    response.raise_for_status()
                    wav = response.content
            owner.check()
            with tempfile.NamedTemporaryFile(prefix="cube-kokoro-", suffix=".wav") as tmp:
                tmp.write(wav)
                tmp.flush()
                await self.play(owner, tmp.name, guard=guard)
            return True
        except (httpx.HTTPError, TimeoutError, OSError):
            owner.check()  # Cancellation never starts Piper fallback.
        owner.check()
        try:
            with tempfile.NamedTemporaryFile(prefix="cube-piper-", suffix=".wav") as tmp:
                await process(owner, [speech.PIPER, "--model", speech.VOICE,
                                      "--output_file", tmp.name], text=text)
                await self.play(owner, tmp.name, guard=guard)
            return True
        except OSError:
            owner.check()
            return False

    async def cue(self, owner):
        with tempfile.NamedTemporaryFile(suffix=".wav") as tmp:
            tmp.write(speech.progress_wav())
            tmp.flush()
            try:
                await self.play(owner, tmp.name, speaking=False)
            except OSError:
                owner.check()
