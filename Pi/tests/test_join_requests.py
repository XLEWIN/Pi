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
from bot.database import db  # noqa: E402
from bot.modules import requests as req  # noqa: E402

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


class _FakeMessage:
    def __init__(self, chat_id: int = CHAT_ID, chat_type: str = "supergroup"):
        self.chat = SimpleNamespace(id=chat_id, type=chat_type, title="Test")
        self.replies: list = []

    async def reply_text(self, text, parse_mode=None, reply_markup=None, **kw):
        self.replies.append({"text": text, "markup": reply_markup})

    @property
    def last(self):
        return self.replies[-1] if self.replies else None


class _FakeQuery:
    def __init__(self, data: str, user=None):
        self.data = data
        self.from_user = user or _user(7, "Admin Clicker")
        self.answers: list = []
        self.edits: list = []

    async def answer(self, text=None, show_alert=False, **kw):
        self.answers.append({"text": text, "show_alert": show_alert})

    async def edit_message_text(self, text, parse_mode=None, reply_markup=None, **kw):
        self.edits.append(text)


def _cmd_update(args=None, chat_type: str = "supergroup",
                chat_id: int = CHAT_ID, user=None):
    msg = _FakeMessage(chat_id, chat_type)
    return SimpleNamespace(
        message=msg,
        effective_message=msg,
        effective_chat=msg.chat,
        effective_user=user or _user(7, "Admin Clicker"),
    ), SimpleNamespace(args=args or [], bot=_FakeBot())


def _jr_update(enabled_via_bot: _FakeBot, chat_id: int = CHAT_ID):
    return SimpleNamespace(
        chat_join_request=SimpleNamespace(
            chat=SimpleNamespace(id=chat_id, type="supergroup", title="Test"),
            from_user=_user(),
        )
    )


def _cb_update(data: str, status: str = "administrator", user=None):
    bot = _FakeBot(status)
    upd = SimpleNamespace(
        callback_query=_FakeQuery(data, user=user),
        bot=bot,
    )
    return upd, SimpleNamespace(bot=bot)


def _flat(markup):
    return [b for row in markup.inline_keyboard for b in row]


