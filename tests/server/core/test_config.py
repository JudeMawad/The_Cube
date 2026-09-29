import unittest
from unittest.mock import patch

from core.config import AINodeConfig, BackendConfig


class ConfigTests(unittest.TestCase):
    def test_local_defaults_and_override(self):
        self.assertEqual(BackendConfig.from_env({}).whisper_model, "base.en")
        self.assertEqual(BackendConfig.from_env({"CUBE_WHISPER_MODEL": " "}).whisper_model, "base.en")
        self.assertEqual(BackendConfig.from_env({"CUBE_WHISPER_MODEL": " small.en "}).whisper_model, "small.en")

    def test_missing_node_is_disabled(self):
        for values in [{}, {"CUBE_AI_NODE_URL": " "}]:
            self.assertIsNone(AINodeConfig.from_env(values).url)

    def test_remote_and_local_node_use_same_configuration(self):
        for url in ["http://remote-ai-node:8766", "http://127.0.0.1:8766", "https://ai.example/cube"]:
            self.assertEqual(AINodeConfig.from_env({"CUBE_AI_NODE_URL": url + "/"}).url, url)

    def test_invalid_node_disables_only_optional_configuration(self):
        for url in ["ftp://ai", "http://", "http://ai:bad", "http://ai:99999", "http://ai:0",
                    "http://user:SECRET@ai", "http://ai?key=SECRET", "http://ai#SECRET", "http://a i"]:
            with self.subTest(url=url), self.assertLogs("core.config", level="WARNING") as logs:
                values = {"CUBE_AI_NODE_URL": url}
                self.assertIsNone(AINodeConfig.from_env(values).url)
                self.assertEqual(BackendConfig.from_env(values).whisper_model, "base.en")
            self.assertNotIn("SECRET", " ".join(logs.output))

    def test_environment_is_read_when_requested(self):
        with patch.dict("os.environ", {"CUBE_AI_NODE_URL": "http://ai:8766"}, clear=True):
            self.assertEqual(AINodeConfig.from_env().url, "http://ai:8766")

    def test_transcription_timeout_defaults_and_valid_values(self):
        for value, expected in [("", 1.0), (" ", 1.0), ("0.2", 0.2), (" 2 ", 2.0)]:
            with self.subTest(value=value):
                config = AINodeConfig.from_env({
                    "CUBE_AI_NODE_URL": "http://ai:8766",
                    "CUBE_AI_TRANSCRIPTION_TIMEOUT": value,
                })
                self.assertEqual(config.transcription_timeout, expected)
                self.assertEqual(config.url, "http://ai:8766")
        self.assertEqual(AINodeConfig.from_env({}).transcription_timeout, 1.0)

    def test_invalid_transcription_timeout_uses_safe_default(self):
        for value in ["0", "-1", "nan", "inf", "-inf", "SECRET"]:
            with self.subTest(value=value), self.assertLogs("core.config", level="WARNING") as logs:
                config = AINodeConfig.from_env({
                    "CUBE_AI_NODE_URL": "http://ai:8766",
                    "CUBE_AI_TRANSCRIPTION_TIMEOUT": value,
                })
            self.assertEqual(config.url, "http://ai:8766")
            self.assertEqual(config.transcription_timeout, 1.0)
            self.assertNotIn("SECRET", " ".join(logs.output))

    def test_process_timeout_is_independent_and_defaults_safely(self):
        for raw, expected in [("", 10.0), (" ", 10.0), ("0.05", 0.05), (" 3 ", 3.0)]:
            config = AINodeConfig.from_env({
                "CUBE_AI_NODE_URL": "http://ai.invalid",
                "CUBE_AI_TRANSCRIPTION_TIMEOUT": "0.7", "CUBE_AI_PROCESS_TIMEOUT": raw})
            self.assertEqual(config.process_timeout, expected)
            self.assertEqual(config.transcription_timeout, 0.7)
            self.assertEqual(config.url, "http://ai.invalid")
        self.assertEqual(AINodeConfig.from_env({}).process_timeout, 10.0)
        for raw in ["0", "-1", "nan", "inf", "-inf", "SECRET"]:
            with self.subTest(raw=raw), self.assertLogs("core.config", level="WARNING") as logs:
                config = AINodeConfig.from_env({"CUBE_AI_PROCESS_TIMEOUT": raw})
            self.assertEqual(config.process_timeout, 10.0)
            self.assertIsNone(config.url)
            self.assertNotIn("SECRET", " ".join(logs.output))
