"""Tests for /info, /myinfo, /userinfo, /id and info:* callbacks
(bot/modules/users.py).

Run from the Pi/Pi root:

    python tests/test_user_info.py
    python -m unittest discover -s tests

Environment isolation (BEFORE any bot import):
    * BOT_TOKEN is forced — importing `bot` pulls bot.config, which
      exits without one.
    * LOCALAPPDATA points at a temp dir so any DB touch stays isolated.
No network: handlers run against fakes that record replies/edits.
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
_TEST_DIR = tempfile.mkdtemp(prefix="pi_userinfo_test_")
os.environ["LOCALAPPDATA"] = _TEST_DIR
atexit.register(shutil.rmtree, _TEST_DIR, ignore_errors=True)

# ── Imports (after env) ───────────────────────────────────────────
from bot.database import db  # noqa: E402
from bot.modules import users as users_mod  # noqa: E402

ME_ID = 42
TARGET_ID = 777


# ── Fakes ─────────────────────────────────────────────────────────

def _user(uid: int = ME_ID, first: str = "Nobara", last: str = "Kugisaki",
          username: str | None = "nobara", is_bot: bool = False):
    return SimpleNamespace(
        id=uid, first_name=first, last_name=last,
        full_name=f"{first} {last}".strip(), username=username, is_bot=is_bot,
    )


def _chat_private(uid: int, bio: str | None = None, first: str = "Target",
                  username: str | None = "targetuser"):
    return SimpleNamespace(
        id=uid, type="private", first_name=first, last_name=None,
        username=username, bio=bio,
    )


class _FakeBot:
    def __init__(self, bio: str | None = "Hello there",
                 photos: int = 5, target_chat_ok: bool = True):
        self.username = "PiModulerBot"
        self.bio = bio
        self.photos = photos
        self.target_chat_ok = target_chat_ok
        self.get_chat_calls: list = []

    async def get_chat(self, chat_id):
        self.get_chat_calls.append(chat_id)
        # Resolution + bio lookup may both call; unknown ids still fail.
        if not self.target_chat_ok or chat_id not in (TARGET_ID, ME_ID):
            raise RuntimeError("chat not found")
        return _chat_private(chat_id, bio=self.bio)

    async def get_user_profile_photos(self, user_id, limit=1, **kw):
        return SimpleNamespace(total_count=self.photos)

    async def get_chat_member(self, chat_id, user_id):
        if user_id == "@mentioned":
            return SimpleNamespace(status="member", user=_user(TARGET_ID, "Mentioned"))
        raise RuntimeError("user not found")


class _FakeMessage:
    def __init__(self, chat_type: str = "supergroup", reply=None):
        self.chat = SimpleNamespace(id=-100123, type=chat_type, title="Test")
        self.reply_to_message = reply
        self.replies: list = []
        self.deleted = False

    async def reply_text(self, text, parse_mode=None, reply_markup=None, **kw):
        self.replies.append({"text": text, "markup": reply_markup})

    async def delete(self):
        self.deleted = True

    @property
    def last(self):
        return self.replies[-1] if self.replies else None


def _cmd_update(args=None, reply=None, user=None, chat_type="supergroup"):
    msg = _FakeMessage(chat_type, reply=reply)
    return SimpleNamespace(
        message=msg,
        effective_message=msg,
        effective_chat=msg.chat,
        effective_user=user or _user(),
    ), SimpleNamespace(args=args or [], bot=_FakeBot())


def _cb_update(data: str, from_user=None):
    query = SimpleNamespace(
        data=data,
        from_user=from_user or _user(TARGET_ID, "Clicker", "Person"),
        answers=[],
        edits=[],
        message=_FakeMessage(),
    )

    async def answer(text=None, show_alert=False, **kw):
        query.answers.append({"text": text, "show_alert": show_alert})

    async def edit_message_text(text, parse_mode=None, reply_markup=None, **kw):
        query.edits.append({"text": text, "markup": reply_markup})

    async def edit_message_reply_markup(reply_markup=None, **kw):
        query.markup_stripped = reply_markup is None

    query.answer = answer
    query.edit_message_text = edit_message_text
    query.edit_message_reply_markup = edit_message_reply_markup
    return SimpleNamespace(callback_query=query), SimpleNamespace(bot=_FakeBot())


def _flat(markup):
    return [b for row in markup.inline_keyboard for b in row]


# ═════════════════════════════════════════════════════════════════
# Info card content
# ═════════════════════════════════════════════════════════════════

class TestInfoCardContent(unittest.IsolatedAsyncioTestCase):
    async def test_all_fields_present(self):
        text = await users_mod._build_info_text(_FakeBot(), _user())
        for label in (
            "ID", "First Name", "Last Name", "Username", "Mention",
            "DC ID", "Bio", "Custom Bio", "Custom Tag", "Profile Photos",
            "Health", "AFK Status", "Common Groups",
            "Globally Banned", "Globally Muted",
        ):
            with self.subTest(label=label):
                self.assertIn(f"{label}:", text)

    async def test_real_data_sources(self):
        bot = _FakeBot(bio="Sorcerer Supreme", photos=7)
        text = await users_mod._build_info_text(bot, _user())
        self.assertIn(f"<code>{ME_ID}</code>", text)
        self.assertIn("First Name: Nobara", text)
        self.assertIn("Last Name: Kugisaki", text)
        self.assertIn("Username: @nobara", text)
        self.assertIn("Sorcerer Supreme", text)
        self.assertIn("Profile Photos: 7 photos", text)
        self.assertIn(f"tg://user?id={ME_ID}", text)
        # Fresh DB defaults: no warnings → 100%, not banned/muted.
        self.assertIn("Health: 100% ▰▰▰▰▰▰▰▰▰▰", text)
        self.assertIn("Globally Banned: No", text)
        self.assertIn("Globally Muted: No", text)
        self.assertIn("AFK Status: No", text)

    async def test_missing_bio_is_na(self):
        text = await users_mod._build_info_text(_FakeBot(bio=None), _user())
        self.assertIn("Bio: n/a", text)

    async def test_health_derived_from_warnings(self):
        orig_get_user = db.get_user
        orig_banned = db.is_gbanned
        db.get_user = lambda uid: {"warnings": 2, "is_muted": 1}
        db.is_gbanned = lambda uid: True
        try:
            text = await users_mod._build_info_text(_FakeBot(), _user())
        finally:
            db.get_user = orig_get_user
            db.is_gbanned = orig_banned
        self.assertIn("Health: 50% ▰▰▰▰▰▱▱▱▱", text)
        self.assertIn("Globally Banned: Yes", text)
        self.assertIn("Globally Muted: Yes", text)

    async def test_fields_use_custom_emoji(self):
        """Field icons must come from the owner's custom emoji set (E.*)."""
        text = await users_mod._build_info_text(_FakeBot(), _user())
        self.assertIn("<tg-emoji", text)
        # Plain unicode icons that must no longer appear anywhere.
        for plain in ("🆔", "📛", "🔗", "📝", "✍️", "🏷", "📸", "❤️", "👥", "🚫", "🤐"):
            with self.subTest(plain=plain):
                self.assertNotIn(plain, text)
        # Card title follows the spec template wording.
        self.assertIn("User Information", text)

    async def test_keyboard_my_info_and_close(self):
        markup = users_mod.info_keyboard()
        buttons = _flat(markup)
        self.assertEqual(len(buttons), 2)
        self.assertEqual(buttons[0].text, "My Info")
        self.assertEqual(buttons[0].callback_data, "info:me")
        self.assertEqual(
            (getattr(buttons[0], "api_kwargs", None) or {}).get("style"),
            "primary",
        )
        self.assertEqual(buttons[1].text, "Close")
        self.assertEqual(buttons[1].callback_data, "info:close")
        self.assertEqual(
            (getattr(buttons[1], "api_kwargs", None) or {}).get("style"),
            "danger",
        )


