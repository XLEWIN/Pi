"""Opt-in integration test: the REAL async driver against a REAL MongoDB.

Everything else in this suite runs on the mongomock shim, so
``pymongo.AsyncMongoClient`` — the backend production actually uses — has
never been exercised.  This file closes that gap.

It is **skipped unless ``MONGO_URI`` is set**, and even then it never
touches your data: the URI's database path is rewritten to a throwaway
``pi_async_it_<pid>`` database that is dropped in teardown.

Run it (locally or as a Railway one-off):

    MONGO_URI="mongodb+srv://..." python -m unittest discover -s tests -p "test_pymongo_integration.py"

Why each check exists
---------------------
* ``startup()`` — ping + index build on the *caller's* loop.  Fails fast
  on a bad URI instead of booting an empty database.
* buffered write -> flush -> read — the read-your-writes guarantee every
  ``/stats``-style command depends on, now over a real network round trip
  instead of in-memory mongomock.
* ``async for`` over ``AsyncCursor`` — the path ``bot/database.py`` uses.
* plain ``for`` over ``AsyncCursor`` from a worker thread — the path the
  synchronous module databases (``bind``, ``tagging``) use; it resolves
  through ``run_sync`` on the bound loop, which only works if that loop
  is actually the one the client is bound to.  PyMongo raises
  ``RuntimeError: Cannot use AsyncMongoClient in different event loop``
  the moment that diverges, so this check is the loop contract itself.

Environment isolation (BEFORE any bot import): BOT_TOKEN is forced and
``unittest`` in ``sys.orig_argv`` makes ``_resolve_uri()`` return ``None``,
so the module-global ``db`` stays on mongomock and cannot reach your
cluster — only the instance built in this file opens a real client.
"""

from __future__ import annotations

import asyncio
import atexit
import inspect
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# ── Environment isolation — must precede bot imports ──────────────
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["BOT_TOKEN"] = "1:TEST-TOKEN-FOR-UNIT-TESTS"
_TEST_DIR = tempfile.mkdtemp(prefix="pi_async_it_test_")
os.environ["LOCALAPPDATA"] = _TEST_DIR
atexit.register(shutil.rmtree, _TEST_DIR, ignore_errors=True)

# ── Imports (after env) ───────────────────────────────────────────
import bot.async_bridge as bridge  # noqa: E402
import bot.database as bd  # noqa: E402
from bot.mongo_async import AsyncCursor  # noqa: E402

MONGO_URI = os.getenv("MONGO_URI", "").strip()
TEMP_DB = f"pi_async_it_{os.getpid()}"
CHAT = -930001
USER = 730001
TODAY = "2099-01-01"


def _with_db(uri: str, name: str) -> str:
    """Same server/auth/options, different database path."""
    q = ""
    if "?" in uri:
        uri, q = uri.split("?", 1)
        q = "?" + q
    scheme, _, rest = uri.partition("://")
    # credentials never contain '/', so the last '/' starts the path.
    slash = rest.rfind("/")
    rest = rest[:slash] + "/" + name if slash != -1 else rest + "/" + name
    return f"{scheme}://{rest}{q}"


