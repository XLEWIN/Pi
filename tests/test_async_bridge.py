"""bot/async_bridge.py — one call site, three worlds.

``box()`` decides from *where* the caller is running:

======================================  ================================
context                                 result of ``box(coro)``
======================================  ================================
on an executor loop (private, inline,   a lazy :class:`Hybrid` — the
or the bound bot loop): we are    caller must ``await``; blocking
running database code                   that loop would deadlock it
--------------------------------------  -------------------------------
no loop running (sync test,             the **plain value**: ``is None``,
worker thread)                          ``isinstance()`` etc. behave as
                                        they did before the migration
--------------------------------------  -------------------------------
someone else's loop (an async test)     the **plain value**, resolved on
                                        an executor loop
======================================  ================================

Handler code that must work in every world therefore writes::

    row = await adb(db.get_user(uid))

``adb`` awaits a Hybrid when it gets one and passes a plain value
straight through.

Run:  python -m unittest discover -s tests -p "test_async_bridge.py"
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("BOT_TOKEN", "1:TEST-TOKEN-FOR-UNIT-TESTS")

from bot import async_bridge as ab  # noqa: E402
from bot.async_bridge import Hybrid, adb, box, make_facade, run_sync  # noqa: E402


async def _add(a, b):
    await asyncio.sleep(0)
    return a + b


async def _boom():
    raise ValueError("nope")


async def _nothing():
    return None


async def _str_hello():
    return "hello"


class _OnExecutorLoop:
    """Mixin: run the body with this test's loop bound as the bot loop."""

    async def asyncSetUp(self):
        self._bound = asyncio.get_running_loop()
        ab.bind_loop(self._bound)

    async def asyncTearDown(self):
        ab.unbind_loop()


class TestOnExecutorLoop(_OnExecutorLoop, unittest.IsolatedAsyncioTestCase):
    """On the bot loop, box() must stay lazy — the caller awaits it."""

    async def test_box_is_lazy_then_await_drives_it(self):
        h = box(_add(2, 3))
        self.assertIsInstance(h, Hybrid)
        self.assertFalse(h.done)          # blocking here would starve the loop
        self.assertEqual(await h, 5)
        self.assertTrue(h.done)
        self.assertEqual(await h, 5)      # cached, coroutine not re-run

    async def test_awaited_exception_is_cached_and_re_raised(self):
        h = box(_boom())
        with self.assertRaises(ValueError):
            await h
        with self.assertRaises(ValueError):
            await h                       # coroutine must NOT be re-run

    async def test_adb_awaits_the_box(self):
        self.assertEqual(await adb(box(_add(4, 6))), 10)

    async def test_adb_on_a_plain_value_passes_through(self):
        self.assertIsNone(await adb(box(_nothing())))


class TestHybridForce(unittest.IsolatedAsyncioTestCase):
    """The forcing path — only reachable off the coroutine's own loop.

    Forcing *on* the target loop is impossible (it would mean
    ``run_until_complete`` on a live loop, or blocking that loop against
    itself), which is exactly why ``run_sync`` refuses.  So force from a
    worker thread, which is what any sync caller really is.
    """

    async def _force(self, h):
        # `h + 0` would work for numbers only — _force() is the general
        # form and does the same run_sync() underneath.
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, h._force)

    async def test_await_after_forced_returns_cached_value(self):
        h = Hybrid(_add(1, 1))
        self.assertEqual(await self._force(h), 2)   # forced synchronously
        self.assertEqual(await h, 2)                # await is a cache hit

    async def test_forced_exception_is_re_raised_by_await(self):
        h = Hybrid(_boom())
        with self.assertRaises(ValueError):
            await self._force(h)
        with self.assertRaises(ValueError):
            await h

    async def test_coroutine_runs_at_most_once(self):
        calls = []

        async def _counted():
            calls.append(1)
            return len(calls)

        h = Hybrid(_counted())
        self.assertEqual(await self._force(h), 1)
        self.assertEqual(await h, 1)
        self.assertEqual(calls, [1])

    async def test_forwarding_dunders(self):
        async def _list():
            return [1, 2, 3]

        async def _dict():
            return {"a": 1}

        h = Hybrid(_list())
        await self._force(h)
        self.assertEqual(len(h), 3)
        self.assertEqual(list(h), [1, 2, 3])
        self.assertIn(2, h)
        self.assertEqual(h[0], 1)

        d = Hybrid(_dict())
        await self._force(d)
        self.assertTrue(d)
        self.assertEqual(d["a"], 1)
        self.assertIsNone(d.get("missing"))

        s = Hybrid(_str_hello())
        await self._force(s)
        self.assertEqual(str(s), "hello")
        self.assertEqual(f"{s}", "hello")

        r = Hybrid(_add(1, 1))
        await self._force(r)
        self.assertEqual(repr(r), "Hybrid(2)")


