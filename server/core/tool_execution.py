"""Run one synchronous backend operation without abandoning it on cancellation."""
import asyncio
import logging
import threading

from .schemas import ToolResult
from .cancellation import check
from .tool_registry import ToolContext, ValidatedTool


logger = logging.getLogger("uvicorn.error.processing")


async def guarded_operation(operation):
    """Finish started side effects; cancelled queued operations never start."""
    check()
    if asyncio.current_task().cancelling():
        raise asyncio.CancelledError
    gate = threading.Lock()
    cancelled = False

    def run():
        with gate:
            check()
            if cancelled:
                return None
        return operation()

    worker = asyncio.create_task(asyncio.to_thread(run))
    while True:
        try:
            result = await asyncio.shield(worker)
            break
        except asyncio.CancelledError:
            with gate:
                cancelled = True
            # Repeated cancellation still waits for this same worker. Keep a
            # strong reference and consume its outcome before releasing the lock.
            if worker.done():
                result = worker.result()
                break
    if cancelled:
        raise asyncio.CancelledError
    return result


async def execute_tool(tool: ValidatedTool, context: ToolContext) -> ToolResult:
    def operation():
        try:
            result = tool.handler(tool.arguments, context)
            if not isinstance(result, ToolResult) or result.tool != tool.name:
                raise ValueError("Invalid tool result")
            return ToolResult.model_validate(result.model_dump(warnings=False), strict=True)
        except (Exception, SystemExit, asyncio.CancelledError):
            logger.warning("Backend tool operation failed")
            return ToolResult(tool=tool.name, success=False, error="execution_failed")
    return await guarded_operation(operation)
