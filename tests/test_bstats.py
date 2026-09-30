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
_TEST_DIR = tempfile.mkdtemp(prefix="pi_bstats_test_")
os.environ["LOCALAPPDATA"] = _TEST_DIR
atexit.register(shutil.rmtree, _TEST_DIR, ignore_errors=True)

# ── Imports (after env) ───────────────────────────────────────────
from aiogram.dispatcher.event.handler import FilterObject, HandlerObject  # noqa: E402

from aiofakes import FakeMessage, call, command_filters, make_callback  # noqa: E402
from bot import pipeline  # noqa: E402
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


class _Msg(FakeMessage):
    """Command/card message — reply/answer returns the message itself, so
    /ping can edit the card it just sent.  Every send/edit lands in .calls."""

    def __init__(self, text: str = "/bstats", *, user_id: int = OWNER_ID,
                 **kw) -> None:
        super().__init__(text, user_id=user_id, **kw)

    async def answer(self, text, **kw):
        self.calls.append(("answer", text, kw))
        return self

    async def reply(self, text, **kw):
        self.calls.append(("reply", text, kw))
        return self


class _StaleMsg(_Msg):
    """Fake message for the Close path — delete() refuses ("too old")."""

    async def delete(self, **kw):
        raise RuntimeError("message is too old")


def _sent(msg):
    """{"text", **kw} of the last ("answer"/"reply", …) record on msg."""
    for kind, text, kw in reversed(msg.calls):
        if kind in ("answer", "reply"):
            return {"text": text, **kw}
    return None


def _edits(msg):
    """[{"text", **kw}] for every ("edit_text", …) record on msg."""
    return [{"text": t, **kw} for (k, t, kw) in msg.calls if k == "edit_text"]


def _kinds(msg):
    """Every recorded action kind, in order."""
    return [k for (k, _, _) in msg.calls]


def _query(data: str, *, user_id=OWNER_ID, message=None):
    return make_callback(data, user_id=user_id, message=message)


def _matches(flt, data: str) -> bool:
    """True when a callback_query pipeline filter accepts ``data``."""
    handler = HandlerObject(callback=bm.stats_callback,
                            filters=[FilterObject(flt)])
    ok, _ = asyncio.run(handler.check(make_callback(data)))
    return ok


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
        msg = _Msg(user_id=99)
        with _owner(), _fake_db():
            await call(bm.bstats_command, msg)
        self.assertEqual(msg.last[0], "reply")
        self.assertIn("Only the bot owner", msg.last[1])
        self.assertIn("/bstats", msg.last[1])
        self.assertNotIn("reply_markup", msg.last[2])

    async def test_missing_user_denied(self):
        msg = _Msg()
        msg.from_user = None
        with _owner(), _fake_db():
            await call(bm.bstats_command, msg)
        self.assertIn("Only the bot owner", msg.last[1])

    async def test_unconfigured_owner_denies_everyone(self):
        msg = _Msg(user_id=0)
        with mock.patch.object(bm, "settings", SimpleNamespace(owner_id=0)), \
                _fake_db():
            await call(bm.bstats_command, msg)
        self.assertIn("Only the bot owner", msg.last[1])

    async def test_owner_gets_card_with_counts_and_buttons(self):
        msg = _Msg()
        with _owner(), _fake_db(users=51, chats=1, sudos=2, gbanned=7):
            await call(bm.bstats_command, msg)
        self.assertEqual(len(msg.sent_texts), 1)
        card = _sent(msg)
        self.assertEqual(card["parse_mode"], "HTML")
        self.assertIn("Total Users: 51", card["text"])
        self.assertIn("Total Sudos: 2", card["text"])
        self.assertIn("GBanned Users: 7", card["text"])
        self.assertIn("Total Rules: 1", card["text"])  # 1 rules_db entry
        markup = card["reply_markup"]
        labels = [b.text for row in markup.inline_keyboard for b in row]
        self.assertEqual(labels, ["Refresh", "Close"])
        for row in markup.inline_keyboard:
            for b in row:
                self.assertEqual(b.style,
                                 "success" if b.text == "Refresh" else "danger")


# ═════════════════════════════════════════════════════════════════
# bstats callbacks
# ═════════════════════════════════════════════════════════════════

