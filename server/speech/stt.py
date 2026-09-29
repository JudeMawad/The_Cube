"""Local fallback transcription, independent of model construction and HTTP."""
import time
from core.cancellation import inference_lock, segment_text


def transcribe_audio(path: str, *, model, transcription_lock) -> dict:
    started = time.perf_counter()

    with inference_lock(transcription_lock):
        segments, info = model.transcribe(
            path,
            beam_size=5,
            vad_filter=True,
        )

        text = segment_text(segments)

    return {
        "text": text,
        "language": info.language,
        "processing_time": round(
            time.perf_counter() - started,
            3,
        ),
    }
