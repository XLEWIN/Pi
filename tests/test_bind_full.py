"""Full internal test-suite for the bind module (force-join + message gates).

Run from the repo root:

    python -m pytest -q tests/test_bind_full.py
    python -m unittest discover -s tests

What this locks down
--------------------
1. **"I've Joined" is not an admin action.**  ``bind_callback`` used to
   run the group-admin gate before dispatching *any* action, so the exact
   person force-join exists for — an ordinary member — got a silent
   ``query.answer("")`` and a ``return``.  The button looked dead, ``record_join``
   never ran, and ``gate_message_handler`` kept deleting their messages.
   The action now runs *before* the gate, and no callback path may answer
   with an empty toast (an unanswered callback is indistinguishable from
   a broken one).

2. **The gate must not punish members for a rights problem.**  When the
   bot cannot see the bound chat, membership is ``unknown`` — never
   ``not_member`` — and the message is allowed through.

Environment isolation (BEFORE any bot import):
    * BOT_TOKEN is forced — importing `bot` pulls bot.config, which
      exits without one.
    * unittest is already imported -> bot.database always selects
      mongomock, so the suite never touches a real MongoDB server.
"""

from __future__ import annotations

import asyncio
import atexit
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

# ── Environment isolation ── must precede bot imports ────────────────
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["BOT_TOKEN"] = "1:TEST-TOKEN-FOR-UNIT-TESTS"
_TEST_DIR = tempfile.mkdtemp(prefix="pi_bind_full_test_")
os.environ["LOCALAPPDATA"] = _TEST_DIR
atexit.register(shutil.rmtree, _TEST_DIR, ignore_errors=True)

# ── Imports (after env) ──────────────────────────────────────────────
from aiofakes import FakeBot, FakeMessage, call, make_callback  # noqa: E402
from bot.modules.bind import checks  # noqa: E402
from bot.modules.bind import database as bdb  # noqa: E402
from bot.modules.bind.callbacks import bind_callback  # noqa: E402
from bot.modules.bind.checks import (  # noqa: E402
    MEMBERSHIP_MEMBER,
    MEMBERSHIP_NOT_MEMBER,
    MEMBERSHIP_UNKNOWN,
    clear_cache,
    in_grace,
    membership_state,
    message_gates,
    should_enforce,
)
from bot.modules.bind.config import AUTO_DELETE_OPTIONS, GRACE_OPTIONS  # noqa: E402
from bot.modules.bind.handlers import bind_command, gate_message_handler  # noqa: E402

# Unique ids so parallel bind tests never collide on CHANNEL_TAKEN.
CHAT = -1007711011
CHANNEL = -1007711012
USER = 4242420
BOT_ID = 1


class _ChannelBot(FakeBot):
    """Resolves ``@pi_bind_full``; ``blind=True`` → no rights in the channel."""

    def __init__(self, blind: bool = False) -> None:
        super().__init__()
        self.blind = blind
        channel = SimpleNamespace(
            id=CHANNEL, type="channel",
            title="Bind Full Test Channel", username="pi_bind_full",
        )
        self.chats["pi_bind_full"] = channel
        self.chats[CHANNEL] = channel

    async def get_chat_member(self, chat_id, user_id):
        if self.blind and chat_id == CHANNEL:
            raise RuntimeError("Bad Request: chat not found")
        return await super().get_chat_member(chat_id, user_id)


class _BlindBot(_ChannelBot):
    """A bot with no rights in the bound channel — getChatMember is refused."""

    def __init__(self) -> None:
        super().__init__(blind=True)


def _admin(bot: FakeBot) -> FakeBot:
    """Make the default sender a group admin AND the bot able to delete."""
    return _group_admin(_bot_can_delete(bot))


def _group_admin(bot: FakeBot, user_id: int = USER) -> FakeBot:
    bot.chat_members[(CHAT, user_id)] = SimpleNamespace(
        status="administrator", user=SimpleNamespace(id=user_id)
    )
    return bot


