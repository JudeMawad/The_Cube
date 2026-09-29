"""Terminal playback receipts owned by the interaction lifecycle."""
from enum import Enum
from threading import RLock
from time import monotonic
from uuid import uuid4


class PlaybackOutcome(Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PlaybackInterrupted(Exception):
    """Interaction cancellation, distinct from a recoverable playback failure."""


class PlaybackReceipt:
    def __init__(self, session_id, *, clock=monotonic):
        self.session_id = session_id
        self.request_id = uuid4().hex
        self.playback_id = uuid4().hex
        self.clock = clock
        self.deadline = clock() + 90
        self.lock = RLock()
        self.outcome = None
        self.reason = None
        self.command_id = None
        self.expected_action = None
        self._release = None
        self.authorized = False
        self.started = False

    def live(self):
        with self.lock:
            if self.clock() >= self.deadline:
                self.finish(PlaybackOutcome.CANCELLED, "expired")
            return self.outcome is None

    def claim(self, command_id):
        with self.lock:
            if not self.live() or self.command_id is not None:
                return False
            self.command_id = command_id
            return True

    def arm(self, action, release):
        with self.lock:
            if not self.live():
                return False
            self.expected_action = "cube_" + action
            self._release = release
            self.deadline = self.clock() + 30
            return True

    def authorize(self, result):
        with self.lock:
            if not self.live():
                return
            if self.started:
                self.finish(PlaybackOutcome.CANCELLED, "superseded_playback")
                return
            self.started = True
            self.authorized = (result.get("success") is True
                               and result.get("action") == self.expected_action)
            if self._release is not None and not self.authorized:
                self.finish(PlaybackOutcome.CANCELLED, "unmatched_response")

    def finish(self, outcome, reason=None, *, release_scheduler=None):
        """First terminal outcome wins; cancellation can be reported by a future player."""
        if not isinstance(outcome, PlaybackOutcome):
            raise ValueError("Invalid playback outcome")
        with self.lock:
            if self.outcome is not None:
                return False
            if self.clock() >= self.deadline:
                outcome, reason = PlaybackOutcome.CANCELLED, "expired"
            self.outcome, self.reason = outcome, reason
            release, self._release = self._release, None
            # Consumption and release are serialized with cancellation/supersession.
            if outcome is PlaybackOutcome.COMPLETED and self.authorized and release is not None:
                if release_scheduler is None:
                    release()
                else:
                    # The frame loop commits the terminal outcome but schedules
                    # potentially blocking hardware effects away from capture.
                    release_scheduler(release)
            return True


class PlaybackLifecycle:
    def __init__(self):
        self.lock = RLock()
        self.current = None

    def begin(self, session_id):
        with self.lock:
            self.discard("superseded")
            self.current = PlaybackReceipt(session_id)
            return self.current

    def matching(self, session_id, request_id):
        with self.lock:
            receipt = self.current
            if (receipt is not None and receipt.session_id == session_id
                    and receipt.request_id == request_id and receipt.live()):
                return receipt
            return None

    def discard(self, reason="interrupted"):
        with self.lock:
            if self.current is not None:
                self.current.finish(PlaybackOutcome.CANCELLED, reason)
            self.current = None
