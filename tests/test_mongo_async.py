"""The async Mongo backend (bot/mongo_async.py).

Guarantees under test:
  * the coroutine surface covers every op `bot/database.py::_CollProxy`
    wraps, so no backend op can silently fall back to sync;
  * the mongomock shim round-trips data exactly like the sync layer;
  * the PyMongo async client builds synchronously and non-blocking (no
    socket is opened until the first `await`), so `Database.__init__`
    may build it before the event loop exists.

Run:  python -m unittest discover -s tests -p "test_mongo_async.py"
"""

from __future__ import annotations

import asyncio
import inspect
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("BOT_TOKEN", "1:TEST-TOKEN-FOR-UNIT-TESTS")

from bot import mongo_async  # noqa: E402
from bot.mongo_async import AsyncCursor, open_backend  # noqa: E402
from bot.database import _CollProxy  # noqa: E402


class TestBackendSurface(unittest.TestCase):
    def test_every_proxy_op_is_covered(self):
        """_CollProxy._READS ∪ _WRITES must be a subset of MONGO_ASYNC_OPS."""
        expected = _CollProxy._READS | _CollProxy._WRITES
        missing = expected - mongo_async.MONGO_ASYNC_OPS
        self.assertEqual(missing, set(),
                         f"ops used by the sync layer but missing from the "
                         f"async backend: {sorted(missing)}")

    def test_unknown_op_raises_rather_than_returning_sync_object(self):
        coll = mongo_async._AsyncColl(object())
        with self.assertRaises(AttributeError):
            coll.write_concern

    def test_mongomock_backend_is_selected_when_uri_is_none(self):
        client, mongo, label = open_backend(None)
        self.assertIn("mongomock", label)
        self.assertIn("tests", label)
        self.assertEqual(mongo.name, "pi_bot_test")

    def test_async_backend_builds_without_touching_the_network(self):
        """Construction must not open a socket — __init__ runs pre-loop."""
        client, mongo, label = open_backend("mongodb://127.0.0.1:59999/pi_bot")
        self.assertTrue(label.startswith("mongodb (async/"))
        self.assertEqual(mongo.name, "pi_bot")
        # PyMongo's async client is loop-bound, but only binds on first
        # await, so building it here (no running loop) is safe.
        self.assertTrue(client.__class__.__module__.startswith("pymongo"))
        # close() is a coroutine on the async client and there is no loop
        # here — discard it rather than leak a "never awaited" warning.
        res = client.close()
        if inspect.isawaitable(res):
            res.close()


