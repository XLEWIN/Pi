"""Regression tests: MongoDB backend thread safety.

Run from the Pi/Pi root:

    python tests/test_db_threads.py

The old SQLite layer needed per-thread connections because one shared
connection corrupted Python's transaction state under PTB's event loop
+ run_in_executor workers ("cannot commit - no transaction is active",
"error return without exception set"). The Mongo layer uses ONE pooled,
thread-safe MongoClient instead — these tests hammer the shared backend
from multiple threads and verify it stays consistent.

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
import threading
import unittest
from pathlib import Path

# ── Environment isolation — must precede bot imports ──────────────
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["BOT_TOKEN"] = "1:TEST-TOKEN-FOR-UNIT-TESTS"
_TEST_DIR = tempfile.mkdtemp(prefix="pi_dbthread_test_")
os.environ["LOCALAPPDATA"] = _TEST_DIR
atexit.register(shutil.rmtree, _TEST_DIR, ignore_errors=True)

# ── Imports (after env) ───────────────────────────────────────────
from bot.database import db  # noqa: E402


class TestMongoThreadSafety(unittest.TestCase):
    def test_backend_is_mongomock_in_tests(self):
        self.assertIn("mongomock", db.backend)

    def test_concurrent_writes_do_not_corrupt(self):
        """8 threads × 20 mixed write cycles — the original failure mode."""
        errors = []

        def worker(n: int) -> None:
            try:
                for i in range(20):
                    uid = 100000 + n * 100 + i
                    ok = db.add_user(
                        user_id=uid,
                        username=f"u{n}_{i}",
                        first_name=f"F{n}",
                        last_name=None,
                        is_bot=False,
                    )
                    if not ok:
                        errors.append(f"add_user False n={n} i={i}")
                    db.update_user_activity(
                        user_id=uid,
                        action="test",
                        chat_id=-1001,
                        chat_title="ThreadHammer",
                    )
            except Exception as e:  # noqa: BLE001 — every raise is a bug
                errors.append(f"{type(e).__name__}: {e}")

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        n = db.collection("users").count_documents(
            {"user_id": {"$gte": 100000, "$lt": 200000}}
        )
        self.assertEqual(n, 160)

    def test_shared_counter_is_atomic_under_threads(self):
        """6 threads × 25 count_message on one bucket must sum exactly."""
        chat, uid, day = -100999901, 880000001, "2026-01-01"
        db.collection("daily_messages").delete_many({"chat_id": chat})
        errors = []

        def worker() -> None:
            try:
                for _ in range(25):
                    db.count_message(chat, uid, day)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{type(e).__name__}: {e}")

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(db.get_chat_message_total(chat), 150)
        db.collection("daily_messages").delete_many({"chat_id": chat})


if __name__ == "__main__":
    unittest.main(verbosity=2)
