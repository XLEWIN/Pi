"""Offline backlog drain: startup must not answer commands sent while off.

Telegram keeps undelivered updates for up to 24 hours, so every command
sent while the bot was down sits in the queue until it is *confirmed*
by a getUpdates call with an offset above the batch's last update_id.
Booting straight into that backlog replies to a pile of old /commands
in one burst and trips Telegram's flood limits ("spam error").

``main._drop_update_backlog`` walks the queue in short-poll batches and
confirms it before ``start_polling`` can hand anything to a handler.
These tests pin that contract with a fake bot.

Environment isolation (BEFORE any bot import):
    * BOT_TOKEN is forced — bot.config exits without one.
    * unittest is already imported → bot.database always selects
      mongomock, so importing main never touches a real MongoDB server.
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

# ── Environment isolation — must precede bot imports ──────────────
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["BOT_TOKEN"] = "1:TEST-TOKEN-FOR-UNIT-TESTS"
_TEST_DIR = tempfile.mkdtemp(prefix="pi_backlog_test_")
os.environ["LOCALAPPDATA"] = _TEST_DIR
atexit.register(shutil.rmtree, _TEST_DIR, ignore_errors=True)

# ── Imports (after env) ───────────────────────────────────────────
from aiogram.exceptions import TelegramRetryAfter  # noqa: E402
from aiogram.methods import GetUpdates  # noqa: E402

from main import _drop_update_backlog  # noqa: E402


class _Update:
    """Minimal stand-in for aiogram's Update (only update_id matters)."""

    def __init__(self, update_id: int) -> None:
        self.update_id = update_id


class _FakeBot:
    """Scripted get_updates: serves batches in order, then empty lists."""

    def __init__(self, batches, *, retry_first: int = 0) -> None:
        self._batches = list(batches)
        self._retry_first = retry_first  # raise TelegramRetryAfter N times first
        self.calls = []                  # {"offset": …, "limit": …, "timeout": …}

    async def get_updates(self, offset=None, limit=None, timeout=None,
                          allowed_updates=None):
        self.calls.append(
            {"offset": offset, "limit": limit, "timeout": timeout}
        )
        if self._retry_first > 0:
            self._retry_first -= 1
            raise TelegramRetryAfter(
                method=GetUpdates(), message="Too Many Requests", retry_after=0
            )
        if self._batches:
            return self._batches.pop(0)
        return []


class TestDropUpdateBacklog(unittest.TestCase):
    def test_empty_queue_is_one_short_poll(self):
        bot = _FakeBot([])
        n = asyncio.run(_drop_update_backlog(bot))
        self.assertEqual(n, 0)
        self.assertEqual(len(bot.calls), 1)
        self.assertIsNone(bot.calls[0]["offset"])
        self.assertEqual(bot.calls[0]["timeout"], 0)  # short poll, no hang

    def test_walks_batches_and_confirms_past_the_last_id(self):
        bot = _FakeBot([[_Update(1), _Update(2)], [_Update(3)]])
        n = asyncio.run(_drop_update_backlog(bot))
        # All three stale updates drained; the final empty fetch with
        # offset 4 is what CONFIRMS them server-side.
        self.assertEqual(n, 3)
        self.assertEqual(
            [c["offset"] for c in bot.calls], [None, 3, 4]
        )
        for call in bot.calls:
            self.assertEqual(call["limit"], 100)
            self.assertEqual(call["timeout"], 0)

    def test_retry_after_waits_and_resumes_same_offset(self):
        bot = _FakeBot([[_Update(7), _Update(8)]], retry_first=1)
        n = asyncio.run(_drop_update_backlog(bot))
        self.assertEqual(n, 2)
        # First call: offset None (429 raised, nothing confirmed).
        # Second call: SAME offset None — the batch must be re-fetched,
        # then the confirm fetch at offset 9.
        self.assertEqual([c["offset"] for c in bot.calls], [None, None, 9])


if __name__ == "__main__":
    unittest.main()
