"""Actual shared safety functions, independent of optional backend packages."""
import json
from pathlib import Path
from unittest import TestCase

from core.command_safety import _non_action_request, _status_question, classify_music

RULES = {(row["feature"], row["id"]): row for row in json.loads(
    (Path(__file__).resolve().parents[2] / "server/core/voice_commands.json").read_text())["commands"]}


class MusicSafetyTests(TestCase):
    def test_negative_title_allowed_only_in_affirmative_play_envelope(self):
        for text in ("play Don't Stop Me Now by Queen", 'play "Don\'t Stop Me Now" by Queen',
                     "Could you please play track Don't Stop Believin'", "play track I Can't Get No Satisfaction",
                     "play track Never Gonna Give You Up", "play track Imagine", "play artist The Weeknd",
                     "play my Discover Weekly"):
            with self.subTest(text=text):
                self.assertEqual(classify_music(text, RULES).state, "executable")
                self.assertFalse(_status_question(text))

    def test_questions_negations_quotations_and_compound_refusals(self):
        for text in ("don't play music", "do not play Don't Stop Me Now", "never play Queen",
                     "can you not play music", "why play music", "what does play music mean",
                     '"play music"', "I said play music", "what if you play Queen",
                     "play Queen but don't start it", "play Queen, don't start it",
                     "play track Blinding Lights, actually don't",
                     "play track Blinding Lights only if I ask later",
                     "play track Blinding Lights but do not start it",
                     "play nothing", "when I say play Queen what happens", "can I play music"):
            with self.subTest(text=text):
                self.assertNotEqual(classify_music(text, RULES).state, "executable")

    def test_existing_guards_still_reject_nonmusic_discussion(self):
        for text in ("Don't turn on lights", "don't set volume 40", 'What does "lights on" mean?',
                     "why reboot cube", "don't skip", "don't go next", "don't go previous"):
            with self.subTest(text=text):
                self.assertTrue(_non_action_request(text))

    def test_catalog_owns_play_envelope(self):
        self.assertEqual(classify_music("play Queen", {}).state, "not_music")
        rules = {("music", "play_request"): {"patterns": [{"regex": r"listen to (?P<query>.+)", "fast_path": True}]}}
        self.assertEqual(classify_music("listen to Queen", rules).state, "executable")
        self.assertEqual(classify_music("play Queen", rules).state, "not_music")

    def test_wrappers_establish_ownership_but_never_execution(self):
        for command in ("play artist Queen", "pause music", "pause", "next", "set music volume 40"):
            for wrapper in ("don't {}", "do not {}", "never {}", "why {}?", "what if you {}",
                            '"{}"', "‘{}’", "I said {}", 'what does "{}" mean?',
                            "if I say {}", "when I say {}", "suppose you {}"):
                with self.subTest(command=command, wrapper=wrapper):
                    decision = classify_music(wrapper.format(command), RULES)
                    self.assertEqual(decision.state, "blocked")
                    self.assertIsNone(decision.operation)
                    self.assertEqual(decision.arguments, {})

    def test_nonmusic_ownership_precedes_music_safety(self):
        for text in ("play chess only if you can", "play chess with me, but don't let me win",
                     "put on the lights", "Why do people like music?", "next week",
                     "play Queen", "don't turn on lights"):
            with self.subTest(text=text):
                self.assertEqual(classify_music(text, RULES).state, "not_music")
