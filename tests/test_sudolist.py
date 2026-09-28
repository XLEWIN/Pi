"""Tests for /sudolist (bot/modules/bans.py).

Regression guard: the owner-visible path with sudo users configured once
crashed with `TypeError: can only join an iterable` — an `await` inside
a generator expression silently makes an async_generator, which str.join
cannot iterate. The join must use an eager list comprehension.

Run from the Pi/Pi root:

    python tests/test_sudolist.py
    python -m unittest discover -s tests

Environment isolation (BEFORE any bot import):
    * BOT_TOKEN is forced — bot.config exits without one.
    * LOCALAPPDATA points at a temp dir so bot.database creates a fresh DB.
No network: the bot is a fake.
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
_TEST_DIR = tempfile.mkdtemp(prefix="pi_sudolist_test_")
os.environ["LOCALAPPDATA"] = _TEST_DIR
_PIBOT = Path(_TEST_DIR) / "PiBot"
_PIBOT.mkdir(parents=True, exist_ok=True)
(_PIBOT / "bot_database.db").touch()
atexit.register(shutil.rmtree, _TEST_DIR, ignore_errors=True)

# ── Imports (after env) ───────────────────────────────────────────
from aiofakes import call, make_message  # noqa: E402
from bot.database import db  # noqa: E402
from bot.modules.bans import OWNER_ID, sudolist_command  # noqa: E402


class _FakeBot:
    """get_chat resolves every uid to a named user with a username."""

    def __init__(self):
        self.queried = []

    async def get_chat(self, user_id):
        self.queried.append(user_id)
        return SimpleNamespace(
            id=user_id,
            first_name=f"User{user_id}",
            username=f"user{user_id}",
            title=None,
        )


def _msg(user_id):
    """Group command message — replies are recorded in .calls/.sent_texts."""
    return make_message("/sudolist", user_id=user_id)


def _clear_sudo():
    for uid in db.get_sudo_users():
        db.remove_sudo_user(uid)


class TestSudolist(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _clear_sudo()

    def tearDown(self):
        _clear_sudo()

    async def test_lists_sudo_users_and_owner_with_links(self):
        # THE regression path: sudo users configured → the join runs.
        db.add_sudo_user(222, added_by=OWNER_ID)
        db.add_sudo_user(111, added_by=OWNER_ID)
        msg = _msg(OWNER_ID)
        bot = _FakeBot()

        await call(sudolist_command, msg, bot=bot)

        text = msg.sent_texts[-1]
        self.assertIn("Sudo Users:", text)
        self.assertIn('tg://user?id=111', text)
        self.assertIn('tg://user?id=222', text)
        # Ascending id order (sorted), owner rendered on their own line.
        self.assertLess(text.index("id=111"), text.index("id=222"))
        self.assertIn("Owner:", text)
        self.assertIn(f"tg://user?id={OWNER_ID}", text)
        self.assertIn("@user111", text)  # username preferred over name

    async def test_no_sudo_users_message(self):
        msg = _msg(OWNER_ID)
        await call(sudolist_command, msg, bot=_FakeBot())
        self.assertIn("No sudo users", msg.sent_texts[-1])

    async def test_non_owner_denied(self):
        msg = _msg(123456789)
        await call(sudolist_command, msg, bot=_FakeBot())
        self.assertIn("Only the bot owner", msg.sent_texts[-1])

    async def test_get_chat_failure_falls_back_to_code_id(self):
        db.add_sudo_user(333, added_by=OWNER_ID)

        class _BoomBot(_FakeBot):
            async def get_chat(self, user_id):
                raise RuntimeError("network")

        msg = _msg(OWNER_ID)
        await call(sudolist_command, msg, bot=_BoomBot())

        self.assertIn("<code>333</code>", msg.sent_texts[-1])
        self.assertIn(f"<code>{OWNER_ID}</code>", msg.sent_texts[-1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
