"""Local DSP-only attenuation. This module has no playback control capability."""
import json
import math
import subprocess
from time import monotonic

DUCK_GAIN = .10
FADE_DOWN = .150
FADE_UP = .400
CADENCE = .020


class PipeWireGain:
    def __init__(self, run=subprocess.run):
        self.run = run

    def command(self, *args):
        result = self.run(args, capture_output=True, text=True, timeout=.5, check=True)
        # pw-cli may report a missing object on stderr with exit status zero.
        if result.stderr.strip():
            raise OSError("PipeWire command unavailable")
        return result.stdout

    def inspect(self):
        nodes = json.loads(self.command("pw-dump"))
        matches = [n for n in nodes if n.get("type") == "PipeWire:Interface:Node"
                   and n.get("info", {}).get("props", {}).get("node.name") == "cube.spotify"]
        if len(matches) != 1:
            raise OSError("Unique Spotify bus unavailable")
        node = matches[0]
        props = node["info"]["props"]
        if props.get("media.class") != "Audio/Sink" or props.get("cube.audio.bus") != "spotify":
            raise OSError("Wrong Spotify bus identity")
        values = {}
        for param in node["info"].get("params", {}).get("Props", []):
            pairs = param.get("params", [])
            values.update(zip(pairs[::2], pairs[1::2]))
        gains = [values.get("duck_left:Mult"), values.get("duck_right:Mult")]
        if any(type(v) not in (float, int) or not math.isfinite(v) or not 0 <= v <= 1 for v in gains):
            raise OSError("Spotify DSP controls unavailable")
        return str(props["object.serial"]), min(gains)

    def apply(self, gain, *, serial=None):
        if not math.isfinite(gain) or not 0 <= gain <= 1:
            raise ValueError("Invalid music gain")
        current_serial, _ = self.inspect()  # Revalidate before every mutation.
        if serial is not None and current_serial != serial:
            raise OSError("Spotify graph changed during fade")
        # Resolve by exact name in pw-cli's own registry, never an old numeric
        # node ID which could have been recycled for an unrelated application.
        self.command("pw-cli", "set-param", "cube.spotify", "Props", json.dumps({
            "params": ["duck_left:Mult", gain, "duck_right:Mult", gain]}))


class Ducking:
    def __init__(self, adapter, *, clock=monotonic):
        self.adapter = adapter
        self.clock = clock
        self.serial = None
        self.gain = DUCK_GAIN
        self.target = None
        self.started = clock()
        self.start_gain = self.gain
        self.next_inspect = 0

    def tick(self, active):
        now = self.clock()
        target = DUCK_GAIN if active else 1.0
        try:
            if now >= self.next_inspect or self.serial is None:
                serial, actual = self.adapter.inspect()
                self.next_inspect = now + .25
                if serial != self.serial:
                    self.serial, self.gain = serial, actual
                    self.target = None
                    # Recreated graphs start attenuated. Read a surviving graph's
                    # actual gain; active recovery must not first run at neutral.
                    if active:
                        self.adapter.apply(DUCK_GAIN, serial=self.serial)
                        self.gain = DUCK_GAIN
            if target != self.target:
                self.target, self.start_gain, self.started = target, self.gain, now
            duration = FADE_DOWN if target < self.start_gain else FADE_UP
            fraction = min(1.0, max(0.0, (now - self.started) / duration))
            desired = self.start_gain + (target - self.start_gain) * fraction
            if desired != self.gain:
                self.adapter.apply(desired, serial=self.serial)
                self.gain = desired
            return True
        except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError):
            self.serial = None
            return False
