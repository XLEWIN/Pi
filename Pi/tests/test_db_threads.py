"""Regression tests: per-thread SQLite connections (thread safety).

Run from the Pi/Pi root:

    python tests/test_db_threads.py

Reproduces the production failure —
    "cannot commit - no transaction is active"
    "error return without exception set"
— that occurred when the PTB event loop and the run_in_executor DB
workers shared ONE sqlite3 connection (check_same_thread=False, no
serialization of the transaction state machine).

Environment isolation (BEFORE any bot import):
    * BOT_TOKEN is forced — bot.config exits without one.
    * LOCALAPPDATA points at a temp dir so bot.database creates a fresh,
      empty SQLite file (a placeholder file prevents the legacy-DB copy).
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
_PIBOT = Path(_TEST_DIR) / "PiBot"
_PIBOT.mkdir(parents=True, exist_ok=True)
(_PIBOT / "bot_database.db").touch()
atexit.register(shutil.rmtree, _TEST_DIR, ignore_errors=True)

# ── Imports (after env) ───────────────────────────────────────────
from bot.database import ThreadLocalConn, db  # noqa: E402


class TestThreadLocalConnections(unittest.TestCase):
    def test_connection_is_per_thread(self):
        main_conn = db.connection
        seen = {}

        def worker():
            seen["conn"] = db.connection
            db.close()  # this thread's own connection

        t = threading.Thread(target=worker)
        t.start()
        t.join()
        # A different thread must get its own connection …
        self.assertIsNot(seen["conn"], main_conn)
        # … while the calling thread keeps reusing its own.
        self.assertIs(db.connection, main_conn)

    def test_proxy_delegates_to_current_thread(self):
        proxy = ThreadLocalConn(db)
        main_cursor_conn = proxy.cursor().connection
        self.assertIs(main_cursor_conn, db.connection)

        seen = {}

        def worker():
            seen["conn"] = proxy.cursor().connection
            db.close()  # this thread's own connection

        t = threading.Thread(target=worker)
        t.start()
        t.join()
        self.assertIsNot(seen["conn"], db.connection)

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
            finally:
                db.close()  # this thread's own connection

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        row = db.connection.execute(
            "SELECT COUNT(*) FROM users "
            "WHERE user_id >= 100000 AND user_id < 200000"
        ).fetchone()
        self.assertEqual(row[0], 160)


if __name__ == "__main__":
    unittest.main(verbosity=2)