class _FakeApp:
    def __init__(self):
        self.handlers: list = []

    def add_handler(self, handler, group=0):
        self.handlers.append((group, handler))


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
        self.assertEqual(
            (getattr(accept, "api_kwargs", None) or {}).get("style"), "success"
        )
        self.assertEqual(decline.text, "Decline")
        self.assertEqual(
            decline.callback_data,
            f"joinreq:decline:{CHAT_ID}:{REQUESTER_ID}",
        )
        self.assertEqual(
            (getattr(decline, "api_kwargs", None) or {}).get("style"), "danger"
        )


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
        upd, ctx = _cmd_update()
        await req.request_command(upd, ctx)
        self.assertIn("Disabled", upd.message.last["text"])

    async def test_enable_and_disable(self):
        upd, ctx = _cmd_update(["on"])
        await req.request_command(upd, ctx)
        self.assertTrue(db.get_join_requests(CHAT_ID))
        self.assertIn("Enabled", upd.message.last["text"])

        upd, ctx = _cmd_update(["off"])
        await req.request_command(upd, ctx)
        self.assertFalse(db.get_join_requests(CHAT_ID))
        self.assertIn("Disabled", upd.message.last["text"])

    async def test_invalid_arg_shows_status(self):
        upd, ctx = _cmd_update(["maybe"])
        await req.request_command(upd, ctx)
        self.assertIn("Status", upd.message.last["text"])
        self.assertFalse(db.get_join_requests(CHAT_ID))

    async def test_non_admin_denied(self):
        upd, ctx = _cmd_update(["on"])
        ctx.bot.member_status = "member"
        await req.request_command(upd, ctx)
        self.assertIn("admin rights", upd.message.last["text"])
        self.assertFalse(db.get_join_requests(CHAT_ID))

    async def test_private_denied(self):
        upd, ctx = _cmd_update(["on"], chat_type="private")
        await req.request_command(upd, ctx)
        self.assertIn("groups", upd.message.last["text"])
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
        upd = _jr_update(bot)
        await req.on_join_request(upd, SimpleNamespace(bot=bot))
        self.assertEqual(bot.sent, [])

    async def test_enabled_posts_card_with_buttons(self):
        db.set_join_requests(CHAT_ID, True)
        bot = _FakeBot()
        upd = _jr_update(bot)
        await req.on_join_request(upd, SimpleNamespace(bot=bot))
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
        upd, ctx = _cb_update(f"joinreq:accept:{CHAT_ID}:{REQUESTER_ID}")
        await req.join_request_callback(upd, ctx)
        self.assertEqual(ctx.bot.approved, [(CHAT_ID, REQUESTER_ID)])
        self.assertEqual(ctx.bot.declined, [])
        self.assertEqual(len(upd.callback_query.answers), 1)
        self.assertFalse(upd.callback_query.answers[0]["show_alert"])
        self.assertEqual(len(upd.callback_query.edits), 1)
        text = upd.callback_query.edits[0]
        self.assertIn("accepted join request of", text)
        self.assertIn("Admin Clicker", text)
        self.assertIn("Nobara", text)  # live member lookup after approve

    async def test_admin_decline_declines_and_edits_result(self):
        upd, ctx = _cb_update(f"joinreq:decline:{CHAT_ID}:{REQUESTER_ID}")
        await req.join_request_callback(upd, ctx)
        self.assertEqual(ctx.bot.declined, [(CHAT_ID, REQUESTER_ID)])
        self.assertEqual(ctx.bot.approved, [])
        text = upd.callback_query.edits[0]
        self.assertIn("declined join request of", text)
        self.assertIn("Nobara", text)  # card-time cache fallback

    async def test_non_admin_cannot_process(self):
        upd, ctx = _cb_update(
            f"joinreq:accept:{CHAT_ID}:{REQUESTER_ID}", status="member"
        )
        await req.join_request_callback(upd, ctx)
        self.assertEqual(ctx.bot.approved, [])
        self.assertEqual(ctx.bot.declined, [])
        self.assertEqual(upd.callback_query.edits, [])
        self.assertEqual(len(upd.callback_query.answers), 1)
        self.assertTrue(upd.callback_query.answers[0]["show_alert"])

    async def test_owner_status_accepted(self):
        upd, ctx = _cb_update(
            f"joinreq:accept:{CHAT_ID}:{REQUESTER_ID}", status="creator"
        )
        await req.join_request_callback(upd, ctx)
        self.assertEqual(ctx.bot.approved, [(CHAT_ID, REQUESTER_ID)])

    async def test_invalid_data_alerts(self):
        upd, ctx = _cb_update("joinreq:accept:bad:bad")
        await req.join_request_callback(upd, ctx)
        self.assertEqual(ctx.bot.approved, [])
        self.assertTrue(upd.callback_query.answers[0]["show_alert"])
        self.assertEqual(upd.callback_query.edits, [])

    async def test_api_failure_edits_error(self):
        upd, ctx = _cb_update(f"joinreq:accept:{CHAT_ID}:{REQUESTER_ID}")

        async def boom(chat_id, user_id):
            raise RuntimeError("Bad Request: user not found")

        ctx.bot.approve_chat_join_request = boom
        await req.join_request_callback(upd, ctx)
        self.assertEqual(len(upd.callback_query.edits), 1)
        self.assertIn("Could not accept", upd.callback_query.edits[0])

    async def test_admin_gate_failure_alerts(self):
        upd, ctx = _cb_update(f"joinreq:accept:{CHAT_ID}:{REQUESTER_ID}")

        async def boom(chat_id, user_id):
            raise RuntimeError("network down")

        ctx.bot.get_chat_member = boom
        await req.join_request_callback(upd, ctx)
        self.assertEqual(ctx.bot.approved, [])
        self.assertTrue(upd.callback_query.answers[0]["show_alert"])


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
        app = _FakeApp()
        routes = req.setup(app)
        kinds = [type(h).__name__ for _, h in app.handlers]
        self.assertEqual(len(app.handlers), 3)
        self.assertTrue(any("CommandHandler" in k for k in kinds))
        self.assertTrue(any("ChatJoinRequestHandler" in k for k in kinds))
        self.assertTrue(any("CallbackQueryHandler" in k for k in kinds))
        self.assertEqual(routes[0], "/request on|off")


if __name__ == "__main__":
    unittest.main(verbosity=2)
