"""One background-loaded model, with serialized inference and independent readiness."""
import logging
import threading
import time

from .config import STTConfig
from .cancellation import inference_lock, segment_text
from .schemas import TranscriptionResult

logger = logging.getLogger(__name__)


def load_model(model, *, device, compute_type, download_root=STTConfig().cache_dir):
    # Imported only in the startup worker. Importing the app never downloads models.
    from faster_whisper import WhisperModel

    return WhisperModel(model, device=device, compute_type=compute_type, download_root=download_root)


class STTUnavailable(RuntimeError):
    pass


class SpeechToText:
    def __init__(self, config: STTConfig, *, model_factory=load_model):
        self.config = config
        self._factory = model_factory
        self._model = None
        self._ready = threading.Event()
        self._load_lock = threading.Lock()
        self._inference_lock = threading.Lock()
        self._attempted = False

    @property
    def ready(self):
        return self._ready.is_set()

    def load(self):
        with self._load_lock:
            if self._attempted:
                return
            self._attempted = True
            try:
                # 'auto' could select CPU when CUDA is unavailable. Require intent.
                if self.config.device not in {"cuda", "cpu"}:
                    raise ValueError("CUBE_AI_WHISPER_DEVICE must be cuda or cpu")
                self._model = self._factory(
                    self.config.model, device=self.config.device,
                    compute_type=self.config.compute_type,
                    download_root=self.config.cache_dir,
                )
                if self._model is None:
                    raise RuntimeError("Model factory returned no model")
            except Exception:
                logger.exception("AI-node transcription model loading failed; restart to retry")
                return
            self._ready.set()
            logger.info("AI-node transcription model loaded (inference has not been warmed up)")

    def transcribe(self, path: str) -> TranscriptionResult:
        started = time.perf_counter()
        if not self.ready:
            raise STTUnavailable("Transcription is unavailable.")
        with inference_lock(self._inference_lock):
            segments, info = self._model.transcribe(path, beam_size=5, vad_filter=True)
            text = segment_text(segments)
        return TranscriptionResult(
            text=text, language=info.language,
            processing_time=round(time.perf_counter() - started, 3),
        )
