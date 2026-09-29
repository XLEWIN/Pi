"""_next_id must never hold a threading.Lock across an await.

The production bug this pins: ``_next_id`` used to do ``with _SEQ_LOCK:
await update_one(...); await find_one(...)``.  A plain ``threading.Lock``
is not reentrant — while coroutine A yielded at the I/O await, coroutine
B ran on the SAME event-loop thread and blocked forever inside
``Lock.acquire()``, freezing the entire loop.  Symptom on Railway:
``track_message DB failed: `` / ``background flush failed:`` with
nothing after the colon (empty ``TimeoutError`` from run_sync's 60s
wait) and a bot that stops answering entirely.

The fake counters collection suspends for real (``await asyncio.sleep``)
exactly like network I/O, so the race window actually opens.  With the
lock-based implementation this test fails via timeout instead of
hanging the suite; with the atomic ``find_one_and_update`` it passes.

Environment isolation (BEFORE any bot import): same pattern as
test_db_lifecycle / test_offline_backlog.
"""

from __future__ import annotations

import asyncio
import atexit
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["BOT_TOKEN"] = "1:TEST-TOKEN-FOR-UNIT-TESTS"
_TEST_DIR = tempfile.mkdtemp(prefix="pi_nextid_test_")
os.environ["LOCALAPPDATA"] = _TEST_DIR
atexit.register(shutil.rmtree, _TEST_DIR, ignore_errors=True)

from bot.database import Database  # noqa: E402


class _SlowCounters:
    """counters collection whose write SUSPENDS like real network I/O."""

    def __init__(self) -> None:
        self.seq = 0

    async def find_one_and_update(self, flt, update, upsert=False,
                                  return_document=None):
        await asyncio.sleep(0.005)  # the race window (old code deadlocks)
        self.seq += 1
        return {"_id": flt["_id"], "seq": self.seq}


class TestNextIdConcurrency(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_next_id_returns_distinct_ids(self):
        db = object.__new__(Database)          # skip __init__ (no backend)
        db._mongo = {"counters": _SlowCounters()}

        ids = await asyncio.wait_for(           # old impl: TimeoutError
            asyncio.gather(*(db._next_id("x") for _ in range(4))),
            timeout=2.0,
        )
        self.assertEqual(sorted(ids), [1, 2, 3, 4])


if __name__ == "__main__":
    unittest.main()
