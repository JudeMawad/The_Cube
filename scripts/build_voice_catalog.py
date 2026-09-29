"""Validate the canonical voice vocabulary and build service-local snapshots."""

import argparse
import json
from pathlib import Path
import re
import tomllib

ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = {
    "server": ROOT / "server/core/voice_commands.json",
    "client": ROOT / "client/app/config/voice_commands.json",
}


def normalized(value, feature):
    if feature in {"movie", "volume", "cube"}:
        return " ".join(re.sub(r"[^\w\s']", " ", value.lower().replace("’", "'")).split())
    return " ".join(re.sub(r"[^\w\s]", " ", value.lower()).split())


def validate(document):
    if set(document) != {"version", "commands"} or document["version"] != 1:
        raise ValueError("catalog must have version = 1 and commands")
    commands = document["commands"]
    if not isinstance(commands, list):
        raise ValueError("commands must be a list")
    ids, aliases, fast_rules = set(), {}, []
    for row in commands:
        if not isinstance(row, dict) or set(row) - {"service", "feature", "id", "exact", "patterns", "colors", "target_words", "fast_path_exact", "restricted_when_cancelling"}:
            raise ValueError("invalid command fields")
        service, feature, command_id = (row.get(key) for key in ("service", "feature", "id"))
        if service not in DEFAULTS or not isinstance(feature, str) or not feature or not isinstance(command_id, str) or not command_id:
            raise ValueError("invalid command identity")
        key = (service, feature, command_id)
        if key in ids:
            raise ValueError(f"duplicate command id: {key}")
        ids.add(key)
        exact, patterns = row.get("exact", []), row.get("patterns", [])
        if not isinstance(exact, list) or not isinstance(patterns, list) or not exact and not patterns:
            raise ValueError(f"command needs exact aliases or patterns: {key}")
        if "fast_path_exact" in row and not isinstance(row["fast_path_exact"], bool):
            raise ValueError("fast_path_exact must be boolean")
        for alias in exact:
            if not isinstance(alias, str) or not alias or normalized(alias, feature) != alias:
                raise ValueError(f"exact alias is not normalized: {alias!r}")
            alias_key = (service, normalized(alias, feature))
            if alias_key in aliases:
                raise ValueError(f"duplicate normalized exact alias: {alias!r}")
            aliases[alias_key] = key
            if row.get("fast_path_exact", False):
                fast_rules.append((key, alias, lambda text, alias=alias: text == alias))
        restricted = row.get("restricted_when_cancelling", [])
        if not isinstance(restricted, list) or any(alias not in exact for alias in restricted):
            raise ValueError("restricted dialogue aliases must be exact aliases")
        targets = row.get("target_words", [])
        if not isinstance(targets, list) or any(not isinstance(word, str) or not word for word in targets) or len(set(targets)) != len(targets):
            raise ValueError("invalid target words")
        colors = row.get("colors", [])
        if not isinstance(colors, list) or any(not isinstance(color, str) or not color for color in colors) or len(set(colors)) != len(colors):
            raise ValueError("invalid colors")
        for pattern in patterns:
            if not isinstance(pattern, dict) or set(pattern) - {"label", "regex", "mode", "fast_path", "examples"}:
                raise ValueError("invalid pattern fields")
            if not isinstance(pattern.get("label"), str) or not pattern["label"] or not isinstance(pattern.get("regex"), str) or pattern.get("mode") not in {"fullmatch", "match", "search"}:
                raise ValueError("pattern needs label, regex and match mode")
            if "fast_path" in pattern and not isinstance(pattern["fast_path"], bool):
                raise ValueError("fast_path must be boolean")
            compiled = re.compile(pattern["regex"])
            examples = pattern.get("examples", [])
            if (not isinstance(examples, list) or any(not isinstance(example, str) or not example for example in examples)
                    or pattern.get("fast_path", False) and not examples):
                raise ValueError("invalid pattern examples")
            matcher = getattr(compiled, pattern["mode"])
            for example in examples:
                if not matcher(example):
                    raise ValueError(f"pattern example does not match: {example!r}")
                if pattern.get("fast_path", False):
                    fast_rules.append((key, example, matcher))
    for _, example, _ in fast_rules:
        matches = {key for key, _, matcher in fast_rules if matcher(example)}
        if len(matches) > 1:
            raise ValueError(f"conflicting fast-path example: {example!r}")
    return commands


def build(catalog, outputs, check=False):
    with catalog.open("rb") as handle:
        commands = validate(tomllib.load(handle))
    stale = []
    for service, destination in outputs.items():
        content = json.dumps({"version": 1, "commands": [row for row in commands if row["service"] == service]}, indent=2, ensure_ascii=False) + "\n"
        if check:
            if not destination.exists() or destination.read_text() != content:
                stale.append(str(destination))
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content)
    if stale:
        raise ValueError("stale voice catalog snapshot: " + ", ".join(stale))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=ROOT / "config/voice_commands.toml")
    parser.add_argument("--server-output", type=Path, default=DEFAULTS["server"])
    parser.add_argument("--client-output", type=Path, default=DEFAULTS["client"])
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        build(args.catalog, {"server": args.server_output, "client": args.client_output}, args.check)
    except (ValueError, OSError, tomllib.TOMLDecodeError, re.error) as error:
        parser.exit(1, f"voice catalog: {error}\n")


if __name__ == "__main__":
    main()
