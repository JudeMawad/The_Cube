"""Offline execution tests: no feature services, models, CUDA, or HTTP."""
import asyncio
import threading
from time import perf_counter
from types import SimpleNamespace
import unittest
import warnings
from unittest.mock import AsyncMock, Mock, patch

from core.ai_turn import process_turn
from core.client_state import ClientStateManager
from core.schemas import ToolDefinition, ToolIntent, ToolResult
from core.tool_registry import ToolArguments, ToolContext, ToolRegistry


class Power(ToolArguments):
    on: bool


class ToolExecutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.states = ClientStateManager(max_clients=1)
        self.client = SimpleNamespace(
            config=SimpleNamespace(url="http://ai.invalid"),
            process_remote=AsyncMock(return_value=ToolIntent(
                type="tool", tool="test.power", arguments={"on": True})),
        )
        self.legacy = AsyncMock(side_effect=AssertionError("No legacy after interpretation"))
        guard = patch("socket.socket.connect", side_effect=AssertionError("Network forbidden"))
        guard.start()
        self.addCleanup(guard.stop)

    async def process(self, handler):
        registry = ToolRegistry()
        registry.register(ToolDefinition(name="test.power", description="Test", parameters={}),
                          arguments_model=Power, handler=handler)
        return await process_turn(
            {"text": "lights on", "processing_time": 0.1}, "trusted-client",
            voice_started=perf_counter(), ai_client=self.client,
            client_states=self.states, legacy=self.legacy, tool_registry=registry,
        )

    async def test_success_executes_once_off_loop_with_trusted_context(self):
        loop_thread = threading.get_ident()
        def execute(arguments, context):
            self.assertNotEqual(threading.get_ident(), loop_thread)
            self.assertIsInstance(arguments, Power)
            self.assertTrue(arguments.on)
            self.assertEqual(context, ToolContext(client_id="trusted-client"))
            return ToolResult(tool="test.power", success=True)
        handler = Mock(side_effect=execute)
        result = await self.process(handler)
        handler.assert_called_once()
        self.assertEqual(self.client.process_remote.await_count, 1)
        self.legacy.assert_not_awaited()
        self.assertTrue(result["success"])
        self.assertEqual(result["processing_source"], "ai_tool")
        self.assertEqual(result["timings"]["command"], result["processing_timings"]["execution"])
        self.assertGreaterEqual(result["processing_timings"]["execution"], 0)
        async with self.states.turn("trusted-client") as turn:
            self.assertEqual([m.content for m in turn.history], ["lights on", result["response"]])

    async def test_failed_or_invalid_outcome_never_retries_or_uses_legacy(self):
        outcomes = [
            RuntimeError("SECRET"), SystemExit("SECRET"), asyncio.CancelledError(),
            None, {"tool": "test.power", "success": True},
            ToolResult(tool="wrong.tool", success=True),
            ToolResult.model_construct(tool="test.power", success="SECRET"),
            ToolResult(tool="test.power", success=False, error="SECRET"),
        ]
        for outcome in outcomes:
            with self.subTest(outcome=type(outcome).__name__):
                self.client.process_remote.reset_mock()
                handler = Mock(side_effect=outcome) if isinstance(outcome, BaseException) else Mock(return_value=outcome)
                with warnings.catch_warnings(record=True) as emitted:
                    result = await self.process(handler)
                self.assertNotIn("SECRET", str([str(w.message) for w in emitted]))
                self.assertFalse(result["success"])
                self.assertNotIn("SECRET", str(result))
                handler.assert_called_once()
                self.assertEqual(self.client.process_remote.await_count, 1)
                self.legacy.assert_not_awaited()

    async def test_running_cancellation_waits_for_state_and_keeps_client_lock(self):
        for fails in (False, True):
            with self.subTest(fails=fails):
                self.states = ClientStateManager(max_clients=1)
                self.client.process_remote.reset_mock()
                loop = asyncio.get_running_loop()
                entered = asyncio.Event()
                release = threading.Event()
                authoritative = []
                def execute(arguments, context):
                    loop.call_soon_threadsafe(entered.set)
                    if not release.wait(5):
                        raise RuntimeError("Test did not release worker")
                    authoritative.append("finalized")
                    if fails:
                        raise RuntimeError("operation failed after state cleanup")
                    return ToolResult(tool="test.power", success=True)
                handler = Mock(side_effect=execute)
                task = asyncio.create_task(self.process(handler))
                waiter = None
                waiter_entered = asyncio.Event()
                async def next_turn():
                    async with self.states.turn("trusted-client") as turn:
                        self.assertEqual(authoritative, ["finalized"])
                        self.assertEqual(turn.history, [])
                        waiter_entered.set()
                try:
                    await asyncio.wait_for(entered.wait(), 1)
                    task.cancel()
                    await asyncio.sleep(0)
                    task.cancel()  # A second cancellation must not abandon the worker.
                    waiter = asyncio.create_task(next_turn())
                    await asyncio.sleep(0)
                    self.assertFalse(task.done())
                    self.assertFalse(waiter_entered.is_set())
                    self.assertEqual(self.states._clients["trusted-client"].reservations, 2)
                    self.assertTrue(self.states._clients["trusted-client"].lock.locked())
                    release.set()
                    with self.assertRaises(asyncio.CancelledError):
                        await asyncio.wait_for(task, 1)
                    await asyncio.wait_for(waiter, 1)
                    handler.assert_called_once()
                    self.client.process_remote.assert_awaited_once()
                    self.legacy.assert_not_awaited()
                    self.assertEqual(self.states._clients["trusted-client"].reservations, 0)
                finally:
                    release.set()
                    await asyncio.gather(task, *([waiter] if waiter else []), return_exceptions=True)

    async def test_cancelled_queued_worker_never_invokes_handler(self):
        queued = asyncio.Event()
        dispatch = asyncio.Event()
        real_to_thread = asyncio.to_thread
        async def delayed_dispatch(function):
            queued.set()
            await dispatch.wait()
            return await real_to_thread(function)
        handler = Mock(return_value=ToolResult(tool="test.power", success=True))
        with patch("core.tool_execution.asyncio.to_thread", side_effect=delayed_dispatch):
            task = asyncio.create_task(self.process(handler))
            try:
                await asyncio.wait_for(queued.wait(), 1)
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                dispatch.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 1)
            finally:
                dispatch.set()
                await asyncio.gather(task, return_exceptions=True)
        handler.assert_not_called()
        self.client.process_remote.assert_awaited_once()
        self.legacy.assert_not_awaited()
        async with self.states.turn("trusted-client") as turn:
            self.assertEqual(turn.history, [])

    async def test_cancellation_already_requested_does_not_schedule_execution(self):
        async def interpret(request):
            asyncio.current_task().cancel()
            return ToolIntent(type="tool", tool="test.power", arguments={"on": True})
        self.client.process_remote.side_effect = interpret
        handler = Mock()
        with patch("core.tool_execution.asyncio.to_thread") as dispatch:
            task = asyncio.create_task(self.process(handler))
            with self.assertRaises(asyncio.CancelledError):
                await task
            dispatch.assert_not_called()
        handler.assert_not_called()
        self.legacy.assert_not_awaited()
        async with self.states.turn("trusted-client") as turn:
            self.assertEqual(turn.history, [])

    async def test_cancellation_racing_completed_worker_drops_history(self):
        real_to_thread = asyncio.to_thread
        task = None
        async def finish_then_cancel(function):
            result = await real_to_thread(function)
            task.cancel()
            return result
        handler = Mock(return_value=ToolResult(tool="test.power", success=True))
        with patch("core.tool_execution.asyncio.to_thread", side_effect=finish_then_cancel):
            task = asyncio.create_task(self.process(handler))
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
        handler.assert_called_once()
        self.client.process_remote.assert_awaited_once()
        self.legacy.assert_not_awaited()
        async with self.states.turn("trusted-client") as turn:
            self.assertEqual(turn.history, [])
