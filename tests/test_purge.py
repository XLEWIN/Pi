"""Tests for /purge and /spurge (bot/modules/admin.py).

Both were carried over from boa2's ``Yumeko/modules/admin.py`` (Pyrogram)
and re-implemented for aiogram / the Bot API, in Pi's card style and with
only the owner's emoji.

Behaviour preserved from boa2
-----------------------------
* reply-only, supergroup-only;
* the range is ``reply.message_id .. command.message_id - 1`` (the command
  is excluded from the count and deleted separately);
* ``/purge`` announces the count, then removes the card and the command
  after ``_PURGE_CONFIRM_TTL`` seconds;
* ``/spurge`` is the silent twin — nothing announced, command gone;
* a refusal that would apply to the whole range (no delete rights) is
  reported instead of being swallowed.

Environment isolation (BEFORE any bot import):
    * BOT_TOKEN is forced — importing `bot` pulls bot.config, which
      exits without one.
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

# ── Environment isolation ── must precede bot imports ────────────────
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["BOT_TOKEN"] = "1:TEST-TOKEN-FOR-UNIT-TESTS"
_TEST_DIR = tempfile.mkdtemp(prefix="pi_purge_test_")
os.environ["LOCALAPPDATA"] = _TEST_DIR
atexit.register(shutil.rmtree, _TEST_DIR, ignore_errors=True)

# ── Imports (after env) ──────────────────────────────────────────────
import bot.pipeline as pipeline  # noqa: E402
from aiofakes import FakeBot, FakeMessage, call  # noqa: E402
from bot.modules import admin as admin_mod  # noqa: E402

CHAT_ID = -1007711021
USER_ID = 880001
BOT_ID = 1

CARD_TITLE = "Purge Complete"
RIGHTS_TEXT = "I can't delete messages here."


def _member(status: str, **kw):
    m = SimpleNamespace(status=status, user=SimpleNamespace(id=USER_ID), **kw)
    return m


def _bot(can_delete: bool = True, *, rights: bool = True) -> FakeBot:
    """Admin caller + a bot that may (or may not) delete messages."""
    bot = FakeBot()
    bot.chat_members[(CHAT_ID, USER_ID)] = _member("administrator")
    if rights:
        bot.chat_members[(CHAT_ID, BOT_ID)] = _member(
            "administrator", can_delete_messages=can_delete
        )
    return bot


def _reply_target(message_id: int = 10) -> FakeMessage:
    return FakeMessage(text="older message", chat_id=CHAT_ID,
                       chat_type="supergroup", message_id=message_id)


def _msg(reply=True, *, command: str = "/purge", chat_type: str = "supergroup",
         message_id: int = 50) -> FakeMessage:
    return FakeMessage(
        text=command, chat_id=CHAT_ID, chat_type=chat_type, user_id=USER_ID,
        message_id=message_id,
        reply_to_message=_reply_target() if reply else None,
    )


def _deletes(bot: FakeBot) -> list:
    return [e["delete"] for e in bot.sent if "delete" in e]


class _PurgeBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self._ttl = admin_mod._PURGE_CONFIRM_TTL
        admin_mod._PURGE_CONFIRM_TTL = 0     # never sleep in tests

    def tearDown(self):
        admin_mod._PURGE_CONFIRM_TTL = self._ttl
        super().tearDown()


# ════════════════════════════════════════════════════════════════════
# Guards
# ════════════════════════════════════════════════════════════════════

class TestPurgeGuards(_PurgeBase):

    async def test_without_a_reply_shows_usage(self):
        msg = _msg(reply=False)
        bot = _bot()
        await call(admin_mod.purge_command, msg, bot=bot)
        self.assertTrue(msg.sent_texts)
        self.assertIn("/purge", msg.sent_texts[-1])
        self.assertEqual(_deletes(bot), [])

    async def test_spurge_usage_mentions_its_own_name(self):
        msg = _msg(reply=False, command="/spurge")
        await call(admin_mod.spurge_command, msg, bot=_bot())
        self.assertTrue(msg.sent_texts)
        self.assertIn("/spurge", msg.sent_texts[-1])

    async def test_basic_group_is_refused(self):
        msg = _msg(chat_type="group")
        await call(admin_mod.purge_command, msg, bot=_bot())
        self.assertTrue(msg.sent_texts)
        self.assertIn("basic group", msg.sent_texts[-1])
        self.assertEqual(_deletes(_bot()), [])

    async def test_private_chat_is_refused(self):
        msg = _msg(chat_type="private")
        await call(admin_mod.purge_command, msg, bot=_bot())
        self.assertTrue(msg.sent_texts)
        self.assertIn("groups", msg.sent_texts[-1])

    async def test_non_admin_gets_nothing_at_all(self):
        """Consistent with the rest of the module: silently ignored."""
        msg = _msg()
        bot = FakeBot()      # no admin entries → caller is a plain member
        await call(admin_mod.purge_command, msg, bot=bot)
        self.assertEqual(msg.calls, [])
        self.assertEqual(bot.sent, [])

    async def test_bot_without_delete_rights_is_told_what_to_grant(self):
        msg = _msg()
        bot = _bot(rights=False)      # bot is not in an admin role
        await call(admin_mod.purge_command, msg, bot=bot)

        self.assertTrue(msg.sent_texts)
        self.assertIn(RIGHTS_TEXT, msg.sent_texts[-1])
        self.assertIn("Can Delete Messages", msg.sent_texts[-1])
        self.assertEqual(_deletes(bot), [])

    async def test_bot_admin_without_the_right_is_refused(self):
        msg = _msg()
        bot = _bot(can_delete=False)  # admin, but can_delete_messages off
        await call(admin_mod.purge_command, msg, bot=bot)

        self.assertTrue(msg.sent_texts)
        self.assertIn(RIGHTS_TEXT, msg.sent_texts[-1])
        self.assertEqual(_deletes(bot), [])


# ════════════════════════════════════════════════════════════════════
# /purge
# ════════════════════════════════════════════════════════════════════

class TestPurge(_PurgeBase):

    async def test_deletes_the_whole_range(self):
        msg = _msg()
        bot = _bot()
        await call(admin_mod.purge_command, msg, bot=bot)

        # reply id 10 .. command id 50-1  →  40 messages
        self.assertEqual(_deletes(bot), [(CHAT_ID, i) for i in range(10, 50)])

    async def test_reports_the_count_in_a_card(self):
        msg = _msg()
        bot = _bot()
        await call(admin_mod.purge_command, msg, bot=bot)

        self.assertEqual(len(msg.sent_texts), 1)
        text = msg.sent_texts[0]
        self.assertIn(CARD_TITLE, text)
        self.assertIn("40 messages", text)
        self.assertIn("PURGED BY", text)

    async def test_card_and_command_are_removed_after_the_ttl(self):
        msg = _msg()
        bot = _bot()
        await call(admin_mod.purge_command, msg, bot=bot)

        deletes = [c for c in msg.calls if c[0] == "delete"]
        self.assertTrue(deletes, "the command message survived the purge")

    async def test_singular_count(self):
        msg = _msg(message_id=11)          # range(10, 11) → exactly one
        bot = _bot()
        await call(admin_mod.purge_command, msg, bot=bot)

        self.assertEqual(_deletes(bot), [(CHAT_ID, 10)])
        self.assertIn("1 message", msg.sent_texts[0])
        self.assertNotIn("1 messages", msg.sent_texts[0])

    async def test_partial_range_reports_the_rights_failure(self):
        """A refusal that applies to the whole run is surfaced, not hidden."""
        msg = _msg(message_id=15)
        bot = _bot()
        calls = {"n": 0}
        original = bot.delete_message

        async def _flaky(chat_id, message_id):
            calls["n"] += 1
            if calls["n"] == 3:
                raise RuntimeError("Forbidden: bot can't delete messages")
            return await original(chat_id, message_id)

        bot.delete_message = _flaky
        await call(admin_mod.purge_command, msg, bot=bot)

        self.assertTrue(msg.sent_texts)
        self.assertIn(RIGHTS_TEXT, msg.sent_texts[-1])
        self.assertFalse(
            [t for t in msg.sent_texts if CARD_TITLE in t],
            "a failed purge must not claim success",
        )

    async def test_one_undeletable_message_does_not_abort_the_rest(self):
        """Too old / already gone → skipped, and the purge still reports."""
        msg = _msg(message_id=15)
        bot = _bot()
        original = bot.delete_message
        seen = []

        async def _flaky(chat_id, message_id):
            seen.append(message_id)
            if message_id == 12:
                raise RuntimeError("Bad Request: message to delete not found")
            return await original(chat_id, message_id)

        bot.delete_message = _flaky
        await call(admin_mod.purge_command, msg, bot=bot)

        self.assertEqual(seen, list(range(10, 15)))
        self.assertIn("4 messages", msg.sent_texts[0])


# ════════════════════════════════════════════════════════════════════
# /spurge  — the silent twin
# ════════════════════════════════════════════════════════════════════

class TestSpurge(_PurgeBase):

    async def test_deletes_the_same_range(self):
        msg = _msg(command="/spurge")
        bot = _bot()
        await call(admin_mod.spurge_command, msg, bot=bot)

        self.assertEqual(_deletes(bot), [(CHAT_ID, i) for i in range(10, 50)])

    async def test_announces_nothing(self):
        msg = _msg(command="/spurge")
        bot = _bot()
        await call(admin_mod.spurge_command, msg, bot=bot)

        self.assertEqual(msg.sent_texts, [], "spurge must stay silent")
        self.assertFalse([c for c in msg.calls if c[0] == "reply"])

    async def test_the_command_message_is_removed_too(self):
        msg = _msg(command="/spurge")
        bot = _bot()
        await call(admin_mod.spurge_command, msg, bot=bot)

        self.assertTrue([c for c in msg.calls if c[0] == "delete"])

    async def test_non_admin_gets_nothing(self):
        msg = _msg(command="/spurge")
        bot = FakeBot()
        await call(admin_mod.spurge_command, msg, bot=bot)
        self.assertEqual(msg.calls, [])
        self.assertEqual(bot.sent, [])


# ════════════════════════════════════════════════════════════════════
# Units
# ════════════════════════════════════════════════════════════════════

class TestPurgeUnits(unittest.TestCase):

    def test_purge_ids_span_reply_to_just_before_the_command(self):
        self.assertEqual(admin_mod._purge_ids(_msg()), list(range(10, 50)))

    def test_purge_ids_never_include_the_command_itself(self):
        self.assertNotIn(50, admin_mod._purge_ids(_msg()))

    def test_purge_ids_falls_back_when_the_reply_is_newer(self):
        msg = _msg(message_id=5)
        msg.reply_to_message = _reply_target(message_id=90)
        self.assertEqual(admin_mod._purge_ids(msg), [90])

    def test_fatal_delete_recognises_rights_refusals(self):
        self.assertTrue(admin_mod._fatal_delete(
            RuntimeError("Forbidden: bot is not a member")))
        self.assertTrue(admin_mod._fatal_delete(
            RuntimeError("Bad Request: message can't be deleted")))
        self.assertTrue(admin_mod._fatal_delete(
            RuntimeError("Bad Request: not enough rights")))

    def test_fatal_delete_ignores_per_message_failures(self):
        self.assertFalse(admin_mod._fatal_delete(
            RuntimeError("Bad Request: message to delete not found")))


class TestPurgeRegistration(unittest.TestCase):

    def test_setup_registers_both_commands(self):
        before = list(pipeline.entries())
        pipeline.clear()
        try:
            admin_mod.setup()
            names = {e.fn.__name__ for e in pipeline.entries()}
            self.assertIn("purge_command", names)
            self.assertIn("spurge_command", names)
        finally:
            pipeline.clear()
            for e in before:
                pipeline.on(e.event, e.fn, group=e.group, flt=e.flt)

    def test_module_exports_both_handlers(self):
        self.assertTrue(callable(admin_mod.purge_command))
        self.assertTrue(callable(admin_mod.spurge_command))


if __name__ == "__main__":
    unittest.main()