class TestForeignLoopResolvesToValue(unittest.IsolatedAsyncioTestCase):
    """An async test's own loop is *not* an executor loop.

    A bare ``db.foo(...)`` there must still resolve, so the 90-odd async
    call sites that never ``await`` keep working.
    """

    async def test_box_returns_the_value_itself(self):
        self.assertEqual(box(_add(2, 3)), 5)
        self.assertIsNone(box(_nothing()))

    async def test_box_resolves_even_when_ignored(self):
        calls = []

        async def _counted():
            calls.append(1)
            return 1

        self.assertEqual(box(_counted()), 1)
        self.assertEqual(calls, [1])       # side effect happened

    async def test_box_failure_raises_at_call_time(self):
        with self.assertRaises(ValueError):
            box(_boom())

    async def test_adb_passes_a_plain_value_through(self):
        self.assertIsNone(await adb(None))
        self.assertEqual(await adb(box(_add(1, 2))), 3)


class TestSyncCaller(unittest.TestCase):
    """No running loop at all: plain values, side effects performed."""

    def test_run_sync_returns_the_value(self):
        self.assertEqual(run_sync(_add(10, 5)), 15)

    def test_box_returns_the_value_itself(self):
        self.assertEqual(box(_add(4, 4)), 8)
        self.assertIsNone(box(_nothing()))

    def test_box_without_await_still_has_side_effect(self):
        calls = []

        async def _counted():
            calls.append(1)
            return len(calls)

        self.assertEqual(box(_counted()), 1)
        self.assertEqual(calls, [1])

    def test_plain_values_work_with_identity_checks(self):
        # The whole reason box() does not hand back a Hybrid here: the
        # pre-existing suite asserts `is None` / isinstance on results.
        self.assertIsNone(box(_nothing()))

    def test_failure_raises_at_call_time(self):
        with self.assertRaises(ValueError):
            box(_boom())

    def test_repr_of_pending_box_does_not_block(self):
        h = Hybrid(_add(1, 2))            # never forced
        self.assertEqual(repr(h), "Hybrid(<pending>)")
        self.assertFalse(h.done)

    def test_re_entrant_lock_is_not_deadlocked_by_the_bridge(self):
        # Regression: a sync caller holding Database._lock then making a
        # db call must not hop threads — an RLock is per-thread, so the
        # hop would deadlock against itself.
        lock = threading.RLock()
        with lock:
            self.assertEqual(box(_add(1, 1)), 2)
            self.assertTrue(lock.acquire(blocking=False))
            lock.release()


class TestFacade(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        class Impl:
            def __init__(self):
                self.touched = 0

            async def write(self, n):
                await asyncio.sleep(0)
                self.touched += n
                return self.touched

            def plain(self):
                return "raw"

        self.impl = Impl()
        self.facade = make_facade(self.impl)

    async def test_awaited_method_uses_adb(self):
        # On a foreign loop facade.write() resolves eagerly, so handlers
        # (which must work in prod too) go through adb.
        self.assertEqual(await adb(self.facade.write(2)), 2)
        self.assertEqual(self.impl.touched, 2)

    async def test_unawaited_method_still_runs_on_a_foreign_loop(self):
        self.assertEqual(self.facade.write(3), 3)
        self.assertEqual(self.impl.touched, 3)

    def test_unawaited_method_still_runs_in_sync_context(self):
        self.facade.write(3)
        self.assertEqual(self.impl.touched, 3)

    async def test_non_coroutine_attributes_pass_through(self):
        self.assertEqual(self.facade.plain(), "raw")

    async def test_state_attributes_pass_through(self):
        self.facade.touched = 99
        self.assertEqual(self.impl.touched, 99)


class TestBoundLoopRouting(unittest.IsolatedAsyncioTestCase):
    async def test_off_loop_call_blocks_on_the_bot_loop(self):
        """Production worker threads must run on the bound bot loop."""
        loop = asyncio.get_running_loop()
        ran_on = {}

        async def _probe():
            ran_on["loop"] = asyncio.get_running_loop()
            return 7

        ab.bind_loop(loop)
        try:
            # Simulated worker thread: no running loop of its own, so it
            # must block on the bound (bot) loop. run_in_executor keeps
            # this test's own loop spinning while the worker waits.
            def _worker():
                return run_sync(_probe())

            out = await loop.run_in_executor(None, _worker)
            self.assertEqual(out, 7)
            self.assertIs(ran_on["loop"], loop)
        finally:
            ab.unbind_loop()

    async def test_on_loop_call_is_lazy_not_blocking(self):
        loop = asyncio.get_running_loop()
        ab.bind_loop(loop)
        try:
            h = box(_add(1, 2))
            # On the bound loop the box must NOT have run yet — blocking
            # here would starve the bot's own event loop.
            self.assertIsInstance(h, Hybrid)
            self.assertFalse(h.done)
            self.assertEqual(await h, 3)
        finally:
            ab.unbind_loop()

    async def test_run_sync_from_the_target_loop_refuses_rather_than_hangs(self):
        loop = asyncio.get_running_loop()
        ab.bind_loop(loop)
        try:
            with self.assertRaises(RuntimeError):
                run_sync(_add(1, 1))
        finally:
            ab.unbind_loop()


if __name__ == "__main__":
    unittest.main()
