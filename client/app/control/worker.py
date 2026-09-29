"""Ephemeral structured controls, independent of the media notification worker."""
from pathlib import Path
from threading import Event, RLock, Thread
from uuid import uuid4
import re
import requests

from audio.playback import PlaybackLifecycle, PlaybackOutcome
from .hardware import Hardware, ControlFailure

TOKEN_PATH = Path.home() / ".config/cube/events.token"
_ID = re.compile(r"[0-9a-f]{32}\Z")


def valid_command(data):
    if type(data) is not dict:
        return False
    base = {"session_id", "request_id", "command_id", "operation"}
    operation = data.get("operation")
    if type(operation) is not str:
        return False
    extra = ({"state"} if operation == "set_display"
             else {"percent"} if operation in {"set_brightness", "set_volume"}
             else {"delta"} if operation == "adjust_volume" else set())
    if set(data) != base | extra:
        return False
    if any(type(data[key]) is not str or not _ID.fullmatch(data[key])
           for key in ("session_id", "request_id", "command_id")):
        return False
    if operation == "set_display":
        return type(data["state"]) is str and data["state"] in {"on", "off"}
    if operation in {"set_brightness", "set_volume"}:
        return type(data["percent"]) is int and 0 <= data["percent"] <= 100
    if operation == "adjust_volume":
        return type(data["delta"]) is int and 0 < abs(data["delta"]) <= 20
    return operation in {"get_status", "shutdown", "reboot", "get_volume", "mute", "unmute"}


class Controls:
    def __init__(self, base_url, client_id, *, hardware=None, lifecycle=None):
        self.base_url, self.client_id = base_url, client_id
        self.hardware = hardware or Hardware()
        self.lifecycle = lifecycle or PlaybackLifecycle()
        self.stopped = Event()
        self.lock = RLock()
        self.session_id = None
        self.thread = None
        self.power_failure = False

    def _headers(self):
        token = TOKEN_PATH.read_text().strip()
        if len(token) < 32 or not token.isascii() or any(c.isspace() for c in token):
            raise ValueError("Cube authentication unavailable")
        return {"X-Cube-Token": token, "X-Cube-Client-ID": self.client_id}

    def begin_request(self):
        with self.lock:
            if self.session_id is None:
                return None, {"X-Cube-Client-ID": self.client_id}
            try:
                headers = self._headers()
            except (OSError, ValueError, UnicodeError):
                return None, {"X-Cube-Client-ID": self.client_id}
            receipt = self.lifecycle.begin(self.session_id)
            headers.update({"X-Cube-Control-Session": self.session_id,
                            "X-Cube-Request-ID": receipt.request_id})
            return receipt, headers

    def handle(self, command):
        if not valid_command(command):
            return {"success": False, "error": "command_invalid"}
        receipt = self.lifecycle.matching(command["session_id"], command["request_id"])
        if receipt is None or not receipt.claim(command["command_id"]):
            return {"success": False, "error": "command_expired"}
        operation = command["operation"]
        try:
            if operation == "get_status":
                return {"success": True, "status": self.hardware.status()}
            if operation in {"get_volume", "set_volume", "adjust_volume", "mute", "unmute"}:
                volume = self.hardware.volume(operation, percent=command.get("percent"),
                                              delta=command.get("delta"))
                return {"success": True, "volume": volume}
            if operation in {"shutdown", "reboot"}:
                self.hardware.check_power(operation)

                def release():
                    try:
                        self.hardware.power(operation)
                    except ControlFailure:
                        self.power_failure = True
                if not receipt.arm(operation, release):
                    return {"success": False, "error": "command_expired"}
            else:
                self.hardware.renderer(operation, command.get("state", command.get("percent")))
            return {"success": True}
        except ControlFailure as error:
            return {"success": False, "error": error.code}

    def refresh_volume_display(self):
        self.hardware.refresh_volume_display()

    def take_power_failure(self):
        failed, self.power_failure = self.power_failure, False
        return failed

    def start(self):
        self.thread = Thread(target=self._run, name="cube-controls", daemon=True)
        self.thread.start()

    def _disconnect(self):
        with self.lock:
            self.lifecycle.discard("session_ended")
            self.session_id = None

    def close(self):
        self.stopped.set()
        self._disconnect()
        self.hardware.close()

    def _run(self):
        with requests.Session() as http:
            http.trust_env = False
            while not self.stopped.is_set():
                try:
                    headers = self._headers()
                    if self.session_id is None:
                        session_id = uuid4().hex
                        response = http.post(self.base_url + "/cube/control/session",
                                             json={"session_id": session_id}, headers=headers,
                                             timeout=(3, 5), allow_redirects=False)
                        if response.status_code != 200 or response.json() != {"registered": True}:
                            raise ValueError()
                        with self.lock:
                            if self.stopped.is_set():
                                break
                            self.session_id = session_id
                    session_id = self.session_id
                    response = http.get(self.base_url + "/cube/control/next",
                                        params={"session_id": session_id}, headers=headers,
                                        timeout=(3, 30), allow_redirects=False)
                    if self.stopped.is_set():
                        break
                    if response.status_code == 204:
                        continue
                    if response.status_code != 200:
                        raise ValueError()
                    command = response.json()
                    if not valid_command(command) or command["session_id"] != session_id:
                        raise ValueError()
                    result = self.handle(command)
                    response = http.post(self.base_url + "/cube/control/" + command["command_id"] + "/result",
                                         json={"session_id": session_id, **result}, headers=headers,
                                         timeout=(3, 5), allow_redirects=False)
                    if response.status_code != 200 or response.json() != {"accepted": True}:
                        raise ValueError()
                except (requests.RequestException, OSError, ValueError, UnicodeError):
                    self._disconnect()
                    if not self.stopped.is_set():
                        print("Cube control channel unavailable.", flush=True)
                        self.stopped.wait(3)
            self._disconnect()
