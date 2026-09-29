"""Bounded conversation history and turn serialization on one asyncio event loop."""
import asyncio
from collections import OrderedDict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import math
from time import monotonic

from .schemas import ChatMessage
from .cancellation import check


@dataclass
class _ClientState:
    history: deque
    last_used: float
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    reservations: int = 0  # Includes the holder and every waiting turn.
    volume_direction: int | None = None
    volume_at: float = 0.0


class ClientTurn:
    """A scoped history view. Stage a completed exchange; commit on normal exit."""

    def __init__(self, state, clock):
        self._state = state
        self._clock = clock
        self._active = True
        self._completed = None
        self._completion_set = False
        self._volume_direction = None

    def _check_active(self):
        if not self._active:
            raise RuntimeError("Client turn has already ended")

    @property
    def history(self) -> list[ChatMessage]:
        self._check_active()
        return [message.model_copy(deep=True)
                for exchange in self._state.history for message in exchange]

    @property
    def volume_direction(self) -> int | None:
        self._check_active()
        if self._clock() - self._state.volume_at <= 30:
            return self._state.volume_direction
        return None

    def complete(self, user: str, assistant: str | None, *, volume_direction=None) -> None:
        self._check_active()
        if self._completion_set:
            raise RuntimeError("Client turn was already completed")
        # No assistant reply (e.g. legacy unhandled input) is not a chat exchange.
        user = user.strip()[:2000]
        assistant = assistant.strip()[:500] if assistant is not None else ""
        if user and assistant:
            self._completed = (
                ChatMessage(role="user", content=user),
                ChatMessage(role="assistant", content=assistant),
            )
        self._volume_direction = volume_direction if type(volume_direction) is int and volume_direction in {-1, 1} else None
        self._completion_set = True


class ClientStateManager:
    """Own history and locks together; never evict acquired or awaited locks.

    Methods must run on the same event loop. Registry changes and release have
    no await points, so cancellation cannot interrupt reservation bookkeeping.
    Expiry is lazy on admission (or explicit prune_idle); no background tasks.
    """

    def __init__(self, *, max_clients=128, max_turns=6, idle_ttl=600.0, clock=monotonic):
        if type(max_clients) is not int or max_clients < 1:
            raise ValueError("max_clients must be a positive integer")
        if type(max_turns) is not int or not 1 <= max_turns <= 6:
            raise ValueError("max_turns must be between one and six")
        if not math.isfinite(idle_ttl) or idle_ttl <= 0:
            raise ValueError("idle_ttl must be finite and positive")
        self.max_clients = max_clients
        self.max_turns = max_turns
        self.idle_ttl = idle_ttl
        self._clock = clock
        self._clients = OrderedDict()
        self._changed = asyncio.Event()

    def __len__(self):
        return len(self._clients)

    def _notify(self):
        previous = self._changed
        self._changed = asyncio.Event()
        previous.set()

    def prune_idle(self):
        now = self._clock()
        expired = [key for key, state in self._clients.items()
                   if state.reservations == 0 and now - state.last_used >= self.idle_ttl]
        for key in expired:
            del self._clients[key]
        if expired:
            self._notify()

    def _reserve(self, client_id):
        self.prune_idle()
        state = self._clients.get(client_id)
        if state is None:
            if len(self._clients) >= self.max_clients:
                idle = next((key for key, value in self._clients.items()
                             if value.reservations == 0), None)
                if idle is None:
                    return None
                del self._clients[idle]
            state = _ClientState(deque(maxlen=self.max_turns), self._clock())
            self._clients[client_id] = state
        state.reservations += 1
        self._clients.move_to_end(client_id)
        return state

    @asynccontextmanager
    async def turn(self, client_id: str):
        if not isinstance(client_id, str) or not client_id.strip():
            raise ValueError("client_id must be nonempty")
        state = self._reserve(client_id)
        while state is None:
            # No entry or per-client lock allocated while awaiting capacity.
            await self._changed.wait()
            state = self._reserve(client_id)

        acquired = False
        turn = None
        try:
            await state.lock.acquire()
            acquired = True
            turn = ClientTurn(state, self._clock)
            yield turn
            check()
            # Also avoid committing if cancellation was requested but not yet
            # delivered at an await point in the caller's body.
            if turn._completion_set and not asyncio.current_task().cancelling():
                if turn._completed is not None:
                    state.history.append(turn._completed)
                state.volume_direction = turn._volume_direction
                state.volume_at = self._clock() if turn._volume_direction is not None else 0.0
        finally:
            if turn is not None:
                turn._active = False
            if acquired:
                state.lock.release()
            state.reservations -= 1
            state.last_used = self._clock()
            self._clients.move_to_end(client_id)
            self._notify()
