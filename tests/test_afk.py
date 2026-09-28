"""AFK module tests (bot/modules/afk.py) - the boa port.

Run from the Pi/Pi root:

    python tests/test_afk.py
    python -m unittest discover -s tests

Covers the full boa feature set in Pi form: the /afk|/brb + plain-text
toggle, reason/replied-media storage, reply/text-mention/@username
notices, auto-return, and the group wiring (mention=22, return=23,
toggle texts excluded from the return handler).

Environment isolation (BEFORE any bot import):
    * BOT_TOKEN is forced - importing `bot` pulls bot.config, which
      exits without one.
    * LOCALAPPDATA points at a temp dir so any DB use at import is
      isolated (unittest already imported -> mongomock backend).
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

# ── Environment isolation - must precede bot imports ──────────────
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["BOT_TOKEN"] = "1:TEST-TOKEN-FOR-UNIT-TESTS"
_TEST_DIR = tempfile.mkdtemp(prefix="pi_afk_test_")
os.environ["LOCALAPPDATA"] = _TEST_DIR
atexit.register(shutil.rmtree, _TEST_DIR, ignore_errors=True)

# ── Imports (after env) ───────────────────────────────────────────
from aiogram.dispatcher.event.handler import FilterObject, HandlerObject  # noqa: E402

from aiofakes import FakeBot, call, make_message  # noqa: E402
from bot import pipeline  # noqa: E402
from bot.database import db  # noqa: E402
from bot.loader import load_modules  # noqa: E402
from bot.modules import afk as am  # noqa: E402

CHAT_ID = -100888001
USER_A = 8880001    # sender / viewer
USER_B = 8880002    # the AFK target


def _msg(text, *, user_id=USER_A, first_name="Lewin", username="lewin",
         reply_to=None, **kw):
    return make_message(
        text, chat_id=CHAT_ID, chat_type="supergroup",
        user_id=user_id, first_name=first_name, username=username,
        reply_to_message=reply_to, **kw,
    )


def _sent(msg):
    return msg.last[1] if msg.last else None


def _kw(msg):
    return msg.last[2] if msg.last else {}


def _cleanup():
    db.collection("afk").delete_many({})


def _iso(delta: timedelta) -> str:
    return (datetime.now() - delta).isoformat()


# ── Toggle (/afk, /brb, plain off/brb) ────────────────────────────
class TestToggle(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _cleanup()

    def tearDown(self):
        _cleanup()

    async def test_set_afk_stores_reason_and_replies_card(self):
        msg = _msg("/afk gone for lunch")
        await call(am.afk_command, msg)
        doc = db.get_afk(USER_A)
        self.assertIsNotNone(doc)
        self.assertEqual(doc["afk_reason"], "gone for lunch")
        self.assertEqual(doc["user_first_name"], "Lewin")
        self.assertEqual(doc["username"], "lewin")  # lowercased
        self.assertIsNone(doc["media_id"])
        text = _sent(msg)
        self.assertIn("AFK Mode Enabled", text)
        self.assertIn("gone for lunch", text)
        self.assertIn("<tg-emoji", text)
        self.assertEqual(_kw(msg).get("parse_mode"), "HTML")

    async def test_set_without_reason(self):
        msg = _msg("/afk")
        await call(am.afk_command, msg)
        doc = db.get_afk(USER_A)
        self.assertIsNotNone(doc)
        self.assertIsNone(doc["afk_reason"])
        self.assertIn("AFK Mode Enabled", _sent(msg))
        self.assertNotIn("Reason", _sent(msg))

    async def test_reply_media_captured(self):
        photo_msg = make_message("old photo", chat_id=CHAT_ID,
                                 chat_type="supergroup")
        photo_msg.photo = SimpleNamespace(file_id="PHOTO_FILE_1")
        msg = _msg("/afk check this", reply_to=photo_msg)
        await call(am.afk_command, msg)
        doc = db.get_afk(USER_A)
        self.assertEqual(doc["media_id"], "PHOTO_FILE_1")
        self.assertEqual(doc["media_type"], "photo")

    async def test_second_toggle_clears_with_welcome(self):
        msg1 = _msg("/afk out")
        await call(am.afk_command, msg1)
        self.assertIsNotNone(db.get_afk(USER_A))

        msg2 = _msg("/afk")
        await call(am.afk_command, msg2)
        self.assertIsNone(db.get_afk(USER_A))
        text = _sent(msg2)
        self.assertIn("Welcome Back", text)
        self.assertIn("Away", text)
        self.assertIn("<tg-emoji", text)
        self.assertEqual(_kw(msg2).get("parse_mode"), "HTML")


# ── Notices (reply / text-mention / @username) ────────────────────
class TestMention(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _cleanup()
        db.set_afk(
            USER_B, "Sam", "Sammy", "in a meeting",
            _iso(timedelta(minutes=75)),
        )

    def tearDown(self):
        _cleanup()

    async def test_reply_to_afk_user_notifies(self):
        target = make_message("old message", chat_id=CHAT_ID,
                              chat_type="supergroup",
                              user_id=USER_B, first_name="Sam",
                              username="sammy")
        msg = _msg("hello there", reply_to=target)
        bot = FakeBot()
        await call(am.afk_mention_handler, msg, bot=bot)
        text = _sent(msg)
        self.assertIsNotNone(text, "no notice sent")
        self.assertIn("User Is Away", text)
        self.assertIn(">Sam<", text)          # first name, not @username
        self.assertIn("in a meeting", text)
        self.assertIn("AFK for", text)
        self.assertRegex(text, r"\d+ (hour|minute|second)")
        self.assertEqual(_kw(msg).get("parse_mode"), "HTML")
        self.assertEqual(bot.sent, [], "text notice uses reply, not bot.send")

    async def test_username_word_is_case_insensitive(self):
        msg = _msg("yo @SAMMY wrap up")  # stored lowercased
        bot = FakeBot()
        await call(am.afk_mention_handler, msg, bot=bot)
        self.assertIn("User Is Away", _sent(msg))

    async def test_text_mention_entity_notifies(self):
        entity = SimpleNamespace(
            type="text_mention",
            user=SimpleNamespace(id=USER_B, is_bot=False, first_name="Sam"),
        )
        msg = _msg("ping over here", entities=[entity])
        bot = FakeBot()
        await call(am.afk_mention_handler, msg, bot=bot)
        self.assertIn("User Is Away", _sent(msg))

    async def test_plain_at_mention_not_in_afk_is_silent(self):
        msg = _msg("hi @nobodyhere")
        bot = FakeBot()
        await call(am.afk_mention_handler, msg, bot=bot)
        self.assertIsNone(_sent(msg))

    async def test_reply_to_non_afk_is_silent(self):
        target = make_message("old", chat_id=CHAT_ID, chat_type="supergroup",
                              user_id=999, first_name="Other")
        msg = _msg("hi", reply_to=target)
        bot = FakeBot()
        await call(am.afk_mention_handler, msg, bot=bot)
        self.assertIsNone(_sent(msg))

    async def test_sender_pinging_own_afk_row_is_ignored(self):
        own = make_message("mine", chat_id=CHAT_ID, chat_type="supergroup",
                           user_id=USER_B, first_name="Sam")
        msg = _msg("scratch", reply_to=own, user_id=USER_B,
                   first_name="Sam", username="sammy")
        bot = FakeBot()
        await call(am.afk_mention_handler, msg, bot=bot)
        self.assertIsNone(_sent(msg))

    async def test_reason_is_html_escaped(self):
        _cleanup()
        db.set_afk(USER_B, "Sam", "sammy", "<b> & \"urgent\"",
                   _iso(timedelta(minutes=3)))
        target = make_message("old", chat_id=CHAT_ID, chat_type="supergroup",
                              user_id=USER_B, first_name="Sam")
        msg = _msg("ping", reply_to=target)
        bot = FakeBot()
        await call(am.afk_mention_handler, msg, bot=bot)
        text = _sent(msg)
        self.assertIn("&lt;b&gt; &amp;", text)
        self.assertNotIn("<b>", text)

    async def test_photo_notice_carries_caption(self):
        _cleanup()
        db.set_afk(USER_B, "Sam", "sammy", None,
                   _iso(timedelta(minutes=10)),
                   media_id="PIC_1", media_type="photo")
        target = make_message("old", chat_id=CHAT_ID, chat_type="supergroup",
                              user_id=USER_B, first_name="Sam")
        msg = _msg("ping", reply_to=target)
        bot = FakeBot()
        await call(am.afk_mention_handler, msg, bot=bot)
        self.assertEqual(len(bot.sent), 1)
        self.assertEqual(bot.sent[0].get("photo"), "PIC_1")
        self.assertIn("User Is Away", bot.sent[0].get("caption", ""))
        self.assertIsNone(_sent(msg), "media path must not double-post")

    async def test_video_note_posts_then_card(self):
        _cleanup()
        db.set_afk(USER_B, "Sam", "sammy", None,
                   _iso(timedelta(minutes=10)),
                   media_id="VN_1", media_type="video_note")
        target = make_message("old", chat_id=CHAT_ID, chat_type="supergroup",
                              user_id=USER_B, first_name="Sam")
        msg = _msg("ping", reply_to=target)
        bot = FakeBot()
        await call(am.afk_mention_handler, msg, bot=bot)
        self.assertEqual(len(bot.sent), 1)          # the video note
        self.assertEqual(bot.sent[0].get("video_note"), "VN_1")
        self.assertIn("User Is Away", _sent(msg))    # follow-up card

    async def test_expired_media_falls_back_to_text(self):
        _cleanup()
        db.set_afk(USER_B, "Sam", "sammy", None,
                   _iso(timedelta(minutes=10)),
                   media_id="DEAD_ID", media_type="photo")
        target = make_message("old", chat_id=CHAT_ID, chat_type="supergroup",
                              user_id=USER_B, first_name="Sam")
        msg = _msg("ping", reply_to=target)
        bot = FakeBot()

        async def boom(*a, **k):
            raise RuntimeError("file_id expired")

        bot.send_photo = boom
        await call(am.afk_mention_handler, msg, bot=bot)
        self.assertIn("User Is Away", _sent(msg))    # text fallback


# ── Auto-return (first message after AFK) ─────────────────────────
class TestReturn(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _cleanup()

    def tearDown(self):
        _cleanup()

    async def test_message_clears_and_welcomes(self):
        db.set_afk(USER_A, "Lewin", "lewin", "brb",
                   _iso(timedelta(hours=2, minutes=5)))
        msg = _msg("I am back now")
        await call(am.afk_return_handler, msg)
        self.assertIsNone(db.get_afk(USER_A))
        text = _sent(msg)
        self.assertIn("Welcome Back", text)
        self.assertIn(">Lewin<", text)
        self.assertRegex(text, r"2 hours, 5 minutes")

    async def test_not_afk_is_silent(self):
        msg = _msg("just chatting")
        await call(am.afk_return_handler, msg)
        self.assertIsNone(_sent(msg))

    async def test_bot_sender_ignored(self):
        db.set_afk(USER_B, "Bot", None, None, _iso(timedelta(minutes=1)))
        msg = _msg("beep", user_id=USER_B, first_name="Bot",
                   username=None, is_bot=True)
        await call(am.afk_return_handler, msg)
        self.assertIsNone(_sent(msg))
        self.assertIsNotNone(db.get_afk(USER_B))      # untouched


# ── Dispatch wiring: filters + group numbers ──────────────────────
class TestDispatchWiring(unittest.TestCase):
    """Replay the real loader registrations (production filter setup)."""

    @classmethod
    def setUpClass(cls):
        cls.loaded = load_modules()
        cls.entries = pipeline.snapshot()

    def _fired(self, text):
        msg = make_message(text, chat_id=CHAT_ID, chat_type="supergroup",
                           user_id=USER_A)
        bot = FakeBot()
        fired = []

        async def _run():
            for entry in self.entries:
                flts = [f for f in (entry.flt,) if f is not None]
                handler = HandlerObject(
                    callback=entry.fn,
                    filters=[FilterObject(f) for f in flts],
                )
                ok, _ = await handler.check(msg, bot=bot)
                if ok:
                    fired.append(entry)

        import asyncio
        asyncio.run(_run())
        return [e.key for e in fired]

    def test_module_loaded_with_new_groups(self):
        keys = [e.key for e in self.entries
                if e.key.startswith("bot.modules.afk.")]
        self.assertEqual(len(keys), 3)
        groups = {e.key: e.group for e in self.entries
                  if e.key.startswith("bot.modules.afk.")}
        self.assertEqual(groups["bot.modules.afk.afk_mention_handler"], 22)
        self.assertEqual(groups["bot.modules.afk.afk_return_handler"], 23)

    def test_brb_text_reaches_toggle_not_return(self):
        keys = self._fired("brb running errand")
        self.assertIn("bot.modules.afk.afk_command", keys)
        self.assertNotIn("bot.modules.afk.afk_return_handler", keys)

    def test_off_text_reaches_toggle_not_return(self):
        keys = self._fired("off gone")
        self.assertIn("bot.modules.afk.afk_command", keys)
        self.assertNotIn("bot.modules.afk.afk_return_handler", keys)

    def test_afk_command_not_returned(self):
        keys = self._fired("/afk out for lunch")
        self.assertIn("bot.modules.afk.afk_command", keys)
        self.assertNotIn("bot.modules.afk.afk_return_handler", keys)

    def test_plain_text_reaches_mention_and_return(self):
        keys = self._fired("hello everyone")
        self.assertNotIn("bot.modules.afk.afk_command", keys)
        self.assertIn("bot.modules.afk.afk_mention_handler", keys)
        self.assertIn("bot.modules.afk.afk_return_handler", keys)

    def test_help_documents_afk(self):
        from bot.constants import HELP_MENU
        users = next(m for m in HELP_MENU if m["key"] == "users")
        lines = [line for _, cmds in users["sections"] for line in cmds]
        self.assertTrue(any(l.startswith("/afk") for l in lines))


# ── DB helpers + duration ─────────────────────────────────────────
class TestDbAndHelpers(unittest.TestCase):
    def setUp(self):
        _cleanup()

    def tearDown(self):
        _cleanup()

    def test_username_case_roundtrip(self):
        db.set_afk(USER_B, "Sam", "MixedCase", None,
                   datetime.now().isoformat())
        self.assertIsNotNone(db.get_afk_by_username("mixedcase"))
        self.assertIsNotNone(db.get_afk_by_username("MIXEDCASE"))
        self.assertIsNotNone(db.get_afk_by_username("MixedCase"))
        self.assertIsNone(db.get_afk_by_username("nobody"))

    def test_clear_afk_is_idempotent(self):
        db.set_afk(USER_B, "Sam", "sam", None, datetime.now().isoformat())
        db.clear_afk(USER_B)
        db.clear_afk(USER_B)  # no error, still None
        self.assertIsNone(db.get_afk(USER_B))
        self.assertIsNone(db.get_afk(USER_A))  # others untouched

    def test_unique_user_row_upsert(self):
        now = datetime.now().isoformat()
        db.set_afk(USER_B, "Sam", "sam", "one", now)
        db.set_afk(USER_B, "Sam", "sam", "two", now)
        self.assertEqual(
            db.collection("afk").count_documents({"user_id": USER_B}), 1
        )
        self.assertEqual(db.get_afk(USER_B)["afk_reason"], "two")

    def test_duration_format(self):
        now = datetime.now()
        cases = {
            timedelta(seconds=0): "0 seconds",
            timedelta(seconds=45): "45 seconds",
            timedelta(minutes=3, seconds=10): "3 minutes, 10 seconds",
            timedelta(hours=1, minutes=5): "1 hour, 5 minutes",
            timedelta(hours=2, minutes=5, seconds=1): (
                "2 hours, 5 minutes, 1 second"
            ),
            timedelta(hours=1): "1 hour",
        }
        for delta, want in cases.items():
            got = am._duration_since((now - delta).isoformat())
            self.assertEqual(got, want, delta)

    def test_duration_bad_input(self):
        self.assertEqual(am._duration_since(None), "a while")
        self.assertEqual(am._duration_since("not-a-date"), "a while")


if __name__ == "__main__":
    unittest.main(verbosity=2)
