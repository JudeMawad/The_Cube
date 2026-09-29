"""Deterministic light handling; vocabulary comes from the server snapshot."""
import json
from pathlib import Path
import re

from .power import power_reply


def _load_rules():
    path = Path(__file__).resolve().parents[2] / "core/voice_commands.json"
    rows = json.loads(path.read_text())["commands"]
    return {row["id"]: row for row in rows if row["feature"] == "lights"}


RULES = _load_rules()
COLORS = set(RULES["color"]["colors"])


def normalize(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^\w\s%]", " ", text)
    return " ".join(text.split())


def _polite_command(text: str) -> str:
    command = normalize(text)
    command = re.sub(r"^(?:(?:can|could|would|will) you (?:please )?|please )", "", command)
    return re.sub(r" please$", "", command)


def _matches(pattern, command):
    return getattr(re, pattern["mode"])(pattern["regex"], command)


def is_direct_command(text: str) -> bool:
    """Only a unique complete light request may skip interpretation."""
    command = _polite_command(text)
    matches = {row["id"] for row in RULES.values() for pattern in row.get("patterns", [])
               if pattern.get("fast_path", False) and _matches(pattern, command)}
    matches.update(row["id"] for row in RULES.values()
                   if row.get("fast_path_exact", False) and command in row.get("exact", []))
    return len(matches) == 1


def handle_command(text: str, *, control_lights, power_commands=None):
    command = _polite_command(text)
    for rule_id, enabled, action in (("power_on", True, "lights_on"), ("power_off", False, "lights_off")):
        if command in RULES[rule_id].get("exact", []) or any(
                _matches(pattern, command) for pattern in RULES[rule_id]["patterns"]):
            if power_commands is not None:
                return power_reply(power_commands.set_power("all", enabled))
            control_lights("on" if enabled else "off")
            return {"action": action, "response": "Done."}

    brightness = next((match for pattern in RULES["brightness"]["patterns"]
                       if pattern["mode"] == "search"
                       if (match := _matches(pattern, command))), None)
    if brightness:
        value = int(brightness.group(1))
        if 1 <= value <= 100:
            control_lights("brightness", value)
            return {"action": "lights_brightness", "value": value,
                    "response": f"Brightness set to {value} percent."}

    words = set(command.split())
    if words & set(RULES["color"]["target_words"]):
        for color in RULES["color"]["colors"]:
            if color in words:
                control_lights("color", color)
                return {"action": "lights_color", "value": color,
                        "response": f"Lights set to {color}."}
    return None
