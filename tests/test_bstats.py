"""Tests for /bstats + /ping (bot/modules/bstats.py).

Run from the repo root:

    python tests/test_bstats.py
    python -m unittest discover -s tests

Environment isolation (BEFORE any bot import):
    * BOT_TOKEN is forced — importing `bot` pulls bot.config, which
      exits without one.
    * LOCALAPPDATA points at a temp dir so runtime files are isolated.

No network: fake messages/queries; bm.db and bm.moderation patched.
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

# ── Environment isolation — must precede bot imports ──────────────
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["BOT_TOKEN"] = "1:TEST-TOKEN-FOR-UNIT-TESTS"
_TEST_DIR = tempfile.mkdtemp(prefix="pi_bstats_test_")
os.environ["LOCALAPPDATA"] = _TEST_DIR
atexit.register(shutil.rmtree, _TEST_DIR, ignore_errors=True)

# ── Imports (after env) ───────────────────────────────────────────
from bot.constants import BOT_START_TIME, HELP_MENU  # noqa: E402
from bot.emojis import E, EID  # noqa: E402
from bot.modules import bstats as bm  # noqa: E402

OWNER_ID = 42


# ═════════════════════════════════════════════════════════════════
# Fakes + patches
# ═════════════════════════════════════════════════════════════════

def _db(users=51, chats=1, sudos=2, filters_=0, locks=(0, 0),
        gbanned=7, gmuted=0):
    return SimpleNamespace(
        get_user_count=lambda: users,
        get_group_count=lambda: chats,
        get_sudo_users=lambda: list(range(sudos)),
        count_filters=lambda: filters_,
        get_lock_stats=lambda: locks,
        get_gbanned_users=lambda: [{"user_id": i} for i in range(gbanned)],
        count_gmuted=lambda: gmuted,
    )


@contextmanager
def _owner():
    with mock.patch.object(bm, "settings", SimpleNamespace(owner_id=OWNER_ID)):
        yield


@contextmanager
def _fake_db(rules=None, **kw):
    rules_db = {1: {"text": "be kind"}, 2: {}} if rules is None else rules
    with mock.patch.object(bm, "db", _db(**kw)), \
            mock.patch.object(bm, "moderation",
                              SimpleNamespace(rules_db=rules_db)):
        yield


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
    def __init__(self, text="/bstats") -> None:
        self.text = text
        self.replies: list = []
        self.sent: _Sent | None = None

    async def reply_text(self, text, **kw):
        self.replies.append({"text": text, **kw})
        self.sent = _Sent()
        return self.sent

    @property
    def last(self):
        return self.replies[-1] if self.replies else None


class _DelMsg:
    """Fake message for the Close path — ``query.message.delete()``."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.deleted = False

    async def delete(self):
        if self.fail:
            raise RuntimeError("message is too old")
        self.deleted = True


class _Query:
    def __init__(self, data: str, user_id=OWNER_ID, message=None) -> None:
        self.data = data
        self.from_user = (
            None if user_id is None
            else SimpleNamespace(id=user_id, username=None, first_name="T")
        )
        self.message = message if message is not None else _DelMsg()
        self.answers: list = []
        self.edits: list = []
        self.reply_markup_stripped = False

    async def answer(self, text=None, show_alert=False):
        self.answers.append({"text": text, "show_alert": show_alert})

    async def edit_message_text(self, text, **kw):
        self.edits.append({"text": text, **kw})
        return True

    async def edit_message_reply_markup(self, reply_markup=None):
        self.reply_markup_stripped = True

    @property
    def last_edit(self):
        return self.edits[-1] if self.edits else None


def _update(msg, user_id=OWNER_ID):
    user = None if user_id is None else SimpleNamespace(
        id=user_id, username=None, first_name="T"
    )
    return SimpleNamespace(effective_message=msg, effective_user=user,
                           message=msg)


def _cb_update(query):
    return SimpleNamespace(callback_query=query)


class _FakeBot:
    async def get_me(self):
        return SimpleNamespace(id=1, username="pi_bot")


class _Ctx:
    def __init__(self, bot=None):
        self.bot = bot if bot is not None else _FakeBot()
        self.bot_data: dict = {}
        self.args: list = []


# ═════════════════════════════════════════════════════════════════
# Uptime / started-at
# ═════════════════════════════════════════════════════════════════

class TestFmtUptime(unittest.TestCase):
    def test_sample_value(self):
        # 1d 1h 28m 10s — the owner's example
        self.assertEqual(bm._fmt_uptime(86400 + 3600 + 28 * 60 + 10),
                         "1d 1h 28m 10s")

    def test_edges(self):
        self.assertEqual(bm._fmt_uptime(0), "0s")
        self.assertEqual(bm._fmt_uptime(5), "5s")
        self.assertEqual(bm._fmt_uptime(61), "1m 1s")
        self.assertEqual(bm._fmt_uptime(3661), "1h 1m 1s")
        self.assertEqual(bm._fmt_uptime(-3), "0s")

    def test_started_at_format(self):
        # "2026-09-25 18:38:03 UTC"
        self.assertRegex(
            bm._STARTED_AT,
            r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC$",
        )
        self.assertGreater(BOT_START_TIME, 0)


