"""Tests for /admincount and /adminlist bot-aware counting
(bot/modules/admin.py).

Run from the Pi/Pi root:

    python tests/test_admin_counts.py
    python -m unittest discover -s tests

Environment isolation (BEFORE any bot import):
    * BOT_TOKEN is forced — importing `bot` pulls bot.config, which
      exits without one.
    * LOCALAPPDATA points at a temp dir so any DB touch stays isolated.
No network: handlers run against fakes.
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
_TEST_DIR = tempfile.mkdtemp(prefix="pi_admin_test_")
os.environ["LOCALAPPDATA"] = _TEST_DIR
atexit.register(shutil.rmtree, _TEST_DIR, ignore_errors=True)

# ── Imports (after env) ───────────────────────────────────────────
from aiofakes import FakeMessage, call  # noqa: E402
from bot.modules import admin as admin_mod  # noqa: E402


# ── Fakes ─────────────────────────────────────────────────────────

def _user(uid: int, name: str, username: str | None = None, is_bot: bool = False):
    return SimpleNamespace(
        id=uid, first_name=name, last_name=None, username=username,
        is_bot=is_bot,
    )


def _member(status: str, user):
    return SimpleNamespace(status=status, user=user)


class _FakeBot:
    """Owner + 2 human admins + 1 bot admin (4 total)."""

    def __init__(self):
        self.members = [
            _member("creator", _user(1, "Owner", "owner")),
            _member("administrator", _user(2, "Human One", "human1")),
            _member("administrator", _user(3, "Pi Helper", "pihelper", is_bot=True)),
            _member("administrator", _user(4, "Human Two", None)),
        ]

    async def get_chat_administrators(self, chat_id):
        return self.members


class _Msg(FakeMessage):
    """Command message: replies land in .calls / .sent_texts.

    ``bot.responses.reply_card`` still calls the PTB-era
    ``message.reply_text(...)`` shortcut, so route it exactly like
    ``bot.reply.reply_text`` does (group → reply, private → answer).
    """

    async def reply_text(self, text, **kw):
        if self.chat.type == "private":
            return await self.answer(text, **kw)
        return await self.reply(text, **kw)


def _cmd_msg(chat_type: str = "supergroup") -> _Msg:
    return _Msg("/admincount", chat_id=-100123, chat_type=chat_type,
                title="Test Group", user_id=42)


# ═════════════════════════════════════════════════════════════════
# /admincount
# ═════════════════════════════════════════════════════════════════

class TestAdminCount(unittest.IsolatedAsyncioTestCase):
    async def test_counts_owner_humans_bots_total(self):
        msg = _cmd_msg()
        await call(admin_mod.admin_count_command, msg, bot=_FakeBot())
        text = msg.sent_texts[-1]
        self.assertIn("Owner: 1", text)
        self.assertIn("Admins: 2", text)   # humans only
        self.assertIn("Bots: 1", text)     # bot admins counted
        self.assertIn("Total: 4", text)
        self.assertIn("Test Group", text)

    async def test_private_denied(self):
        msg = _cmd_msg(chat_type="private")
        await call(admin_mod.admin_count_command, msg, bot=_FakeBot())
        self.assertIn("groups", msg.sent_texts[-1])


# ═════════════════════════════════════════════════════════════════
# /adminlist
# ═════════════════════════════════════════════════════════════════

class TestAdminList(unittest.IsolatedAsyncioTestCase):
    async def test_sections_and_counts(self):
        msg = _cmd_msg()
        await call(admin_mod.adminlist_command, msg, bot=_FakeBot())
        text = msg.sent_texts[-1]
        # Header counts
        self.assertIn("Admins: 2", text)
        self.assertIn("Bots: 1", text)
        self.assertIn("Total: 4", text)
        # Sections
        self.assertIn("Owner:", text)
        self.assertIn("@owner", text)
        self.assertIn("Administrators:", text)
        self.assertIn("@human1", text)
        self.assertIn("Human Two", text)  # no username → name shown
        self.assertIn("Bots:", text)
        self.assertIn("@pihelper", text)

    async def test_bot_not_listed_as_regular_admin(self):
        msg = _cmd_msg()
        await call(admin_mod.adminlist_command, msg, bot=_FakeBot())
        text = msg.sent_texts[-1]
        admins_section = text.split("Administrators:")[1].split("Bots:")[0]
        self.assertNotIn("@pihelper", admins_section)

    async def test_private_denied(self):
        msg = _cmd_msg(chat_type="private")
        await call(admin_mod.adminlist_command, msg, bot=_FakeBot())
        self.assertIn("groups", msg.sent_texts[-1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
