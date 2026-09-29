"""Bounded, one-delivery control broker for the single-worker backend."""
from dataclasses import dataclass
from threading import Condition
from time import monotonic
from uuid import uuid4

from .protocol import Command, Result


class ClientUnavailable(Exception):
    pass


@dataclass
class Connection:
    session_id: str
    expires: float
    command: Command | None = None
    delivered: bool = False
    deadline: float = 0
    result: Result | None = None


class ControlChannel:
    def __init__(self, *, timeout=5.0, clock=monotonic):
        self.timeout, self.clock = timeout, clock
        self.condition = Condition()
        self.connections = {}
        self.closed = False

    def register(self, client_id, session_id):
        with self.condition:
            now = self.clock()
            self.connections = {key: value for key, value in self.connections.items()
                                if value.expires > now}
            if self.closed or len(self.connections) >= 128 and client_id not in self.connections:
                raise ClientUnavailable()
            # Registration is a new generation, even for the same session ID.
            self.connections[client_id] = Connection(session_id, now + 40)
            self.condition.notify_all()

    def _connection(self, client_id, session_id):
        connection = self.connections.get(client_id)
        if (self.closed or connection is None or connection.session_id != session_id
                or connection.expires <= self.clock()):
            raise ClientUnavailable()
        return connection

    def next(self, client_id, session_id, wait=25):
        with self.condition:
            connection = self._connection(client_id, session_id)
            connection.expires = self.clock() + 40
            deadline = self.clock() + wait
            while True:
                if self._connection(client_id, session_id) is not connection:
                    raise ClientUnavailable()
                if (connection.command is not None and not connection.delivered
                        and connection.deadline > self.clock()):
                    connection.delivered = True
                    return connection.command
                remaining = deadline - self.clock()
                if remaining <= 0:
                    return None
                self.condition.wait(remaining)

    def request(self, client_id, session_id, request_id, operation, **arguments):
        with self.condition:
            connection = self._connection(client_id, session_id)
            if connection.command is not None:
                raise ClientUnavailable()
            command = Command(session_id=session_id, request_id=request_id,
                              command_id=uuid4().hex, operation=operation, **arguments)
            connection.command = command
            connection.delivered = False
            connection.result = None
            connection.deadline = self.clock() + self.timeout
            self.condition.notify_all()
            try:
                while connection.result is None:
                    if self._connection(client_id, session_id) is not connection:
                        raise ClientUnavailable()
                    remaining = connection.deadline - self.clock()
                    if remaining <= 0:
                        raise ClientUnavailable()
                    self.condition.wait(remaining)
                return connection.result
            finally:
                connection.command = None
                connection.result = None

    def complete(self, client_id, command_id, result):
        with self.condition:
            connection = self._connection(client_id, result.session_id)
            if (connection.command is None or connection.command.command_id != command_id
                    or not connection.delivered or connection.deadline <= self.clock()
                    or connection.result is not None):
                return False
            if result.success and ((result.status is not None) != (connection.command.operation == "get_status")):
                return False
            if result.success and ((result.volume is not None) !=
                    (connection.command.operation in {"get_volume", "set_volume", "adjust_volume", "mute", "unmute"})):
                return False
            connection.result = result
            self.condition.notify_all()
            return True

    def close(self):
        with self.condition:
            self.closed = True
            self.connections.clear()
            self.condition.notify_all()
