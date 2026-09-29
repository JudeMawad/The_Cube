from __future__ import annotations

import asyncio
import re
from collections import OrderedDict
from contextlib import asynccontextmanager
from threading import Lock
import io
import os
import tempfile
import time
import traceback
from pathlib import Path

import httpx
from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile
from faster_whisper import WhisperModel
from pydantic import BaseModel

from core.client_identity import client_identity
from core.cancellation import disconnect_aware, check, worker, headers as cancellation_headers, inference_lock
from core.client_state import ClientStateManager
from core.ai_turn import process_turn
from core.ai_client import AIClient
from core.config import AINodeConfig, BackendConfig, KokoroConfig
from core.transcription import select_transcription
from core.tool_registry import ToolRegistry, ToolContext
from core.http_auth import authenticate as authenticate_control
from features.cube import ControlChannel, CubeCommands, control_router
from features.music.service import MusicService
from features.music.routes import music_router
from features.music.commands import MusicCommands
from features.music.tools import register_tools as register_music_tools
from features.cube.protocol import Session
from features.cube.tools import register_tools as register_cube_tools
from core.local_commands import match as match_local_command
from core.voice_pipeline import process_transcription, route_command
from features.lights import (
    LightsCommands,
    COLORS,
    normalize,
    control_lights,
    handle_command as handle_light_command,
)
from features.lights.commands import is_direct_command
from features.media import MovieCommands, EventStore, event_router, close_commands
from features.lights.tools import register_tools as register_light_tools
from features.media.tools import MediaTools
from features.plugs import PlugCommands
from features.plugs.tools import register_tools as register_plug_tools
from speech.stt import transcribe_audio as local_transcribe


@asynccontextmanager
async def lifespan(app):
    try:
        yield
    finally:
        try:
            control_channel.close()
            music_service.close()
            await remote_tts_client.aclose()
            close_commands(movie_commands)
        finally:
            plug_commands.close()


app = FastAPI(lifespan=lifespan)

client_states = ClientStateManager()
tool_registry = ToolRegistry()

ai_config = AINodeConfig.from_env()
ai_client = AIClient(ai_config) if ai_config.url is not None else None

# Shared pool; cancelling an individual request closes its exchange, not peers.
remote_tts_client = httpx.AsyncClient(timeout=httpx.Timeout(2.5, connect=.5))

transcription_lock = Lock()
tts_lock = Lock()
tts_cache = OrderedDict()

event_store = EventStore()
movie_commands = MovieCommands(store=event_store)
media_tools = MediaTools(movie_commands)
plug_commands = PlugCommands()
light_commands = LightsCommands(control_govee=lambda *args: control_lights(*args), plugs=plug_commands)

media_tools.register(tool_registry)

register_light_tools(
    tool_registry,
    control_lights=lambda *args: control_lights(*args),
    power_commands=light_commands,
    continuation=lambda client_id: movie_commands.continuation(client_id),
)

register_plug_tools(
    tool_registry,
    commands=plug_commands,
    include_power=False,
    continuation=lambda client_id: movie_commands.continuation(client_id),
)

app.include_router(event_router(event_store))
control_channel = ControlChannel()
cube_commands = CubeCommands(control_channel)
register_cube_tools(tool_registry, commands=cube_commands)
app.include_router(control_router(control_channel))
music_service = MusicService()
app.include_router(music_router(music_service))
register_music_tools(tool_registry, commands=MusicCommands(music_service, check=check))

SERVER_DIR = Path(__file__).resolve().parent


class TTSRequest(BaseModel):
    text: str


tts: KokoroTTS | None = None

print("Loading Kokoro...", flush=True)

try:
    import soundfile as sf

    from tts.kokoro_tts import KokoroTTS

    kokoro_config = KokoroConfig.from_env()
    tts = KokoroTTS(
        model_path=kokoro_config.model_path,
        voices_path=kokoro_config.voices_path,
        voice="bm_lewis",
        speed=1.3,
        lang="en-gb",
    )

except Exception as error:
    print(
        f"Kokoro TTS unavailable: {error}",
        flush=True,
    )
    traceback.print_exc()

else:
    print(
        "Kokoro TTS ready.",
        flush=True,
    )


print("Loading Whisper...", flush=True)

backend_config = BackendConfig.from_env()
model = WhisperModel(
    backend_config.whisper_model,
    device="cpu",
    compute_type="int8",
    download_root=backend_config.whisper_cache_dir,
)

print("Whisper ready.", flush=True)


def transcribe_audio(path: str) -> dict:
    # Resolve globals at call time to retain existing integrations/test patch points.
    return local_transcribe(
        path,
        model=model,
        transcription_lock=transcription_lock,
    )


def handle_command(text: str, client_id: str = "cube"):
    return route_command(
        text,
        client_id,
        primary=movie_commands.handle,
        fallback=lambda text: handle_light_command(
            text,
            control_lights=control_lights,
            power_commands=light_commands,
        ),
        continuation=movie_commands.continuation,
    )


async def save_audio(file: UploadFile) -> str:
    with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp:
        try:
            # Retain the upload while its disk-backed read is running, even if
            # the client disconnects before inference starts.
            data = await worker(file.file.read)
            check()
            tmp.write(data)
            return tmp.name
        except BaseException:
            os.unlink(tmp.name)
            raise


