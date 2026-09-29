"""Catalog-backed local tool intents, executed through the normal tool registry."""
import json
from pathlib import Path
import re

from .schemas import ToolIntent
from .command_safety import classify_music, normalized_command
from features.cube.volume_commands import match as match_volume


def _load_rules():
    rows = json.loads(Path(__file__).with_name("voice_commands.json").read_text())["commands"]
    return {(row["feature"], row["id"]): row for row in rows
            if row["feature"] in {"cube", "light_targets", "plugs", "music"}}


RULES = _load_rules()


def music_decision(text):
    return classify_music(text, RULES)


def match(text, previous_direction=None):
    music = music_decision(text)
    if music.state == "executable":
        return ToolIntent(type="tool", tool="music." + music.operation, arguments=music.arguments)
    if music.state == "clarification":
        return music.reply
    if music.state == "blocked":
        return None
    command = normalized_command(text)
    matches = []
    for key, rule in RULES.items():
        if key[0] == "music":
            continue
        if rule.get("fast_path_exact") and command in rule.get("exact", []):
            matches.append((key, {}))
        for pattern in rule.get("patterns", []):
            if pattern.get("fast_path") and (found := re.fullmatch(pattern["regex"], command)):
                matches.append((key, found.groupdict()))
    if not matches:
        for pattern in RULES[("cube", "set_brightness")].get("patterns", []):
            if pattern["label"] == "malformed numeric setting guard" and re.fullmatch(pattern["regex"], command):
                return "Please use a whole display brightness percentage from 0 to 100."
        return match_volume(text, previous_direction)
    if len(matches) != 1:
        return None
    (feature, operation), arguments = matches[0]
    if feature == "cube":
        if operation in {"display_on", "display_off"}:
            subject, state = operation.split("_")
            operation, arguments = "set_" + subject, {"state": state}
        elif operation == "set_brightness":
            raw = arguments["percent"]
            if not re.fullmatch(r"[0-9]+", raw) or int(raw) > 100:
                return "Please use a whole display brightness percentage from 0 to 100."
            arguments = {"percent": int(raw)}
        tool = "cube." + operation
    elif feature == "light_targets":
        tool = "lights.set_power"
        if arguments["target"] in {"smart life", "small"}:
            arguments["target"] = "tuya"
    else:
        tool = "plugs.get_state"
    return ToolIntent(type="tool", tool=tool, arguments=arguments)