@unittest.skipUnless(
    MONGO_URI, "set MONGO_URI to run the real-async-backend integration test"
)
class TestAsyncBackendAgainstRealMongo(unittest.IsolatedAsyncioTestCase):
    def _temp_uri(self) -> str:
        return _with_db(MONGO_URI, TEMP_DB)

    async def asyncSetUp(self):
        # Force Database() onto the real async client even though this is
        # a test process; the URI already points at a throwaway database.
        with mock.patch.object(bd, "_resolve_uri", return_value=self._temp_uri()):
            self.db = bd.Database()
        self.assertIn("async", self.db.backend.lower(), self.db.backend)
        await self.db.startup()          # ping + indexes + snapshot, binds loop

    async def asyncTearDown(self):
        try:
            await self.db.flush_buffers()
        finally:
            try:
                await self.db._client.drop_database(TEMP_DB)
            finally:
                # close() is a coroutine on the async client; accept a
                # plain return too so this never leaks an un-awaited one.
                res = self.db._client.close()
                if inspect.isawaitable(res):
                    await res
                # A bound loop that dies here would wedge every later
                # async test in this process (box would block on it).
                bridge.unbind_loop()

    # ── checks ────────────────────────────────────────────────────

    async def test_startup_proved_the_connection(self):
        self.assertTrue(self.db._is_test is False, "should not be the test backend")
        info = await self.db._mongo["daily_messages"].index_information()
        self.assertIsInstance(info, dict)
        self.assertTrue(info, "startup() did not build the expected indexes")

    async def test_buffered_write_flush_read_your_writes(self):
        for _ in range(3):
            await self.db.count_message(CHAT, USER, TODAY, "async-it")

        # pending counters are still buffered — nothing written yet
        self.assertGreater(len(self.db._buf_msg), 0)

        await self.db.flush_buffers()

        # NOTE: `await coll.find(...).to_list(...)` is illegal on the
        # driver loop — find() hands back a lazy box and touching an
        # attribute on it raises "run_sync() called from the loop that
        # must execute the coroutine".  Await the cursor first.
        cur = await self.db._mongo["daily_messages"].find(
            {"chat_id": CHAT, "date": TODAY}
        )
        raw = await cur.to_list(length=None)
        self.assertEqual(sum(int(r["messages"]) for r in raw), 3)

        # read-your-writes: the aggregate must agree with the raw rows
        total = await self.db.get_chat_day_total(CHAT, TODAY)
        self.assertEqual(total, 3)

    async def test_async_cursor_supports_async_for_on_the_loop(self):
        await self.db.count_message(CHAT, USER, TODAY, "async-it")
        await self.db.flush_buffers()

        cur = await self.db._mongo["daily_messages"].find({"chat_id": CHAT})
        self.assertIsInstance(cur, AsyncCursor)
        rows = [d async for d in cur]
        self.assertEqual(len(rows), 1)

    async def test_sync_iteration_from_a_worker_thread(self):
        """The bind/tagging module databases iterate cursors in a thread.

        ``AsyncCursor.__iter__`` resolves through ``run_sync`` on the
        bound loop — that only works when the bound loop is really the
        loop the client lives on, which is exactly what startup() must
        have done correctly.
        """
        await self.db.count_message(CHAT, USER, TODAY, "async-it")
        await self.db.flush_buffers()

        cur = await self.db._mongo["daily_messages"].find({"chat_id": CHAT})
        rows = await asyncio.to_thread(list, cur)
        self.assertEqual(len(rows), 1)

    async def test_aggregate_over_the_wire(self):
        await self.db.count_message(CHAT, USER, TODAY, "async-it")
        await self.db.flush_buffers()

        cur = await self.db._mongo["daily_messages"].aggregate(
            [{"$match": {"chat_id": CHAT}}, {"$group": {"_id": None, "n": {"$sum": "$messages"}}}]
        )
        self.assertIsInstance(cur, AsyncCursor)
        out = [d async for d in cur]
        self.assertEqual(out[0]["n"], 1)

    async def test_write_through_the_proxy_invalidates_the_read_cache(self):
        await self.db.count_message(CHAT, USER, TODAY, "async-it")
        await self.db.flush_buffers()
        self.assertEqual(await self.db.get_chat_day_total(CHAT, TODAY), 1)

        await self.db._mongo["daily_messages"].update_one(
            {"chat_id": CHAT, "date": TODAY}, {"$set": {"messages": 42}}
        )
        # a raw write through the proxy must clear the memo
        self.assertEqual(await self.db.get_chat_day_total(CHAT, TODAY), 42)


class TestUriSurgery(unittest.TestCase):
    """The temp-database rewrite must never touch host, auth or options."""

    def test_mongodb_srv(self):
        u = _with_db("mongodb+srv://user:pw@cluster.abc/pi_bot?retryWrites=true", "tmp")
        self.assertEqual(
            u, "mongodb+srv://user:pw@cluster.abc/tmp?retryWrites=true"
        )

    def test_replica_set_without_path(self):
        u = _with_db("mongodb://h1:27017,h2:27017", "tmp")
        self.assertEqual(u, "mongodb://h1:27017,h2:27017/tmp")

    def test_preserves_sharded_path(self):
        u = _with_db("mongodb://h1:27017/prod_db", "tmp")
        self.assertEqual(u, "mongodb://h1:27017/tmp")


if __name__ == "__main__":
    unittest.main(verbosity=2)