# ═════════════════════════════════════════════════════════════════
# /info, /myinfo, /userinfo
# ═════════════════════════════════════════════════════════════════

class TestInfoCommands(unittest.IsolatedAsyncioTestCase):
    async def test_info_defaults_to_self(self):
        upd, ctx = _cmd_update()
        await users_mod.info_command(upd, ctx)
        self.assertIn(f"<code>{ME_ID}</code>", upd.message.last["text"])
        self.assertIsNotNone(upd.message.last["markup"])
        self.assertEqual(
            _flat(upd.message.last["markup"])[0].callback_data, "info:me"
        )

    async def test_info_numeric_target(self):
        upd, ctx = _cmd_update(args=[str(TARGET_ID)])
        await users_mod.info_command(upd, ctx)
        self.assertIn(f"<code>{TARGET_ID}</code>", upd.message.last["text"])
        self.assertIn("First Name: Target", upd.message.last["text"])
        # Resolved by numeric id (and used again for the bio lookup).
        self.assertIn(TARGET_ID, ctx.bot.get_chat_calls)

    async def test_info_unknown_target_errors(self):
        upd, ctx = _cmd_update(args=["999999"])
        await users_mod.info_command(upd, ctx)
        self.assertIn("Could not find", upd.message.last["text"])
        self.assertIn("Usage", upd.message.last["text"])

    async def test_info_reply_target(self):
        upd, ctx = _cmd_update(reply=SimpleNamespace(from_user=_user(TARGET_ID, "Replied")))
        await users_mod.info_command(upd, ctx)
        self.assertIn(f"<code>{TARGET_ID}</code>", upd.message.last["text"])
        self.assertIn("First Name: Replied", upd.message.last["text"])

    async def test_info_mention_target(self):
        upd, ctx = _cmd_update(args=["@mentioned"])
        await users_mod.info_command(upd, ctx)
        self.assertIn(f"<code>{TARGET_ID}</code>", upd.message.last["text"])

    async def test_myinfo_always_self(self):
        upd, ctx = _cmd_update(args=["ignored"])
        await users_mod.myinfo_command(upd, ctx)
        self.assertIn(f"<code>{ME_ID}</code>", upd.message.last["text"])

    async def test_userinfo_requires_target(self):
        upd, ctx = _cmd_update()
        await users_mod.userinfo_command(upd, ctx)
        self.assertIn("Please specify a user", upd.message.last["text"])

    async def test_userinfo_with_target(self):
        upd, ctx = _cmd_update(args=[str(TARGET_ID)])
        await users_mod.userinfo_command(upd, ctx)
        self.assertIn(f"<code>{TARGET_ID}</code>", upd.message.last["text"])


