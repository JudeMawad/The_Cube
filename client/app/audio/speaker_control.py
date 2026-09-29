"""Connect/disconnect the paired Logitech speaker without changing capture devices."""

import json
import os
from pathlib import Path
import re
import subprocess
import time


ADDRESS_PATTERN = r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}"
AUDIO_SINK_UUID = "0000110b-0000-1000-8000-00805f9b34fb"
def _load_phrases():
    path = Path(__file__).resolve().parents[1] / "config/voice_commands.json"
    rows = json.loads(path.read_text())["commands"]
    return {row["id"]: set(row["exact"]) for row in rows if row["feature"] == "speaker"}


_PHRASES = _load_phrases()
CONNECT_PHRASES = _PHRASES["connect"]
DISCONNECT_PHRASES = _PHRASES["disconnect"]


class SpeakerError(Exception):
    pass


def _run(args, timeout=5, check=True):
    # cube-voice is a system service running as the configured user, outside the login session.
    env = os.environ.copy()
    env["LC_ALL"] = "C"
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    env.setdefault("PULSE_SERVER", f"unix:{env['XDG_RUNTIME_DIR']}/pulse/native")
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, env=env,
        )
    except FileNotFoundError:
        raise SpeakerError("Bluetooth or audio control tools are missing.") from None
    except subprocess.TimeoutExpired:
        raise SpeakerError("The speaker command timed out. Please try again.") from None
    except OSError:
        raise SpeakerError("The Cube could not access Bluetooth or its audio service.") from None
    if check and result.returncode:
        raise SpeakerError("The Cube could not access Bluetooth or its audio service.")
    return result


def _property(info, name, value):
    return bool(re.search(rf"^\s*{re.escape(name)}:\s*{re.escape(value)}\s*$", info, re.M))


def _speaker():
    override = os.environ.get("CUBE_SPEAKER_MAC", "").strip()
    if override:
        if not re.fullmatch(ADDRESS_PATTERN, override):
            raise SpeakerError("The configured speaker address is invalid.")
        addresses = [override.upper()]
    else:
        paired = _run(["bluetoothctl", "devices", "Paired"]).stdout
        addresses = [
            match.group(1).upper()
            for line in paired.splitlines()
            if (match := re.fullmatch(rf"Device ({ADDRESS_PATTERN}) (.+)", line.strip()))
            and "logitech" in match.group(2).lower()
        ]

    speakers = []
    for address in addresses:
        info = _run(["bluetoothctl", "info", address]).stdout
        if _property(info, "Paired", "yes") and AUDIO_SINK_UUID in info.lower():
            speakers.append((address, info))
    if not speakers:
        raise SpeakerError("I couldn't find a paired Logitech speaker on this Cube.")
    if len(speakers) != 1:
        raise SpeakerError("More than one Logitech speaker is paired. Set CUBE_SPEAKER_MAC to choose one.")
    return speakers[0]


def _find_sink(address):
    try:
        sinks = json.loads(_run(["pactl", "--format=json", "list", "sinks"]).stdout)
        if not isinstance(sinks, list):
            raise ValueError()
        prefix = "bluez_output." + address.replace(":", "_") + "."
        for sink in sinks:
            name = sink.get("name", "")
            properties = sink.get("properties") or {}
            device_address = properties.get("api.bluez5.address", properties.get("device.string", ""))
            if (name.lower().startswith(prefix.lower())
                    or (name.startswith("bluez_output.") and device_address.upper() == address)):
                return name
    except (ValueError, TypeError, AttributeError):
        raise SpeakerError("The Cube audio service returned an unexpected response.") from None
    return None


def connect_speaker():
    address, info = _speaker()
    if _property(info, "Blocked", "yes"):
        raise SpeakerError("The Logitech speaker is blocked in the Cube Bluetooth settings.")
    if not _property(_run(["bluetoothctl", "show"]).stdout, "Powered", "yes"):
        _run(["bluetoothctl", "--timeout", "5", "power", "on"], timeout=7)

    if not _property(info, "Connected", "yes") or not _find_sink(address):
        # Reuse pairing. Connect the playback profile; never switch the microphone.
        result = _run(
            ["bluetoothctl", "--timeout", "15", "connect", address, "a2dp-sink"],
            timeout=18, check=False,
        )
        if result.returncode:
            current = _run(["bluetoothctl", "info", address]).stdout
            if not _property(current, "Connected", "yes"):
                raise SpeakerError(
                    "I couldn't connect to the Logitech speaker. "
                    "Check that it is on, nearby, and not busy with another device."
                )

    # PipeWire may publish the audio sink shortly after Bluetooth connects.
    sink = None
    for attempt in range(10):
        sink = _find_sink(address)
        if sink:
            break
        if attempt < 9:
            time.sleep(0.5)
    if not sink:
        raise SpeakerError("Bluetooth connected, but the Logitech audio output is not ready. Try again.")

    current = _run(["bluetoothctl", "info", address]).stdout
    if not _property(current, "Connected", "yes"):
        raise SpeakerError("The Logitech speaker disconnected before it was ready.")
    _run(["pactl", "set-default-sink", sink])
    if _run(["pactl", "get-default-sink"]).stdout.strip() != sink:
        raise SpeakerError("The speaker connected, but I couldn't select it as the audio output.")
    return {"speaker_address": address, "speaker_sink": sink}



