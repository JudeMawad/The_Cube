"""Coordinator activity handoff on tmpfs; never do filesystem I/O on capture."""
import json
import math
import os
from pathlib import Path
import stat
import tempfile
from threading import Event, Thread
from time import monotonic
from uuid import uuid4

LEASE_SECONDS = 2.0


def runtime_dir():
    return Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "cube-audio"


def boot_id():
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def atomic_json(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise OSError("Unsafe Cube audio runtime directory")
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as output:
            name = output.name
            json.dump(value, output, allow_nan=False)
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def read_json(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as source:
        info = os.fstat(source.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > 16384):
            raise ValueError("Invalid Cube audio snapshot")
        return json.load(source)


class ActivityPublisher:
    def __init__(self, *, clock=monotonic, path=None, boot=None):
        self.clock = clock
        self.path = path or runtime_dir() / "interaction.json"
        self.boot = boot or boot_id()
        self.instance = uuid4().hex
        self.started = clock()
        self.sequence = 0
        self.latest = None
        self.changed = Event()
        self.stopped = Event()
        self.thread = Thread(target=self._write, name="cube-audio-activity", daemon=True)

    def start(self):
        self.thread.start()

    def publish(self, generation, active):
        # Called only by the Coordinator's thread. Assignment is atomic; an
        # overwritten intermediate heartbeat cannot release a newer generation.
        self.sequence += 1
        self.latest = dict(boot=self.boot, instance=self.instance, started=self.started,
                           generation=generation, sequence=self.sequence,
                           sent=self.clock(), active=active)
        self.changed.set()

    def _write(self):
        while not self.stopped.is_set():
            self.changed.wait()
            self.changed.clear()
            value = self.latest
            if value is not None:
                try:
                    atomic_json(self.path, value)
                except (OSError, ValueError):
                    pass  # Audio control failure must not stop voice capture.

    def close(self):
        self.stopped.set()
        self.changed.set()
        if self.thread.is_alive():
            self.thread.join(timeout=.5)
        # Never manufacture an idle transition on cancellation/shutdown. Expiry
        # handles crashes and orderly shutdown alike.


class ActivityLease:
    def __init__(self, *, boot=None):
        self.boot = boot or boot_id()
        self.latest = None

    def accept(self, value, now):
        if not isinstance(value, dict) or value.get("boot") != self.boot:
            return False
        if not isinstance(value.get("instance"), str) or not value["instance"]:
            return False
        if type(value.get("active")) is not bool:
            return False
        for key in ("generation", "sequence"):
            if type(value.get(key)) is not int or value[key] < 0:
                return False
        for key in ("sent", "started"):
            if (type(value.get(key)) not in (int, float) or not math.isfinite(value[key])
                    or not 0 <= value[key] <= now):
                return False
        if value["started"] > value["sent"] or now - value["sent"] >= LEASE_SECONDS:
            return False
        previous = self.latest
        if previous:
            if value["instance"] == previous["instance"]:
                if (value["sequence"] <= previous["sequence"]
                        or value["generation"] < previous["generation"]
                        or value["sent"] < previous["sent"]
                        or value["started"] != previous["started"]):
                    return False
            elif value["started"] <= previous["started"]:
                return False
        self.latest = dict(value)
        return True

    def active(self, now):
        return bool(self.latest and self.latest["active"]
                    and 0 <= now - self.latest["sent"] < LEASE_SECONDS)