class TestBstatsCallback(unittest.IsolatedAsyncioTestCase):
    async def test_non_owner_refresh_alerted_without_edit(self):
        q = _query("bstats:refresh", user_id=99)
        with _owner(), _fake_db():
            await call(bm.stats_callback, q)
        self.assertTrue(q.answers[0]["show_alert"])
        self.assertIn("Only the bot owner", q.answers[0]["text"])
        self.assertEqual(_edits(q.message), [])

    async def test_refresh_edits_fresh_card(self):
        q = _query("bstats:refresh")
        with _owner(), _fake_db(users=7):
            await call(bm.stats_callback, q)
        self.assertEqual(len(_edits(q.message)), 1)
        self.assertIn("Total Users: 7", _edits(q.message)[-1]["text"])
        self.assertIn("Refresh", str(_edits(q.message)[-1]["reply_markup"]))

    async def test_close_deletes_message(self):
        target = _Msg()
        q = _query("bstats:close", message=target)
        with _owner(), _fake_db():
            await call(bm.stats_callback, q)
        self.assertIn("delete", _kinds(target))

    async def test_close_falls_back_to_stripping_buttons(self):
        q = _query("bstats:close", message=_StaleMsg())
        with _owner(), _fake_db():
            await call(bm.stats_callback, q)
        self.assertNotIn("delete", _kinds(q.message))
        self.assertIn("edit_reply_markup", _kinds(q.message))

    async def test_unknown_action_flagged(self):
        q = _query("bstats:bogus")
        with _owner(), _fake_db():
            await call(bm.stats_callback, q)
        self.assertEqual(q.answers[-1]["text"], "Unknown option")


# ═════════════════════════════════════════════════════════════════
# /ping
# ═════════════════════════════════════════════════════════════════

class TestPing(unittest.IsolatedAsyncioTestCase):
    async def test_command_measures_and_edits_card(self):
        msg = _Msg(text="/ping", user_id=99)
        await call(bm.ping_command, msg)
        # ping is public — non-owner works
        self.assertEqual(len(msg.sent_texts), 1)
        card = _edits(msg)
        self.assertEqual(len(card), 1)
        card = card[0]
        self.assertIn("Pong!", card["text"])
        self.assertRegex(card["text"], r"Ping: \d+\.\d{2} ms")
        self.assertIn("Uptime:", card["text"])
        self.assertEqual(card["parse_mode"], "HTML")
        btn = card["reply_markup"].inline_keyboard[0][0]
        self.assertEqual(btn.text, "Ping Again")
        self.assertEqual(btn.style, "success")
        self.assertEqual(btn.icon_custom_emoji_id, EID.FIRE)

    async def test_again_callback_remeasures(self):
        q = _query("ping:again", user_id=99)   # public — any user
        await call(bm.stats_callback, q)
        self.assertEqual(len(_edits(q.message)), 1)
        self.assertRegex(_edits(q.message)[-1]["text"], r"Ping: \d+\.\d{2} ms")
        self.assertIn("Pong!", _edits(q.message)[-1]["text"])

    async def test_again_survives_api_error(self):
        class _DeadBot:
            async def get_me(self):
                raise RuntimeError("network down")

        q = _query("ping:again", user_id=99)
        await call(bm.stats_callback, q, bot=_DeadBot())
        self.assertEqual(len(_edits(q.message)), 1)   # still edits (0.00 ms card)

    async def test_unknown_ping_action_flagged(self):
        q = _query("ping:bogus", user_id=99)
        await call(bm.stats_callback, q)
        self.assertEqual(q.answers[-1]["text"], "Unknown option")


# ═════════════════════════════════════════════════════════════════
# Wiring + help
# ═════════════════════════════════════════════════════════════════

class TestWiring(unittest.TestCase):
    def test_setup_registers_both_commands(self):
        pipeline.clear()
        routes = bm.setup()
        self.assertEqual(routes, ["/bstats", "/ping"])
        entries = pipeline.snapshot()
        cmds = [e for e in entries if e.event == "message"]
        cbs = [e for e in entries if e.event == "callback_query"]
        self.assertEqual(
            sorted(set().union(*(
                set(cf.commands) for e in cmds
                for cf in command_filters(e.flt)
            ))),
            ["bstats", "ping"],
        )
        self.assertEqual(len(cbs), 1)
        flt = cbs[0].flt          # ^(bstats|ping):
        self.assertTrue(_matches(flt, "bstats:refresh"))
        self.assertTrue(_matches(flt, "ping:again"))
        self.assertFalse(_matches(flt, "other:x"))

    def test_help_documents_bstats_and_ping(self):
        general = next(m for m in HELP_MENU if m["key"] == "general")
        lines = [line for _, cmds in general["sections"] for line in cmds]
        self.assertTrue(any(line.startswith("/bstats") for line in lines))
        self.assertTrue(any(line.startswith("/ping") for line in lines))

    def test_gban_sudo_card_removed_from_help(self):
        keys = [m["key"] for m in HELP_MENU]
        self.assertNotIn("gban", keys, "Gban & Sudo section must stay removed")

    def test_broadcast_and_massban_documented_in_general(self):
        general = next(m for m in HELP_MENU if m["key"] == "general")
        lines = [line for _, cmds in general["sections"] for line in cmds]
        self.assertTrue(
            any(line.startswith("/broadcast") for line in lines),
            "General help is missing the /broadcast entry",
        )
        self.assertTrue(
            any(line.startswith("/massban") for line in lines),
            "General help is missing the /massban entry",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
