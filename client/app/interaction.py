"""Pi interaction ownership and incremental recording; no hardware or networking."""
from collections import deque
from dataclasses import dataclass, field
from threading import Event, RLock
from time import monotonic
from uuid import uuid4

from audio.playback import PlaybackInterrupted, PlaybackOutcome


@dataclass(eq=False)
class Interaction:
    generation: int
    wake_preroll: bool = False
    identifier: str = field(default_factory=lambda: uuid4().hex)
    cancelled: Event = field(default_factory=Event)
    lock: RLock = field(default_factory=RLock)
    receipt: object = None
    future: object = None
    speech_end: float | None = None

    def check(self):
        if self.cancelled.is_set():
            raise PlaybackInterrupted("Interaction cancelled")

    def attach_receipt(self, receipt):
        with self.lock:
            self.check()
            self.receipt = receipt

    def cancel(self):
        with self.lock:
            self.cancelled.set()
            if self.receipt is not None:
                self.receipt.finish(PlaybackOutcome.CANCELLED, "barge_in")
            if self.future is not None:
                self.future.cancel()


class WakeLatch:
    """Accept one threshold episode; inference itself never pauses."""
    def __init__(self):
        self.armed = True
        self.low_frames = 0

    def update(self, active):
        if active:
            self.low_frames = 0
            if self.armed:
                self.armed = False
                return True
        else:
            self.low_frames += 1
            if self.low_frames >= 3:
                self.armed = True
        return False


class Recorder:
    """Existing RMS endpointing expressed as one frame at a time."""
    def __init__(self, noise_floor, now, waiting_seconds=5, *, preroll=()):
        self.start_threshold = max(0.010, noise_floor * 2.2)
        self.continue_threshold = max(0.007, noise_floor * 1.7)
        self.deadline = now + waiting_seconds
        self.pre_roll = deque(preroll, maxlen=4)
        self.recorded = bytearray()
        self.started = None
        self.last_speech = None
        self.done = False

    def feed(self, data, level, now):
        if self.done:
            raise RuntimeError("Recorder already finished")
        if self.started is None:
            self.pre_roll.append(data)
            if level >= self.start_threshold:
                self.started = self.last_speech = now
                self.recorded.extend(b"".join(self.pre_roll))
            elif now >= self.deadline:
                self.done = True
        else:
            self.recorded.extend(data)
            if level >= self.continue_threshold:
                self.last_speech = now
            if now - self.last_speech >= 0.75 or now - self.started >= 20:
                self.done = True
        return bytes(self.recorded) if self.done and self.recorded else None


class Coordinator:
    """The frame-loop thread alone owns current state and LED messages."""
    def __init__(self, submit, publish_display_state, *, clock=monotonic, activity=None):
        self.submit = submit
        self.publish_display_state = publish_display_state
        self.clock = clock
        self.current = None
        self.generation = 0
        self.recorder = None
        self.state = "idle"
        self.last_display_state = 0
        self.latch = WakeLatch()
        self.pre_roll = deque(maxlen=4)
        self.activity = activity
        self.last_activity = float("-inf")

    def publish_activity(self):
        if self.activity is not None:
            self.activity(self.generation, self.state != "idle")
        self.last_activity = self.clock()

    def set_state(self, state):
        self.state = state
        self.publish_activity()
        self.publish_display_state(state)
        self.last_display_state = self.clock()

    def owns(self, owner):
        return owner is self.current and not owner.cancelled.is_set()

    def begin(self, noise_floor, *, waiting_seconds=5, wake=False):
        if self.current is not None:
            self.current.cancel()
        self.generation += 1
        self.current = Interaction(self.generation, wake_preroll=wake)
        # Retain the detection frame and its neighbours: the command can already
        # have begun when the detector reports the wake. Backend strips only a
        # leading wake prefix on explicitly marked wake captures.
        self.recorder = Recorder(noise_floor, self.clock(), waiting_seconds,
                                 preroll=self.pre_roll if wake else ())
        self.set_state("listening" if wake else "followup")
        return self.current

    def frame(self, data, level, active, noise_floor):
        self.pre_roll.append(data)
        accepted = self.latch.update(active)
        if accepted:
            self.begin(noise_floor, wake=True)
            # Keep this frame as pre-roll, but its wake-word energy must not
            # itself start the command's end-silence timer. The very next frame
            # can start speech, even while the wake score is still high.
        if self.recorder is not None and not accepted:
            audio = self.recorder.feed(data, level, self.clock())
            if self.recorder.done:
                self.current.speech_end = self.recorder.last_speech
                self.recorder = None
                if audio:
                    self.set_state("thinking")
                    self.submit(self.current, audio)
                else:
                    self.set_state("idle")
        interval = 0.1 if self.state.startswith("speech ") else 5
        if self.clock() - self.last_display_state >= interval:
            self.set_state(self.state)
        if self.clock() - self.last_activity >= .25:
            self.publish_activity()
        return accepted

    def complete(self, owner, listen_seconds, noise_floor):
        if not self.owns(owner):
            return
        if listen_seconds:
            self.pre_roll.clear()
            self.begin(noise_floor, waiting_seconds=listen_seconds)
        else:
            self.set_state("idle")

    def close(self):
        if self.current is not None:
            self.current.cancel()
