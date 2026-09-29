"""Backend-owned transcription selection; feature execution stays in the pipeline."""
from dataclasses import dataclass
import logging
from pathlib import Path
from time import perf_counter
from typing import Literal


from .ai_client import AIClient, AINodeError
from .cancellation import check, worker


logger = logging.getLogger("uvicorn.error.transcription")


@dataclass(frozen=True)
class SelectedTranscription:
    transcription: dict
    source: Literal["remote_ai", "local_cpu"]
    timings: dict[str, float]


async def select_transcription(path, *, local_transcribe, ai_client: AIClient | None = None):
    check()
    started = perf_counter()
    remote_elapsed = local_elapsed = 0.0
    transcription = None
    source = "local_cpu"

    if ai_client is not None and ai_client.config.url is not None:
        remote_started = perf_counter()
        try:
            audio = await worker(Path(path).read_bytes)
            # Await async HTTP directly: timeout/connection cleanup finishes before
            # local inference starts. CancelledError deliberately propagates.
            result = await ai_client.transcribe_remote(audio)
            transcription = result.model_dump()
            source = "remote_ai"
        except (AINodeError, OSError) as error:
            reason = error.reason if isinstance(error, AINodeError) else "audio_read"
            status = error.status_code if isinstance(error, AINodeError) else None
            logger.warning(
                "Remote transcription failed; falling back to local_cpu "
                "reason=%s status=%s remote_attempt=%.3fs",
                reason, status, perf_counter() - remote_started,
            )
        finally:
            remote_elapsed = perf_counter() - remote_started

    if transcription is None:
        local_started = perf_counter()
        check()
        transcription = await worker(local_transcribe, path)
        local_elapsed = perf_counter() - local_started

    check()
    timings = {
        "remote_attempt": round(remote_elapsed, 3),
        "local_attempt": round(local_elapsed, 3),
        "total": round(perf_counter() - started, 3),
    }
    logger.info(
        "Transcription source=%s remote_attempt=%.3fs local_attempt=%.3fs total=%.3fs",
        source, timings["remote_attempt"], timings["local_attempt"], timings["total"],
    )
    return SelectedTranscription(transcription, source, timings)
