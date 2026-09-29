"""Local-only voice orchestration. No AI probes or feature implementation imports."""
import time
import logging
import os

logger = logging.getLogger(__name__)

from .tool_execution import guarded_operation
from .cancellation import check


def route_command(text, client_id, *, primary, fallback, continuation=None):
    """Keep primary dialogue precedence; attach its context to fallback replies."""
    result = primary(text, client_id)
    if result is not None:
        return result
    result = fallback(text)
    if result is not None and continuation is not None:
        result.update(continuation(client_id))
    return result


async def process_transcription(transcription, client_id, *, voice_started,
                                handle_command, continuation):
    text = transcription["text"]

    if os.environ.get("CUBE_LOG_TRANSCRIPTS") == "1":
        logger.info("Transcript: %s", text)

    command_started = time.perf_counter()
    try:
        # Keep the event loop available while Overseerr processes the request.
        check()
        command = await guarded_operation(lambda: handle_command(text, client_id))

    except Exception:
        logger.warning("Local command failed")

        return {
            "transcript": text,
            "type": "command",
            "success": False,
            "response": "I couldn't complete that command. Please try again.",
            "processing_time": transcription["processing_time"],
        }

    timings = {
        "transcription": transcription["processing_time"],
        "command": round(time.perf_counter() - command_started, 3),
        "voice_total": round(time.perf_counter() - voice_started, 3),
        **(command.get("timings", {}) if command else {}),
    }
    if command:
        logger.info("Action: %s", command["action"])

        return {
            "transcript": text,
            "type": "command",
            "success": True,
            **command,
            "timings": timings,
            "processing_time": transcription["processing_time"],
        }

    return {
        "transcript": text,
        "type": "unhandled",
        "success": True,
        "response": None,
        "timings": timings,
        **(continuation(client_id) if text.strip() else {"listen_for_seconds": 0}),
        "processing_time": transcription["processing_time"],
    }
