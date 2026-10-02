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

import asyncio
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

# ── Environment isolation — must precede bot imports ──────────────
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["BOT_TOKEN"] = "1:TEST-TOKEN-FOR-UNIT-TESTS"
_TEST_DIR = tempfile.mkdtemp(prefix="pi_bcast_test_")
os.environ["LOCALAPPDATA"] = _TEST_DIR
atexit.register(shutil.rmtree, _TEST_DIR, ignore_errors=True)

# ── Imports (after env) ───────────────────────────────────────────
from aiogram.dispatcher.event.handler import FilterObject, HandlerObject  # noqa: E402
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter  # noqa: E402

from aiofakes import FakeMessage, call, command_filters, make_callback  # noqa: E402
from bot import pipeline  # noqa: E402
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
                              SimpleNamespace(sleep=_instant_sleep,
                                              to_thread=asyncio.to_thread)), \
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
            raise TelegramRetryAfter(
                SimpleNamespace(chat_id=chat_id), "Flood control", 0
            )
        if chat_id in self.fail_ids:
            raise TelegramAPIError(
                SimpleNamespace(chat_id=chat_id),
                f"Forbidden: cannot send to {chat_id}",
            )
        self.forwards.append(chat_id)
        return SimpleNamespace(message_id=9000 + len(self.forwards))


class _Msg(FakeMessage):
    """Command message — reply/answer returns itself, so the broadcast
    status card can be edited in place (every action lands in .calls)."""

    def __init__(self, reply: bool = True, *, user_id: int = OWNER_ID,
                 **kw) -> None:
        super().__init__(
            "/broadcast", user_id=user_id,
            reply_to_message=(
                SimpleNamespace(
                    chat=SimpleNamespace(id=-100999), message_id=4242
                ) if reply else None
            ),
            **kw,
        )

    async def answer(self, text, **kw):
        self.calls.append(("answer", text, kw))
        return self

    async def reply(self, text, **kw):
        self.calls.append(("reply", text, kw))
        return self


def _sends(msg):
    """[{"text", **kw}] for every ("answer"/"reply", …) record on msg."""
    return [{"text": t, **kw} for (k, t, kw) in msg.calls
            if k in ("answer", "reply")]


def _edits(msg):
    """[{"text", **kw}] for every ("edit_text", …) record on msg."""
    return [{"text": t, **kw} for (k, t, kw) in msg.calls if k == "edit_text"]


def _query(data: str, *, user_id=OWNER_ID, message=None):
    return make_callback(data, user_id=user_id, message=message)


def _matches(flt, data: str) -> bool:
    """True when a callback_query pipeline filter accepts ``data``."""
    handler = HandlerObject(callback=bm.broadcast_callback,
                            filters=[FilterObject(flt)])
    ok, _ = asyncio.run(handler.check(make_callback(data)))
    return ok


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
        msg = _Msg(user_id=99)
        bot = _FakeBot()
        with _owner(), _fake_db():
            await call(bm.broadcast_command, msg, bot=bot, args=[])
        self.assertIsNone(msg.last)
        self.assertEqual(bot.forwards, [])

    async def test_missing_reply_shows_usage(self):
        msg = _Msg(reply=False)
        with _owner(), _fake_db():
            await call(bm.broadcast_command, msg, bot=_FakeBot(), args=[])
        self.assertIn("Reply to a message", msg.last[1])
        self.assertIn("/broadcast -user", msg.last[1])
        self.assertIn("/broadcast -pin", msg.last[1])

    async def test_no_targets_reports_empty_db(self):
        msg = _Msg()
        with _owner(), _fake_db(groups=(), users=()):
            await call(bm.broadcast_command, msg, bot=_FakeBot(), args=[])
        self.assertIn("No broadcast targets", msg.last[1])

    async def test_unconfigured_owner_denies_everyone(self):
        msg = _Msg(user_id=0)
        with mock.patch.object(bm, "settings", SimpleNamespace(owner_id=0)), \
                _fake_db():
            await call(bm.broadcast_command, msg, bot=_FakeBot(), args=[])
        self.assertIsNone(msg.last)


# ═════════════════════════════════════════════════════════════════
# The broadcast run
# ═════════════════════════════════════════════════════════════════

