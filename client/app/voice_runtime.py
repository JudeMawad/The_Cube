"""Background I/O for the continuously running Pi frame coordinator."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from queue import SimpleQueue, Empty
from threading import Thread, Event

import httpx

from audio.async_speech import Speech, finish_cleanup
from audio.playback import PlaybackInterrupted, PlaybackOutcome
from audio.speaker_control import handle_speaker_command


class VoiceRuntime:
    def __init__(self, endpoint, client_id, controls, make_wav, listening_seconds, *, transport=None):
        self.endpoint, self.client_id, self.controls = endpoint, client_id, controls
        self.make_wav, self.listening_seconds = make_wav, listening_seconds
        self.transport = transport
        self.events = SimpleQueue()
        self.loop = asyncio.new_event_loop()
        self.ready = Event()
        self.tasks = set()
        self.thread = Thread(target=self._run, name="cube-voice-io", daemon=True)
        self.thread.start()
        self.ready.wait()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        # Only blocking local speaker/display adapters use these workers. HTTP
        # and synthesis subprocesses are asynchronous and do not occupy them.
        self.loop.set_default_executor(ThreadPoolExecutor(max_workers=2, thread_name_prefix="cube-local"))
        self.speech = Speech(lambda owner, state: self.events.put((owner, "state", state)),
                             transport=self.transport)
        self.ready.set()
        self.loop.run_forever()
        self.loop.run_until_complete(self.loop.shutdown_default_executor())
        self.loop.close()

    def _schedule(self, owner, coroutine):
        with owner.lock:
            owner.check()
            owner.future = asyncio.run_coroutine_threadsafe(self._tracked(coroutine), self.loop)

    async def _tracked(self, coroutine):
        task = asyncio.current_task()
        self.tasks.add(task)
        try:
            return await coroutine
        finally:
            self.tasks.discard(task)

    def submit(self, owner, audio):
        receipt, headers = self.controls.begin_request()
        owner.attach_receipt(receipt)
        headers["X-Cube-Interaction-ID"] = owner.identifier
        if owner.wake_preroll:
            headers["X-Cube-Wake-Preroll"] = "1"
        self._schedule(owner, self._voice(owner, audio, headers))

    async def _cue(self, owner):
        await asyncio.sleep(1.5)
        owner.check()
        await self.speech.cue(owner)

    async def _blocking(self, owner, operation):
        def guarded():
            with owner.lock:
                owner.check()
                # Crossing this guarded boundary starts the operation. A
                # Bluetooth action already in flight is not undone by a wake.
            return operation()
        task = asyncio.create_task(asyncio.to_thread(guarded))
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            await finish_cleanup(task)
            raise
        owner.check()
        return result

    async def _voice(self, owner, audio, headers):
        cue = asyncio.create_task(self._cue(owner))
        played, duration = False, 0
        try:
            owner.check()
            try:
                async with httpx.AsyncClient(transport=self.transport, follow_redirects=True) as client:
                    async with asyncio.timeout(45):
                        response = await client.post(self.endpoint,
                            files={"file": ("command.wav", self.make_wav(audio), "audio/wav")},
                            headers=headers, timeout=45)
                        response.raise_for_status()
                        result = response.json()
                owner.check()
                if type(result) is not dict:
                    raise ValueError("Invalid voice response")
                result.pop("_cube_playback", None)
                local = await self._blocking(owner, lambda: handle_speaker_command(
                    str(result.get("transcript", ""))))
                if local is not None:
                    result.update(local)
            except (httpx.HTTPError, TimeoutError, ValueError):
                owner.check()
                result = {"response": "I can't reach the server right now. Please try again."}
            if owner.receipt is not None:
                owner.receipt.authorize(result)
            duration = self.listening_seconds(result)
            if result.get("success") is True and result.get("action") == "cube_mute":
                duration = 0
            elif result.get("response"):
                async def guard():
                    # Stop any pending/playing cue before acquiring reply output.
                    cue.cancel()
                    await asyncio.gather(cue, return_exceptions=True)
                    owner.check()
                    if result.get("success") is True and result.get("action") in {
                        "cube_get_volume", "cube_set_volume", "cube_adjust_volume", "cube_unmute"}:
                        await self._blocking(owner, self.controls.refresh_volume_display)
                played = await self.speech.say(owner, result["response"], guard=guard)
            owner.check()
            self.events.put((owner, "done", (played, duration)))
        except (asyncio.CancelledError, PlaybackInterrupted):
            pass
        except Exception:
            if not owner.cancelled.is_set():
                self.events.put((owner, "done", (False, 0)))
        finally:
            cue.cancel()
            async def collect_cue():
                await asyncio.gather(cue, return_exceptions=True)
            await finish_cleanup(asyncio.create_task(collect_cue()))

    def notification(self, owner, notifications, item):
        self._schedule(owner, self._notification(owner, notifications, item))

    async def _notification(self, owner, notifications, item):
        played = False
        try:
            async def guard():
                owner.check()
                valid = await notifications.validate_async(item, owner.identifier)
                owner.check()
                if not valid:
                    raise PlaybackInterrupted("Obsolete notification")
            await guard()
            played = await self.speech.say(owner, item.payload["response"], guard=guard)
            owner.check()
            self.events.put((owner, "done", (played, 0)))
        except (asyncio.CancelledError, PlaybackInterrupted):
            if not owner.cancelled.is_set():
                self.events.put((owner, "done", (False, 0)))
        except Exception:
            if not owner.cancelled.is_set():
                self.events.put((owner, "done", (False, 0)))
        finally:
            notifications.complete(item, played and not owner.cancelled.is_set())

    def drain(self, coordinator, noise_floor):
        while True:
            try:
                owner, kind, value = self.events.get_nowait()
            except Empty:
                return
            if not coordinator.owns(owner):
                continue
            if kind == "state":
                coordinator.set_state(value)
            elif kind == "done":
                played, duration = value
                releases = []
                if owner.receipt is not None:
                    owner.receipt.finish(PlaybackOutcome.COMPLETED if played else PlaybackOutcome.FAILED,
                                         release_scheduler=releases.append)
                if releases:
                    self._schedule(owner, self._release_power(owner, releases[0], duration))
                else:
                    coordinator.complete(owner, duration, noise_floor)
            elif kind == "power_done":
                failed, duration = value
                if failed:
                    coordinator.set_state("thinking")
                    self._schedule(owner, self._power_failure(owner))
                else:
                    coordinator.complete(owner, duration, noise_floor)

    async def _release_power(self, owner, release, duration):
        try:
            await self._blocking(owner, release)
        except (asyncio.CancelledError, PlaybackInterrupted):
            pass
        finally:
            failed = self.controls.take_power_failure()
            self.events.put((owner, "power_done", (failed, duration)))

    async def _power_failure(self, owner):
        try:
            await self.speech.say(owner, "I couldn't complete that power action.")
        finally:
            self.events.put((owner, "done", (False, 0)))

    def close(self):
        async def shutdown():
            tasks = list(self.tasks)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        future = asyncio.run_coroutine_threadsafe(shutdown(), self.loop)
        future.result()
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join()
