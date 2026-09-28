"""Regression: a leave-only message must clear the join stamp.

Telegram sends three service-message shapes to a group:

* join-only          -> ``new_chat_members`` set, ``left_chat_member`` None
* leave-only         -> ``new_chat_members`` empty,  ``left_chat_member`` set
* join+leave         -> both set (rare; username/photo changes send neither)

``join_tracker`` used to bail out with ``if not message.new_chat_members:
return``, so the leave-only shape never reached ``_track_joins`` and
``bdb.clear_join`` never ran.  The leaver kept a join stamp from whenever
they first joined, and the bind grace period kept judging them by it.

Environment isolation (BEFORE any bot import):
    * BOT_TOKEN is forced — bot.config exits without one.
    * unittest is already imported -> bot.database always selects
      mongomock, so the suite never touches a real MongoDB server.
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

# ── Environment isolation — must precede bot imports ──────────────
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["BOT_TOKEN"] = "1:TEST-TOKEN-FOR-UNIT-TESTS"
_TEST_DIR = tempfile.mkdtemp(prefix="pi_jointracker_test_")
os.environ["LOCALAPPDATA"] = _TEST_DIR
atexit.register(shutil.rmtree, _TEST_DIR, ignore_errors=True)

# ── Imports (after env) ───────────────────────────────────────────
from bot.modules.bind import database as bdb  # noqa: E402
from bot.modules.bind.handlers import join_tracker  # noqa: E402

CHAT = -920001
CHANNEL = -920099          # unique so upsert_binding never hits CHANNEL_TAKEN
USER = 720001
OTHER = 720002
BOT_ID = 999


def _msg(*, members=(), left=None, chat_type="supergroup"):
    """Minimal stand-in for aiogram's Message (only what join_tracker reads)."""
    return SimpleNamespace(
        chat=SimpleNamespace(id=CHAT, type=chat_type),
        new_chat_members=tuple(members),
        left_chat_member=left,
    )


def _user(uid):
    return SimpleNamespace(id=uid, is_bot=False)


class TestJoinTracker(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        # _track_joins only writes when the chat is actually bound.
        try:
            bdb.upsert_binding(CHAT, CHANNEL, channel_username="pi_join_tracker")
        except ValueError:
            pass  # already bound by an earlier test
        bdb.clear_join(CHAT, USER)
        bdb.clear_join(CHAT, OTHER)

    def tearDown(self):
        bdb.clear_join(CHAT, USER)
        bdb.clear_join(CHAT, OTHER)
        bdb.remove_binding(CHAT)
        super().tearDown()

    async def test_leave_only_message_clears_the_stamp(self):
        bdb.record_join(CHAT, USER, joined_at=1_600_000_000.0)
        self.assertIsNotNone(bdb.get_join_time(CHAT, USER))

        await join_tracker(_msg(left=_user(USER)), SimpleNamespace(id=BOT_ID))

        self.assertIsNone(
            bdb.get_join_time(CHAT, USER),
            "leave-only message did not clear the join stamp — grace period "
            "would keep using a months-old join time",
        )

    async def test_join_only_message_records_the_stamp(self):
        await join_tracker(_msg(members=[_user(USER)]), SimpleNamespace(id=BOT_ID))
        self.assertIsNotNone(bdb.get_join_time(CHAT, USER))

    async def test_join_and_leave_in_one_message(self):
        await join_tracker(
            _msg(members=[_user(USER)], left=_user(OTHER)),
            SimpleNamespace(id=BOT_ID),
        )
        self.assertIsNotNone(bdb.get_join_time(CHAT, USER), "joiner not recorded")
        self.assertIsNone(bdb.get_join_time(CHAT, OTHER), "leaver stamp not cleared")

    async def test_unrelated_service_message_is_a_no_op(self):
        # username / photo changes arrive with neither field populated.
        await join_tracker(_msg(), SimpleNamespace(id=BOT_ID))
        self.assertIsNone(bdb.get_join_time(CHAT, USER))

    async def test_private_chat_returns_before_touching_the_db(self):
        bdb.record_join(CHAT, USER, joined_at=1_600_000_000.0)
        await join_tracker(
            _msg(left=_user(USER), chat_type="private"), SimpleNamespace(id=BOT_ID)
        )
        self.assertIsNotNone(bdb.get_join_time(CHAT, USER))

    async def test_bot_joins_do_not_get_a_stamp(self):
        bot_member = SimpleNamespace(id=BOT_ID + 1, is_bot=True)
        await join_tracker(_msg(members=[bot_member]), SimpleNamespace(id=BOT_ID))
        self.assertIsNone(bdb.get_join_time(CHAT, BOT_ID + 1))


if __name__ == "__main__":
    unittest.main(verbosity=2)
