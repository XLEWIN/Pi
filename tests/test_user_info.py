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
from aiofakes import call, make_callback, make_message  # noqa: E402

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


def _cmd(args=None, *, reply=None, chat_type="supergroup"):
    """Command message + bot — reply_text records on ``msg.calls``."""
    msg = make_message(
        "/info", chat_id=-100123, chat_type=chat_type,
        reply_to_message=reply,
    )
    return msg, _FakeBot()


def _cb(data: str, *, user_id=TARGET_ID, first="Clicker", last="Person"):
    """info:* callback — its message records edits/deletes."""
    cb = make_callback(data, user_id=user_id)
    cb.from_user.first_name = first
    cb.from_user.last_name = last
    cb.from_user.full_name = f"{first} {last}".strip()
    return cb


def _sends(msg, kinds=("reply", "answer")):
    """(text, markup) dicts for the given send kinds, oldest first."""
    return [
        {"text": t, "markup": kw.get("reply_markup")}
        for (k, t, kw) in msg.calls if k in kinds
    ]


def _last(msg):
    sent = _sends(msg)
    return sent[-1] if sent else None


def _edits(msg):
    return _sends(msg, kinds=("edit_text",))


def _deleted(msg):
    return any(k == "delete" for (k, _, _) in msg.calls)


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
        self.assertEqual(getattr(buttons[0], "style", None), "primary")
        self.assertEqual(buttons[1].text, "Close")
        self.assertEqual(buttons[1].callback_data, "info:close")
        self.assertEqual(getattr(buttons[1], "style", None), "danger")


# ═════════════════════════════════════════════════════════════════
# /info, /myinfo, /userinfo
# ═════════════════════════════════════════════════════════════════

class TestInfoCommands(unittest.IsolatedAsyncioTestCase):
    async def test_info_defaults_to_self(self):
        msg, bot = _cmd()
        await call(users_mod.info_command, msg, bot=bot)
        last = _last(msg)
        self.assertIn(f"<code>{ME_ID}</code>", last["text"])
        self.assertIsNotNone(last["markup"])
        self.assertEqual(_flat(last["markup"])[0].callback_data, "info:me")

    async def test_info_numeric_target(self):
        msg, bot = _cmd()
        await call(users_mod.info_command, msg, bot=bot,
                   args=[str(TARGET_ID)])
        last = _last(msg)
        self.assertIn(f"<code>{TARGET_ID}</code>", last["text"])
        self.assertIn("First Name: Target", last["text"])
        # Resolved by numeric id (and used again for the bio lookup).
        self.assertIn(TARGET_ID, bot.get_chat_calls)

    async def test_info_unknown_target_errors(self):
        msg, bot = _cmd()
        await call(users_mod.info_command, msg, bot=bot, args=["999999"])
        last = _last(msg)
        self.assertIn("Could not find", last["text"])
        self.assertIn("Usage", last["text"])

    async def test_info_reply_target(self):
        msg, bot = _cmd(
            reply=SimpleNamespace(from_user=_user(TARGET_ID, "Replied"))
        )
        await call(users_mod.info_command, msg, bot=bot)
        last = _last(msg)
        self.assertIn(f"<code>{TARGET_ID}</code>", last["text"])
        self.assertIn("First Name: Replied", last["text"])

    async def test_info_mention_target(self):
        msg, bot = _cmd()
        await call(users_mod.info_command, msg, bot=bot, args=["@mentioned"])
        self.assertIn(f"<code>{TARGET_ID}</code>", _last(msg)["text"])

    async def test_myinfo_always_self(self):
        msg, bot = _cmd()
        await call(users_mod.myinfo_command, msg, bot=bot, args=["ignored"])
        self.assertIn(f"<code>{ME_ID}</code>", _last(msg)["text"])

    async def test_userinfo_requires_target(self):
        msg, bot = _cmd()
        await call(users_mod.userinfo_command, msg, bot=bot, args=[])
        self.assertIn("Please specify a user", _last(msg)["text"])

    async def test_userinfo_with_target(self):
        msg, bot = _cmd()
        await call(users_mod.userinfo_command, msg, bot=bot,
                   args=[str(TARGET_ID)])
        self.assertIn(f"<code>{TARGET_ID}</code>", _last(msg)["text"])


# ═════════════════════════════════════════════════════════════════
# info:* callbacks
# ═════════════════════════════════════════════════════════════════

class TestInfoCallback(unittest.IsolatedAsyncioTestCase):
    async def test_my_info_renders_for_clicker(self):
        cb = _cb("info:me")
        await call(users_mod.info_callback, cb, bot=_FakeBot())
        self.assertEqual(len(cb.answers), 1)
        edits = _edits(cb.message)
        self.assertEqual(len(edits), 1)
        # Clicker is TARGET_ID, not the original sender.
        self.assertIn(f"<code>{TARGET_ID}</code>", edits[0]["text"])
        self.assertIn("Clicker Person", edits[0]["text"])
        self.assertIsNotNone(edits[0]["markup"])
        self.assertFalse(_deleted(cb.message))

    async def test_close_deletes_message(self):
        cb = _cb("info:close")
        await call(users_mod.info_callback, cb, bot=_FakeBot())
        self.assertTrue(_deleted(cb.message))
        self.assertEqual(_edits(cb.message), [])

    async def test_unknown_action_alerts(self):
        cb = _cb("info:bogus")
        await call(users_mod.info_callback, cb, bot=_FakeBot())
        self.assertTrue(cb.answers[0]["show_alert"])
        self.assertEqual(_edits(cb.message), [])

    async def test_foreign_callback_ignored(self):
        cb = _cb("tag:whatever")
        await call(users_mod.info_callback, cb, bot=_FakeBot())
        self.assertEqual(cb.answers, [])


# ═════════════════════════════════════════════════════════════════
# /id
# ═════════════════════════════════════════════════════════════════

class TestIdCommand(unittest.IsolatedAsyncioTestCase):
    async def test_id_chat_and_user(self):
        msg, _bot = _cmd()
        await call(users_mod.id_command, msg)
        text = _last(msg)["text"]
        self.assertIn("Chat ID: <code>-100123</code>", text)
        self.assertIn("Your ID: <code>42</code>", text)
        self.assertNotIn("Target", text)

    async def test_id_with_reply_target(self):
        msg, _bot = _cmd(reply=SimpleNamespace(from_user=_user(TARGET_ID)))
        await call(users_mod.id_command, msg)
        text = _last(msg)["text"]
        self.assertIn("Chat ID:", text)
        self.assertIn(f"<code>{TARGET_ID}</code>", text)
        self.assertIn("tg://user?id=777", text)

    async def test_id_private_chat(self):
        msg, _bot = _cmd(chat_type="private")
        msg.chat.id = ME_ID  # DM chat id == user id
        await call(users_mod.id_command, msg)
        text = _last(msg)["text"]
        self.assertIn(f"<code>{ME_ID}</code>", text)

    async def test_id_uses_custom_emoji(self):
        """Chat/Your/Target icons must be custom, never plain 💬/🎯."""
        msg, _bot = _cmd(reply=SimpleNamespace(from_user=_user(TARGET_ID)))
        await call(users_mod.id_command, msg)
        text = _last(msg)["text"]
        self.assertIn("<tg-emoji", text)
        self.assertNotIn("💬", text)
        self.assertNotIn("🎯", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
