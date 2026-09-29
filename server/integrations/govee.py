#!/usr/bin/env python3

import json
import sys
import uuid
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

API_URL = "https://openapi.api.govee.com/router/api/v1/device/control"
CONFIG_PATH = Path.home() / ".config/cube/govee.json"


class GoveeError(RuntimeError):
    """Bounded diagnostic, without credentials or upstream response bodies."""


class GoveeConfigError(GoveeError):
    pass


def load_settings(path=None):
    try:
        data = json.loads((CONFIG_PATH if path is None else Path(path)).read_text())
        if not isinstance(data, dict) or set(data) != {"sku", "devices", "all_spots_group"}:
            raise ValueError()
        devices, group = data["devices"], data["all_spots_group"]
        if (not isinstance(devices, dict) or not devices or "all" in devices
                or not isinstance(group, dict) or set(group) != {"sku", "device"}):
            raise ValueError()
        strings = [data["sku"], *devices.keys(), *devices.values(), *group.values()]
        if any(not isinstance(value, str) or not value.strip() for value in strings):
            raise ValueError()
        return data
    except (OSError, UnicodeError, ValueError, TypeError):
        raise GoveeConfigError("Govee device configuration is missing or invalid.") from None


COLORS = {
    "red": (255, 0, 0),
    "green": (0, 255, 0),
    "blue": (0, 0, 255),
    "white": (255, 255, 255),
    "yellow": (255, 255, 0),
    "orange": (255, 128, 0),
    "purple": (128, 0, 255),
    "pink": (255, 40, 140),
    "cyan": (0, 255, 255),
}


def load_api_key() -> str:
    path = Path.home() / ".config/cube/govee_api_key"

    try:
        key = path.read_text().strip()
        if not key or any(c.isspace() for c in key):
            raise ValueError()
        return key
    except (OSError, UnicodeError, ValueError):
        raise GoveeConfigError("Govee API key is missing or invalid.") from None


def rgb_integer(red: int, green: int, blue: int) -> int:
    return (red << 16) | (green << 8) | blue


def send_control(
    api_key: str,
    sku:str,
    device: str,
    capability_type: str,
    instance: str,
    value,
) -> None:
    body = {
        "requestId": str(uuid.uuid4()),
        "payload": {
            "sku": sku,
            "device": device,
            "capability": {
                "type": capability_type,
                "instance": instance,
                "value": value,
            },
        },
    }

    request = urllib.request.Request(
        API_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Govee-API-Key": api_key,
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise GoveeError(f"Govee HTTP request failed (status {error.code}).") from None
    except (urllib.error.URLError, OSError, ValueError, UnicodeError):
        raise GoveeError("Govee request could not be verified.") from None

    if not isinstance(result, dict) or result.get("code") != 200:
        raise GoveeError("Govee rejected the command.")


def capability(action, value=None):
    if action in {"on", "off"}:
        return "devices.capabilities.on_off", "powerSwitch", int(action == "on")
    if action == "brightness":
        value = int(value)
        if not 1 <= value <= 100:
            raise ValueError("Brightness must be 1-100.")
        return "devices.capabilities.range", "brightness", value
    if action == "color":
        value = str(value).lower()
        if value not in COLORS:
            raise ValueError("Unknown colour.")
        return "devices.capabilities.color_setting", "colorRgb", rgb_integer(*COLORS[value])
    if action in {"temperature", "temp"}:
        value = int(value)
        if not 2700 <= value <= 6500:
            raise ValueError("Temperature must be 2700-6500 K.")
        return "devices.capabilities.color_setting", "colorTemperatureK", value
    raise ValueError("Unknown action.")


def control(action, value=None, target="all", *, report=lambda name: None):
    action, target = action.lower(), target.lower()
    operation = capability(action, value)
    settings = load_settings()
    devices = settings["devices"]
    if target != "all" and target not in devices:
        raise ValueError("Unknown target.")
    selected = list(devices.items()) if target == "all" else [(target, devices[target])]
    key = load_api_key()
    if target == "all" and action in {"on", "off"}:
        group = settings["all_spots_group"]
        send_control(key, group["sku"], group["device"], *operation)
        report("all")
        return
    failures = 0
    with ThreadPoolExecutor(max_workers=len(selected)) as executor:
        futures = {executor.submit(send_control, key, settings["sku"], device, *operation): name
                   for name, device in selected}
        for future in as_completed(futures):
            try:
                future.result()
            except Exception:
                failures += 1
            else:
                report(futures[future])
    if failures:
        raise GoveeError(f"Govee control failed for {failures} device(s).")


def main():
    if len(sys.argv) not in {3, 4}:
        print("Usage: python3 govee.py TARGET on|off|brightness|color|temperature [VALUE]")
        raise SystemExit(1)
    try:
        action = sys.argv[2].lower()
        if len(sys.argv) != (3 if action in {"on", "off"} else 4):
            raise ValueError("Incorrect number of arguments.")
        control(action, sys.argv[3] if len(sys.argv) == 4 else None, sys.argv[1],
                report=lambda name: print(f"{name}: command successful"))
    except (GoveeError, ValueError, TypeError) as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    main()
