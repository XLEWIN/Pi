"""Tests for nightmode (bot/modules/nightmode.py).

Run from the repo root:

    python tests/test_nightmode.py

Environment isolation (BEFORE any bot import):
    * BOT_TOKEN is forced — importing `bot` pulls bot.config, which
      exits without one.
    * LOCALAPPDATA points at a temp dir so runtime files are isolated.

No network: fake messages/callbacks/bots; db and admin checks patched.
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

# ── Environment isolation — must precede bot imports ──────────────
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["BOT_TOKEN"] = "1:TEST-TOKEN-FOR-UNIT-TESTS"
_TEST_DIR = tempfile.mkdtemp(prefix="pi_nightmode_test_")
os.environ["LOCALAPPDATA"] = _TEST_DIR
atexit.register(shutil.rmtree, _TEST_DIR, ignore_errors=True)

# ── Imports (after env) ───────────────────────────────────────────
from aiofakes import FakeBot, FakeMessage, call, make_callback  # noqa: E402

from bot.modules import nightmode as nm  # noqa: E402

CHAT = -100777001
ADMIN = 42
IST = timezone(timedelta(hours=5, minutes=30))


# ═════════════════════════════════════════════════════════════════
# Fakes + patches
# ═════════════════════════════════════════════════════════════════

def _msg(text: str = "/nightmode") -> FakeMessage:
    return FakeMessage(text, chat_id=CHAT, user_id=ADMIN)


def _db(enabled=False):
    state = {"enabled": enabled, "calls": [], "chats": []}

    def is_nightmode(chat_id):
        return state["enabled"]

    def set_nightmode(chat_id, on):
        state["calls"].append(("set", chat_id, on))
        state["enabled"] = bool(on)

    def get_nightmode_chats():
        return list(state["chats"])

    fake = SimpleNamespace(
        is_nightmode=is_nightmode,
        set_nightmode=set_nightmode,
        get_nightmode_chats=get_nightmode_chats,
    )
    return fake, state


def _async_result(value):
    async def _fn(*a, **k):
        return value
    return _fn


@contextmanager
def _patched(enabled=False, is_admin=False, member_admin=None):
    """Patch db + admin checks.  ``member_admin=None`` leaves the REAL
    ``security._member_is_admin`` in place (driven by FakeBot members)."""
    fake, state = _db(enabled)
    with mock.patch.object(nm, "db", fake), \
            mock.patch.object(nm, "_is_admin", new=_async_result(is_admin)):
        if member_admin is None:
            yield state
        else:
            with mock.patch.object(nm, "_member_is_admin",
                                   new=_async_result(member_admin)):
                yield state


def _admin_bot(admin: bool = True) -> FakeBot:
    bot = FakeBot()
    status = "administrator" if admin else "member"
    bot.chat_members[(CHAT, ADMIN)] = SimpleNamespace(
        user=SimpleNamespace(id=ADMIN, is_bot=False, first_name="A"),
        status=status,
    )
    return bot


def _last_reply(msg):
    for kind, text, kw in reversed(msg.calls):
        if kind in ("reply", "answer"):
            return text, kw
    return None, None


class _PermBot(FakeBot):
    """FakeBot that records set_chat_permissions calls."""

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.perm_calls = []

    async def set_chat_permissions(self, chat_id, permissions, **kw):
        self.perm_calls.append((chat_id, permissions))


# ═════════════════════════════════════════════════════════════════
# Core logic
# ═════════════════════════════════════════════════════════════════

class TestPhase(unittest.TestCase):
    def test_night_window(self):
        for hour in (23, 0, 3, 6):
            dt = datetime(2026, 9, 30, hour, 59, tzinfo=IST)
            self.assertEqual(nm._phase(dt), "night", hour)

    def test_day_window(self):
        for hour in (7, 12, 18, 22):
            dt = datetime(2026, 9, 30, hour, 0, tzinfo=IST)
            self.assertEqual(nm._phase(dt), "day", hour)


class TestPermissions(unittest.TestCase):
    def test_night_is_text_only(self):
        self.assertTrue(nm.NIGHT.can_send_messages)
        self.assertFalse(nm.NIGHT.can_send_photos)
        self.assertFalse(nm.NIGHT.can_send_videos)
        self.assertFalse(nm.NIGHT.can_send_polls)
        self.assertFalse(nm.NIGHT.can_add_web_page_previews)
        self.assertFalse(nm.NIGHT.can_send_other_messages)

    def test_day_allows_everything(self):
        for field in type(nm.DAY).model_fields:
            self.assertTrue(getattr(nm.DAY, field), field)

    def test_night_only_text(self):
        for field in type(nm.NIGHT).model_fields:
            if field == "can_send_messages":
                continue
            self.assertFalse(getattr(nm.NIGHT, field), field)


# ═════════════════════════════════════════════════════════════════
# /nightmode command + callback
# ═════════════════════════════════════════════════════════════════

class TestCommand(unittest.IsolatedAsyncioTestCase):
    async def test_private_rejected(self):
        msg = FakeMessage("/nightmode", chat_id=CHAT, chat_type="private")
        with _patched(is_admin=True):
            await call(nm.nightmode_command, msg, args=[])
        text, _ = _last_reply(msg)
        self.assertIn("groups", text)

    async def test_requires_admin(self):
        msg = _msg()
        with _patched(is_admin=False):
            await call(nm.nightmode_command, msg, args=[])
        text, _ = _last_reply(msg)
        self.assertIn("Only admins", text)

    async def test_off_shows_enable_button(self):
        msg = _msg()
        with _patched(enabled=False, is_admin=True):
            await call(nm.nightmode_command, msg, args=[])
        text, kw = _last_reply(msg)
        self.assertIn("disabled", text)
        rows = kw["reply_markup"].inline_keyboard
        self.assertEqual(len(rows), 1)
        button = rows[0][0]
        self.assertEqual(button.callback_data, "nm:on")
        self.assertEqual(button.style, "success")
        self.assertIn("Enable", button.text)

    async def test_on_shows_disable_button(self):
        msg = _msg()
        with _patched(enabled=True, is_admin=True):
            await call(nm.nightmode_command, msg, args=[])
        text, kw = _last_reply(msg)
        self.assertIn("enabled", text)
        button = kw["reply_markup"].inline_keyboard[0][0]
        self.assertEqual(button.callback_data, "nm:off")
        self.assertEqual(button.style, "danger")
        self.assertIn("Disable", button.text)


class TestCallback(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Real _member_is_admin caches for 30s — clear between tests so
        # admin/member flips aren't masked by a stale hit.
        from bot.modules import security
        security._admin_cache.clear()

    async def test_admin_enables(self):
        cb = make_callback("nm:on", user_id=ADMIN, chat_id=CHAT)
        with _patched(enabled=False) as state:
            bot = _admin_bot(admin=True)
            await call(nm.nightmode_callback, cb, bot=bot)
        self.assertEqual(state["calls"], [("set", CHAT, True)])
        self.assertEqual(len(cb.answers), 1)
        self.assertIn("enabled", cb.answers[0]["text"])
        # flipped card
        edits = [c for c in cb.message.calls if c[0] == "edit_text"]
        self.assertEqual(len(edits), 1)
        self.assertIn("enabled", edits[0][1])
        self.assertEqual(
            edits[0][2]["reply_markup"].inline_keyboard[0][0].callback_data,
            "nm:off",
        )

    async def test_admin_disables(self):
        cb = make_callback("nm:off", user_id=ADMIN, chat_id=CHAT)
        with _patched(enabled=True) as state:
            bot = _admin_bot(admin=True)
            await call(nm.nightmode_callback, cb, bot=bot)
        self.assertEqual(state["calls"], [("set", CHAT, False)])
        self.assertIn("disabled", cb.answers[0]["text"])

    async def test_non_admin_denied(self):
        cb = make_callback("nm:on", user_id=ADMIN, chat_id=CHAT)
        with _patched(enabled=False) as state:
            bot = _admin_bot(admin=False)
            await call(nm.nightmode_callback, cb, bot=bot)
        self.assertEqual(state["calls"], [])
        self.assertTrue(cb.answers[0]["show_alert"])
        self.assertIn("Only admins", cb.answers[0]["text"])
        edits = [c for c in cb.message.calls if c[0] == "edit_text"]
        self.assertEqual(edits, [])


# ═════════════════════════════════════════════════════════════════
# Permission flips
# ═════════════════════════════════════════════════════════════════

class TestApply(unittest.IsolatedAsyncioTestCase):
    async def test_apply_night_with_announcement(self):
        bot = _PermBot()
        await nm._apply(bot, [CHAT], night=True, announce=True)
        self.assertEqual(len(bot.perm_calls), 1)
        chat_id, perms = bot.perm_calls[0]
        self.assertEqual(chat_id, CHAT)
        self.assertIs(perms, nm.NIGHT)
        self.assertEqual(len(bot.sent), 1)
        self.assertIn("Nightmode", bot.sent[0]["text"])
        self.assertIn("7:00 AM", bot.sent[0]["text"])

    async def test_apply_day_restores(self):
        bot = _PermBot()
        await nm._apply(bot, [CHAT], night=False, announce=True)
        _, perms = bot.perm_calls[0]
        self.assertIs(perms, nm.DAY)
        self.assertIn("restored", bot.sent[0]["text"])

    async def test_apply_silent_skips_announcement(self):
        bot = _PermBot()
        await nm._apply(bot, [CHAT, CHAT - 1], night=True, announce=False)
        self.assertEqual(len(bot.perm_calls), 2)
        self.assertEqual(bot.sent, [])

    async def test_apply_survives_bad_chat(self):
        bot = _PermBot()

        async def boom(chat_id, permissions, **kw):
            if chat_id == CHAT:
                raise RuntimeError("bot was kicked")
            bot.perm_calls.append((chat_id, permissions))

        bot.set_chat_permissions = boom
        await nm._apply(bot, [CHAT, CHAT - 1], night=True, announce=True)
        self.assertEqual(len(bot.perm_calls), 1)


# ═════════════════════════════════════════════════════════════════
# Registration
# ═════════════════════════════════════════════════════════════════

class TestSetup(unittest.TestCase):
    def test_setup_registers_everything(self):
        result = nm.setup()
        self.assertIn("/nightmode", result)
        self.assertIn("nm-callback", result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
