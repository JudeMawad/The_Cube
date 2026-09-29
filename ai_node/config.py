"""Independent AI-node settings; no backend configuration imports."""
from dataclasses import dataclass
import logging
import math
import os
from pathlib import Path
from urllib.parse import urlsplit


SERVICE_DIR = Path(__file__).resolve().parent
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
            asset_path(values, "CUBE_AI_KOKORO_MODEL_PATH", "assets/models/kokoro/kokoro-v1.0.onnx"),
            asset_path(values, "CUBE_AI_KOKORO_VOICES_PATH", "assets/models/kokoro/voices-v1.0.bin"),
        )


DEFAULT_LLM_TIMEOUT = 20.0


@dataclass(frozen=True)
class STTConfig:
    model: str = "base.en"
    device: str = "cuda"
    compute_type: str = "float16"
    cache_dir: str = DEFAULT_WHISPER_CACHE

    @classmethod
    def from_env(cls, environ=None):
        values = os.environ if environ is None else environ
        return cls(
            cache_dir=str(asset_path(values, "CUBE_AI_WHISPER_CACHE_DIR", DEFAULT_WHISPER_CACHE)),
            **{
                field: values.get(key, "").strip() or default
                for field, key, default in (
                    ("model", "CUBE_AI_WHISPER_MODEL", "base.en"),
                    ("device", "CUBE_AI_WHISPER_DEVICE", "cuda"),
                    ("compute_type", "CUBE_AI_WHISPER_COMPUTE_TYPE", "float16"),
                )
            },
        )


@dataclass(frozen=True)
class LLMConfig:
    base_url: str = ""
    model: str = ""
    timeout: float = DEFAULT_LLM_TIMEOUT

    def __post_init__(self):
        object.__setattr__(self, "base_url", self.base_url.strip().rstrip("/"))
        object.__setattr__(self, "model", self.model.strip())
        try:
            timeout = float(self.timeout)
            if not math.isfinite(timeout) or timeout <= 0:
                raise ValueError()
        except (TypeError, ValueError, OverflowError):
            timeout = DEFAULT_LLM_TIMEOUT
            logging.getLogger(__name__).warning(
                "Invalid CUBE_AI_LLM_TIMEOUT; using %.1f seconds", timeout,
            )
        object.__setattr__(self, "timeout", timeout)

    @property
    def configured(self):
        try:
            url = urlsplit(self.base_url)
            return bool(
                self.model and url.scheme in {"http", "https"} and url.hostname
                and (url.port is None or url.port > 0)
                and url.username is None and url.password is None
                and "?" not in self.base_url and "#" not in self.base_url
                and "\\" not in self.base_url
                and not any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in self.base_url)
            )
        except ValueError:
            return False

    @classmethod
    def from_env(cls, environ=None):
        values = os.environ if environ is None else environ
        return cls(values.get("CUBE_AI_LLM_BASE_URL", ""), values.get("CUBE_AI_LLM_MODEL", ""),
                   values.get("CUBE_AI_LLM_TIMEOUT", DEFAULT_LLM_TIMEOUT))