def _bot_can_delete(bot: FakeBot) -> FakeBot:
    """Bot is a group admin with the delete-messages right."""
    bot.chat_members[(CHAT, bot.id)] = SimpleNamespace(
        status="administrator", user=SimpleNamespace(id=bot.id),
        can_delete_messages=True,
    )
    return bot


def _cb(action: str, *, user_id: int = USER, chat_type: str = "supergroup", **kw):
    return make_callback(
        f"bind:{action}", user_id=user_id, chat_id=CHAT, chat_type=chat_type, **kw
    )


def _gate_msg(**kw) -> FakeMessage:
    """aiogram Message stand-in carrying every field message_gates reads."""
    kw.setdefault("text", "hello there")
    kw.setdefault("message_id", 5)
    kw.setdefault("user_id", USER)
    for field in ("photo", "video", "video_note", "document", "animation",
                  "audio", "voice", "sticker", "entities", "caption_entities"):
        kw.setdefault(field, None)
    msg = FakeMessage(chat_id=CHAT, chat_type="supergroup", **kw)
    # aiogram's User exposes full_name; aiofakes' stand-in does not.
    msg.from_user.full_name = getattr(msg.from_user, "first_name", None) or "User"
    return msg


def _texts(cb) -> list:
    return [a.get("text") or "" for a in cb.answers]


def _msg_deleted(msg) -> list:
    """Deletes the handler issued through the Message (not the Bot)."""
    return [c for c in msg.calls if c[0] == "delete"]


def _warnings(bot) -> list:
    return [e for e in bot.sent if "text" in e]


class _BoundBase(unittest.IsolatedAsyncioTestCase):
    """Fresh binding + a clean membership cache for every test."""

    def setUp(self):
        super().setUp()
        clear_cache()
        bdb.remove_binding(CHAT)
        bdb.upsert_binding(
            CHAT, CHANNEL,
            channel_username="pi_bind_full",
            channel_title="Bind Full Test Channel",
            channel_link="https://t.me/pi_bind_full",
        )

    def tearDown(self):
        clear_cache()
        bdb.remove_binding(CHAT)
        super().tearDown()


# ════════════════════════════════════════════════════════════════════
# 1. The regression: "I've Joined" for a NON-admin
# ════════════════════════════════════════════════════════════════════

class TestJoinButtonWorksForEveryone(_BoundBase):
    """force-join's target user is an ordinary member — never gate them out."""

    async def test_non_admin_pressing_join_gets_a_real_answer(self):
        bot = FakeBot()                       # sender stays a plain "member"
        cb = _cb("join", user_id=USER)

        await call(bind_callback, cb, bot=bot, chat_data={})

        self.assertTrue(cb.answers, "join was swallowed before any answer")
        self.assertTrue(_texts(cb)[0], "an empty toast looks exactly like a dead button")
        self.assertEqual(_texts(cb)[0], "Membership verified")

    async def test_non_admin_join_edits_the_message_and_records_the_join(self):
        bot = FakeBot()
        cb = _cb("join", user_id=USER)

        await call(bind_callback, cb, bot=bot, chat_data={})

        edits = [c for c in cb.message.calls if c[0] == "edit_text"]
        self.assertTrue(edits, "the warning message was never edited away")
        self.assertIn("you can chat now", edits[-1][1])
        self.assertIsNotNone(
            bdb.get_join_time(CHAT, USER),
            "record_join never ran, so the grace bookkeeping stays empty",
        )

    async def test_non_member_pressing_join_is_told_so(self):
        bot = FakeBot()
        bot.chat_members[(CHANNEL, USER)] = SimpleNamespace(
            status="left", user=SimpleNamespace(id=USER)
        )
        cb = _cb("join", user_id=USER)

        await call(bind_callback, cb, bot=bot, chat_data={})

        self.assertTrue(_texts(cb)[0])
        self.assertIn("Still not a member", _texts(cb)[0])
        self.assertTrue(cb.answers[0].get("show_alert"))
        # Nothing was recorded and the message was not edited.
        self.assertIsNone(bdb.get_join_time(CHAT, USER))
        self.assertFalse([c for c in cb.message.calls if c[0] == "edit_text"])

    async def test_join_when_the_bot_cannot_see_the_channel_says_why(self):
        bot = _BlindBot()
        cb = _cb("join", user_id=USER)

        await call(bind_callback, cb, bot=bot, chat_data={})

        self.assertTrue(_texts(cb)[0])
        self.assertIn("can't check", _texts(cb)[0])
        self.assertTrue(cb.answers[0].get("show_alert"))

    async def test_join_on_an_unbound_group_says_so(self):
        bdb.remove_binding(CHAT)
        bot = FakeBot()
        cb = _cb("join", user_id=USER)

        await call(bind_callback, cb, bot=bot, chat_data={})

        self.assertTrue(_texts(cb)[0])
        self.assertIn("isn't bound", _texts(cb)[0])

    async def test_admin_pressing_join_is_treated_the_same(self):
        bot = _admin(FakeBot())
        cb = _cb("join", user_id=USER)

        await call(bind_callback, cb, bot=bot, chat_data={})

        self.assertEqual(_texts(cb)[0], "Membership verified")


