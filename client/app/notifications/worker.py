"""Network-only event worker. The main audio loop owns all speech."""

from dataclasses import dataclass, field
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Thread

import requests

TOKEN_PATH = Path.home() / ".config/cube/events.token"


@dataclass
class Notification:
    payload: dict
    finished: Event = field(default_factory=Event)
    played: bool = False
    discarded: bool = False


class Notifications:
    def __init__(self, base_url, client_id):
        self.base_url = base_url.rstrip("/")
        self.client_id = client_id
        self.queue = Queue(maxsize=1)
        self.stopped = Event()
        self.thread = Thread(target=self._run, name="cube-events", daemon=True)

    def start(self):
        self.thread.start()

    def close(self):
        self.stopped.set()

    def take(self):
        try:
            return self.queue.get_nowait()
        except Empty:
            return None

    def validate(self, notification):
        # Called immediately before playback by the sole audio owner.
        with requests.Session() as session:
            session.trust_env = False
            response = session.post(
                f"{self.base_url}/events/{notification.payload['id']}/validate",
                json={"ack_token": notification.payload["ack_token"]}, headers=self._headers(),
                timeout=(3, 5), allow_redirects=False,
            )
        response.raise_for_status()
        payload = response.json()
        if response.status_code != 200 or not isinstance(payload, dict) or type(payload.get("valid")) is not bool:
            raise ValueError("Unexpected notification validation")
        notification.discarded = not payload["valid"]
        return payload["valid"]

    async def validate_async(self, notification, interaction_id):
        """Validation for the continuous voice loop; never blocks capture."""
        import httpx
        headers = {**self._headers(), "X-Cube-Interaction-ID": interaction_id}
        async with httpx.AsyncClient(trust_env=False) as session:
            response = await session.post(
                f"{self.base_url}/events/{notification.payload['id']}/validate",
                json={"ack_token": notification.payload["ack_token"]}, headers=headers,
                timeout=httpx.Timeout(5, connect=3))
        response.raise_for_status()
        payload = response.json()
        if response.status_code != 200 or not isinstance(payload, dict) or type(payload.get("valid")) is not bool:
            raise ValueError("Unexpected notification validation")
        notification.discarded = not payload["valid"]
        return payload["valid"]

    @staticmethod
    def complete(notification, played):
        notification.played = bool(played)
        notification.finished.set()

    def _headers(self):
        token = TOKEN_PATH.read_text().strip()
        if len(token) < 32 or not token.isascii() or any(c.isspace() for c in token):
            raise ValueError("Event authentication is not configured")
        return {"X-Cube-Token": token, "X-Cube-Client-ID": self.client_id}

    def _run(self):
        backoff = 5
        awaiting_ack = None
        with requests.Session() as session:
            session.trust_env = False
            while not self.stopped.is_set():
                try:
                    headers = self._headers()
                    if awaiting_ack is not None:
                        # Retry an acknowledgement without speaking again if its response was lost.
                        response = session.post(
                            f"{self.base_url}/events/{awaiting_ack['id']}/ack",
                            json={"ack_token": awaiting_ack["ack_token"]}, headers=headers,
                            timeout=(5, 10), allow_redirects=False,
                        )
                        if response.status_code == 404:
                            # Cancelled/obsolete notifications no longer accept acknowledgements.
                            awaiting_ack = None
                            continue
                        response.raise_for_status()
                        if response.status_code != 200:
                            raise ValueError("Unexpected acknowledgement")
                        awaiting_ack = None
                    else:
                        response = session.get(
                            f"{self.base_url}/events/next", headers=headers,
                            timeout=(5, 35), allow_redirects=False,
                        )
                        response.raise_for_status()
                        if response.status_code == 204:
                            backoff = 5
                            continue
                        payload = response.json()
                        if (response.status_code != 200 or not isinstance(payload, dict)
                                or type(payload.get("id")) is not int or payload["id"] <= 0
                                or not isinstance(payload.get("response"), str)
                                or not 0 < len(payload["response"]) <= 500
                                or not isinstance(payload.get("ack_token"), str)):
                            raise ValueError("Invalid notification")
                        item = Notification(payload)
                        self.queue.put_nowait(item)
                        # One in flight: no extra polling while Cube is busy or speaking.
                        while not item.finished.wait(0.5):
                            if self.stopped.is_set():
                                return
                        if item.played:
                            awaiting_ack = payload
                        elif not item.discarded:
                            self.stopped.wait(30)
                    backoff = 5
                except (requests.RequestException, OSError, ValueError):
                    # Exceptions can contain headers/URLs. Print only a fixed diagnostic.
                    if backoff == 5:
                        print("Media events unavailable; retrying in the background.", flush=True)
                    self.stopped.wait(backoff)
                    backoff = min(backoff * 2, 60)