class TestMongomockCoroutines(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client, self.mongo, self.label = open_backend(None)
        self.coll = self.mongo["roundtrip"]

    async def test_write_then_read(self):
        res = await self.coll.insert_one({"user_id": 1, "n": 7})
        self.assertIsNotNone(res.inserted_id)
        self.assertEqual(await self.coll.count_documents({}), 1)
        doc = await self.coll.find_one({"user_id": 1})
        self.assertEqual(doc["n"], 7)
        # Mongo's `_id` is still present here on purpose: `Database._clean`
        # is what strips it, one layer up. The backend must not guess.

    async def test_update_replace_delete(self):
        await self.coll.insert_one({"user_id": 2, "n": 1})
        await self.coll.update_one({"user_id": 2}, {"$inc": {"n": 4}})
        self.assertEqual((await self.coll.find_one({"user_id": 2}))["n"], 5)

        await self.coll.replace_one({"user_id": 2}, {"user_id": 2, "n": 99})
        self.assertEqual((await self.coll.find_one({"user_id": 2}))["n"], 99)

        await self.coll.delete_one({"user_id": 2})
        self.assertEqual(await self.coll.count_documents({}), 0)

    async def test_bulk_write_tolerates_pymongo_sort_keyword(self):
        """pymongo >= 4.10 always passes sort= from UpdateOne._add_to_bulk.

        mongomock 4.3.0 declares no such parameter on
        BulkOperationBuilder.add_update, so without
        mongo_async._patch_mongomock_bulk() every bulk_write raises
        ``TypeError: ... unexpected keyword argument 'sort'``. This is the
        test that pins that compat shim in place.
        """
        from pymongo import UpdateOne

        res = await self.coll.bulk_write([
            UpdateOne({"user_id": 42}, {"$set": {"n": 1}}, upsert=True),
            UpdateOne({"user_id": 42}, {"$inc": {"n": 1}}, upsert=True),
        ])
        self.assertEqual(res.upserted_count, 1)
        self.assertEqual(res.modified_count, 1)
        self.assertEqual((await self.coll.find_one({"user_id": 42}))["n"], 2)

    async def test_update_many_delete_many_distinct(self):
        for i in range(5):
            await self.coll.insert_one({"user_id": i, "bucket": i % 2})
        await self.coll.update_many({}, {"$set": {"seen": True}})
        self.assertEqual(
            await self.coll.distinct("bucket"), [0, 1])
        self.assertEqual(
            await self.coll.count_documents({"seen": True}), 5)
        res = await self.coll.delete_many({"bucket": 0})
        self.assertEqual(res.deleted_count, 3)  # user_id 0, 2, 4
        self.assertEqual(await self.coll.count_documents({}), 2)

    async def test_find_cursor_sort_limit_and_iteration(self):
        for i in (3, 1, 2):
            await self.coll.insert_one({"user_id": i})
        # find() returns the cursor synchronously, exactly like pymongo.
        cur = self.coll.find({}, {}).sort([("user_id", -1)]).limit(2)
        rows = [d async for d in cur]
        self.assertEqual([d["user_id"] for d in rows], [3, 2])

        cur2 = self.coll.find({}, {}).sort([("user_id", 1)])
        self.assertEqual([d["user_id"] async for d in cur2], [1, 2, 3])
        self.assertEqual(await cur2.to_list(), [])  # already drained

    async def test_aggregate_and_estimated_count(self):
        for i in range(3):
            await self.coll.insert_one({"user_id": i, "g": "a" if i else "b"})
        cur = self.coll.aggregate([{"$group": {"_id": "$g",
                                               "n": {"$sum": 1}}}])
        grouped = {d["_id"]: d["n"] async for d in cur}
        self.assertEqual(grouped, {"a": 2, "b": 1})
        self.assertEqual(await self.coll.estimated_document_count(), 3)

    async def test_bulk_write_and_indexes(self):
        from pymongo import UpdateOne, InsertOne
        await self.coll.create_index("user_id", unique=True)
        try:
            await self.coll.bulk_write([
                InsertOne({"user_id": 10}),
                UpdateOne({"user_id": 11}, {"$set": {"n": 1}}, upsert=True),
            ])
            self.assertEqual(await self.coll.count_documents({}), 2)
            with self.assertRaises(Exception):
                await self.coll.insert_one({"user_id": 10})
        finally:
            await self.coll.drop()

    async def test_find_one_and_update_returns_doc(self):
        await self.coll.insert_one({"user_id": 5, "hits": 0})
        doc = await self.coll.find_one_and_update(
            {"user_id": 5}, {"$inc": {"hits": 1}}, return_document=True)
        self.assertIsNotNone(doc)

    async def test_getitem_is_composable(self):
        # `client["db"]` → database, `db["coll"]` → collection. One level
        # per `[]`, exactly like pymongo, so `db.collection(name)`
        # and the module DBs land where they expect.
        self.assertIsInstance(self.mongo, mongo_async._AsyncCollDB)
        nested = self.mongo["other"]
        self.assertIsInstance(nested, mongo_async._AsyncColl)
        await nested.insert_one({"k": 1})
        self.assertEqual(await nested.count_documents({}), 1)
        # Same name → same underlying mongomock collection.
        self.assertIs(self.mongo["other"]._inner, nested._inner)

class TestAsyncCursorChain(unittest.IsolatedAsyncioTestCase):
    async def test_sync_iterator_wrapped(self):
        cur = AsyncCursor(iter([1, 2, 3]))
        self.assertEqual([x async for x in cur], [1, 2, 3])

    async def test_list_like_inner_drains(self):
        cur = AsyncCursor([7, 8])
        self.assertEqual([x async for x in cur], [7, 8])

    async def test_limit_stops_early(self):
        cur = AsyncCursor(iter(range(100)))
        out = []
        async for x in cur:
            out.append(x)
            if len(out) >= 3:
                break
        self.assertEqual(out, [0, 1, 2])


if __name__ == "__main__":
    unittest.main()
