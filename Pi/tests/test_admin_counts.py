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


class _FakeMessage:
    def __init__(self, chat_type: str = "supergroup"):
        self.chat = SimpleNamespace(
            id=-100123, type=chat_type, title="Test Group"
        )
        self.replies: list = []

    async def reply_text(self, text, parse_mode=None, reply_markup=None, **kw):
        self.replies.append({"text": text, "markup": reply_markup})

    @property
    def last(self):
        return self.replies[-1] if self.replies else None


def _cmd_update(chat_type: str = "supergroup"):
    msg = _FakeMessage(chat_type)
    return SimpleNamespace(
        message=msg,
        effective_message=msg,
        effective_chat=msg.chat,
        effective_user=_user(42, "Admin", "admin"),
    ), SimpleNamespace(bot=_FakeBot(), args=[])


# ═════════════════════════════════════════════════════════════════
# /admincount
# ═════════════════════════════════════════════════════════════════

class TestAdminCount(unittest.IsolatedAsyncioTestCase):
    async def test_counts_owner_humans_bots_total(self):
        upd, ctx = _cmd_update()
        await admin_mod.admin_count_command(upd, ctx)
        text = upd.message.last["text"]
        self.assertIn("Owner: 1", text)
        self.assertIn("Admins: 2", text)   # humans only
        self.assertIn("Bots: 1", text)     # bot admins counted
        self.assertIn("Total: 4", text)
        self.assertIn("Test Group", text)

    async def test_private_denied(self):
        upd, ctx = _cmd_update(chat_type="private")
        await admin_mod.admin_count_command(upd, ctx)
        self.assertIn("groups", upd.message.last["text"])


# ═════════════════════════════════════════════════════════════════
# /adminlist
# ═════════════════════════════════════════════════════════════════

class TestAdminList(unittest.IsolatedAsyncioTestCase):
    async def test_sections_and_counts(self):
        upd, ctx = _cmd_update()
        await admin_mod.adminlist_command(upd, ctx)
        text = upd.message.last["text"]
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
        upd, ctx = _cmd_update()
        await admin_mod.adminlist_command(upd, ctx)
        text = upd.message.last["text"]
        admins_section = text.split("Administrators:")[1].split("Bots:")[0]
        self.assertNotIn("@pihelper", admins_section)

    async def test_private_denied(self):
        upd, ctx = _cmd_update(chat_type="private")
        await admin_mod.adminlist_command(upd, ctx)
        self.assertIn("groups", upd.message.last["text"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
