"""Service for Coordinator leases and local gain; independent of Soloist events."""
import signal
import sys
from threading import Event
from time import monotonic

from .activity import ActivityLease, read_json, runtime_dir
from .ducking import CADENCE, Ducking, PipeWireGain


def main():
    adapter = PipeWireGain()
    if sys.argv[1:] == ["neutral"]:
        # systemd ExecStopPost also runs after SIGKILL. Never stop the graph or
        # receiver just because its controller died. Failure is retried on start.
        try:
            adapter.inspect()
            adapter.apply(1.0)
        except Exception:
            return
        return
    stopped = Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopped.set())
    lease = ActivityLease()
    duck = Ducking(adapter)
    path = runtime_dir() / "interaction.json"
    previous_health = None
    while not stopped.is_set():
        now = monotonic()
        try:
            lease.accept(read_json(path), now)
        except (OSError, ValueError, TypeError):
            pass
        healthy = duck.tick(lease.active(now))
        if healthy != previous_health:
            print("Music DSP connected" if healthy else "Music DSP unavailable; retrying", flush=True)
            previous_health = healthy
        stopped.wait(max(0, (CADENCE if healthy else .5) - (monotonic() - now)))


if __name__ == "__main__":
    main()