@app.get("/health")
def health():
    return {
        "status": "ok",
        "whisper": "ready",
        "govee": "ready",
        "tts": "ready" if tts is not None else "unavailable",
    }


# Keep this endpoint for raw STT testing.
@app.post("/transcribe")
@disconnect_aware
async def transcribe(request: Request, file: UploadFile = File(...)):
    path = await save_audio(file)

    try:
        return await worker(transcribe_audio, path)

    finally:
        os.unlink(path)


def local_tts_wav(text):
    # The worker owns this lock until native inference and encoding finish,
    # including when its request has disconnected.
    with inference_lock(tts_lock):
        check()
        samples, sample_rate = tts.synthesize(text)
        check()
        audio = io.BytesIO()
        sf.write(audio, samples, sample_rate, format="WAV")
        check()
        return audio.getvalue()


@app.post("/tts")
@disconnect_aware
async def synthesize_speech(request: TTSRequest, http_request: Request):
    text = request.text.strip()
    if not text:
        raise HTTPException(400, "Text is empty.")
    if len(text) > 500:
        raise HTTPException(400, "Text is too long.")
    started = time.perf_counter()
    key = ("bm_lewis", 1.3, "en-gb", text)
    remote_attempt = local_attempt = 0.0
    try:
        # Async admission retains the original one-at-a-time cache/provider
        # behavior without parking a thread or blocking disconnect observation.
        # Lazily bind to this service's event loop (also supports TestClient).
        loop = asyncio.get_running_loop()
        if getattr(app.state, "tts_loop", None) is not loop:
            app.state.tts_loop = loop
            app.state.tts_admission = asyncio.Lock()
        async with app.state.tts_admission:
            check()
            wav = tts_cache.get(key)
            source = "cache"
            if wav is None:
                if ai_config.url is not None:
                    remote_started = time.perf_counter()
                    try:
                        check()
                        async with asyncio.timeout(2.5):
                            response = await remote_tts_client.post(
                                f"{ai_config.url.rstrip('/')}/tts", json={"text": text},
                                headers=cancellation_headers())
                            response.raise_for_status()
                            if not response.content:
                                raise ValueError("Empty remote audio")
                            wav = response.content
                            source = "remote_gpu"
                    except (httpx.HTTPError, TimeoutError, ValueError):
                        check()
                    finally:
                        remote_attempt = time.perf_counter() - remote_started
                if wav is None:
                    check()
                    if tts is None:
                        raise HTTPException(503, "TTS is unavailable.")
                    local_started = time.perf_counter()
                    wav = await worker(local_tts_wav, text)
                    local_attempt = time.perf_counter() - local_started
                    source = "local_fallback"
                check()
                tts_cache[key] = wav
                if len(tts_cache) > 64:
                    tts_cache.popitem(last=False)
            check()
            tts_cache.move_to_end(key)
        elapsed = time.perf_counter() - started
        print(f"TTS source={source} remote_attempt={remote_attempt:.3f}s "
              f"local_attempt={local_attempt:.3f}s total={elapsed:.3f}s", flush=True)
        return Response(content=wav, media_type="audio/wav", headers={
            "X-TTS-Time": f"{elapsed:.3f}", "X-TTS-Voice": "bm_lewis", "X-TTS-Source": source})
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(500, "TTS synthesis failed.") from None


# Main Cube endpoint.
@app.post("/voice")
@disconnect_aware
async def voice(
    request: Request,
    file: UploadFile = File(...),
):
    voice_started = time.perf_counter()
    context = None
    session = request.headers.get("X-Cube-Control-Session")
    request_id = request.headers.get("X-Cube-Request-ID")
    if session is not None or request_id is not None:
        authenticate_control(request)
        try:
            Session(session_id=session)
            Session(session_id=request_id)
        except ValueError:
            raise HTTPException(400, "Invalid Cube control context") from None
        context = ToolContext(client_id=client_identity(request), control_session=session, request_id=request_id)
    path = await save_audio(file)

    try:
        selected = await select_transcription(
            path,
            local_transcribe=transcribe_audio,
            ai_client=ai_client,
        )

    finally:
        os.unlink(path)

    check()
    if request.headers.get("X-Cube-Wake-Preroll") == "1":
        # Only wake-triggered recordings include detector pre-roll. Preserve
        # normal/follow-up transcripts, including discussion of the wake word.
        selected.transcription["text"] = re.sub(
            r"^\s*(?:hey[\s,]+)?cube\b[\s,.!?:;—-]*", "",
            selected.transcription["text"], count=1, flags=re.IGNORECASE)
    client_id = client_identity(request)

    async def legacy():
        return await process_transcription(
            selected.transcription,
            client_id,
            voice_started=voice_started,
            handle_command=handle_command,
            continuation=movie_commands.continuation,
        )

    result = await process_turn(
        selected.transcription,
        client_id,
        voice_started=voice_started,
        ai_client=ai_client,
        client_states=client_states,
        legacy=legacy,
        tool_registry=tool_registry,
        media_context=media_tools.context,
        tool_context=context,
        local_command=is_direct_command,
        local_intent=match_local_command,
        continuation=movie_commands.continuation,
    )

    result.update(
        transcription_source=selected.source,
        transcription_timings=selected.timings,
    )

    return result