# ════════════════════════════════════════════════════════════════════
# 2. No callback path may answer with an empty toast
# ════════════════════════════════════════════════════════════════════

class TestCallbacksAlwaysAnswer(_BoundBase):

    async def test_non_admin_config_action_is_visibly_denied(self):
        bot = FakeBot()                       # plain member
        cb = _cb("menu", user_id=USER)

        await call(bind_callback, cb, bot=bot, chat_data={})

        self.assertTrue(cb.answers, "silent denial is indistinguishable from a bug")
        self.assertEqual(_texts(cb)[0], "Admins only.")

    async def test_every_config_action_answers_an_admin(self):
        actions = [
            "menu", "refresh", "back", "status", "gates", "gate:text",
            "grace", "grace_set:5", "grace_set:0",
            "autodel", "autodel_set:10", "autodel_set:0",
            "custom", "custom_set", "custom_reset", "custom_preview",
            "change", "replace_yes", "unbind", "close", "help_bind",
            "toggle:force_join", "toggle:admin_bypass", "not_a_real_action",
        ]
        for action in actions:
            with self.subTest(action=action):
                bot = _admin(FakeBot())
                cb = _cb(action, user_id=USER)
                await call(bind_callback, cb, bot=bot, chat_data={})
                self.assertTrue(cb.answers, f"{action} answered nothing")
                self.assertTrue(
                    _texts(cb)[0],
                    f"{action} answered with an empty toast",
                )

    async def test_every_grace_and_autodel_option_is_accepted(self):
        for minutes in GRACE_OPTIONS:
            with self.subTest(grace=minutes):
                bot = _admin(FakeBot())
                cb = _cb(f"grace_set:{minutes}", user_id=USER)
                await call(bind_callback, cb, bot=bot, chat_data={})
                self.assertTrue(_texts(cb)[0])
        for secs in AUTO_DELETE_OPTIONS:
            with self.subTest(autodel=secs):
                bot = _admin(FakeBot())
                cb = _cb(f"autodel_set:{secs}", user_id=USER)
                await call(bind_callback, cb, bot=bot, chat_data={})
                self.assertTrue(_texts(cb)[0])

    async def test_callback_from_a_private_chat_is_refused_visibly(self):
        cb = _cb("join", user_id=USER, chat_type="private")
        await call(bind_callback, cb, bot=FakeBot(), chat_data={})
        self.assertTrue(_texts(cb)[0])


# ════════════════════════════════════════════════════════════════════
# 3. Membership probe / state machine
# ════════════════════════════════════════════════════════════════════

