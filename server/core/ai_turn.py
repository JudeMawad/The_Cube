"""Hybrid voice interpretation and cancellation-safe backend tool execution."""
import asyncio
import logging
from time import perf_counter

from .ai_client import AINodeError
from .cancellation import check
from .schemas import ConversationResult, ProcessRequest, ToolIntent
from .tool_execution import execute_tool
from .tool_registry import ToolContext, ToolRegistry, ToolValidationError
from .local_commands import music_decision
from .command_safety import _non_action_request, _status_question


logger = logging.getLogger("uvicorn.error.processing")
_INFORMATION_TOOLS = frozenset({"media.movie_status", "media.check_availability", "plugs.get_state", "cube.get_status", "cube.get_volume"})

async def process_turn(transcription, client_id, *, voice_started, ai_client,
                       client_states, legacy, tool_registry=None, media_context=None, tool_context=None,
                       local_command=None, local_intent=None, continuation=None):
    check()
    registry = tool_registry if tool_registry is not None else ToolRegistry()
    started = perf_counter()
    interpretation = 0.0
    execution = 0.0
    response_generation = 0.0
    turn_wait = 0.0
    context_time = 0.0
    request_build = 0.0
    text = transcription["text"]

    def finish(result, source):
        check()
        result.update(
            processing_source=source,
            processing_timings={
                "turn_wait": round(turn_wait, 3),
                "context": round(context_time, 3),
                "request_build": round(request_build, 3),
                "interpretation": round(interpretation, 3),
                "execution": round(execution, 3),
                "response_generation": round(response_generation, 3),
                "total": round(perf_counter() - started, 3),
            },
        )
        logger.info(
            "Processing source=%s "
            "turn_wait=%.3fs context=%.3fs request_build=%.3fs "
            "interpretation=%.3fs execution=%.3fs "
            "response_generation=%.3fs total=%.3fs",
            source,
            turn_wait,
            context_time,
            request_build,
            interpretation,
            execution,
            response_generation,
            result["processing_timings"]["total"],
        )
        return result

    def ai_response(response, *, success, kind, listen):
        return {
            "transcript": text, "type": kind, "success": success, "response": response,
            "listen_for_seconds": listen,
            "processing_time": transcription["processing_time"],
            "timings": {
                "transcription": transcription["processing_time"],
                "command": round(execution, 3),
                "voice_total": round(perf_counter() - voice_started, 3),
            },
        }

    async def run_tool(tool):
        nonlocal execution
        check()
        execution_started = perf_counter()
        try:
            outcome = await execute_tool(tool, tool_context or ToolContext(client_id=client_id))
        finally:
            execution = perf_counter() - execution_started
        command = outcome.data.get("reply", {})
        if not isinstance(command, dict):
            command = {}
        fallback = command.get("response")
        if not isinstance(fallback, str) or not fallback.strip():
            fallback = "Done." if outcome.success else "I couldn't complete that request."
        if len(fallback) > 500:
            fallback = "I couldn't summarize that result. Please check its status."
        result = ai_response(fallback, success=outcome.success, kind="command", listen=0)
        for key in ("action", "value", "media_id", "title", "year", "follow_up",
                    "expires_in", "candidates", "listen_for_seconds", "context_expires_in"):
            if key in command:
                result[key] = command[key]
        result["timings"].update(command.get("timings", {}))
        result["processing_response_source"] = "backend"
        result["timings"]["voice_total"] = round(perf_counter() - voice_started, 3)
        direction = None
        if outcome.success and tool.name == "cube.adjust_volume":
            direction = 1 if tool.arguments.delta > 0 else -1
        return result, direction

    async def context_snapshot():
        if media_context is None:
            return None
        try:
            # Read-only snapshot; feature state writes are inside execute_tool.
            return await asyncio.to_thread(media_context, client_id)
        except Exception:
            logger.warning("Backend media context unavailable")
            return None

    if not text.strip():
        # Preserve the existing silence path; no AI request or conversation entry.
        return finish(await legacy(), "legacy")

    turn_wait_started = perf_counter()

    async with client_states.turn(client_id) as turn:
        check()
        turn_wait = perf_counter() - turn_wait_started
        non_action = _non_action_request(text)
        status_question = _status_question(text)
        direction = None
        music = music_decision(text)
        if music.state in {"blocked", "clarification"}:
            result = ai_response(music.reply, success=False, kind="command", listen=10)
            result["processing_response_source"] = "backend"
            turn.complete(text, result["response"])
            return finish(result, "local_clarification")
        candidate = None
        if music.state == "executable":
            candidate = ToolIntent(type="tool", tool="music." + music.operation, arguments=music.arguments)
        elif local_intent is not None:
            candidate = local_intent(text, turn.volume_direction)
            # Exact catalog status questions may mention lights, which the
            # broad discussion guard otherwise withholds from all tools.
            if (non_action or status_question) and not (
                    isinstance(candidate, ToolIntent) and candidate.tool in _INFORMATION_TOOLS):
                candidate = None
        if isinstance(candidate, str):
            result = ai_response(candidate, success=True, kind="conversation", listen=10)
            result["processing_response_source"] = "backend"
            turn.complete(text, result["response"])
            return finish(result, "local_clarification")
        if candidate is not None:
            try:
                tool = registry.validate_intent(candidate)
            except ToolValidationError:
                result = ai_response("I couldn't complete that request.", success=False,
                                     kind="command", listen=0)
                result["processing_response_source"] = "backend"
            else:
                result, direction = await run_tool(tool)
            turn.complete(text, result.get("response"), volume_direction=direction)
            return finish(result, "local_fast")
        if not non_action and local_command is not None and local_command(text):
            # Use the existing media-first legacy route and executor exactly once.
            result = await legacy()
            turn.complete(text, result.get("response"))
            return finish(result, "local_fast")

        interpreted = None
        if ai_client is not None and ai_client.config.url is not None:
            context_started = perf_counter()
            context = await context_snapshot()
            context_time = perf_counter() - context_started

            request_build_started = perf_counter()
            advertised_tools = registry.list_executable_tools()
            if non_action:
                advertised_tools = []
            elif status_question:
                advertised_tools = [tool for tool in advertised_tools if tool.name in _INFORMATION_TOOLS]

            request = ProcessRequest(
                transcript=text,
                client_id=client_id,
                history=turn.history,
                tools=advertised_tools,
                context=context,
            )
            request_build = perf_counter() - request_build_started
            attempt_started = perf_counter()
            try:
                # Async HTTP and cancellation cleanup are owned by AIClient.
                check()
                interpreted = await ai_client.process_remote(request)
            except AINodeError as error:
                logger.warning("AI interpretation unavailable reason=%s status=%s",
                               error.reason, error.status_code)
            finally:
                interpretation = perf_counter() - attempt_started

        check()
        if interpreted is None:
            if non_action:
                result = ai_response(None, success=True, kind="unhandled", listen=0)
                if continuation is not None:
                    result.update(await asyncio.to_thread(continuation, client_id))
                source = "safety"
            else:
                result = await legacy()
                source = "legacy"
        elif isinstance(interpreted, ConversationResult):
            result = ai_response(interpreted.response, success=True, kind="conversation", listen=10)
            source = "ai_conversation"
        else:
            # A remote node must not be able to execute an unadvertised tool.
            # Validation failures never reach the legacy parser or retry AI.
            try:
                if non_action:
                    raise ToolValidationError("non_action_request")
                if interpreted.tool not in {item.name for item in advertised_tools}:
                    raise ToolValidationError("unadvertised_tool")
                tool = registry.validate_intent(interpreted)
            except ToolValidationError as error:
                logger.warning("AI tool intent declined reason=%s", error.reason)
                result = ai_response("I couldn't complete that request.", success=False,
                                     kind="command", listen=0)
            else:
                result, direction = await run_tool(tool)
            source = "ai_tool"

        turn.complete(text, result.get("response"), volume_direction=direction)
        return finish(result, source)
