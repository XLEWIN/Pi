"""Tests for the join-request approval module (bot/modules/requests.py).

Run from the Pi/Pi root:

    python tests/test_join_requests.py
    python -m unittest discover -s tests

Environment isolation (BEFORE any bot import):
    * BOT_TOKEN is forced — importing `bot` pulls bot.config, which
      exits without one.
    * LOCALAPPDATA points at a temp dir so any DB touch stays isolated.
No network: handlers run against fakes that record sends/approvals.
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
_TEST_DIR = tempfile.mkdtemp(prefix="pi_joinreq_test_")
os.environ["LOCALAPPDATA"] = _TEST_DIR
atexit.register(shutil.rmtree, _TEST_DIR, ignore_errors=True)

# ── Imports (after env) ───────────────────────────────────────────
from bot import pipeline  # noqa: E402
from bot.command_handler import CommandFilter  # noqa: E402
from bot.database import db  # noqa: E402
from bot.modules import requests as req  # noqa: E402
from aiofakes import call, make_callback, make_message  # noqa: E402

CHAT_ID = -1009999999991
OTHER_CHAT = -1009999999992
REQUESTER_ID = 8290121113


# ── Fakes ─────────────────────────────────────────────────────────

def _user(uid: int = REQUESTER_ID, name: str = "Nobara Kugisaki",
          username: str | None = "nobara"):
    return SimpleNamespace(
        id=uid, first_name=name.split()[0], last_name=" ".join(name.split()[1:]),
        full_name=name, username=username, is_bot=False,
    )


def _member(status: str, user=None):
    return SimpleNamespace(status=status, user=user or _user(42, "Admin User"))


class _FakeBot:
    def __init__(self, status: str = "administrator"):
        self.status = status
        self.sent: list = []          # (chat_id, text, markup)
        self.approved: list = []      # (chat_id, user_id)
        self.declined: list = []      # (chat_id, user_id)
        self.member_status = status   # status returned for clickers
        self.requester_member_ok = False  # post-approve membership

    async def get_chat_member(self, chat_id, user_id):
        if user_id == REQUESTER_ID:
            if self.requester_member_ok:
                return _member("member", _user())
            raise RuntimeError("user not found")
        return _member(self.member_status, _user(user_id, "Clicker User"))

    async def send_message(self, chat_id, text, parse_mode=None,
                           reply_markup=None, **kw):
        self.sent.append((chat_id, text, reply_markup))

    async def approve_chat_join_request(self, chat_id, user_id):
        self.approved.append((chat_id, user_id))
        self.requester_member_ok = True  # approval makes them a member
        return True

    async def decline_chat_join_request(self, chat_id, user_id):
        self.declined.append((chat_id, user_id))
        return True


def _cmd(*, chat_type: str = "supergroup", chat_id: int = CHAT_ID):
    """Command message + bot — sends record on ``msg.calls``."""
    msg = make_message(
        "/request", chat_id=chat_id, chat_type=chat_type,
        user_id=7, first_name="Admin",
    )

    async def reply_text(text, **kw):
        # bot.responses.reply_card still calls the PTB-era
        # message.reply_text(...) shortcut, so route it exactly like
        # bot.reply.reply_text does (group → reply, private → answer).
        if msg.chat.type != "private":
            return await msg.reply(text, **kw)
        return await msg.answer(text, **kw)

    msg.reply_text = reply_text
    return msg, _FakeBot()


def _jr(chat_id: int = CHAT_ID):
    """Duck ChatJoinRequest event."""
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id, type="supergroup", title="Test"),
        from_user=_user(),
    )


def _cb(data: str, status: str = "administrator"):
    """joinreq:* callback + its bot; message records edits."""
    bot = _FakeBot(status)
    cb = make_callback(data, user_id=7)
    cb.from_user.first_name = "Admin"
    cb.from_user.last_name = "Clicker"
    cb.from_user.full_name = "Admin Clicker"
    return cb, bot


def _text(msg):
    """Last reply/answer text recorded on a FakeMessage."""
    for kind, t, _ in reversed(msg.calls):
        if kind in ("reply", "answer"):
            return t
    return None


def _edits(msg):
    """All edit_text payloads, oldest first."""
    return [t for (k, t, _) in msg.calls if k == "edit_text"]


def _flat(markup):
    return [b for row in markup.inline_keyboard for b in row]


# ═════════════════════════════════════════════════════════════════
# Card + keyboard
# ═════════════════════════════════════════════════════════════════

class TestCardAndKeyboard(unittest.TestCase):
    def test_card_matches_reference_layout(self):
        text = req.request_card(_user())
        self.assertIn("New join request is available", text)
        self.assertIn("USER'S INFO", text)
        self.assertIn("Name: Nobara Kugisaki", text)
        self.assertIn(f"ID: <code>{REQUESTER_ID}</code>", text)
        self.assertIn("Scan: False", text)
        self.assertIn("Username: @nobara", text)
        self.assertIn(f"tg://user?id={REQUESTER_ID}", text)

    def test_card_username_missing(self):
        text = req.request_card(_user(username=None))
        self.assertIn("Username: n/a", text)

    def test_keyboard_accept_decline(self):
        kb = req.request_keyboard(CHAT_ID, REQUESTER_ID)
        buttons = _flat(kb)
        self.assertEqual(len(buttons), 2)
        accept, decline = buttons
        self.assertEqual(accept.text, "Accept")
        self.assertEqual(
            accept.callback_data,
            f"joinreq:accept:{CHAT_ID}:{REQUESTER_ID}",
        )
        self.assertEqual(getattr(accept, "style", None), "success")
        self.assertEqual(decline.text, "Decline")
        self.assertEqual(
            decline.callback_data,
            f"joinreq:decline:{CHAT_ID}:{REQUESTER_ID}",
        )
        self.assertEqual(getattr(decline, "style", None), "danger")


# ═════════════════════════════════════════════════════════════════
# /request on|off
# ═════════════════════════════════════════════════════════════════

class TestRequestCommand(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        db.set_join_requests(CHAT_ID, False)
        db.set_join_requests(OTHER_CHAT, False)

    async def asyncTearDown(self):
        db.set_join_requests(CHAT_ID, False)
        db.set_join_requests(OTHER_CHAT, False)

    async def test_status_when_no_args(self):
        msg, bot = _cmd()
        await call(req.request_command, msg, bot=bot)
        self.assertIn("Disabled", _text(msg))

    async def test_enable_and_disable(self):
        msg, bot = _cmd()
        await call(req.request_command, msg, bot=bot, args=["on"])
        self.assertTrue(db.get_join_requests(CHAT_ID))
        self.assertIn("Enabled", _text(msg))

        msg, bot = _cmd()
        await call(req.request_command, msg, bot=bot, args=["off"])
        self.assertFalse(db.get_join_requests(CHAT_ID))
        self.assertIn("Disabled", _text(msg))

    async def test_invalid_arg_shows_status(self):
        msg, bot = _cmd()
        await call(req.request_command, msg, bot=bot, args=["maybe"])
        self.assertIn("Status", _text(msg))
        self.assertFalse(db.get_join_requests(CHAT_ID))

    async def test_non_admin_denied(self):
        msg, bot = _cmd()
        bot.member_status = "member"
        await call(req.request_command, msg, bot=bot, args=["on"])
        self.assertIsNone(_text(msg))
        self.assertFalse(db.get_join_requests(CHAT_ID))

    async def test_private_denied(self):
        msg, bot = _cmd(chat_type="private")
        await call(req.request_command, msg, bot=bot, args=["on"])
        self.assertIn("groups", _text(msg))
        self.assertFalse(db.get_join_requests(CHAT_ID))


# ═════════════════════════════════════════════════════════════════
# ChatJoinRequest → approval card
# ═════════════════════════════════════════════════════════════════

class TestOnJoinRequest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        req._PENDING.clear()
        db.set_join_requests(CHAT_ID, False)

    async def asyncTearDown(self):
        db.set_join_requests(CHAT_ID, False)
        req._PENDING.clear()

    async def test_disabled_sends_nothing(self):
        bot = _FakeBot()
        await call(req.on_join_request, _jr(), bot=bot)
        self.assertEqual(bot.sent, [])

    async def test_enabled_posts_card_with_buttons(self):
        db.set_join_requests(CHAT_ID, True)
        bot = _FakeBot()
        await call(req.on_join_request, _jr(), bot=bot)
        self.assertEqual(len(bot.sent), 1)
        chat_id, text, markup = bot.sent[0]
        self.assertEqual(chat_id, CHAT_ID)
        self.assertIn("New join request is available", text)
        buttons = _flat(markup)
        self.assertEqual(
            [b.callback_data for b in buttons],
            [f"joinreq:accept:{CHAT_ID}:{REQUESTER_ID}",
             f"joinreq:decline:{CHAT_ID}:{REQUESTER_ID}"],
        )
        # Cache filled for the decline-path name lookup.
        self.assertIn((CHAT_ID, REQUESTER_ID), req._PENDING)


# ═════════════════════════════════════════════════════════════════
# Accept / Decline callbacks
# ═════════════════════════════════════════════════════════════════

class TestJoinRequestCallback(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        req._PENDING.clear()
        req._PENDING[(CHAT_ID, REQUESTER_ID)] = req._mention(_user())

    async def asyncTearDown(self):
        req._PENDING.clear()

    async def test_admin_accept_approves_and_edits_result(self):
        cb, bot = _cb(f"joinreq:accept:{CHAT_ID}:{REQUESTER_ID}")
        await call(req.join_request_callback, cb, bot=bot)
        self.assertEqual(bot.approved, [(CHAT_ID, REQUESTER_ID)])
        self.assertEqual(bot.declined, [])
        self.assertEqual(len(cb.answers), 1)
        self.assertFalse(cb.answers[0]["show_alert"])
        edits = _edits(cb.message)
        self.assertEqual(len(edits), 1)
        self.assertIn("accepted join request of", edits[0])
        self.assertIn("Admin Clicker", edits[0])
        self.assertIn("Nobara", edits[0])  # live member lookup after approve

    async def test_admin_decline_declines_and_edits_result(self):
        cb, bot = _cb(f"joinreq:decline:{CHAT_ID}:{REQUESTER_ID}")
        await call(req.join_request_callback, cb, bot=bot)
        self.assertEqual(bot.declined, [(CHAT_ID, REQUESTER_ID)])
        self.assertEqual(bot.approved, [])
        edits = _edits(cb.message)
        self.assertIn("declined join request of", edits[0])
        self.assertIn("Nobara", edits[0])  # card-time cache fallback

    async def test_non_admin_cannot_process(self):
        cb, bot = _cb(
            f"joinreq:accept:{CHAT_ID}:{REQUESTER_ID}", status="member"
        )
        await call(req.join_request_callback, cb, bot=bot)
        self.assertEqual(bot.approved, [])
        self.assertEqual(bot.declined, [])
        self.assertEqual(_edits(cb.message), [])
        self.assertEqual(len(cb.answers), 1)
        self.assertFalse(cb.answers[0]["show_alert"])

    async def test_owner_status_accepted(self):
        cb, bot = _cb(
            f"joinreq:accept:{CHAT_ID}:{REQUESTER_ID}", status="creator"
        )
        await call(req.join_request_callback, cb, bot=bot)
        self.assertEqual(bot.approved, [(CHAT_ID, REQUESTER_ID)])

    async def test_invalid_data_alerts(self):
        cb, bot = _cb("joinreq:accept:bad:bad")
        await call(req.join_request_callback, cb, bot=bot)
        self.assertEqual(bot.approved, [])
        self.assertTrue(cb.answers[0]["show_alert"])
        self.assertEqual(_edits(cb.message), [])

    async def test_api_failure_edits_error(self):
        cb, bot = _cb(f"joinreq:accept:{CHAT_ID}:{REQUESTER_ID}")

        async def boom(chat_id, user_id):
            raise RuntimeError("Bad Request: user not found")

        bot.approve_chat_join_request = boom
        await call(req.join_request_callback, cb, bot=bot)
        edits = _edits(cb.message)
        self.assertEqual(len(edits), 1)
        self.assertIn("Could not accept", edits[0])

    async def test_admin_gate_failure_alerts(self):
        cb, bot = _cb(f"joinreq:accept:{CHAT_ID}:{REQUESTER_ID}")

        async def boom(chat_id, user_id):
            raise RuntimeError("network down")

        bot.get_chat_member = boom
        await call(req.join_request_callback, cb, bot=bot)
        self.assertEqual(bot.approved, [])
        self.assertTrue(cb.answers[0]["show_alert"])


# ═════════════════════════════════════════════════════════════════
# DB settings + setup() registration
# ═════════════════════════════════════════════════════════════════

class TestDatabaseAndSetup(unittest.TestCase):
    def test_join_request_settings_roundtrip(self):
        self.assertFalse(db.get_join_requests(OTHER_CHAT))
        db.set_join_requests(OTHER_CHAT, True)
        self.assertTrue(db.get_join_requests(OTHER_CHAT))
        db.set_join_requests(OTHER_CHAT, False)
        self.assertFalse(db.get_join_requests(OTHER_CHAT))

    def test_unknown_chat_defaults_off(self):
        self.assertFalse(db.get_join_requests(-1001234567890))

    def test_count_user_groups(self):
        # Fresh DB — no tracked memberships for a random user.
        self.assertEqual(db.count_user_groups(987654321), 0)

    def test_setup_registers_handlers(self):
        pipeline.clear()
        try:
            routes = req.setup()
            self.assertEqual(routes[0], "/request on|off")

            entries = [
                e for e in pipeline.snapshot()
                if e.fn.__module__ == "bot.modules.requests"
            ]
            self.assertEqual(len(entries), 3)
            self.assertEqual(
                {e.event: e.fn.__name__ for e in entries},
                {
                    "message": "request_command",
                    "chat_join_request": "on_join_request",
                    "callback_query": "join_request_callback",
                },
            )
            msg_entry = next(e for e in entries if e.event == "message")
            self.assertIsInstance(msg_entry.flt, CommandFilter)
            self.assertEqual(msg_entry.flt.commands, frozenset({"request"}))
        finally:
            pipeline.clear()


if __name__ == "__main__":
    unittest.main(verbosity=2)
