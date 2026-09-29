"""Request-owned cancellation. Kept service-local for independent deployment."""
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
import inspect
import logging
import re
from threading import Event
from uuid import uuid4

from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger("uvicorn.error.cancellation")


@dataclass
class Scope:
    identifier: str
    cancelled: Event = field(default_factory=Event)


current_scope = ContextVar("cube_request_scope", default=None)


def check():
    scope = current_scope.get()
    if scope is not None and scope.cancelled.is_set():
        raise asyncio.CancelledError


def headers():
    scope = current_scope.get()
    return {"X-Cube-Interaction-ID": scope.identifier} if scope is not None else {}


@contextmanager
def inference_lock(lock):
    """Skip cancelled waiters without entering another native inference."""
    if current_scope.get() is None:
        with lock:
            yield
        return
    check()
    while not lock.acquire(timeout=.05):
        check()
    try:
        check()
        yield
    finally:
        lock.release()


def segment_text(segments):
    parts = []
    try:
        iterator = iter(segments)
        while True:
            check()
            try:
                segment = next(iterator)
            except StopIteration:
                break
            check()
            parts.append(segment.text.strip())
        return " ".join(parts).strip()
    finally:
        close = getattr(segments, "close", None)
        if close is not None:
            close()


async def finish_worker(task):
    """Retain native resources and side-effect ownership until the worker exits."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
            scope = current_scope.get()
            if scope is not None:
                scope.cancelled.set()
        except BaseException:
            break
    if cancelled:
        if not task.cancelled():
            task.exception()
        raise asyncio.CancelledError
    return task.result()


async def worker(function, *args, **kwargs):
    check()
    def run():
        check()
        result = function(*args, **kwargs)
        check()
        return result
    return await finish_worker(asyncio.create_task(asyncio.to_thread(run)))


def disconnect_aware(function):
    """Supervise consumed-body HTTP handlers without changing their wire shape."""
    signature = inspect.signature(function, eval_str=True)

    @wraps(function)
    async def wrapped(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        request = next((value for value in bound.arguments.values() if isinstance(value, Request)), None)
        if request is None:  # Internal/diagnostic callers retain the old API.
            return await function(*args, **kwargs)
        supplied = request.headers.get("X-Cube-Interaction-ID", "")
        identifier = supplied if re.fullmatch(r"[0-9a-f]{32}", supplied) else uuid4().hex
        scope = Scope(identifier)
        token = current_scope.set(scope)
        async def watch():
            # FastAPI has consumed JSON/multipart before invoking this handler.
            # No concurrent consumer touches the request body here.
            while True:
                message = await request.receive()
                if message["type"] == "http.disconnect":
                    scope.cancelled.set()
                    logger.info("interaction=%s disconnected path=%s", identifier, request.url.path)
                    task.cancel()
                    return

        # Give an already-delivered disconnect priority over starting inference.
        # Both tasks are assigned before this coroutine yields to the loop.
        watcher = asyncio.create_task(watch())
        task = asyncio.create_task(function(*args, **kwargs))
        try:
            result = await asyncio.shield(task)
            check()
            return result
        except asyncio.CancelledError:
            disconnected = scope.cancelled.is_set()
            scope.cancelled.set()
            task.cancel()
            try:
                await finish_worker(task)
            except asyncio.CancelledError:
                pass
            logger.info("interaction=%s cancelled path=%s", identifier, request.url.path)
            if disconnected:
                return Response(status_code=499)
            raise
        finally:
            watcher.cancel()
            try:
                await finish_worker(asyncio.create_task(_consume(watcher)))
            finally:
                current_scope.reset(token)
    wrapped.__signature__ = signature
    return wrapped


async def _consume(task):
    await asyncio.gather(task, return_exceptions=True)