# ═════════════════════════════════════════════════════════════════
# Card text
# ═════════════════════════════════════════════════════════════════

class TestBstatsCard(unittest.TestCase):
    def test_exact_labels_from_sample(self):
        """Every label + value from the owner's sample, E.* icons only."""
        got = bm.bstats_text({
            "users": 51, "chats": 1, "sudos": 2, "filters": 0, "rules": 0,
            "lock_chats": 0, "locks": 0, "gbanned": 7, "gmuted": 0,
            "uptime": "1d 1h 28m 10s", "started_at": "2026-09-25 18:38:03 UTC",
        })
        expected = (
            f"{E.SAVE} Database Stats\n"
            f"├ {E.USER} Total Users: 51\n"
            f"├ {E.ANNOUNCE} Total Chats: 1\n"
            f"├ {E.CROWN} Total Sudos: 2\n"
            f"├ {E.EYES} Total Filters: 0\n"
            f"├ {E.BOOKMARK} Total Rules: 0\n"
            f"├ {E.SETTINGS} Lock Stats:\n"
            "   - Chats with Locks: 0\n"
            "   - Total Locks: 0\n"
            f"├ {E.BAN} GBanned Users: 7\n"
            f"└ {E.MUTE} GMuted Users: 0\n"
            "\n"
            f"{E.TIME} Time Details\n"
            f"├ {E.CLOCK} Uptime: 1d 1h 28m 10s\n"
            f"└ {E.NEW} Started At: 2026-09-25 18:38:03 UTC"
        )
        self.assertEqual(got, expected)

    def test_no_stock_sample_emojis(self):
        """The boa stock icons are replaced by the owner's E.* set."""
        got = bm.bstats_text({
            "users": 1, "chats": 1, "sudos": 0, "filters": 0, "rules": 0,
            "lock_chats": 0, "locks": 0, "gbanned": 0, "gmuted": 0,
            "uptime": "0s", "started_at": "x",
        })
        for stock in ("\U0001f3b5", "\U0001f50d", "\U0001f4dc",
                      "\U0001f510", "\U0001f6ab", "\U0001f507"):
            self.assertNotIn(stock, got)

    def test_uses_custom_emoji_markup(self):
        got = bm.bstats_text({
            "users": 1, "chats": 1, "sudos": 0, "filters": 0, "rules": 0,
            "lock_chats": 0, "locks": 0, "gbanned": 0, "gmuted": 0,
            "uptime": "0s", "started_at": "x",
        })
        self.assertIn("<tg-emoji", got)


# ═════════════════════════════════════════════════════════════════
# /bstats command
# ═════════════════════════════════════════════════════════════════

class TestBstatsCommand(unittest.IsolatedAsyncioTestCase):
    async def test_non_owner_denied(self):
        msg = _Msg()
        with _owner(), _fake_db():
            await bm.bstats_command(_update(msg, user_id=99), _Ctx())
        self.assertIn("Only the bot owner", msg.last["text"])
        self.assertIn("/bstats", msg.last["text"])
        self.assertNotIn("reply_markup", msg.last)

    async def test_missing_user_denied(self):
        msg = _Msg()
        with _owner(), _fake_db():
            await bm.bstats_command(_update(msg, user_id=None), _Ctx())
        self.assertIn("Only the bot owner", msg.last["text"])

    async def test_unconfigured_owner_denies_everyone(self):
        msg = _Msg()
        with mock.patch.object(bm, "settings", SimpleNamespace(owner_id=0)), \
                _fake_db():
            await bm.bstats_command(_update(msg, user_id=0), _Ctx())
        self.assertIn("Only the bot owner", msg.last["text"])

    async def test_owner_gets_card_with_counts_and_buttons(self):
        msg = _Msg()
        with _owner(), _fake_db(users=51, chats=1, sudos=2, gbanned=7):
            await bm.bstats_command(_update(msg), _Ctx())
        self.assertEqual(len(msg.replies), 1)
        self.assertEqual(msg.last["parse_mode"], "HTML")
        self.assertIn("Total Users: 51", msg.last["text"])
        self.assertIn("Total Sudos: 2", msg.last["text"])
        self.assertIn("GBanned Users: 7", msg.last["text"])
        self.assertIn("Total Rules: 1", msg.last["text"])  # 1 rules_db entry
        markup = msg.last["reply_markup"]
        labels = [b.text for row in markup.inline_keyboard for b in row]
        self.assertEqual(labels, ["Refresh", "Close"])
        for row in markup.inline_keyboard:
            for b in row:
                self.assertEqual(b.api_kwargs.get("style"),
                                 "success" if b.text == "Refresh" else "danger")


# ═════════════════════════════════════════════════════════════════
# bstats callbacks
# ═════════════════════════════════════════════════════════════════

