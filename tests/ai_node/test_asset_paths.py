"""Service-local model paths and independent provisioning."""
from contextlib import chdir
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest

from ai_node.config import SERVICE_DIR, STTConfig, KokoroConfig
from tests.paths import ROOT


class AssetPathTests(unittest.TestCase):
    def test_defaults_are_inside_this_service(self):
        self.assertEqual(SERVICE_DIR, ROOT / "ai_node")
        config = KokoroConfig.from_env({})
        self.assertEqual(config.model_path, SERVICE_DIR / "assets/models/kokoro/kokoro-v1.0.onnx")
        self.assertEqual(config.voices_path, SERVICE_DIR / "assets/models/kokoro/voices-v1.0.bin")
        self.assertEqual(STTConfig.from_env({}).cache_dir, str(SERVICE_DIR / "assets/models/whisper"))

    def test_blank_relative_absolute_and_home_paths(self):
        defaults = KokoroConfig.from_env({})
        self.assertEqual(KokoroConfig.from_env({"CUBE_AI_KOKORO_MODEL_PATH": " "}), defaults)
        config = KokoroConfig.from_env({"CUBE_AI_KOKORO_MODEL_PATH": " custom/model.onnx ",
                                        "CUBE_AI_KOKORO_VOICES_PATH": "~/voices.bin"})
        self.assertEqual(config.model_path, SERVICE_DIR / "custom/model.onnx")
        self.assertEqual(config.voices_path, Path.home() / "voices.bin")
        config = KokoroConfig.from_env({"CUBE_AI_KOKORO_MODEL_PATH": "/shared/model.onnx"})
        self.assertEqual(config.model_path, Path("/shared/model.onnx"))
        self.assertEqual(STTConfig.from_env({"CUBE_AI_WHISPER_CACHE_DIR": "custom/cache"}).cache_dir,
                         str(SERVICE_DIR / "custom/cache"))

    def test_paths_are_independent_of_working_directory(self):
        with tempfile.TemporaryDirectory() as directory, chdir(directory):
            self.assertEqual(KokoroConfig.from_env({}).model_path,
                             SERVICE_DIR / "assets/models/kokoro/kokoro-v1.0.onnx")

    def test_setup_preserves_existing_files_at_configured_destinations(self):
        with tempfile.TemporaryDirectory() as directory:
            model, voices = Path(directory) / "model.onnx", Path(directory) / "voices.bin"
            model.write_bytes(b"existing model")
            voices.write_bytes(b"existing voices")
            result = subprocess.run([sys.executable, str(SERVICE_DIR / "scripts/setup_kokoro.py")],
                                    cwd=directory, capture_output=True, text=True, timeout=10,
                                    env={**os.environ, "CUBE_AI_KOKORO_MODEL_PATH": str(model),
                                         "CUBE_AI_KOKORO_VOICES_PATH": str(voices)})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(model.read_bytes(), b"existing model")
            self.assertEqual(voices.read_bytes(), b"existing voices")
