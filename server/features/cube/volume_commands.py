"""Catalog-backed, complete-utterance volume commands for the hybrid router."""
import json
from pathlib import Path
import re

from core.schemas import ToolIntent


def _load_rules():
    path = Path(__file__).resolve().parents[2] / "core/voice_commands.json"
    rows = json.loads(path.read_text())["commands"]
    return {row["id"]: row for row in rows if row["feature"] == "volume"}


RULES = _load_rules()


def normalize(text):
    text = text.lower().replace("’", "'").strip().rstrip(".?! ")
    text = re.sub(r"[^\w\s%.,+\-']", " ", text)
    text = " ".join(text.split())
    text = re.sub(r"^(?:(?:can|could|would|will) you (?:please )?|please )", "", text)
    return re.sub(r" please$", "", text)


def match(text, previous_direction=None):
    """Return a validated-intent candidate, clarification text, or no match.

    The catalog owns every executable phrase. Numeric syntax is deliberately
    broader than the valid integer range so unsafe numbers are rejected here.
    """
    command = normalize(text)
    matches = []
    for rule in RULES.values():
        if rule.get("fast_path_exact") and command in rule.get("exact", []):
            matches.append((rule["id"], None))
        for pattern in rule.get("patterns", []):
            if pattern.get("fast_path") and (found := getattr(re, pattern["mode"])(pattern["regex"], command)):
                matches.append((rule["id"], found))
    if len(matches) != 1:
        if not matches:
            for pattern in RULES["volume_set"].get("patterns", []):
                if pattern.get("label") == "malformed numeric setting guard" and (
                        getattr(re, pattern["mode"])(pattern["regex"], command)):
                    return "Please use a whole volume percentage from 0 to 100."
        return None
    name, found = matches[0]
    if name == "volume_more_small":
        if previous_direction not in {-1, 1}:
            return "Do you mean louder or quieter?"
        name = "volume_up_small" if previous_direction > 0 else "volume_down_small"
    if name == "volume_set":
        raw = found.group("percent")
        if not re.fullmatch(r"[0-9]+", raw) or int(raw) > 100:
            return "Please use a whole volume percentage from 0 to 100."
        return ToolIntent(type="tool", tool="cube.set_volume", arguments={"percent": int(raw)})
    if name in {"volume_up", "volume_down", "volume_up_small", "volume_down_small"}:
        direction = 1 if "up" in name else -1
        amount = 5 if "small" in name else 10
        return ToolIntent(type="tool", tool="cube.adjust_volume", arguments={"delta": direction * amount})
    if name in {"volume_max", "volume_min"}:
        return ToolIntent(type="tool", tool="cube.set_volume", arguments={"percent": 100 if name == "volume_max" else 0})
    return ToolIntent(type="tool", tool={
        "mute": "cube.mute", "unmute": "cube.unmute", "volume_status": "cube.get_volume",
    }[name], arguments={})
