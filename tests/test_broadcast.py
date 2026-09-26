"""Tests for owner-only /broadcast (bot/modules/broadcast.py).

Run from the repo root:

    python tests/test_broadcast.py
    python -m unittest discover -s tests

Environment isolation (BEFORE any bot import):
    * BOT_TOKEN is forced — importing `bot` pulls bot.config, which
      exits without one.
    * LOCALAPPDATA points at a temp dir so runtime files are isolated.

No network: fake bots/messages; ``bm.asyncio.sleep`` stubbed so the
1.5 s pacing and FloodWait waits never slow the suite.
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from telegram.error import RetryAfter, TelegramError

# ── Environment isolation — must precede bot imports ──────────────
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["BOT_TOKEN"] = "1:TEST-TOKEN-FOR-UNIT-TESTS"
_TEST_DIR = tempfile.mkdtemp(prefix="pi_bcast_test_")
os.environ["LOCALAPPDATA"] = _TEST_DIR
atexit.register(shutil.rmtree, _TEST_DIR, ignore_errors=True)

# ── Imports (after env) ───────────────────────────────────────────
from bot.modules import broadcast as bm  # noqa: E402

OWNER_ID = 42


# ═════════════════════════════════════════════════════════════════
# Fakes + patches
# ═════════════════════════════════════════════════════════════════

async def _instant_sleep(seconds):
    return None


@contextmanager
def _owner():
    with mock.patch.object(bm, "settings", SimpleNamespace(owner_id=OWNER_ID)), \
            mock.patch.object(bm, "asyncio",
                              SimpleNamespace(sleep=_instant_sleep)), \
            mock.patch.object(bm, "_SEND_DELAY", 0):
        yield


def _db(groups=(10, 20, 30), users=(100, 200)):
    return SimpleNamespace(
        get_all_chat_ids=lambda: list(groups),
        get_all_user_ids=lambda: list(users),
    )


@contextmanager
def _fake_db(**kw):
    with mock.patch.object(bm, "db", _db(**kw)):
        yield


class _FakeBot:
    def __init__(self, fail_ids=(), flood_once=()):
        self.fail_ids = set(fail_ids)
        self.flood_once = set(flood_once)   # raises RetryAfter once, then works
        self.forwards: list = []
        self.pins: list = []

    async def forward_message(self, chat_id, from_chat_id, message_id):
        if chat_id in self.flood_once:
            self.flood_once.discard(chat_id)
            raise RetryAfter(0)
        if chat_id in self.fail_ids:
            raise TelegramError(f"Forbidden: cannot send to {chat_id}")
        self.forwards.append(chat_id)
        return SimpleNamespace(message_id=9000 + len(self.forwards))


class _Sent:
    def __init__(self):
        self.edits: list = []

    async def edit_text(self, text, **kw):
        self.edits.append({"text": text, **kw})
        return self

    @property
    def last(self):
        return self.edits[-1] if self.edits else None


class _Msg:
    def __init__(self, reply=True) -> None:
        self.text = "/broadcast"
        self.replies: list = []
        self.sent: _Sent | None = None
        self.reply_to_message = (
            SimpleNamespace(
                chat=SimpleNamespace(id=-100999), message_id=4242
            ) if reply else None
        )

    async def reply_text(self, text, **kw):
        self.replies.append({"text": text, **kw})
        self.sent = _Sent()
        return self.sent

    @property
    def last(self):
        return self.replies[-1] if self.replies else None


class _Query:
    def __init__(self, data: str, user_id=OWNER_ID, message=None) -> None:
        self.data = data
        self.from_user = (
            None if user_id is None
            else SimpleNamespace(id=user_id, username=None, first_name="T")
        )
        self.message = message or _Sent()
        self.answers: list = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append({"text": text, "show_alert": show_alert})


class _Ctx:
    def __init__(self, bot, args=()):
        self.bot = bot
        self.bot_data: dict = {}
        self.args = list(args)


def _update(msg, user_id=OWNER_ID):
    user = None if user_id is None else SimpleNamespace(
        id=user_id, username=None, first_name="T"
    )
    return SimpleNamespace(effective_message=msg, effective_user=user,
                           message=msg)


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bm._cancel = False

    def tearDown(self):
        bm._cancel = False


# ═════════════════════════════════════════════════════════════════
# Gates
# ═════════════════════════════════════════════════════════════════

class TestGates(_Base):
    async def test_non_owner_denied_without_forwarding(self):
        msg = _Msg()
        bot = _FakeBot()
        with _owner(), _fake_db():
            await bm.broadcast_command(_update(msg, user_id=99), _Ctx(bot))
        self.assertIn("Only the bot owner", msg.last["text"])
        self.assertEqual(bot.forwards, [])

    async def test_missing_reply_shows_usage(self):
        msg = _Msg(reply=False)
        with _owner(), _fake_db():
            await bm.broadcast_command(_update(msg), _Ctx(_FakeBot()))
        self.assertIn("Reply to a message", msg.last["text"])
        self.assertIn("/broadcast -user", msg.last["text"])
        self.assertIn("/broadcast -pin", msg.last["text"])

    async def test_no_targets_reports_empty_db(self):
        msg = _Msg()
        with _owner(), _fake_db(groups=(), users=()):
            await bm.broadcast_command(_update(msg), _Ctx(_FakeBot()))
        self.assertIn("No broadcast targets", msg.last["text"])

    async def test_unconfigured_owner_denies_everyone(self):
        msg = _Msg()
        with mock.patch.object(bm, "settings", SimpleNamespace(owner_id=0)), \
                _fake_db():
            await bm.broadcast_command(_update(msg, user_id=0),
                                       _Ctx(_FakeBot()))
        self.assertIn("Only the bot owner", msg.last["text"])


# ═════════════════════════════════════════════════════════════════
# The broadcast run
# ═════════════════════════════════════════════════════════════════

class TestRun(_Base):
    async def test_all_reaches_groups_and_users(self):
        msg = _Msg()
        bot = _FakeBot()
        with _owner(), _fake_db(groups=(10, 20), users=(100, 200, 300)):
            await bm.broadcast_command(_update(msg), _Ctx(bot))
        self.assertEqual(sorted(bot.forwards), [10, 20, 100, 200, 300])
        self.assertIn("Broadcast In Progress", msg.replies[0]["text"])
        final = msg.sent.last
        self.assertIn("Broadcast Completed", final["text"])
        self.assertIn("Users Reached: 3", final["text"])
        self.assertIn("Groups Reached: 2", final["text"])
        self.assertNotIn("reply_markup", final)   # buttons removed at end

    async def test_chat_target_skips_users(self):
        msg = _Msg()
        bot = _FakeBot()
        with _owner(), _fake_db(groups=(10, 20), users=(100, 200)):
            await bm.broadcast_command(_update(msg), _Ctx(bot, ["-chat"]))
        self.assertEqual(sorted(bot.forwards), [10, 20])
        self.assertIn("chats only", msg.replies[0]["text"])

    async def test_user_target_skips_groups(self):
        msg = _Msg()
        bot = _FakeBot()
        with _owner(), _fake_db(groups=(10, 20), users=(100,)):
            await bm.broadcast_command(_update(msg), _Ctx(bot, ["-user"]))
        self.assertEqual(bot.forwards, [100])
        self.assertIn("users only", msg.replies[0]["text"])

    async def test_pin_pins_each_group_exactly_once(self):
        msg = _Msg()
        bot = _FakeBot()
        bot.pin_chat_message = self._make_pinner(bot)
        with _owner(), _fake_db(groups=(10, 20), users=()):
            await bm.broadcast_command(_update(msg),
                                       _Ctx(bot, ["-chat", "-pin"]))
        # one forward per group (no double-send) + one pin each
        self.assertEqual(bot.forwards, [10, 20])
        self.assertEqual([p[0] for p in bot.pins], [10, 20])

    def _make_pinner(self, bot):
        async def pin_chat_message(chat_id, message_id,
                                   disable_notification=False):
            bot.pins.append((chat_id, message_id, disable_notification))
        return pin_chat_message

    async def test_dead_chat_is_skipped(self):
        msg = _Msg()
        bot = _FakeBot(fail_ids=(20,))
        with _owner(), _fake_db(groups=(10, 20, 30), users=()):
            await bm.broadcast_command(_update(msg), _Ctx(bot, ["-chat"]))
        self.assertEqual(bot.forwards, [10, 30])
        self.assertIn("Groups Reached: 2", msg.sent.last["text"])

    async def test_floodwait_retries_same_chat(self):
        msg = _Msg()
        bot = _FakeBot(flood_once=(20,))
        with _owner(), _fake_db(groups=(10, 20), users=()):
            await bm.broadcast_command(_update(msg), _Ctx(bot, ["-chat"]))
        self.assertEqual(sorted(bot.forwards), [10, 20])
        self.assertIn("Groups Reached: 2", msg.sent.last["text"])

    async def test_dead_user_is_skipped(self):
        msg = _Msg()
        bot = _FakeBot(fail_ids=(200,))
        with _owner(), _fake_db(groups=(), users=(100, 200)):
            await bm.broadcast_command(_update(msg), _Ctx(bot, ["-user"]))
        self.assertEqual(bot.forwards, [100])
        self.assertIn("Users Reached: 1", msg.sent.last["text"])

    async def test_cancel_flag_stops_loop_and_reports_cancelled(self):
        msg = _Msg()
        bot = _FakeBot()

        # Flip the flag after the first forward lands (mid-run cancel).
        original = bot.forward_message

        async def flipping(chat_id, from_chat_id, message_id):
            sent = await original(chat_id, from_chat_id, message_id)
            bm._cancel = True
            return sent

        bot.forward_message = flipping
        with _owner(), _fake_db(groups=(10, 20, 30), users=(100,)):
            await bm.broadcast_command(_update(msg), _Ctx(bot, ["-chat"]))
        self.assertEqual(bot.forwards, [10])   # stopped after one
        self.assertIn("Broadcast Cancelled", msg.sent.last["text"])
        self.assertIn("Groups Reached: 1", msg.sent.last["text"])


# ═════════════════════════════════════════════════════════════════
# Cancel callback
# ═════════════════════════════════════════════════════════════════

class TestCancelCallback(_Base):
    async def test_non_owner_gets_alert_and_flag_stays_false(self):
        q = _Query("broadcast:cancel", user_id=99)
        await bm.broadcast_callback(
            SimpleNamespace(callback_query=q), _Ctx(_FakeBot())
        )
        self.assertTrue(q.answers[0]["show_alert"])
        self.assertIn("Only the bot owner", q.answers[0]["text"])
        self.assertFalse(bm._cancel)

    async def test_owner_sets_flag_and_edits(self):
        q = _Query("broadcast:cancel")
        with _owner():
            await bm.broadcast_callback(
                SimpleNamespace(callback_query=q), _Ctx(_FakeBot())
            )
        self.assertTrue(bm._cancel)
        self.assertIn("Broadcast Cancelled", q.message.last["text"])
        self.assertIn("cancelled", q.answers[0]["text"].lower())

    async def test_unknown_action_flagged(self):
        q = _Query("broadcast:bogus")
        with _owner():
            await bm.broadcast_callback(
                SimpleNamespace(callback_query=q), _Ctx(_FakeBot())
            )
        self.assertEqual(q.answers[-1]["text"], "Unknown option")


# ═════════════════════════════════════════════════════════════════
# Wiring
# ═════════════════════════════════════════════════════════════════

class TestWiring(unittest.TestCase):
    def test_setup_registers_command_and_callback(self):
        from telegram.ext import (CallbackQueryHandler,
                                  CommandHandler as PTBCommandHandler)

        class _App:
            def __init__(self):
                self.handlers = []

            def add_handler(self, handler, group=0):
                self.handlers.append(handler)

        app = _App()
        routes = bm.setup(app)
        self.assertEqual(routes, ["/broadcast", "/bcast"])
        cmds = [h for h in app.handlers
                if isinstance(h, PTBCommandHandler)]
        cbs = [h for h in app.handlers
               if isinstance(h, CallbackQueryHandler)]
        self.assertEqual(sorted(cmds[0].commands), ["bcast", "broadcast"])
        self.assertEqual(cbs[0].pattern.pattern, "^broadcast:")


if __name__ == "__main__":
    unittest.main(verbosity=2)
