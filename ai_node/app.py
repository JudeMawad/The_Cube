"""Independent inference service with request-owned cancellation."""
import asyncio
from contextlib import asynccontextmanager
import logging
from pathlib import Path
import shutil
import tempfile
import time
from uuid import uuid4

from fastapi import FastAPI, File, HTTPException, Response, UploadFile, Request
from pydantic import BaseModel

from .cancellation import disconnect_aware, finish_worker, check
from .config import KokoroConfig, LLMConfig, STTConfig
from .process import ProcessError, Processor
from .schemas import Capabilities, HealthResult, ProcessRequest, ProcessResult, TranscriptionResult
from .stt import SpeechToText, STTUnavailable, load_model
from .tts import SpeechSynthesizer, TTSUnavailable

logger = logging.getLogger(__name__)


class EmptyUpload(ValueError):
    pass


class TTSRequest(BaseModel):
    text: str


def transcribe_upload(stt, source):
    check()
    with tempfile.TemporaryDirectory(prefix="cube-ai-stt-") as directory:
        path = Path(directory) / (uuid4().hex + ".audio")
        with path.open("wb") as destination:
            shutil.copyfileobj(source, destination, length=1024 * 1024)
        if path.stat().st_size == 0:
            raise EmptyUpload()
        return stt.transcribe(str(path))


def create_app(config=None, *, model_factory=load_model, llm_config=None, llm_transport=None):
    stt = SpeechToText(
        config if config is not None else STTConfig.from_env(),
        model_factory=model_factory,
    )

    processor = Processor(
        llm_config if llm_config is not None else LLMConfig.from_env(),
        transport=llm_transport,
    )

    kokoro_config = KokoroConfig.from_env()

    tts = SpeechSynthesizer(
        model_path=kokoro_config.model_path,
        voices_path=kokoro_config.voices_path,
        voice="bm_lewis",
        speed=1.3,
        lang="en-gb",
    )

    @asynccontextmanager
    async def lifespan(app):
        if app.state.loader_task is None:
            app.state.loader_task = asyncio.create_task(
                asyncio.to_thread(stt.load)
            )

        if app.state.tts_loader_task is None:
            app.state.tts_loader_task = asyncio.create_task(
                asyncio.to_thread(tts.load)
            )

        try:
            yield
        finally:
            await finish_worker(app.state.loader_task)
            await finish_worker(app.state.tts_loader_task)

    app = FastAPI(title="Cube AI node", lifespan=lifespan)

    app.state.stt = stt
    app.state.loader_task = None
    app.state.tts = tts
    app.state.tts_loader_task = None

    @app.get("/health", response_model=HealthResult)
    async def health():
        return HealthResult(
            status="ok",
            capabilities=Capabilities(
                transcribe=stt.ready,
                process=processor.config.configured,
            ),
        )

    @app.post("/transcribe", response_model=TranscriptionResult)
    @disconnect_aware
    async def transcribe(http_request: Request, file: UploadFile = File(...)):
        if not stt.ready:
            raise HTTPException(
                status_code=503,
                detail="Transcription is unavailable.",
            )

        try:
            return await finish_worker(
                asyncio.create_task(
                    asyncio.to_thread(
                        transcribe_upload,
                        stt,
                        file.file,
                    )
                )
            )
        except EmptyUpload:
            raise HTTPException(
                status_code=400,
                detail="Audio upload is empty.",
            ) from None
        except STTUnavailable:
            raise HTTPException(
                status_code=503,
                detail="Transcription is unavailable.",
            ) from None
        except Exception:
            logger.exception("AI-node transcription failed")
            raise HTTPException(
                status_code=500,
                detail="Transcription failed.",
            ) from None

    @app.post("/tts")
    @disconnect_aware
    async def synthesize_speech(request: TTSRequest, http_request: Request):
        text = request.text.strip()

        if not text:
            raise HTTPException(
                status_code=400,
                detail="Text is empty.",
            )

        if len(text) > 500:
            raise HTTPException(
                status_code=400,
                detail="Text is too long.",
            )

        if not tts.ready:
            raise HTTPException(
                status_code=503,
                detail="TTS is unavailable.",
            )

        started = time.perf_counter()

        try:
            wav = await finish_worker(
                asyncio.create_task(
                    asyncio.to_thread(
                        tts.synthesize_wav,
                        text,
                    )
                )
            )
        except TTSUnavailable:
            raise HTTPException(
                status_code=503,
                detail="TTS is unavailable.",
            ) from None
        except Exception:
            logger.exception("AI-node TTS failed")
            raise HTTPException(
                status_code=500,
                detail="TTS synthesis failed.",
            ) from None

        elapsed = time.perf_counter() - started

        logger.info(
            'AI-node TTS %.3fs: "%s"',
            elapsed,
            text,
        )

        return Response(
            content=wav,
            media_type="audio/wav",
            headers={
                "X-TTS-Time": f"{elapsed:.3f}",
                "X-TTS-Voice": tts.voice,
                "X-TTS-Source": "gpu",
            },
        )

    @app.post("/process", response_model=ProcessResult)
    @disconnect_aware
    async def process(request: ProcessRequest, http_request: Request):
        try:
            return await processor.process(request)
        except ProcessError as error:
            logger.warning(
                "AI-node processing failed status=%s reason=%s",
                error.status_code,
                error.reason,
            )
            raise HTTPException(
                status_code=error.status_code,
                detail=error.detail,
            ) from None

    return app


app = create_app()