def _restore_output(address, previous_default):
    prefix = "bluez_output." + address.replace(":", "_") + "."
    # Preserve a different output that the user had already selected.
    if previous_default and not previous_default.lower().startswith(prefix.lower()):
        return previous_default
    try:
        sinks = json.loads(_run(["pactl", "--format=json", "list", "sinks"]).stdout)
        if not isinstance(sinks, list):
            raise ValueError()
        local = [sink["name"] for sink in sinks
                 if isinstance(sink, dict) and isinstance(sink.get("name"), str)
                 and sink["name"].startswith("alsa_output.")]
    except (ValueError, TypeError, KeyError):
        raise SpeakerError("The Cube audio service returned an unexpected response.") from None
    if not local:
        raise SpeakerError("No local audio output is available.")
    # Prefer the Cube's existing reSpeaker playback device over HDMI outputs.
    sink = next((name for name in local if "respeaker" in name.lower()), local[0])
    _run(["pactl", "set-default-sink", sink])
    if _run(["pactl", "get-default-sink"]).stdout.strip() != sink:
        raise SpeakerError("The Cube could not select its local audio output.")
    return sink


def disconnect_speaker():
    address, info = _speaker()
    try:
        previous_default = _run(["pactl", "get-default-sink"]).stdout.strip()
    except SpeakerError:
        # An unavailable audio service must not prevent Bluetooth disconnection.
        previous_default = None
    if _property(info, "Connected", "yes"):
        _run(
            ["bluetoothctl", "--timeout", "10", "disconnect", address],
            timeout=13, check=False,
        )
    for attempt in range(6):
        current = _run(["bluetoothctl", "info", address]).stdout
        if _property(current, "Connected", "no"):
            break
        if attempt < 5:
            time.sleep(0.2)
    else:
        raise SpeakerError("I couldn't disconnect the Logitech speaker. Please try again.")
    try:
        sink = _restore_output(address, previous_default)
    except SpeakerError as error:
        raise SpeakerError(
            "The Logitech speaker is disconnected, but playback could not be restored. "
            + str(error)
        ) from None
    return {"speaker_address": address, "playback_sink": sink}

def handle_speaker_command(text):
    command = " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())
    if command.startswith("please "):
        command = command[7:]
    if command in CONNECT_PHRASES:
        action, operation = "speaker_connect", connect_speaker
        response = "Connected to the Logitech speaker."
    elif command in DISCONNECT_PHRASES:
        action, operation = "speaker_disconnect", disconnect_speaker
        response = "Disconnected from the Logitech speaker."
    else:
        return None
    try:
        details = operation()
        return {
            "type": "command", "action": action, "success": True,
            "response": response, **details,
        }
    except SpeakerError as error:
        message = str(error)
    except Exception:
        message = "The speaker command failed. Please try again."
    return {
        "type": "command", "action": action, "success": False,
        "response": message, "error": message,
    }


def _volume_state(sink=None):
    """Read only the assistant bus; physical output selection is independent."""
    sink = sink or "cube.assistant"
    if not sink:
        raise SpeakerError("No selected audio output is available.")
    try:
        sinks = json.loads(_run(["pactl", "--format=json", "list", "sinks"]).stdout)
        if not isinstance(sinks, list):
            raise ValueError()
        selected = next(item for item in sinks if isinstance(item, dict) and item.get("name") == sink)
        raw = selected["volume"]
        if not isinstance(raw, dict) or not raw or type(selected["mute"]) is not bool:
            raise ValueError()
        channels = {}
        for name, value in raw.items():
            if not isinstance(name, str) or not isinstance(value, dict):
                raise ValueError()
            percent = value.get("value_percent")
            if not isinstance(percent, str) or not re.fullmatch(r"\d+%", percent):
                raise ValueError()
            channels[name] = int(percent[:-1])
        return {"sink": sink, "channels": channels, "muted": selected["mute"]}
    except (ValueError, TypeError, KeyError, StopIteration, AttributeError):
        raise SpeakerError("The selected audio output cannot report its volume.") from None


def control_volume(operation, *, percent=None, delta=None):
    """Change only cube.assistant; never fall back to the shared physical sink."""
    if operation not in {"get_volume", "set_volume", "adjust_volume", "mute", "unmute"}:
        raise SpeakerError("The volume request is invalid.")
    if operation == "set_volume" and (type(percent) is not int or not 0 <= percent <= 100):
        raise SpeakerError("Please use a whole volume percentage from 0 to 100.")
    if operation == "adjust_volume" and (type(delta) is not int or delta == 0 or not -20 <= delta <= 20):
        raise SpeakerError("The volume adjustment is invalid.")
    if operation not in {"set_volume", "adjust_volume"} and (percent is not None or delta is not None):
        raise SpeakerError("The volume request is invalid.")
    if ((operation == "set_volume" and delta is not None)
            or (operation == "adjust_volume" and percent is not None)):
        raise SpeakerError("The volume request is invalid.")

    current = _volume_state()
    if operation == "get_volume":
        return current
    sink = current["sink"]
    if operation in {"set_volume", "adjust_volume"}:
        levels = list(current["channels"].values())
        if operation == "adjust_volume" and any(level > 100 for level in levels):
            raise SpeakerError("The selected output is above the voice volume limit. Set an exact safe level first.")
        target = ([percent] * len(levels) if operation == "set_volume" else
                  [min(100, max(0, level + delta)) for level in levels])
        if target != levels:
            _run(["pactl", "set-sink-volume", sink, *(f"{value}%" for value in target)])
        should_unmute = current["muted"] and (
            (operation == "set_volume" and percent > 0)
            or (operation == "adjust_volume" and delta > 0)
        )
        if should_unmute:
            _run(["pactl", "set-sink-mute", sink, "0"])
    elif operation == "mute" and not current["muted"]:
        _run(["pactl", "set-sink-mute", sink, "1"])
    elif operation == "unmute" and current["muted"]:
        _run(["pactl", "set-sink-mute", sink, "0"])
    result = _volume_state(sink)
    return result