# ═════════════════════════════════════════════════════════════════
# info:* callbacks
# ═════════════════════════════════════════════════════════════════

class TestInfoCallback(unittest.IsolatedAsyncioTestCase):
    async def test_my_info_renders_for_clicker(self):
        upd, ctx = _cb_update("info:me")
        await users_mod.info_callback(upd, ctx)
        self.assertEqual(len(upd.callback_query.answers), 1)
        self.assertEqual(len(upd.callback_query.edits), 1)
        edit = upd.callback_query.edits[0]
        # Clicker is TARGET_ID, not the original sender.
        self.assertIn(f"<code>{TARGET_ID}</code>", edit["text"])
        self.assertIn("Clicker Person", edit["text"])
        self.assertIsNotNone(edit["markup"])
        self.assertFalse(upd.callback_query.message.deleted)

    async def test_close_deletes_message(self):
        upd, ctx = _cb_update("info:close")
        await users_mod.info_callback(upd, ctx)
        self.assertTrue(upd.callback_query.message.deleted)
        self.assertEqual(upd.callback_query.edits, [])

    async def test_unknown_action_alerts(self):
        upd, ctx = _cb_update("info:bogus")
        await users_mod.info_callback(upd, ctx)
        self.assertTrue(upd.callback_query.answers[0]["show_alert"])
        self.assertEqual(upd.callback_query.edits, [])

    async def test_foreign_callback_ignored(self):
        upd, ctx = _cb_update("tag:whatever")
        await users_mod.info_callback(upd, ctx)
        self.assertEqual(upd.callback_query.answers, [])


# ═════════════════════════════════════════════════════════════════
# /id
# ═════════════════════════════════════════════════════════════════

class TestIdCommand(unittest.IsolatedAsyncioTestCase):
    async def test_id_chat_and_user(self):
        upd, ctx = _cmd_update()
        await users_mod.id_command(upd, ctx)
        text = upd.message.last["text"]
        self.assertIn("Chat ID: <code>-100123</code>", text)
        self.assertIn("Your ID: <code>42</code>", text)
        self.assertNotIn("Target", text)

    async def test_id_with_reply_target(self):
        upd, ctx = _cmd_update(reply=SimpleNamespace(from_user=_user(TARGET_ID)))
        await users_mod.id_command(upd, ctx)
        text = upd.message.last["text"]
        self.assertIn("Chat ID:", text)
        self.assertIn(f"<code>{TARGET_ID}</code>", text)
        self.assertIn("tg://user?id=777", text)

    async def test_id_private_chat(self):
        upd, ctx = _cmd_update(chat_type="private")
        upd.effective_chat.id = ME_ID  # DM chat id == user id
        await users_mod.id_command(upd, ctx)
        text = upd.message.last["text"]
        self.assertIn(f"<code>{ME_ID}</code>", text)

    async def test_id_uses_custom_emoji(self):
        """Chat/Your/Target icons must be custom, never plain 💬/🎯."""
        upd, ctx = _cmd_update(reply=SimpleNamespace(from_user=_user(TARGET_ID)))
        await users_mod.id_command(upd, ctx)
        text = upd.message.last["text"]
        self.assertIn("<tg-emoji", text)
        self.assertNotIn("💬", text)
        self.assertNotIn("🎯", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
