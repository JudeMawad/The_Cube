"""Batch 2: no HTTP, models, feature execution, or production routing."""
import asyncio
from contextlib import AsyncExitStack
import unittest

from core.client_state import ClientStateManager


class ClientStateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = 0.0
        self.manager = ClientStateManager(clock=lambda: self.now)

    async def settle(self):
        # Yield to tasks already scheduled; no wall-clock timing assumptions.
        await asyncio.sleep(0)

    async def test_history_bounds_isolation_and_copy(self):
        for index in range(8):
            async with self.manager.turn("a") as turn:
                turn.complete(f"user {index}", f"assistant {index}")
        async with self.manager.turn("a") as turn:
            messages = turn.history
            self.assertEqual(len(messages), 12)
            self.assertEqual(messages[0].content, "user 2")
            self.assertEqual(messages[-1].content, "assistant 7")
            messages[0].content = "mutated"
            self.assertEqual(turn.history[0].content, "user 2")
        async with self.manager.turn("b") as turn:
            self.assertEqual(turn.history, [])
            turn.complete("u" * 3000, "a" * 1000)
        async with self.manager.turn("b") as turn:
            self.assertEqual([len(m.content) for m in turn.history], [2000, 500])

    async def test_complete_is_staged_and_scoped(self):
        async with self.manager.turn("a") as turn:
            turn.complete("hello", "reply")
            self.assertEqual(turn.history, [])
            with self.assertRaises(RuntimeError):
                turn.complete("duplicate", "duplicate")
        with self.assertRaises(RuntimeError):
            turn.complete("late", "late")
        with self.assertRaises(RuntimeError):
            _ = turn.history
        async with self.manager.turn("a") as next_turn:
            self.assertEqual([m.content for m in next_turn.history], ["hello", "reply"])

    async def test_incomplete_blank_and_failed_turns_do_not_append(self):
        async with self.manager.turn("a"):
            pass
        for user, assistant in [(" ", "reply"), ("hello", None), ("hello", " ")]:
            async with self.manager.turn("a") as turn:
                turn.complete(user, assistant)
        with self.assertRaises(ValueError):
            async with self.manager.turn("a") as turn:
                turn.complete("hello", "reply")
                raise ValueError("failed")
        async with self.manager.turn("a") as turn:
            self.assertEqual(turn.history, [])

    async def test_idle_expiry_preserves_reserved_entries(self):
        async with self.manager.turn("idle") as turn:
            turn.complete("old", "reply")
        async with self.manager.turn("active") as active:
            active.complete("current", "reply")
            self.now = 600.0
            self.manager.prune_idle()
            self.assertEqual(list(self.manager._clients), ["active"])
            self.assertTrue(self.manager._clients["active"].lock.locked())
        async with self.manager.turn("idle") as fresh:
            self.assertEqual(fresh.history, [])

    async def test_lru_evicts_only_idle_history(self):
        manager = ClientStateManager(max_clients=2)
        for client in ("a", "b", "a", "c"):
            async with manager.turn(client):
                pass
        self.assertEqual(list(manager._clients), ["a", "c"])
        async with manager.turn("b") as turn:
            self.assertEqual(turn.history, [])
        self.assertEqual(len(manager), 2)

    async def test_default_capacity_blocks_129th_client_without_allocating(self):
        stack = AsyncExitStack()
        await stack.__aenter__()
        entered = asyncio.Event()
        async def newcomer():
            async with self.manager.turn("new"):
                entered.set()
        task = None
        try:
            for index in range(128):
                await stack.enter_async_context(self.manager.turn(str(index)))
            task = asyncio.create_task(newcomer())
            await self.settle()
            self.assertFalse(entered.is_set())
            self.assertEqual(len(self.manager), 128)
            self.assertNotIn("new", self.manager._clients)
        finally:
            await stack.aclose()
            if task is not None:
                await asyncio.wait_for(task, 1)
        self.assertTrue(entered.is_set())
        self.assertEqual(len(self.manager), 128)
        self.assertTrue(all(s.reservations == 0 for s in self.manager._clients.values()))

    async def test_waiting_turn_protects_same_lock_from_eviction(self):
        manager = ClientStateManager(max_clients=1, clock=lambda: self.now)
        waiting_entered = asyncio.Event()
        release_waiting = asyncio.Event()
        new_entered = asyncio.Event()
        async def waiting():
            async with manager.turn("a"):
                waiting_entered.set()
                await release_waiting.wait()
        async def newcomer():
            async with manager.turn("b"):
                new_entered.set()
        tasks = []
        try:
            async with manager.turn("a"):
                original = manager._clients["a"].lock
                tasks = [asyncio.create_task(waiting()), asyncio.create_task(newcomer())]
                await self.settle()
                self.assertEqual(manager._clients["a"].reservations, 2)
                self.now = 1000
                manager.prune_idle()
                self.assertIs(manager._clients["a"].lock, original)
                self.assertFalse(waiting_entered.is_set())
            await asyncio.wait_for(waiting_entered.wait(), 1)
            self.assertFalse(new_entered.is_set())
            self.assertIs(manager._clients["a"].lock, original)
            release_waiting.set()
            await asyncio.wait_for(asyncio.gather(*tasks), 1)
        finally:
            release_waiting.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        self.assertTrue(new_entered.is_set())

    async def test_cancellation_while_waiting_for_lock_or_capacity(self):
        manager = ClientStateManager(max_clients=1)
        async def wait_for(client):
            async with manager.turn(client):
                self.fail("Cancelled waiter acquired a turn")
        async with manager.turn("a"):
            same = asyncio.create_task(wait_for("a"))
            new = asyncio.create_task(wait_for("b"))
            await self.settle()
            self.assertEqual(manager._clients["a"].reservations, 2)
            for task in (same, new):
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertEqual(manager._clients["a"].reservations, 1)
            self.assertEqual(list(manager._clients), ["a"])
        async with manager.turn("b"):
            self.assertEqual(len(manager), 1)

    async def test_cancelled_holder_discards_staged_history_and_releases(self):
        entered = asyncio.Event()
        async def holder():
            async with self.manager.turn("a") as turn:
                turn.complete("cancelled user", "not delivered")
                entered.set()
                await asyncio.Event().wait()
        task = asyncio.create_task(holder())
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        async with self.manager.turn("a") as turn:
            self.assertEqual(turn.history, [])
        state = self.manager._clients["a"]
        self.assertEqual(state.reservations, 0)
        self.assertFalse(state.lock.locked())

    async def test_same_client_serializes_while_other_clients_progress(self):
        order = []
        release = asyncio.Event()
        entered = asyncio.Event()
        async def first():
            async with self.manager.turn("a") as turn:
                entered.set()
                await release.wait()
                turn.complete("one", "reply one")
                order.append("first")
        async def second():
            async with self.manager.turn("a") as turn:
                self.assertEqual(turn.history[0].content, "one")
                order.append("second")
        first_task = asyncio.create_task(first())
        await asyncio.wait_for(entered.wait(), 1)
        second_task = asyncio.create_task(second())
        try:
            async with self.manager.turn("b"):
                self.assertEqual(order, [])
            release.set()
            await asyncio.wait_for(asyncio.gather(first_task, second_task), 1)
        finally:
            release.set()
            for task in (first_task, second_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(first_task, second_task, return_exceptions=True)
        self.assertEqual(order, ["first", "second"])

    def test_invalid_resource_limits(self):
        for kwargs in [{"max_clients": 0}, {"max_clients": True}, {"max_turns": 0},
                       {"max_turns": 7}, {"idle_ttl": 0}, {"idle_ttl": float("inf")},
                       {"idle_ttl": float("nan")}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                ClientStateManager(**kwargs)
