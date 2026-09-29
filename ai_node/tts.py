"""GPU-backed Kokoro TTS for the independent AI node."""
from __future__ import annotations

from io import BytesIO
import os
from pathlib import Path
from threading import Lock
from .cancellation import check, inference_lock

import onnxruntime as ort
import soundfile as sf


class TTSUnavailable(RuntimeError):
    pass


class SpeechSynthesizer:
    def __init__(
        self,
        model_path: str | Path,
        voices_path: str | Path,
        *,
        voice: str = "bm_lewis",
        speed: float = 1.3,
        lang: str = "en-gb",
    ) -> None:
        self.model_path = Path(model_path)
        self.voices_path = Path(voices_path)
        self.voice = voice
        self.speed = speed
        self.lang = lang

        self._kokoro = None
        self._lock = Lock()

    @property
    def ready(self) -> bool:
        return self._kokoro is not None

    def load(self) -> None:
        if self._kokoro is not None:
            return

        if not self.model_path.is_file():
            raise TTSUnavailable(
                f"Kokoro model not found: {self.model_path}"
            )

        if not self.voices_path.is_file():
            raise TTSUnavailable(
                f"Kokoro voices file not found: {self.voices_path}"
            )

        # Load CUDA/cuDNN libraries from the Python NVIDIA packages.
        ort.preload_dlls(directory="")

        providers = ort.get_available_providers()

        if "CUDAExecutionProvider" not in providers:
            raise TTSUnavailable(
                "CUDAExecutionProvider is unavailable. "
                f"Available providers: {providers}"
            )

        # Force kokoro-onnx onto the RTX.
        os.environ["ONNX_PROVIDER"] = "CUDAExecutionProvider"

        # Import only after the CUDA runtime has been prepared.
        from kokoro_onnx import Kokoro

        with self._lock:
            if self._kokoro is not None:
                return

            kokoro = Kokoro(
                str(self.model_path),
                str(self.voices_path),
            )

            # Run one throwaway inference during startup so CUDA kernels,
            # execution plans, and model state are warm before the Cube's
            # first real TTS request.
            kokoro.create(
                "Ready.",
                voice=self.voice,
                speed=self.speed,
                lang=self.lang,
            )

            # Only report ready after warm-up completed successfully.
            self._kokoro = kokoro

    def synthesize_wav(self, text: str) -> bytes:
        text = text.strip()

        if not text:
            raise ValueError("Text is empty.")

        with inference_lock(self._lock):
            if self._kokoro is None:
                raise TTSUnavailable("Kokoro is not loaded.")

            samples, sample_rate = self._kokoro.create(
                text,
                voice=self.voice,
                speed=self.speed,
                lang=self.lang,
            )

            check()
            audio = BytesIO()

            sf.write(
                audio,
                samples,
                sample_rate,
                format="WAV",
            )

            check()
            return audio.getvalue()