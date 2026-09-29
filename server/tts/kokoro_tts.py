from pathlib import Path
from core.cancellation import check

import numpy as np
from kokoro_onnx import Kokoro
from numpy.typing import NDArray


class KokoroTTS:
    def __init__(
        self,
        model_path: str | Path,
        voices_path: str | Path,
        voice: str = "bm_lewis",
        speed: float = 0.95,
        lang: str = "en-gb",
    ) -> None:
        self.voice = voice
        self.speed = speed
        self.lang = lang
        self.kokoro = Kokoro(
            str(model_path),
            str(voices_path),
        )

    def synthesize(self, text: str) -> tuple[NDArray[np.float32], int]:
        check()
        result = self.kokoro.create(
            text,
            voice=self.voice,
            speed=self.speed,
            lang=self.lang,
        )

        check()
        return result