class TestMembershipState(_BoundBase):

    async def test_member_not_member_unknown(self):
        bot = FakeBot()
        bot.chat_members[(CHANNEL, USER)] = SimpleNamespace(
            status="member", user=SimpleNamespace(id=USER)
        )
        self.assertEqual(await membership_state(bot, CHANNEL, USER), MEMBERSHIP_MEMBER)

        clear_cache()
        bot.chat_members[(CHANNEL, USER)] = SimpleNamespace(
            status="kicked", user=SimpleNamespace(id=USER)
        )
        self.assertEqual(
            await membership_state(bot, CHANNEL, USER), MEMBERSHIP_NOT_MEMBER
        )

        clear_cache()
        self.assertEqual(
            await membership_state(_BlindBot(), CHANNEL, USER), MEMBERSHIP_UNKNOWN
        )

    async def test_unknown_short_circuits_before_touching_the_user_cache(self):
        """A failed probe must never be remembered as 'not a member'."""
        bot = _BlindBot()
        self.assertEqual(await membership_state(bot, CHANNEL, USER), MEMBERSHIP_UNKNOWN)
        # Same key, healthy bot: the probe result must not have been cached
        # against the user.
        clear_cache()
        healthy = FakeBot()
        self.assertEqual(
            await membership_state(healthy, CHANNEL, USER), MEMBERSHIP_MEMBER
        )

    async def test_fresh_bypasses_the_user_cache(self):
        bot = FakeBot()
        bot.chat_members[(CHANNEL, USER)] = SimpleNamespace(
            status="left", user=SimpleNamespace(id=USER)
        )
        self.assertEqual(
            await membership_state(bot, CHANNEL, USER), MEMBERSHIP_NOT_MEMBER
        )
        # User joins the channel; a fresh check must see it at once.
        bot.chat_members[(CHANNEL, USER)] = SimpleNamespace(
            status="member", user=SimpleNamespace(id=USER)
        )
        self.assertEqual(
            await membership_state(bot, CHANNEL, USER, fresh=True), MEMBERSHIP_MEMBER
        )

    async def test_probe_is_cached_per_channel(self):
        calls = {"n": 0}

        class _Counting(_BlindBot):
            async def get_chat_member(self, chat_id, user_id):
                if chat_id == CHANNEL:
                    calls["n"] += 1
                    raise RuntimeError("Bad Request: chat not found")
                return await super().get_chat_member(chat_id, user_id)

        bot = _Counting()
        for _ in range(5):
            await membership_state(bot, CHANNEL, USER)
        self.assertEqual(calls["n"], 1, "the rights probe must be cached")

    async def test_is_channel_member_is_the_bool_view(self):
        bot = FakeBot()
        self.assertTrue(await checks.is_channel_member(bot, CHANNEL, USER))
        clear_cache()
        self.assertFalse(await checks.is_channel_member(_BlindBot(), CHANNEL, USER))


# ════════════════════════════════════════════════════════════════════
# 4. The gate itself
# ════════════════════════════════════════════════════════════════════