class TestBstatsCallback(unittest.IsolatedAsyncioTestCase):
    async def test_non_owner_refresh_alerted_without_edit(self):
        q = _Query("bstats:refresh", user_id=99)
        with _owner(), _fake_db():
            await bm.stats_callback(_cb_update(q), _Ctx())
        self.assertTrue(q.answers[0]["show_alert"])
        self.assertIn("Only the bot owner", q.answers[0]["text"])
        self.assertEqual(q.edits, [])

    async def test_refresh_edits_fresh_card(self):
        q = _Query("bstats:refresh")
        with _owner(), _fake_db(users=7):
            await bm.stats_callback(_cb_update(q), _Ctx())
        self.assertEqual(len(q.edits), 1)
        self.assertIn("Total Users: 7", q.last_edit["text"])
        self.assertIn("Refresh", str(q.last_edit["reply_markup"]))

    async def test_close_deletes_message(self):
        target = _DelMsg()
        q = _Query("bstats:close", message=target)
        with _owner(), _fake_db():
            await bm.stats_callback(_cb_update(q), _Ctx())
        self.assertTrue(target.deleted)

    async def test_close_falls_back_to_stripping_buttons(self):
        q = _Query("bstats:close", message=_DelMsg(fail=True))
        with _owner(), _fake_db():
            await bm.stats_callback(_cb_update(q), _Ctx())
        self.assertFalse(q.message.deleted)
        self.assertTrue(q.reply_markup_stripped)

    async def test_unknown_action_flagged(self):
        q = _Query("bstats:bogus")
        with _owner(), _fake_db():
            await bm.stats_callback(_cb_update(q), _Ctx())
        self.assertEqual(q.answers[-1]["text"], "Unknown option")


# ═════════════════════════════════════════════════════════════════
# /ping
# ═════════════════════════════════════════════════════════════════

class TestPing(unittest.IsolatedAsyncioTestCase):
    async def test_command_measures_and_edits_card(self):
        msg = _Msg(text="/ping")
        await bm.ping_command(_update(msg, user_id=99), _Ctx())
        # ping is public — non-owner works
        self.assertEqual(len(msg.replies), 1)
        card = msg.sent.last
        self.assertIsNotNone(card)
        self.assertIn("Pong!", card["text"])
        self.assertRegex(card["text"], r"Ping: \d+\.\d{2} ms")
        self.assertIn("Uptime:", card["text"])
        self.assertEqual(card["parse_mode"], "HTML")
        btn = card["reply_markup"].inline_keyboard[0][0]
        self.assertEqual(btn.text, "Ping Again")
        self.assertEqual(btn.api_kwargs.get("style"), "success")
        self.assertEqual(btn.api_kwargs.get("icon_custom_emoji_id"),
                         EID.FIRE)

    async def test_again_callback_remeasures(self):
        q = _Query("ping:again", user_id=99)   # public — any user
        await bm.stats_callback(_cb_update(q), _Ctx())
        self.assertEqual(len(q.edits), 1)
        self.assertRegex(q.last_edit["text"], r"Ping: \d+\.\d{2} ms")
        self.assertIn("Pong!", q.last_edit["text"])

    async def test_again_survives_api_error(self):
        class _DeadBot:
            async def get_me(self):
                raise RuntimeError("network down")

        q = _Query("ping:again", user_id=99)
        await bm.stats_callback(_cb_update(q), _Ctx(bot=_DeadBot()))
        self.assertEqual(len(q.edits), 1)   # still edits (0.00 ms card)

    async def test_unknown_ping_action_flagged(self):
        q = _Query("ping:bogus", user_id=99)
        await bm.stats_callback(_cb_update(q), _Ctx())
        self.assertEqual(q.answers[-1]["text"], "Unknown option")


# ═════════════════════════════════════════════════════════════════
# Wiring + help
# ═════════════════════════════════════════════════════════════════

class TestWiring(unittest.TestCase):
    def test_setup_registers_both_commands(self):
        from telegram.ext import (CallbackQueryHandler,
                                  CommandHandler as PTBCommandHandler)

        class _App:
            def __init__(self):
                self.handlers = []

            def add_handler(self, handler, group=0):
                self.handlers.append(handler)

        app = _App()
        routes = bm.setup(app)
        self.assertEqual(routes, ["/bstats", "/ping"])
        cmds = [h for h in app.handlers
                if isinstance(h, PTBCommandHandler)]
        cbs = [h for h in app.handlers
               if isinstance(h, CallbackQueryHandler)]
        self.assertEqual(
            sorted(set().union(*(c.commands for c in cmds))),
            ["bstats", "ping"],
        )
        self.assertEqual(len(cbs), 1)
        self.assertEqual(cbs[0].pattern.pattern, "^(bstats|ping):")

    def test_help_documents_bstats_and_ping(self):
        general = next(m for m in HELP_MENU if m["key"] == "general")
        lines = [line for _, cmds in general["sections"] for line in cmds]
        self.assertTrue(any(line.startswith("/bstats") for line in lines))
        self.assertTrue(any(line.startswith("/ping") for line in lines))

    def test_help_documents_broadcast_in_gban_section(self):
        gban = next(m for m in HELP_MENU if m["key"] == "gban")
        lines = [line for _, cmds in gban["sections"] for line in cmds]
        self.assertTrue(
            any(line.startswith("/broadcast") for line in lines),
            "Gban & Sudo help is missing the /broadcast entry",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
