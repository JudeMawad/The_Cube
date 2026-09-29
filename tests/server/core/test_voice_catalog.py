"""Canonical vocabulary and generated server snapshot checks."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from features.lights import commands as lights
from features.cube import volume_commands as volume
from features.media.movie_commands import MovieCommands, RULES as MOVIE_RULES, parse_intent
from integrations.overseerr_client import Movie

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("build_voice_catalog", ROOT / "scripts/build_voice_catalog.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class VoiceCatalogTests(unittest.TestCase):
    def test_committed_snapshots_match_canonical_toml(self):
        builder.build(ROOT / "config/voice_commands.toml", builder.DEFAULTS, check=True)

    def test_every_movie_exact_alias_reaches_owning_handler(self):
        for rule_id, rule in MOVIE_RULES.items():
            if rule_id.startswith("select_"):
                continue
            for alias in rule.get("exact", []):
                with self.subTest(rule=rule_id, alias=alias):
                    self.assertEqual(parse_intent(alias).kind, rule_id)
                    client = Mock()
                    client.search_movies.return_value = [Movie(1, "Dune", "2021")]
                    result = MovieCommands(client=client).handle(alias)
                    self.assertIsNotNone(result)

    def test_movie_parameter_patterns_and_selection(self):
        for text, kind, query in [
            ("cancel the download of Dune", "cancel_download", "Dune"),
            ("status of Dune", "status", "Dune"),
            ("is Dune available yet", "status", "Dune"),
            ("no i meant Dune", "correct", "Dune"),
            ("download Dune", "request", "Dune"),
            ("i want to watch Dune", "availability", "Dune"),
        ]:
            with self.subTest(text=text):
                intent = parse_intent(text)
                self.assertEqual((intent.kind, intent.query), (kind, query))
        flow = MovieCommands(client=Mock())
        movies = (Movie(1, "Dune", "1984"), Movie(2, "Dune", "2021"))
        self.assertEqual(flow._resolve("the first one", movies), movies[0])
        self.assertEqual(flow._resolve("2021", movies), movies[1])
        for alias in MOVIE_RULES["select_newer"]["exact"]:
            self.assertEqual(flow._resolve(alias, movies), movies[1])
        for alias in MOVIE_RULES["select_older"]["exact"]:
            self.assertEqual(flow._resolve(alias, movies), movies[0])

    def test_every_fast_pattern_example_reaches_light_handler(self):
        for rule in lights.RULES.values():
            for pattern in rule.get("patterns", []):
                if not pattern.get("fast_path", False):
                    continue
                for example in pattern["examples"]:
                    with self.subTest(rule=rule["id"], example=example):
                        self.assertTrue(lights.is_direct_command(example))
                        self.assertIsNotNone(lights.handle_command(example, control_lights=Mock()))

    def test_light_fast_path_and_execution_boundaries(self):
        for text in ("lights on", "turn on the bulbs", "spots 1%", "lights 100 percent", "make the lights cyan"):
            with self.subTest(text=text):
                self.assertTrue(lights.is_direct_command(text))
                self.assertIsNotNone(lights.handle_command(text, control_lights=Mock()))
        for text in ("lights 0%", "lights 101%", "don't turn on lights", "what if lights on", "lights on tomorrow"):
            with self.subTest(text=text):
                self.assertFalse(lights.is_direct_command(text))
        self.assertFalse(lights.is_direct_command("make lights ultraviolet"))

    def test_alias_edit_changes_recognition_without_python_phrase_edit(self):
        with tempfile.TemporaryDirectory() as directory:
            base = (ROOT / "config/voice_commands.toml").read_text()
            catalog = Path(directory) / "commands.toml"
            outputs = {service: Path(directory) / f"{service}.json" for service in builder.DEFAULTS}
            alias = 'exact = ["illuminate now"]\n'
            marker = 'id = "power_on"\n'
            self.assertIsNone(lights.handle_command("illuminate now", control_lights=Mock()))
            catalog.write_text(base.replace(marker, marker + alias, 1))
            builder.build(catalog, outputs)
            rows = json.loads(outputs["server"].read_text())["commands"]
            with patch.object(lights, "RULES", {row["id"]: row for row in rows if row["feature"] == "lights"}):
                self.assertEqual(lights.handle_command("illuminate now", control_lights=Mock())["action"], "lights_on")
                self.assertFalse(lights.is_direct_command("illuminate now"))
            catalog.write_text(base)
            builder.build(catalog, outputs)
            rows = json.loads(outputs["server"].read_text())["commands"]
            with patch.object(lights, "RULES", {row["id"]: row for row in rows if row["feature"] == "lights"}):
                self.assertIsNone(lights.handle_command("illuminate now", control_lights=Mock()))

    def test_volume_alias_edit_changes_actual_routing(self):
        with tempfile.TemporaryDirectory() as directory:
            original = (ROOT / "config/voice_commands.toml").read_text()
            catalog = Path(directory) / "commands.toml"
            outputs = {service: Path(directory) / f"{service}.json" for service in builder.DEFAULTS}
            self.assertIsNone(volume.match("boost cube sound"))
            catalog.write_text(original.replace(
                '"speak louder"]', '"speak louder", "boost cube sound"]', 1))
            builder.build(catalog, outputs)
            rows = json.loads(outputs["server"].read_text())["commands"]
            with patch.object(volume, "RULES", {row["id"]: row for row in rows if row["feature"] == "volume"}):
                self.assertEqual(volume.match("boost cube sound").arguments, {"delta": 10})
            self.assertEqual(json.loads(outputs["client"].read_text()),
                             json.loads((ROOT / "client/app/config/voice_commands.json").read_text()))
            catalog.write_text(original)
            builder.build(catalog, outputs)
            rows = json.loads(outputs["server"].read_text())["commands"]
            with patch.object(volume, "RULES", {row["id"]: row for row in rows if row["feature"] == "volume"}):
                self.assertIsNone(volume.match("boost cube sound"))

    def test_invalid_catalog_and_ambiguous_fast_path_fail_closed(self):
        source = json.loads((ROOT / "server/core/voice_commands.json").read_text())
        duplicate = copy.deepcopy(source)
        duplicate["commands"][1]["exact"] = ["lights on"]
        duplicate["commands"][0]["exact"] = ["lights on"]
        with self.assertRaisesRegex(ValueError, "duplicate normalized"):
            builder.validate(duplicate)
        invalid = copy.deepcopy(source)
        invalid["commands"][0]["patterns"][0]["regex"] = "("
        with self.assertRaises(Exception):
            builder.validate(invalid)
        ambiguous = copy.deepcopy(source)
        ambiguous["commands"][1]["patterns"][0]["regex"] = ambiguous["commands"][0]["patterns"][0]["regex"]
        ambiguous["commands"][1]["patterns"][0]["examples"] = ["lights on"]
        with self.assertRaisesRegex(ValueError, "conflicting fast-path"):
            builder.validate(ambiguous)
        with patch.object(lights, "RULES", {row["id"]: row for row in ambiguous["commands"] if row["feature"] == "lights"}):
            self.assertFalse(lights.is_direct_command("lights on"))
