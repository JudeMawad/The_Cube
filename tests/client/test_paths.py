"""Production entrypoint and configurable asset path regressions."""
from pathlib import Path
import unittest
import tempfile

from config.paths import APP_DIR, CLIENT_DIR, piper_voice_path, wake_model_path
from tests.paths import ROOT


class ClientPathTests(unittest.TestCase):
    def test_user_supplied_wake_path_needs_no_bundled_model(self):
        self.assertEqual(wake_model_path({}), CLIENT_DIR / "assets/models/hey_cube.onnx")
        with tempfile.TemporaryDirectory() as directory:
            supplied = Path(directory) / "detector.onnx"
            self.assertFalse(supplied.exists())
            self.assertEqual(wake_model_path({"CUBE_WAKE_MODEL_PATH": str(supplied)}), supplied)

    def test_default_blank_relative_absolute_and_home_paths(self):
        for variable, resolve in (("CUBE_WAKE_MODEL_PATH", wake_model_path),
                                  ("CUBE_PIPER_VOICE_PATH", piper_voice_path)):
            self.assertEqual(resolve({variable: " "}), resolve({}))
            self.assertEqual(resolve({variable: " custom/model.onnx "}), CLIENT_DIR / "custom/model.onnx")
            self.assertEqual(resolve({variable: "/shared/model.onnx"}), Path("/shared/model.onnx"))
            self.assertEqual(resolve({variable: "~/model.onnx"}), Path.home() / "model.onnx")
        self.assertEqual(piper_voice_path({}), CLIENT_DIR / "assets/voices/en_US-lessac-medium.onnx")

    def test_speech_keeps_the_installed_piper_executable(self):
        from audio import speech
        self.assertEqual(speech.PIPER, APP_DIR / ".venv/bin/piper")

    def test_service_units_preserve_voice_and_relocate_only_renderer(self):
        voice = (ROOT / "client/deploy/cube-voice.service").read_text()
        self.assertIn("ExecStart=@CUBE_REPO@/client/app/.venv/bin/python @CUBE_REPO@/client/app/cube.py", voice)
        renderer = (ROOT / "client/deploy/cube-display.service").read_text()
        self.assertIn("Wants=cube-display.service", voice)
        self.assertIn("After=cube-display.service", voice)
        self.assertIn("Description=Cube Display", renderer)
        self.assertIn("WorkingDirectory=@CUBE_REPO@/client/native/cube-display", renderer)
        self.assertIn("ExecStart=@CUBE_REPO@/client/native/cube-display/cube-display", renderer)
        self.assertIn("--led-pixel-mapper=Rotate:-90", renderer)
        self.assertIn("--led-rp1-pio=1", renderer)
        self.assertIn("--led-brightness=50", renderer)

    def test_one_production_native_matrix_owner(self):
        owners = {
            source.relative_to(ROOT).as_posix()
            for source in ROOT.glob("client/native/*/*.cc")
            if "RGBMatrix::CreateFromFlags" in source.read_text()
        }
        self.assertEqual(owners, {"client/native/cube-display/cube-display.cc"})
