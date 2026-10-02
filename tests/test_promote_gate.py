"""Tests for the /promote permission gate (bot/modules/admin.py).

Run from the repo root:

    python tests/test_promote_gate.py
    python -m unittest discover -s tests

Environment isolation (BEFORE any bot import):
    * BOT_TOKEN is forced — importing `bot` pulls bot.config, which
      exits without one.
    * LOCALAPPDATA points at a temp dir so runtime files are isolated.

No network: handlers run against fakes that record replies.

Gate contract (owner request): the group creator may always promote;
administrators only with the add-admins right; everyone else is ignored
in total silence (no reply at all).  A bot that itself lacks the right
replies with a friendly, actionable notice — never a raw error.
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
_TEST_DIR = tempfile.mkdtemp(prefix="pi_promote_test_")
os.environ["LOCALAPPDATA"] = _TEST_DIR
atexit.register(shutil.rmtree, _TEST_DIR, ignore_errors=True)

# ── Imports (after env) ───────────────────────────────────────────
from aiofakes import FakeBot, FakeMessage, call  # noqa: E402
from bot.modules import admin as admin_mod  # noqa: E402

CHAT_ID = -100777001
USER_ID = 42  # FakeMessage default sender
BOT_ID = 1     # FakeBot default id

# Denied callers get no reply at all; the bot-lacks-rights notice is
# asserted on its stable wording (emoji rendering varies by config).
BOT_RIGHTS_TEXT = "I can't promote users here."
BOT_RIGHTS_PERM = "Add Admins"


def _member(status: str, can_promote: bool | None = None):
    m = SimpleNamespace(status=status, user=SimpleNamespace(id=USER_ID))
    if can_promote is not None:
        m.can_promote_members = can_promote
    return m


def _msg() -> FakeMessage:
    return FakeMessage(text="/promote", chat_type="supergroup",
                       chat_id=CHAT_ID, user_id=USER_ID)


def _bot(member) -> FakeBot:
    bot = FakeBot()
    bot.chat_members[(CHAT_ID, USER_ID)] = member
    # Bot itself stays a plain member → gate passers stop at the
    # bot-rights check, which proves the gate was cleared.
    return bot


class TestCanPromote(unittest.IsolatedAsyncioTestCase):
    async def _gate(self, member):
        bot = _bot(member)
        return await admin_mod._can_promote(_msg(), bot)

    async def test_creator_always_can(self):
        self.assertTrue(await self._gate(_member("creator")))

    async def test_admin_with_right_can(self):
        self.assertTrue(
            await self._gate(_member("administrator", can_promote=True))
        )

    async def test_admin_without_right_cannot(self):
        self.assertFalse(
            await self._gate(_member("administrator", can_promote=False))
        )

    async def test_admin_missing_attr_cannot(self):
        # Duck members without the flag must not silently pass.
        self.assertFalse(await self._gate(_member("administrator")))

    async def test_plain_member_cannot(self):
        self.assertFalse(await self._gate(_member("member")))

    async def test_lookup_failure_cannot(self):
        bot = FakeBot()  # no entry → default member, but force a raise
        async def boom(chat_id, user_id):
            raise RuntimeError("network")
        bot.get_chat_member = boom  # type: ignore[method-assign]
        self.assertFalse(await admin_mod._can_promote(_msg(), bot))


class TestPromoteCommandGate(unittest.IsolatedAsyncioTestCase):
    async def test_admin_without_right_is_ignored_silently(self):
        msg = _msg()
        bot = _bot(_member("administrator", can_promote=False))
        await call(admin_mod.promote_command, msg, bot=bot, args=[])
        self.assertEqual(len(msg.calls), 0)

    async def test_member_is_ignored_silently(self):
        msg = _msg()
        bot = _bot(_member("member"))
        await call(admin_mod.promote_command, msg, bot=bot, args=[])
        self.assertEqual(len(msg.calls), 0)

    async def test_creator_passes_gate_to_bot_rights_check(self):
        msg = _msg()
        bot = _bot(_member("creator"))  # bot itself: plain member
        await call(admin_mod.promote_command, msg, bot=bot, args=[])
        self.assertEqual(len(msg.calls), 1)
        self.assertIn(BOT_RIGHTS_TEXT, msg.sent_texts[0])
        self.assertIn(BOT_RIGHTS_PERM, msg.sent_texts[0])

    async def test_promoted_admin_passes_gate_to_bot_rights_check(self):
        msg = _msg()
        bot = _bot(_member("administrator", can_promote=True))
        await call(admin_mod.promote_command, msg, bot=bot, args=[])
        self.assertIn(BOT_RIGHTS_TEXT, msg.sent_texts[0])
        self.assertIn(BOT_RIGHTS_PERM, msg.sent_texts[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
