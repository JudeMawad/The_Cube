"""Pi-local renderer and fixed systemd operations. No server imports."""
import logging
import math
from pathlib import Path
import subprocess
from threading import RLock, Timer

from audio.speaker_control import SpeakerError, control_volume
from display import transport as display_transport

logger = logging.getLogger(__name__)


class ControlFailure(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__("Cube control unavailable")


class _VolumeDisplay:
    """Own the short-lived native content used for volume readback."""
    DISPLAY_SECONDS = 2.0

    def __init__(self, send, timer_factory=Timer):
        self._send = send
        self._timer_factory = timer_factory
        self._lock = RLock()
        self._timer = None
        self._generation = 0
        self._visible = False
        self._stopped = False
        self._last_text = None

    @staticmethod
    def _text(volume):
        if type(volume) is dict and volume.get("muted") is True:
            return "MUTE"
        channels = volume.get("channels") if type(volume) is dict else None
        if type(channels) is not dict or not channels:
            return "VOL"
        levels = list(channels.values())
        if (any(type(level) is not int or not 0 <= level <= 100 for level in levels)
                or any(level != levels[0] for level in levels[1:])):
            return "VOL"
        text = f"{levels[0]}%"
        return text if len(text) <= 5 else "VOL"

    def show(self, volume):
        text = self._text(volume)
        with self._lock:
            self._last_text = text
            self._present(text)

    def refresh(self):
        with self._lock:
            if self._last_text is not None:
                self._present(self._last_text)

    def _present(self, text):
        if self._stopped:
            return
        try:
            self._send("control show_text " + text)
        except ControlFailure:
            return
        self._generation += 1
        generation = self._generation
        if self._timer is not None:
            self._timer.cancel()
        self._visible = True
        try:
            self._timer = self._timer_factory(
                self.DISPLAY_SECONDS, lambda: self._clear(generation))
            self._timer.daemon = True
            self._timer.start()
        except (OSError, RuntimeError):
            logger.warning("Volume visual release timer unavailable; clearing content")
            self._timer = None
            try:
                self._send("control clear_content")
            except ControlFailure:
                pass
            self._visible = False

    def _clear(self, generation):
        with self._lock:
            if self._stopped or generation != self._generation:
                return
            self._timer = None
            try:
                self._send("control clear_content")
            except ControlFailure:
                return
            self._visible = False

    def close(self):
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            self._last_text = None
            self._generation += 1
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            if self._visible:
                try:
                    self._send("control clear_content")
                except ControlFailure:
                    pass
                self._visible = False


class Hardware:
    def __init__(self, *, timer_factory=Timer):
        self._volume_display = _VolumeDisplay(
            lambda message: self._content_command(message), timer_factory)

    def volume(self, operation, *, percent=None, delta=None):
        try:
            volume = control_volume(operation, percent=percent, delta=delta)
        except SpeakerError:
            raise ControlFailure("audio_unavailable") from None
        self._volume_display.show(volume)
        return volume

    def _renderer_request(self, message):
        try:
            return display_transport.request(message)
        except (OSError, ValueError, UnicodeError):
            raise ControlFailure("display_unavailable") from None

    def _content_command(self, message):
        try:
            data = self._renderer_request(message)
        except ControlFailure:
            logger.warning("Volume visual failed: %s: renderer transport or JSON unavailable", message)
            raise
        if (type(data) is not dict or set(data) != {"accepted"}
                or type(data["accepted"]) is not bool or not data["accepted"]):
            logger.warning("Volume visual failed: %s: incompatible renderer acknowledgement %r",
                           message, data)
            raise ControlFailure("display_unavailable")

    def renderer(self, operation, value=None):
        if operation not in {"set_display", "set_brightness", "get_status"}:
            raise ControlFailure("command_invalid")
        if operation == "set_display" and value not in {"on", "off"}:
            raise ControlFailure("command_invalid")
        if operation == "set_brightness" and (type(value) is not int or not 0 <= value <= 100):
            raise ControlFailure("command_invalid")
        message = "control " + operation + (" " + str(value) if value is not None else "")
        data = self._renderer_request(message)
        if (type(data) is not dict or set(data) != {
                "display_enabled", "master_brightness_percent"}
                or type(data["display_enabled"]) is not bool
                or type(data["master_brightness_percent"]) is not int
                or not 0 <= data["master_brightness_percent"] <= 100):
            raise ControlFailure("display_unavailable")
        return data

    def refresh_volume_display(self):
        self._volume_display.refresh()

    def close(self):
        self._volume_display.close()

    def status(self):
        data = {"display_available": True}
        try:
            data.update(self.renderer("get_status"))
        except ControlFailure:
            data["display_available"] = False
        for name, path, scale, low, high in (
            ("uptime_seconds", "/proc/uptime", 1, 0, float("inf")),
            ("cpu_temperature_c", "/sys/class/thermal/thermal_zone0/temp", 1000, -40, 150),
        ):
            try:
                value = float(Path(path).read_text().split()[0]) / scale
                if math.isfinite(value) and low <= value <= high:
                    data[name] = value
            except (OSError, ValueError, IndexError):
                pass
        return data

    def check_power(self, operation):
        method = {"shutdown": "CanPowerOff", "reboot": "CanReboot"}.get(operation)
        if method is None:
            raise ControlFailure("command_invalid")
        try:
            result = subprocess.run([
                "/usr/bin/busctl", "--system", "call", "org.freedesktop.login1",
                "/org/freedesktop/login1", "org.freedesktop.login1.Manager", method,
            ], check=True, capture_output=True, text=True, timeout=2)
            if result.stdout.strip() != 's "yes"':
                raise ValueError()
        except (OSError, subprocess.SubprocessError, ValueError):
            raise ControlFailure("power_unavailable") from None

    def power(self, operation):
        command = {"shutdown": "poweroff", "reboot": "reboot"}.get(operation)
        if command is None:
            raise ControlFailure("command_invalid")
        try:
            subprocess.run(["/usr/bin/systemctl", "--no-ask-password", "--no-wall",
                            "--check-inhibitors=yes", command],
                           check=True, capture_output=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            raise ControlFailure("power_unavailable") from None
