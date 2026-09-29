"""Optional outbound receiver telemetry. Never commands Soloist or local audio."""
import os
from pathlib import Path
import socket
from threading import Event, Thread
from urllib.parse import urlsplit
from uuid import uuid4

import requests

from .soloist import read_state

TOKEN_PATH = Path.home() / ".config/cube/events.token"


def receiver_packet(state, session, sequence):
    return {"version": 1, "session_id": session, "sequence": sequence,
            "connected": state.connected, "logged_in": state.logged_in,
            "active": state.active, "status": state.status,
            "spotify_volume": state.spotify_volume}


class ReceiverReporter:
    def __init__(self, base_url, client_id, *, read=read_state, token_path=TOKEN_PATH):
        parsed = urlsplit(base_url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in {"", "/"}):
            raise ValueError("Invalid music backend URL")
        self.base_url, self.client_id = base_url.rstrip("/"), client_id
        self.read, self.token_path = read, token_path
        self.session, self.sequence = None, 0
        self.stopped, self.thread = Event(), None

    def send(self, http):
        token = self.token_path.read_text().strip()
        if len(token) < 32 or not token.isascii() or any(c.isspace() for c in token):
            raise ValueError("Cube authentication unavailable")
        headers = {"X-Cube-Token": token, "X-Cube-Client-ID": self.client_id}
        options = {"headers": headers, "timeout": (2, 3), "allow_redirects": False}
        if self.session is None:
            session = uuid4().hex
            response = http.post(self.base_url + "/music/receiver/session", json={"session_id": session}, **options)
            if response.status_code != 200 or response.json() != {"registered": True}:
                raise ValueError("Music registration unavailable")
            self.session = session
        self.sequence += 1
        response = http.post(self.base_url + "/music/receiver/state",
            json=receiver_packet(self.read(), self.session, self.sequence), **options)
        if response.status_code != 200 or response.json() != {"accepted": True}:
            self.session = None
            raise ValueError("Music status unavailable")

    def _run(self):
        delay, health = 5, None
        with requests.Session() as http:
            http.trust_env = False
            while not self.stopped.is_set():
                try:
                    self.send(http)
                    delay, available = 5, True
                except (requests.RequestException, OSError, ValueError, TypeError):
                    self.session = None
                    delay, available = min(60, delay * 2), False
                if available != health:
                    print("Music backend connected" if available else "Music backend unavailable; local audio continues", flush=True)
                    health = available
                self.stopped.wait(delay)

    def start(self):
        self.thread = Thread(target=self._run, name="spotify-reporter", daemon=True)
        self.thread.start()

    def close(self):
        self.stopped.set()
        # HTTP is bounded; never hold receiver/voice shutdown on the backend.


def configured_reporter():
    url = os.environ.get("CUBE_SPOTIFY_BACKEND_URL")
    if not url:
        return None
    try:
        reporter = ReceiverReporter(url, os.environ.get("CUBE_CLIENT_ID", socket.gethostname()))
        reporter.start()
        return reporter
    except (ValueError, OSError, RuntimeError):
        print("Music backend reporting unavailable; local audio continues", flush=True)
        return None


def main():
    import signal
    reporter = configured_reporter()
    if reporter is None:
        return
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: reporter.close())
    reporter.stopped.wait()


if __name__ == "__main__":
    main()