class TestGateHandler(_BoundBase):

    async def test_channel_member_is_let_through(self):
        """Plain group member who IS in the channel: no delete, no warning."""
        bot = _bot_can_delete(FakeBot())     # sender stays a plain member
        bot.chat_members[(CHANNEL, USER)] = SimpleNamespace(
            status="member", user=SimpleNamespace(id=USER)
        )
        msg = _gate_msg()
        await call(gate_message_handler, msg, bot=bot)

        self.assertEqual(_msg_deleted(msg), [])
        self.assertEqual(bot.sent, [])
        self.assertIsNotNone(bdb.get_join_time(CHAT, USER))

    async def test_unknown_membership_fails_open(self):
        """No rights to check → no deletions. The original complaint."""
        bot = _bot_can_delete(_BlindBot())
        msg = _gate_msg()
        await call(gate_message_handler, msg, bot=bot)

        self.assertEqual(_msg_deleted(msg), [], "a blind bot must not delete messages")
        self.assertEqual(bot.sent, [], "no warning either — nothing is provable")

    async def test_non_member_is_gated(self):
        bot = _bot_can_delete(FakeBot())
        bot.chat_members[(CHANNEL, USER)] = SimpleNamespace(
            status="left", user=SimpleNamespace(id=USER)
        )
        msg = _gate_msg()
        await call(gate_message_handler, msg, bot=bot)

        self.assertTrue(_msg_deleted(msg), "the non-member message survived")
        warns = _warnings(bot)
        self.assertEqual(len(warns), 1, "exactly one force-join warning")
        self.assertIn("bind:join", str(warns[0].get("reply_markup")))

    async def test_bot_without_delete_rights_only_warns(self):
        bot = FakeBot()                       # bot is a plain member
        bot.chat_members[(CHANNEL, USER)] = SimpleNamespace(
            status="left", user=SimpleNamespace(id=USER)
        )
        msg = _gate_msg()
        await call(gate_message_handler, msg, bot=bot)

        self.assertEqual(_msg_deleted(msg), [], "no delete rights → do not try")
        self.assertEqual(len(_warnings(bot)), 1)

    async def test_unbound_chat_is_left_alone(self):
        bdb.remove_binding(CHAT)
        bot = _bot_can_delete(FakeBot())
        msg = _gate_msg()
        await call(gate_message_handler, msg, bot=bot)
        self.assertEqual(_msg_deleted(msg), [])
        self.assertEqual(bot.sent, [])

    async def test_admin_bypass_stops_the_delete(self):
        """Group admin who never joined the channel (admin_bypass = ON)."""
        bot = _admin(FakeBot())
        bot.chat_members[(CHANNEL, USER)] = SimpleNamespace(
            status="left", user=SimpleNamespace(id=USER)
        )
        msg = _gate_msg()
        await call(gate_message_handler, msg, bot=bot)

        self.assertEqual(_msg_deleted(msg), [])
        self.assertEqual(bot.sent, [])

    async def test_bots_are_never_gated(self):
        bot = _bot_can_delete(FakeBot())
        bot.chat_members[(CHANNEL, 777)] = SimpleNamespace(
            status="left", user=SimpleNamespace(id=777)
        )
        msg = _gate_msg(user_id=777, is_bot=True)
        await call(gate_message_handler, msg, bot=bot)
        self.assertEqual(_msg_deleted(msg), [])
        self.assertEqual(bot.sent, [])


class TestBindTimeVerification(_BoundBase):
    """/bind must refuse a chat the bot cannot check.

    The other half of the same bug: binding a channel the bot has no
    rights in arms a gate whose only possible answer is "not a member",
    and that guess is what deletes the group's messages.
    """

    def _cmd(self) -> FakeMessage:
        return FakeMessage(text="/bind @pi_bind_full", chat_id=CHAT,
                           chat_type="supergroup", user_id=USER, message_id=1)

    async def test_refused_when_the_bot_cannot_check_the_channel(self):
        bdb.remove_binding(CHAT)
        bot = _admin(_ChannelBot(blind=True))
        msg = self._cmd()

        await call(bind_command, msg, bot=bot, args=["@pi_bind_full"], chat_data={})

        self.assertTrue(msg.sent_texts)
        self.assertIn("can't read membership", msg.sent_texts[-1])
        self.assertIn("admin", msg.sent_texts[-1], "the error must say how to fix it")
        self.assertIsNone(
            bdb.get_settings(CHAT), "an unverifiable channel must not be bound"
        )

    async def test_binds_when_the_bot_can_check_the_channel(self):
        bdb.remove_binding(CHAT)
        bot = _admin(_ChannelBot())
        msg = self._cmd()

        await call(bind_command, msg, bot=bot, args=["@pi_bind_full"], chat_data={})
        await asyncio.sleep(0.01)          # let the spawned reply run

        settings = bdb.get_settings(CHAT)
        self.assertIsNotNone(settings, "a checkable channel must bind")
        self.assertEqual(settings["channel_id"], CHANNEL)
        self.assertTrue(
            any("bound successfully" in t for t in msg.sent_texts), msg.sent_texts
        )