class TestRun(_Base):
    async def test_all_reaches_groups_and_users(self):
        msg = _Msg()
        bot = _FakeBot()
        with _owner(), _fake_db(groups=(10, 20), users=(100, 200, 300)):
            await call(bm.broadcast_command, msg, bot=bot, args=[])
        self.assertEqual(sorted(bot.forwards), [10, 20, 100, 200, 300])
        self.assertIn("Broadcast In Progress", msg.sent_texts[0])
        final = _edits(msg)[-1]
        self.assertIn("Broadcast Completed", final["text"])
        self.assertIn("Users Reached: 3", final["text"])
        self.assertIn("Groups Reached: 2", final["text"])
        self.assertNotIn("reply_markup", final)   # buttons removed at end

    async def test_chat_target_skips_users(self):
        msg = _Msg()
        bot = _FakeBot()
        with _owner(), _fake_db(groups=(10, 20), users=(100, 200)):
            await call(bm.broadcast_command, msg, bot=bot, args=["-chat"])
        self.assertEqual(sorted(bot.forwards), [10, 20])
        self.assertIn("chats only", msg.sent_texts[0])

    async def test_user_target_skips_groups(self):
        msg = _Msg()
        bot = _FakeBot()
        with _owner(), _fake_db(groups=(10, 20), users=(100,)):
            await call(bm.broadcast_command, msg, bot=bot, args=["-user"])
        self.assertEqual(bot.forwards, [100])
        self.assertIn("users only", msg.sent_texts[0])

    async def test_pin_pins_each_group_exactly_once(self):
        msg = _Msg()
        bot = _FakeBot()
        bot.pin_chat_message = self._make_pinner(bot)
        with _owner(), _fake_db(groups=(10, 20), users=()):
            await call(bm.broadcast_command, msg, bot=bot,
                       args=["-chat", "-pin"])
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
            await call(bm.broadcast_command, msg, bot=bot, args=["-chat"])
        self.assertEqual(bot.forwards, [10, 30])
        self.assertIn("Groups Reached: 2", _edits(msg)[-1]["text"])

    async def test_floodwait_retries_same_chat(self):
        msg = _Msg()
        bot = _FakeBot(flood_once=(20,))
        with _owner(), _fake_db(groups=(10, 20), users=()):
            await call(bm.broadcast_command, msg, bot=bot, args=["-chat"])
        self.assertEqual(sorted(bot.forwards), [10, 20])
        self.assertIn("Groups Reached: 2", _edits(msg)[-1]["text"])

    async def test_dead_user_is_skipped(self):
        msg = _Msg()
        bot = _FakeBot(fail_ids=(200,))
        with _owner(), _fake_db(groups=(), users=(100, 200)):
            await call(bm.broadcast_command, msg, bot=bot, args=["-user"])
        self.assertEqual(bot.forwards, [100])
        self.assertIn("Users Reached: 1", _edits(msg)[-1]["text"])

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
            await call(bm.broadcast_command, msg, bot=bot, args=["-chat"])
        self.assertEqual(bot.forwards, [10])   # stopped after one
        self.assertIn("Broadcast Cancelled", _edits(msg)[-1]["text"])
        self.assertIn("Groups Reached: 1", _edits(msg)[-1]["text"])


# ═════════════════════════════════════════════════════════════════
# Cancel callback
# ═════════════════════════════════════════════════════════════════

class TestCancelCallback(_Base):
    async def test_non_owner_gets_silent_ack_and_flag_stays_false(self):
        q = _query("broadcast:cancel", user_id=99)
        await call(bm.broadcast_callback, q)
        self.assertFalse(q.answers[0]["text"])
        self.assertFalse(q.answers[0]["show_alert"])
        self.assertFalse(bm._cancel)

    async def test_owner_sets_flag_and_edits(self):
        q = _query("broadcast:cancel")
        with _owner():
            await call(bm.broadcast_callback, q)
        self.assertTrue(bm._cancel)
        self.assertIn("Broadcast Cancelled", _edits(q.message)[-1]["text"])
        self.assertIn("cancelled", q.answers[0]["text"].lower())

    async def test_unknown_action_flagged(self):
        q = _query("broadcast:bogus")
        with _owner():
            await call(bm.broadcast_callback, q)
        self.assertEqual(q.answers[-1]["text"], "Unknown option")


# ═════════════════════════════════════════════════════════════════
# Wiring
# ═════════════════════════════════════════════════════════════════

class TestWiring(unittest.TestCase):
    def test_setup_registers_command_and_callback(self):
        pipeline.clear()
        routes = bm.setup()
        self.assertEqual(routes, ["/broadcast", "/bcast"])
        entries = pipeline.snapshot()
        cmds = [e for e in entries if e.event == "message"]
        cbs = [e for e in entries if e.event == "callback_query"]
        self.assertEqual(len(cmds), 1)
        cmd_names = set()
        for flt in command_filters(cmds[0].flt):
            cmd_names.update(flt.commands)
        self.assertEqual(sorted(cmd_names), ["bcast", "broadcast"])
        self.assertEqual(len(cbs), 1)
        flt = cbs[0].flt                      # ^broadcast:
        self.assertTrue(_matches(flt, "broadcast:cancel"))
        self.assertFalse(_matches(flt, "xbroadcast:cancel"))
        self.assertFalse(_matches(flt, "other:cancel"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
