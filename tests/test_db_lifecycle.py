"""Lifecycle checks: Database.defer (boot queue) and Database.shutdown.

Why these exist
---------------
Module ``setup()`` hooks run at import time — before any event loop.
Doing database work there either dropped un-awaited coroutines (the
bind/tagging ``create_index`` warnings) or bound the async client to a
throw-away inline loop, which made startup()'s ping on the bot loop die
with ``Cannot use AsyncMongoClient in different event loop``.  Both call
paths now go through ``Database.defer`` and only run during
``startup()`` on the bot loop.  ``shutdown()`` closes the client on the
live loop so its background tasks cannot outlive ``asyncio.run``.

Environment isolation (BEFORE any bot import):
    * BOT_TOKEN is forced — bot.config exits without one.
    * unittest is already imported → bot.database always selects
      mongomock, so the suite never touches a real MongoDB server
      (even when MONGO_URI is configured).
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

# ── Environment isolation — must precede bot imports ──────────────
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["BOT_TOKEN"] = "1:TEST-TOKEN-FOR-UNIT-TESTS"
_TEST_DIR = tempfile.mkdtemp(prefix="pi_db_lifecycle_test_")
os.environ["LOCALAPPDATA"] = _TEST_DIR
atexit.register(shutil.rmtree, _TEST_DIR, ignore_errors=True)

# ── Imports (after env) ───────────────────────────────────────────
import bot.database as bd  # noqa: E402
from bot.async_bridge import run_sync  # noqa: E402
from bot.database import db  # noqa: E402


class TestDefer(unittest.TestCase):
    def test_drain_runs_in_order_and_isolates_failures(self):
        inst = bd.Database()  # mongomock (test process)
        seen = []

        def _ok(label):
            def _fn():
                seen.append(label)
            return _fn

        async def _boom():
            raise RuntimeError("bad boot hook")

        inst.defer(_ok("first"))
        inst.defer(_boom)
        inst.defer(_ok("third"))

        run_sync(inst._drain_deferred())
        # A failing hook must not stop the ones behind it.
        self.assertEqual(seen, ["first", "third"])
        # Drained exactly once — the queue is consumed, not replayed.
        self.assertEqual(inst._deferred, [])
        run_sync(inst._drain_deferred())
        self.assertEqual(seen, ["first", "third"])

    def test_module_setup_paths_queue_instead_of_executing(self):
        """setup() call sites must defer — never touch the client early."""
        from bot.modules.bind import database as bdb
        from bot.modules.tagging import database as tdb

        queue = db._deferred
        start = len(queue)
        try:
            bdb.ensure_tables()
            tdb.ensure_tables()
            tdb.defer_interrupted()
            # Three boot hooks queued, zero executed (no index build, no
            # update_many — executing them here would be the original bug).
            self.assertEqual(len(queue), start + 3)
        finally:
            del queue[start:]  # leave the shared queue as we found it


class TestShutdown(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_flushes_and_closes(self):
        inst = bd.Database()
        await inst.shutdown()  # empty flush + client close — must not raise
        self.assertTrue(inst._flusher_stop.is_set())
        # Idempotent: atexit may still run after us.
        await inst.shutdown()


if __name__ == "__main__":
    unittest.main()
