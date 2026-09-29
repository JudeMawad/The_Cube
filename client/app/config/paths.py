"""Model locations owned by the Pi client deployment."""
import os
from pathlib import Path

CLIENT_DIR = Path(__file__).resolve().parents[2]
APP_DIR = CLIENT_DIR / "app"


def asset_path(name, default, environ=None):
    values = os.environ if environ is None else environ
    path = Path(values.get(name, "").strip() or default).expanduser()
    return path if path.is_absolute() else CLIENT_DIR / path


def wake_model_path(environ=None):
    return asset_path("CUBE_WAKE_MODEL_PATH", "assets/models/hey_cube.onnx", environ)


def piper_voice_path(environ=None):
    return asset_path("CUBE_PIPER_VOICE_PATH", "assets/voices/en_US-lessac-medium.onnx", environ)
