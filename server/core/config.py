"""Architecture settings only; integration credentials keep their existing loaders."""
from dataclasses import dataclass
import logging
import math
import os
from pathlib import Path
from urllib.parse import urlsplit


SERVICE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_WHISPER_CACHE = str(SERVICE_DIR / "assets/models/whisper")


def asset_path(values, name, default):
    """Resolve configured files against this service, independent of the cwd."""
    path = Path(values.get(name, "").strip() or default).expanduser()
    return path if path.is_absolute() else SERVICE_DIR / path


@dataclass(frozen=True)
class KokoroConfig:
    model_path: Path
    voices_path: Path

    @classmethod
    def from_env(cls, environ=None):
        values = os.environ if environ is None else environ
        return cls(
            asset_path(values, "CUBE_KOKORO_MODEL_PATH", "assets/models/kokoro/kokoro-v1.0.onnx"),
            asset_path(values, "CUBE_KOKORO_VOICES_PATH", "assets/models/kokoro/voices-v1.0.bin"),
        )


def _positive_timeout(values, name, default):
    raw = values.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
        if not math.isfinite(value) or value <= 0:
            raise ValueError()
        return value
    except ValueError:
        logging.getLogger(__name__).warning("Invalid %s; using %s seconds", name, default)
        return default


@dataclass(frozen=True)
class BackendConfig:
    whisper_model: str = "base.en"
    whisper_cache_dir: str = DEFAULT_WHISPER_CACHE

    @classmethod
    def from_env(cls, environ=None):
        values = os.environ if environ is None else environ
        return cls(
            whisper_model=values.get("CUBE_WHISPER_MODEL", "").strip() or "base.en",
            whisper_cache_dir=str(asset_path(values, "CUBE_WHISPER_CACHE_DIR", DEFAULT_WHISPER_CACHE)),
        )


@dataclass(frozen=True)
class AINodeConfig:
    url: str | None = None
    transcription_timeout: float = 1.0
    process_timeout: float = 10.0

    @classmethod
    def from_env(cls, environ=None):
        values = os.environ if environ is None else environ
        timeout = _positive_timeout(values, "CUBE_AI_TRANSCRIPTION_TIMEOUT", 1.0)
        process_timeout = _positive_timeout(values, "CUBE_AI_PROCESS_TIMEOUT", 10.0)
        url = values.get("CUBE_AI_NODE_URL", "").strip().rstrip("/")
        if not url:
            return cls(transcription_timeout=timeout, process_timeout=process_timeout)
        try:
            parsed = urlsplit(url)
            if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                    or parsed.username is not None or parsed.password is not None
                    or parsed.query or parsed.fragment or any(c.isspace() for c in url)
                    or parsed.port == 0):
                raise ValueError()
        except ValueError:
            logging.getLogger(__name__).warning("Invalid CUBE_AI_NODE_URL; AI client disabled")
            return cls(transcription_timeout=timeout, process_timeout=process_timeout)
        return cls(url=url, transcription_timeout=timeout, process_timeout=process_timeout)
