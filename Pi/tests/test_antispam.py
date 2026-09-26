"""Tests for anti-flood (bot/modules/antispam.py) — 5 msgs / 3 s window.

Run from the Pi/Pi root:

    python tests/test_antispam.py
    python -m unittest discover -s tests

Covers: window mechanics, escalation (5 → 10 → 20 min, capped),
IST-midnight offence reset, blocked users skipped by counting, the
warning message format, and sudo-only /free.

Environment isolation (BEFORE any bot import):
    * BOT_TOKEN is forced — importing `bot` pulls bot.config, which
      exits without one.
    * LOCALAPPDATA points at a temp dir so the SQLite DB is isolated.
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

# ── Environment isolation — must precede bot imports ──────────────
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["BOT_TOKEN"] = "1:TEST-TOKEN-FOR-UNIT-TESTS"
_TEST_DIR = tempfile.mkdtemp(prefix="pi_antispam_test_")
os.environ["LOCALAPPDATA"] = _TEST_DIR
atexit.register(shutil.rmtree, _TEST_DIR, ignore_errors=True)

# ── Imports (after env) ───────────────────────────────────────────
from telegram.ext import MessageHandler as PTBMessageHandler  # noqa: E402

from bot.database import db  # noqa: E402
from bot.emojis import E  # noqa: E402
from bot.modules import antispam as ap  # noqa: E402
from bot.modules.bans import OWNER_ID  # noqa: E402
from bot.timeutils import ist_date  # noqa: E402

CHAT_S = -100777001
USER_S = 99777001        # the flooder
USER_OTHER = 99777002    # innocent bystander
USER_CLEAN = 99777003    # /free target with no history
USER_NOSUDO = 99777099   # random non-sudo sender
_TOMORROW = "2999-12-31T00:00:00+00:00"
_PAST = "2000-01-01T00:00:00+00:00"


# ═════════════════════════════════════════════════════════════════
# Fakes
# ═════════════════════════════════════════════════════════════════

class _Msg:
    """Message fake — records reply_text calls."""

    def __init__(self, text: str = "hi") -> None:
        self.text = text
        self.reply_to_message = None
        self.replies: list = []

    async def reply_text(self, text, **kw):
        self.replies.append({"text": text, **kw})
        return SimpleNamespace(message_id=1)

    @property
    def last(self):
        return self.replies[-1] if self.replies else None


def _update(
    text: str = "hi",
    *,
    chat_id: int = CHAT_S,
    chat_type: str = "supergroup",
    user_id: int = USER_S,
    is_bot: bool = False,
    first_name: str = "Spammer",
    username: str | None = "spammer",
    message=None,
):
    msg = message if message is not None else _Msg(text)
    chat = SimpleNamespace(id=chat_id, type=chat_type, title="S")
    user = SimpleNamespace(
        id=user_id, is_bot=is_bot, first_name=first_name, username=username
    )
    return SimpleNamespace(
        message=msg, effective_message=msg, effective_chat=chat, effective_user=user
    )


def _ctx(args=None):
    return SimpleNamespace(args=args or [])


def _cleanup() -> None:
    conn = db.connection
    for tbl, col, ids in (
        ("spam_protection", "user_id", (USER_S, USER_OTHER, USER_CLEAN, USER_NOSUDO)),
        ("daily_messages", "user_id", (USER_S, USER_OTHER, USER_CLEAN)),
        ("users", "user_id", (USER_S, USER_OTHER, USER_CLEAN)),
    ):
        conn.execute(
            f"DELETE FROM {tbl} WHERE {col} IN ({','.join('?' * len(ids))})", ids
        )
    conn.commit()


def _unblock(user_id: int) -> None:
    db.connection.execute(
        "UPDATE spam_protection SET blocked_until = ? WHERE user_id = ?",
        (_PAST, user_id),
    )
    db.connection.commit()


def _set_offence_day(user_id: int, day: str) -> None:
    db.connection.execute(
        "UPDATE spam_protection SET offence_day = ? WHERE user_id = ?",
        (day, user_id),
    )
    db.connection.commit()


async def _flood(user_id=USER_S, chat_id=CHAT_S, n=5, **user_kw):
    """Send n messages through flood_watch; return the reply-bearing msgs."""
    got = []
    for _ in range(n):
        msg = _Msg("spam spam spam")
        await ap.flood_watch(
            _update(message=msg, chat_id=chat_id, user_id=user_id, **user_kw),
            None,
        )
        if msg.replies:
            got.append(msg)
    return got


# ═════════════════════════════════════════════════════════════════
# Window mechanics (pure — no DB)
# ═════════════════════════════════════════════════════════════════

class TestWindow(unittest.TestCase):
    def setUp(self):
        ap.reset_windows()

    def tearDown(self):
        ap.reset_windows()

    def test_five_within_three_seconds_triggers(self):
        base = 1000.0
        results = [
            ap._track(CHAT_S, USER_S, base + i * 0.5) for i in range(5)
        ]
        self.assertEqual(results, [False, False, False, False, True])

    def test_four_messages_do_not_trigger(self):
        base = 2000.0
        results = [ap._track(CHAT_S, USER_S, base + i * 0.6) for i in range(4)]
        self.assertEqual(results, [False, False, False, False])

    def test_window_slides_out_after_three_seconds(self):
        base = 3000.0
        for i in range(4):                      # 4 msgs, then a 3s gap
            ap._track(CHAT_S, USER_S, base + i * 0.3)
        self.assertFalse(ap._track(CHAT_S, USER_S, base + 4.0))   # aged out (1 left)
        self.assertFalse(ap._track(CHAT_S, USER_S, base + 4.2))
        self.assertFalse(ap._track(CHAT_S, USER_S, base + 4.4))
        self.assertFalse(ap._track(CHAT_S, USER_S, base + 4.6))   # 4 in window
        self.assertTrue(ap._track(CHAT_S, USER_S, base + 4.8))    # 5th → flood

    def test_keys_are_independent(self):
        base = 4000.0
        for i in range(4):
            self.assertFalse(ap._track(CHAT_S, USER_S, base + i * 0.2))
        # another chat and another user are unaffected
        self.assertFalse(ap._track(CHAT_S + 1, USER_S, base + 0.8))
        self.assertFalse(ap._track(CHAT_S, USER_OTHER, base + 0.9))
        self.assertTrue(ap._track(CHAT_S, USER_S, base + 1.0))    # original floods

    def test_trigger_clears_the_window(self):
        base = 5000.0
        for i in range(4):
            ap._track(CHAT_S, USER_S, base + i * 0.2)
        self.assertTrue(ap._track(CHAT_S, USER_S, base + 0.9))
        # window was cleared — a fresh message starts over at 1/5
        self.assertFalse(ap._track(CHAT_S, USER_S, base + 1.1))

    def test_reset_windows(self):
        for i in range(4):
            ap._track(CHAT_S, USER_S, 6000.0 + i * 0.2)
        ap.reset_windows()
        self.assertFalse(ap._track(CHAT_S, USER_S, 6001.0))


class TestEscalation(unittest.TestCase):
    def test_minutes_table(self):
        self.assertEqual(ap.block_minutes(1), 5)
        self.assertEqual(ap.block_minutes(2), 10)
        self.assertEqual(ap.block_minutes(3), 20)
        self.assertEqual(ap.block_minutes(4), 20, "capped at 20")
        self.assertEqual(ap.block_minutes(99), 20)


class TestWarningFormat(unittest.TestCase):
    def test_flood_text_matches_pi_style(self):
        user = SimpleNamespace(
            id=USER_S, username="spammer", first_name="Spammer"
        )
        text = ap.flood_text(user, 5)
        self.assertIn("is flooding: blocked for 5 minutes for using the bot.", text)
        self.assertIn("tg://user?id=", text)              # tagged mention
        self.assertIn("<tg-emoji", text)                  # owner's custom ❌
        self.assertNotIn("\u26d4", text)                  # no plain ⛔


# ═════════════════════════════════════════════════════════════════
# Handler — blocking + escalation
# ═════════════════════════════════════════════════════════════════

class TestFloodHandler(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _cleanup()
        ap.reset_windows()

    def tearDown(self):
        _cleanup()
        ap.reset_windows()

    async def test_fifth_message_warns_and_blocks(self):
        got = await _flood()
        self.assertEqual(len(got), 1, "warning exactly once")
        text = got[0].last["text"]
        self.assertIn(
            "is flooding: blocked for 5 minutes for using the bot.", text
        )
        self.assertEqual(got[0].last.get("parse_mode"), "HTML")
        self.assertTrue(db.is_spam_blocked(USER_S))

    async def test_escalates_5_10_20_within_same_day(self):
        for expected in (5, 10, 20, 20):
            got = await _flood()
            self.assertEqual(len(got), 1)
            self.assertIn(
                f"blocked for {expected} minutes for using the bot.",
                got[0].last["text"],
            )
            _unblock(USER_S)
            ap.reset_windows()
        self.assertEqual(db.spam_get(USER_S)["offences"], 4)

    async def test_offences_reset_at_ist_midnight(self):
        await _flood()
        self.assertEqual(db.spam_get(USER_S)["offences"], 1)
        _set_offence_day(USER_S, (date.fromisoformat(ist_date())
                                  - timedelta(days=1)).isoformat())
        _unblock(USER_S)
        ap.reset_windows()
        got = await _flood()
        self.assertIn("blocked for 5 minutes", got[0].last["text"],
                      "new IST day = first offence again")

    async def test_blocked_user_gains_no_new_offence(self):
        await _flood()
        self.assertTrue(db.is_spam_blocked(USER_S))
        got = await _flood()      # still blocked — messages ignored
        self.assertEqual(got, [], "no warnings while already blocked")
        self.assertEqual(db.spam_get(USER_S)["offences"], 1)

    async def test_skips_private_and_bots(self):
        self.assertEqual(await _flood(chat_type="private"), [])
        self.assertEqual(await _flood(is_bot=True), [])
        self.assertFalse(db.is_spam_blocked(USER_S))

    async def test_block_does_not_affect_others(self):
        await _flood()
        self.assertFalse(db.is_spam_blocked(USER_OTHER))


# ═════════════════════════════════════════════════════════════════
# /free — sudo only
# ═════════════════════════════════════════════════════════════════

class TestFreeCommand(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _cleanup()

    def tearDown(self):
        _cleanup()

    def _block(self, user_id=USER_S):
        db.spam_bump_offence(user_id, ist_date())
        db.spam_bump_offence(user_id, ist_date())
        db.spam_set_block(user_id, _TOMORROW)

    async def test_denied_for_non_sudo(self):
        msg = _Msg("/free")
        await ap.free_command(
            _update(message=msg, user_id=USER_NOSUDO), _ctx([str(USER_S)])
        )
        self.assertIn("Only sudo/owner users", msg.last["text"])

    async def test_reply_target_clears_everything(self):
        self._block()
        msg = _Msg("/free")
        msg.reply_to_message = SimpleNamespace(
            from_user=SimpleNamespace(
                id=USER_S, username="spammer", first_name="Spammer"
            )
        )
        await ap.free_command(_update(message=msg, user_id=OWNER_ID), _ctx())
        self.assertIn("is free", msg.last["text"])
        self.assertIn("warnings and block cleared", msg.last["text"])
        self.assertIsNone(db.spam_get(USER_S))
        self.assertFalse(db.is_spam_blocked(USER_S))

    async def test_username_arg(self):
        db.add_user(USER_S, "spammer", "Spam", None)
        self._block()
        msg = _Msg("/free")
        await ap.free_command(
            _update(message=msg, user_id=OWNER_ID), _ctx(["@spammer"])
        )
        self.assertIn("is free", msg.last["text"])
        self.assertIsNone(db.spam_get(USER_S))

    async def test_numeric_id_arg(self):
        self._block()
        msg = _Msg("/free")
        await ap.free_command(
            _update(message=msg, user_id=OWNER_ID), _ctx([str(USER_S)])
        )
        self.assertIn("is free", msg.last["text"])
        self.assertIsNone(db.spam_get(USER_S))

    async def test_usage_error_without_target(self):
        msg = _Msg("/free")
        await ap.free_command(_update(message=msg, user_id=OWNER_ID), _ctx())
        self.assertIn("Usage:", msg.last["text"])

    async def test_nothing_to_clear(self):
        msg = _Msg("/free")
        await ap.free_command(
            _update(message=msg, user_id=OWNER_ID), _ctx([str(USER_CLEAN)])
        )
        self.assertIn("no active warnings or block", msg.last["text"])


# ═════════════════════════════════════════════════════════════════
# Wiring
# ═════════════════════════════════════════════════════════════════

class TestWiring(unittest.TestCase):
    def test_setup_registers_flood_watch_and_free(self):
        from bot.command_handler import CommandHandler as PiCommandHandler

        class _App:
            def __init__(self):
                self.handlers = []

            def add_handler(self, handler, group=0):
                self.handlers.append(handler)

        app = _App()
        routes = ap.setup(app)
        self.assertIn("/free", routes)
        cmd_names = set()
        n_msg = 0
        for h in app.handlers:
            if isinstance(h, PiCommandHandler):
                cmd_names.update(h.commands)
            elif isinstance(h, PTBMessageHandler):
                n_msg += 1
        self.assertEqual(cmd_names, {"free"})
        self.assertEqual(n_msg, 1)

    def test_flood_watch_filter_matches_group_text_only(self):
        class _App:
            def __init__(self):
                self.handlers = []

            def add_handler(self, handler, group=0):
                self.handlers.append(handler)

        app = _App()
        ap.setup(app)
        mh = next(h for h in app.handlers if isinstance(h, PTBMessageHandler))
        check = mh.filters.check_update
        from telegram import Chat, Message, Update
        from datetime import datetime, timezone

        def _real(text, chat_type="supergroup"):
            m = Message(
                message_id=1,
                date=datetime(2026, 1, 1, tzinfo=timezone.utc),
                chat=Chat(id=CHAT_S, type=chat_type),
                text=text,
            )
            m._bot = SimpleNamespace(username="PiModulerBot")
            return Update(update_id=1, message=m)

        self.assertTrue(check(_real("flood me")))
        self.assertFalse(check(_real("/free")))
        self.assertFalse(check(_real("hi", chat_type="private")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
