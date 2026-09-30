"""Tests for content locks (bot/modules/locks.py).

Run from the repo root:

    python tests/test_locks.py

Environment isolation (BEFORE any bot import):
    * BOT_TOKEN is forced — importing `bot` pulls bot.config, which
      exits without one.
    * LOCALAPPDATA points at a temp dir so runtime files are isolated.

No network: fake messages/callbacks; db and _is_admin patched.
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
_TEST_DIR = tempfile.mkdtemp(prefix="pi_locks_test_")
os.environ["LOCALAPPDATA"] = _TEST_DIR
atexit.register(shutil.rmtree, _TEST_DIR, ignore_errors=True)

# ── Imports (after env) ───────────────────────────────────────────
from aiofakes import FakeBot, FakeMessage, call, make_callback  # noqa: E402

from bot.modules import locks as lm  # noqa: E402

CHAT = -100777001


# ═════════════════════════════════════════════════════════════════
# Fakes + patches
# ═════════════════════════════════════════════════════════════════

def _msg(text: str = "", **kw) -> FakeMessage:
    """FakeMessage with every attribute _violates() reads defaulted."""
    defaults = dict(
        caption=None,
        entities=None,
        caption_entities=None,
        media_group_id=None,
        sender_chat=None,
        audio=None,
        via_bot=None,
        reply_markup=None,
        contact=None,
        document=None,
        dice=None,
        external_reply=None,
        forward_origin=None,
        game=None,
        animation=None,
        location=None,
        photo=None,
        poll=None,
        sticker=None,
        video=None,
        video_note=None,
        voice=None,
    )
    defaults.update(kw)
    return FakeMessage(text, chat_id=CHAT, **defaults)


def _db(locks=()):
    """Fake db recording set/unset calls; get_locks returns current set."""
    state = {"locks": list(locks), "calls": []}

    def set_lock(chat_id, lock_type):
        state["calls"].append(("set", chat_id, lock_type))
        if lock_type not in state["locks"]:
            state["locks"].append(lock_type)

    def unset_lock(chat_id, lock_type):
        state["calls"].append(("unset", chat_id, lock_type))
        if lock_type == "all":
            state["locks"].clear()
        else:
            state["locks"] = [t for t in state["locks"] if t != lock_type]

    def get_locks(chat_id):
        return sorted(set(state["locks"]))

    fake = SimpleNamespace(
        set_lock=set_lock, unset_lock=unset_lock, get_locks=get_locks
    )
    return fake, state


@contextmanager
def _patched(locks=(), is_admin=False):
    fake, state = _db(locks)
    with mock.patch.object(lm, "db", fake), \
            mock.patch.object(
                lm, "_is_admin",
                new=_async_result(is_admin),
            ):
        yield state


def _async_result(value):
    async def _fn(*a, **k):
        return value
    return _fn


def _entity(type_: str, **kw):
    return SimpleNamespace(type=type_, **kw)


def _deleted(msg) -> bool:
    return any(k == "delete" for (k, _, _) in msg.calls)


def _last_reply(msg):
    for kind, text, kw in reversed(msg.calls):
        if kind in ("reply", "answer"):
            return text, kw
    return None, None


# ═════════════════════════════════════════════════════════════════
# LOCKABLES + _violates (pure logic)
# ═════════════════════════════════════════════════════════════════

class TestLockables(unittest.TestCase):
    def test_boa_types_present(self):
        expected = {
            "all", "album", "anonchannel", "audio", "bot", "botlink",
            "btn", "cjk", "command", "contact", "cyrillic", "document",
            "email", "emoji", "emoji_custom", "dice", "external_reply",
            "forward", "game", "gif", "inline", "invitelink", "location",
            "phone", "photo", "poll", "rtl", "spoiler", "sticker",
            "animated_sticker", "premium_sticker", "text", "url", "video",
            "videonote", "voice",
        }
        self.assertEqual(set(lm.LOCKABLES), expected)

    def test_every_type_has_a_description(self):
        for name, desc in lm.LOCKABLES.items():
            self.assertTrue(desc.strip(), name)


class TestViolates(unittest.TestCase):
    def test_all_locks_everything(self):
        self.assertTrue(lm._violates({"all"}, _msg("hi")))

    def test_text_lock_hits_text_only(self):
        self.assertTrue(lm._violates({"text"}, _msg("hello")))
        self.assertFalse(lm._violates({"text"}, _msg("", photo=object())))

    def test_photo_lock(self):
        self.assertTrue(lm._violates({"photo"}, _msg(photo=object())))
        self.assertFalse(lm._violates({"photo"}, _msg("hello")))

    def test_command_lock(self):
        self.assertTrue(lm._violates({"command"}, _msg("/start")))
        self.assertFalse(lm._violates({"command"}, _msg("start")))

    def test_url_lock_text_and_caption_entities(self):
        msg = _msg("see https://x.co", entities=[_entity("url")])
        self.assertTrue(lm._violates({"url"}, msg))
        cap = _msg("", photo=object(), caption_entities=[_entity("url")])
        self.assertTrue(lm._violates({"url"}, cap))
        self.assertFalse(lm._violates({"url"}, _msg("plain")))

    def test_cyrillic_and_cjk_and_rtl(self):
        self.assertTrue(lm._violates({"cyrillic"}, _msg("привет")))
        self.assertTrue(lm._violates({"cjk"}, _msg("你好")))
        self.assertTrue(lm._violates({"rtl"}, _msg("مرحبا")))
        self.assertFalse(lm._violates({"cyrillic", "cjk", "rtl"},
                                      _msg("hello")))

    def test_emoji_lock(self):
        self.assertTrue(lm._violates({"emoji"}, _msg("hi 😀")))
        self.assertFalse(lm._violates({"emoji"}, _msg("hi")))

    def test_album_lock(self):
        msg = _msg("", photo=object(), media_group_id="123-456")
        self.assertTrue(lm._violates({"album"}, msg))
        self.assertFalse(lm._violates({"album"}, _msg("", photo=object())))

    def test_button_lock(self):
        msg = _msg("x", reply_markup=SimpleNamespace())
        self.assertTrue(lm._violates({"btn"}, msg))

    def test_forward_lock(self):
        msg = _msg("x", forward_origin=SimpleNamespace())
        self.assertTrue(lm._violates({"forward"}, msg))

    def test_email_and_phone_entities(self):
        msg = _msg("a@b.c", entities=[_entity("email")])
        self.assertTrue(lm._violates({"email"}, msg))
        phone = _msg("+123", entities=[_entity("phone_number")])
        self.assertTrue(lm._violates({"phone"}, phone))

    def test_sticker_variant_locks(self):
        plain = SimpleNamespace(is_animated=False, premium_animation=None)
        anim = SimpleNamespace(is_animated=True, premium_animation=None)
        prem = SimpleNamespace(is_animated=False,
                               premium_animation=SimpleNamespace())
        self.assertTrue(lm._violates({"sticker"}, _msg(sticker=plain)))
        self.assertTrue(lm._violates({"animated_sticker"},
                                     _msg(sticker=anim)))
        self.assertFalse(lm._violates({"animated_sticker"},
                                      _msg(sticker=plain)))
        self.assertTrue(lm._violates({"premium_sticker"},
                                     _msg(sticker=prem)))


# ═════════════════════════════════════════════════════════════════
# Commands
# ═════════════════════════════════════════════════════════════════

class TestCommands(unittest.IsolatedAsyncioTestCase):
    async def test_lock_requires_admin(self):
        msg = _msg("/lock photo")
        with _patched(is_admin=False):
            await call(lm.lock_command, msg, args=["photo"])
        text, _ = _last_reply(msg)
        self.assertIn("Only admins", text)

    async def test_lock_usage_without_args(self):
        msg = _msg("/lock")
        with _patched(is_admin=True):
            await call(lm.lock_command, msg, args=[])
        text, _ = _last_reply(msg)
        self.assertIn("Usage", text)

    async def test_lock_invalid_type(self):
        msg = _msg("/lock banana")
        with _patched(is_admin=True):
            await call(lm.lock_command, msg, args=["banana"])
        text, _ = _last_reply(msg)
        self.assertIn("Invalid lock type", text)

    async def test_lock_sets_types(self):
        msg = _msg("/lock photo video")
        with _patched(is_admin=True) as state:
            await call(lm.lock_command, msg, args=["photo", "video"])
        self.assertEqual(
            state["calls"],
            [("set", CHAT, "photo"), ("set", CHAT, "video")],
        )
        text, _ = _last_reply(msg)
        self.assertIn("Locked", text)
        self.assertIn("photo, video", text)

    async def test_lock_all(self):
        msg = _msg("/lock all")
        with _patched(is_admin=True) as state:
            await call(lm.lock_command, msg, args=["all"])
        self.assertEqual(state["calls"], [("set", CHAT, "all")])

    async def test_unlock_clears(self):
        msg = _msg("/unlock photo")
        with _patched(locks=["photo", "video"], is_admin=True) as state:
            await call(lm.unlock_command, msg, args=["photo"])
        self.assertEqual(state["calls"], [("unset", CHAT, "photo")])
        self.assertEqual(state["locks"], ["video"])

    async def test_unlock_all_wipes_chat(self):
        msg = _msg("/unlock all")
        with _patched(locks=["photo", "text"], is_admin=True) as state:
            await call(lm.unlock_command, msg, args=["all"])
        self.assertEqual(state["locks"], [])
        text, _ = _last_reply(msg)
        self.assertIn("Unlocked", text)

    async def test_unlock_requires_admin(self):
        msg = _msg("/unlock photo")
        with _patched(locks=["photo"], is_admin=False):
            await call(lm.unlock_command, msg, args=["photo"])
        text, _ = _last_reply(msg)
        self.assertIn("Only admins", text)

    async def test_locks_listing_empty(self):
        msg = _msg("/locks")
        with _patched(locks=()):
            await call(lm.locks_command, msg, args=[])
        text, _ = _last_reply(msg)
        self.assertIn("No locks", text)

    async def test_locks_listing_active(self):
        msg = _msg("/locks")
        with _patched(locks=["photo", "text"]):
            await call(lm.locks_command, msg, args=[])
        text, _ = _last_reply(msg)
        self.assertIn("Active locks", text)
        self.assertIn("photo, text", text)

    async def test_locks_private_group_only(self):
        msg = FakeMessage("/locks", chat_id=CHAT, chat_type="private")
        with _patched(locks=("photo",)):
            await call(lm.locks_command, msg, args=[])
        text, _ = _last_reply(msg)
        self.assertIn("groups", text)

    async def test_locktypes_keyboard(self):
        msg = _msg("/locktypes")
        with _patched():
            await call(lm.locktypes_command, msg, args=[])
        text, kw = _last_reply(msg)
        self.assertIn("Lock Types", text)
        markup = kw.get("reply_markup")
        self.assertIsNotNone(markup)
        rows = markup.inline_keyboard
        flat = [b for row in rows for b in row]
        self.assertEqual(len(flat), len(lm.LOCKABLES))
        self.assertEqual(len(rows), (len(lm.LOCKABLES) + 2) // 3)
        self.assertTrue(all(b.callback_data.startswith("locktype:")
                            for b in flat))

    async def test_locktype_callback_alert(self):
        cb = make_callback("locktype:text")
        await call(lm.locktype_callback, cb)
        self.assertEqual(len(cb.answers), 1)
        answer = cb.answers[0]
        self.assertTrue(answer["show_alert"])
        self.assertIn("text lock", answer["text"].lower())
        self.assertIn(lm.LOCKABLES["text"], answer["text"])


# ═════════════════════════════════════════════════════════════════
# Enforcement
# ═════════════════════════════════════════════════════════════════

class TestEnforce(unittest.IsolatedAsyncioTestCase):
    async def test_deletes_violation(self):
        msg = _msg("hello world")
        with _patched(locks=("text",), is_admin=False):
            await call(lm.enforce_locks, msg, bot=FakeBot())
        self.assertTrue(_deleted(msg))

    async def test_admin_exempt(self):
        msg = _msg("hello world")
        with _patched(locks=("text",), is_admin=True):
            await call(lm.enforce_locks, msg, bot=FakeBot())
        self.assertFalse(_deleted(msg))

    async def test_no_locks_no_delete(self):
        msg = _msg("hello world")
        with _patched(locks=(), is_admin=False):
            await call(lm.enforce_locks, msg, bot=FakeBot())
        self.assertFalse(_deleted(msg))

    async def test_non_matching_lock_keeps_message(self):
        msg = _msg("hello world")
        with _patched(locks=("photo",), is_admin=False):
            await call(lm.enforce_locks, msg, bot=FakeBot())
        self.assertFalse(_deleted(msg))

    async def test_private_chat_ignored(self):
        msg = FakeMessage("hello", chat_id=CHAT, chat_type="private")
        with _patched(locks=("text",), is_admin=False):
            await call(lm.enforce_locks, msg, bot=FakeBot())
        self.assertFalse(_deleted(msg))

    async def test_bot_own_messages_exempt(self):
        msg = _msg("announcement", user_id=1, is_bot=True)  # bot id=1
        with _patched(locks=("text",), is_admin=False):
            await call(lm.enforce_locks, msg, bot=FakeBot(user_id=1))
        self.assertFalse(_deleted(msg))


# ═════════════════════════════════════════════════════════════════
# Registration
# ═════════════════════════════════════════════════════════════════

class TestSetup(unittest.TestCase):
    def test_setup_registers_everything(self):
        result = lm.setup()
        for item in ("/lock", "/unlock", "/locks", "/locktypes",
                     "lock-enforce"):
            self.assertIn(item, result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