class TestShouldEnforce(unittest.TestCase):
    """Pure decision table — no I/O, no DB."""

    def _settings(self, **kw):
        base = {
            "channel_id": CHANNEL, "force_join": 1, "admin_bypass": 1,
            "gate_text": 0, "gate_media": 0, "gate_link": 0,
            "gate_document": 0, "gate_gif": 0, "gate_audio": 0, "gate_sticker": 0,
        }
        base.update(kw)
        return base

    def _msg(self, **kw):
        kw.setdefault("text", "hi")
        for f in ("photo", "video", "video_note", "document", "animation",
                  "audio", "voice", "sticker", "entities", "caption_entities"):
            kw.setdefault(f, None)
        return SimpleNamespace(**kw)

    def _user(self, is_bot=False):
        return SimpleNamespace(id=USER, is_bot=is_bot)

    def test_force_join_blocks_every_message(self):
        self.assertTrue(should_enforce(
            self._settings(), self._msg(), self._user(),
            is_admin=False, in_grace_window=False,
        ))

    def test_admin_bypass_allows_admins(self):
        self.assertFalse(should_enforce(
            self._settings(), self._msg(), self._user(),
            is_admin=True, in_grace_window=False,
        ))

    def test_grace_window_allows(self):
        self.assertFalse(should_enforce(
            self._settings(), self._msg(), self._user(),
            is_admin=False, in_grace_window=True,
        ))

    def test_unbound_never_enforces(self):
        self.assertFalse(should_enforce(
            self._settings(channel_id=None), self._msg(), self._user(),
            is_admin=False, in_grace_window=False,
        ))

    def test_bots_never_enforced(self):
        self.assertFalse(should_enforce(
            self._settings(), self._msg(), self._user(is_bot=True),
            is_admin=False, in_grace_window=False,
        ))

    def test_no_force_and_no_matching_gate_lets_through(self):
        self.assertFalse(should_enforce(
            self._settings(force_join=0), self._msg(), self._user(),
            is_admin=False, in_grace_window=False,
        ))

    def test_type_gate_matches_with_force_off(self):
        self.assertTrue(should_enforce(
            self._settings(force_join=0, gate_text=1), self._msg(), self._user(),
            is_admin=False, in_grace_window=False,
        ))


class TestGateClassification(unittest.TestCase):

    def test_text_only_message_matches_the_text_gate(self):
        msg = SimpleNamespace(
            text="hi", caption=None, photo=None, video=None, video_note=None,
            document=None, animation=None, audio=None, voice=None, sticker=None,
            entities=None, caption_entities=None,
        )
        self.assertEqual(message_gates(msg), {"text"})

    def test_photo_matches_media_only(self):
        msg = SimpleNamespace(
            text=None, caption=None, photo=object(), video=None, video_note=None,
            document=None, animation=None, audio=None, voice=None, sticker=None,
            entities=None, caption_entities=None,
        )
        self.assertEqual(message_gates(msg), {"media"})

    def test_link_in_text_adds_the_link_gate(self):
        msg = SimpleNamespace(
            text="see https://example.com", caption=None, photo=None, video=None,
            video_note=None, document=None, animation=None, audio=None,
            voice=None, sticker=None, entities=None, caption_entities=None,
        )
        self.assertEqual(message_gates(msg), {"text", "link"})

    def test_grace_only_applies_inside_the_window(self):
        import time
        now = time.time()
        self.assertTrue(in_grace(now - 30, 5))
        self.assertFalse(in_grace(now - 600, 5))
        self.assertFalse(in_grace(None, 5))
        self.assertFalse(in_grace(now - 30, 0))


if __name__ == "__main__":
    unittest.main()
