"""End-to-end dispatch: the force-join gate runs FIRST and ends the chain.

Run from the repo root:

    python -m pytest -q tests/test_force_join_dispatch.py
    python -m unittest discover -s tests

What this locks down
--------------------
1. **A gated message may not reach anything.**  ``gate_message_handler``
   used to sit in group 4 while every command handler sits in group 0, so
   a non-member's ``/help`` was answered (and ``/rankings`` rendered a
   leaderboard) *before* the gate deleted the message.  Counters and XP
   trackers also still saw it.  The gate now dispatches in group -1 and
   raises ``pipeline.StopChain`` on a block, so the update stops there:
   no reply, no count, no XP, message deleted, prompt sent.

2. **It still blocks every shape of message** - text, commands, stickers,
   GIFs, photos, documents, voice, dice - and lets members and admins
   straight through.

3. **Welcome/goodbye really fire on the real Dispatcher.**  A join
   service message reaches ``new_member_handler`` (group 10) after all
   the earlier groups have run, and a disabled welcome does not.

Environment isolation (BEFORE any bot import):
    * BOT_TOKEN is forced - importing `bot` pulls bot.config, which
      exits without one.
    * unittest is already imported -> bot.database always selects
      mongomock, so the suite never touches a real MongoDB server.
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

# ── Environment isolation ── must precede bot imports ────────────────
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["BOT_TOKEN"] = "1:TEST-TOKEN-FOR-UNIT-TESTS"
_TEST_DIR = tempfile.mkdtemp(prefix="pi_gate_dispatch_")
os.environ["LOCALAPPDATA"] = _TEST_DIR
atexit.register(shutil.rmtree, _TEST_DIR, ignore_errors=True)

# ── Imports (after env) ──────────────────────────────────────────────
from aiofakes import FakeBot  # noqa: E402
from aiogram import Dispatcher  # noqa: E402
from aiogram.types import Update  # noqa: E402

from bot import pipeline  # noqa: E402
from bot.loader import load_modules  # noqa: E402
from bot.modules.bind import checks as bind_checks  # noqa: E402
from bot.modules.bind import database as bdb  # noqa: E402
from bot.modules.bind.handlers import gate_message_handler  # noqa: E402

CHAT = -1007712001
CHANNEL = -1007712002
BOT_ID = 1
NOW = datetime.now(timezone.utc)

#: The stock force-join prompt (bind config.DEFAULT_CUSTOM_MESSAGE).
PROMPT_MARKER = "must join"


class _ProbeBot(FakeBot):
    """FakeBot plus aiogram's ``Bot.__call__``.

    Every aiogram send/delete shortcut builds a method object and awaits
    it, which ends in ``await bot(method)``.  Without ``__call__`` every
    reply and every ``message.delete()`` dies with "'FakeBot' object is
    not callable" - and the test would pass for the wrong reason.
    """

    async def __call__(self, method, **kwargs):
        cls = type(method).__name__
        data = method.model_dump(exclude_none=True)
        chat_id = data.pop("chat_id", None)
        if cls == "DeleteMessage":
            return await self.delete_message(chat_id, data["message_id"])
        if cls.startswith("Send"):
            payload = cls[4:]
            key = "text" if payload == "Message" else payload[0].lower() + payload[1:]
            value = data.pop(key, None)
            fn = getattr(self, "send_" + payload[0].lower() + payload[1:], None)
            if fn is None:
                raise NotImplementedError(cls)
            return await fn(chat_id, value, **data)
        raise NotImplementedError(cls)


class _spy:
    """Patch ``module.attr`` and record every positional argument.

    Handlers capture their imports at module scope, so the patch has to
    land on the *module* the handler reads from (``chatstats.reply_text``),
    not on the shared ``bot.reply`` - that one is already bound.
    """

    def __init__(self, module: str, attr: str) -> None:
        self._name, self._attr = module, attr
        self.calls: list = []

    def __enter__(self):
        import importlib

        mod = importlib.import_module(self._name)
        self._mod = mod
        self._orig = getattr(mod, self._attr)

        def wrapper(*args, **kwargs):
            self.calls.append((args, kwargs))
            return self._orig(*args, **kwargs)

        setattr(mod, self._attr, wrapper)
        return self

    def __exit__(self, *exc) -> bool:
        setattr(self._mod, self._attr, self._orig)
        return False


def _base(mid: int, uid: int, *, chat_id: int = CHAT) -> dict:
    return {
        "message_id": mid,
        "date": NOW.isoformat(),
        "chat": {"id": chat_id, "type": "supergroup", "title": "Gate Test"},
        "from_user": {"id": uid, "is_bot": False, "first_name": "Tester",
                      "username": "tester"},
    }


def _text(mid: int, uid: int, text: str, *, chat_id: int = CHAT) -> dict:
    d = _base(mid, uid, chat_id=chat_id)
    d["text"] = text
    return d


#: Every message shape the gate claims to cover (checks.message_gates).
MEDIA_CASES = {
    "sticker": lambda mid, uid: dict(
        **_base(mid, uid),
        sticker={"file_id": "S", "file_unique_id": "sU", "type": "regular",
                 "width": 512, "height": 512,
                 "is_animated": False, "is_video": False}),
    "gif": lambda mid, uid: dict(
        **_base(mid, uid),
        animation={"file_id": "A", "file_unique_id": "aU", "width": 320,
                   "height": 320, "duration": 1, "file_name": "x.gif"}),
    "photo": lambda mid, uid: dict(
        **_base(mid, uid),
        photo=[{"file_id": "P", "file_unique_id": "pU",
                "width": 1, "height": 1}],
        caption="look at this"),
    "document": lambda mid, uid: dict(
        **_base(mid, uid),
        document={"file_id": "D", "file_unique_id": "dU", "file_name": "a.txt",
                  "mime_type": "text/plain", "file_size": 5}),
    "voice": lambda mid, uid: dict(
        **_base(mid, uid),
        voice={"file_id": "V", "file_unique_id": "vU", "duration": 1}),
    "dice": lambda mid, uid: dict(
        **_base(mid, uid), dice={"emoji": "\U0001f3b2", "value": 3}),
}


class _Base(unittest.IsolatedAsyncioTestCase):
    """Real modules on a real Dispatcher, bound group, fresh cache."""

    @classmethod
    def setUpClass(cls) -> None:
        pipeline.clear()
        load_modules()
        cls.dp = Dispatcher()
        pipeline.install(cls.dp)

    def setUp(self) -> None:
        super().setUp()
        bind_checks.clear_cache()
        bdb.remove_binding(CHAT)
        bdb.upsert_binding(
            CHAT, CHANNEL,
            channel_title="Gate Channel",
            channel_username=None,
            channel_link=None,
            bound_by=1,
        )
        bdb.update_field(CHAT, "force_join", 1)
        self._uid = 900000

    def tearDown(self) -> None:
        bind_checks.clear_cache()
        bdb.remove_binding(CHAT)
        super().tearDown()

    # ── helpers ─────────────────────────────────────────────────
    def next_user(self) -> int:
        """A user id no earlier test has touched (no leftover warnings)."""
        self._uid += 1
        return self._uid

    def bot(self, *, member: bool = True, bot_can_delete: bool = True,
            group_admin: bool = False, uid: int | None = None) -> _ProbeBot:
        bot = _ProbeBot(user_id=BOT_ID)
        if not bot_can_delete:
            # Not a group admin at all -> is_bot_admin() is False.
            pass
        else:
            bot.chat_members[(CHAT, BOT_ID)] = SimpleNamespace(
                status="administrator",
                user=SimpleNamespace(id=BOT_ID),
                can_delete_messages=True,
            )
        bot.chat_members[(CHANNEL, BOT_ID)] = SimpleNamespace(
            status="administrator", user=SimpleNamespace(id=BOT_ID),
        )
        if uid is not None:
            bot.chat_members[(CHANNEL, uid)] = SimpleNamespace(
                status="member" if member else "left",
                user=SimpleNamespace(id=uid),
            )
            if group_admin:
                bot.chat_members[(CHAT, uid)] = SimpleNamespace(
                    status="administrator", user=SimpleNamespace(id=uid),
                )
        return bot

    async def feed(self, bot, payload: dict, update_id: int) -> None:
        await self.dp.feed_update(
            bot=bot,
            update=Update.model_validate(
                {"update_id": update_id, "message": payload}
            ),
        )

    @staticmethod
    def prompts(bot) -> list:
        return [s for s in bot.sent
                if PROMPT_MARKER in str(s.get("text", "")).lower()]

    @staticmethod
    def user_deletes(bot) -> list:
        """Deletes of real messages (not of a previous prompt)."""
        return [s for s in bot.sent if s.get("delete")]


# ════════════════════════════════════════════════════════════════════
# 1. The regression: nothing a non-member sends may reach another handler
# ════════════════════════════════════════════════════════════════════

class TestGateStopsTheChain(_Base):

    def test_gate_is_the_first_message_handler_in_dispatch_order(self):
        first = next(e for e in pipeline.snapshot() if e.event == "message")
        self.assertIs(
            first.fn, gate_message_handler,
            "the force-join gate must dispatch before group 0, otherwise a "
            f"non-member's command runs first (got {first.fn} in group "
            f"{first.group})",
        )
        self.assertLess(first.group, 0)

    async def test_non_member_text_is_deleted_and_prompted(self):
        uid = self.next_user()
        bot = self.bot(member=False, uid=uid)

        await self.feed(bot, _text(5001, uid, "hello everyone"), 6001)

        self.assertTrue(self.prompts(bot), "no force-join prompt was sent")
        self.assertTrue(
            self.user_deletes(bot), "the gated message was never deleted",
        )

    async def test_non_member_command_is_never_answered(self):
        """A non-member's /rankings must not reach the reply layer."""
        uid = self.next_user()
        bot = self.bot(member=False, uid=uid)
        with _spy("bot.modules.chatstats", "reply_text") as replied:
            await self.feed(bot, _text(5002, uid, "/rankings"), 6002)

        self.assertTrue(self.prompts(bot))
        self.assertEqual(
            replied.calls, [],
            "the command handler ran before the gate - a gated user got an "
            "answer",
        )

    async def test_member_command_still_works(self):
        """Positive control for the test above."""
        uid = self.next_user()
        bot = self.bot(member=True, uid=uid)
        with _spy("bot.modules.chatstats", "reply_text") as replied:
            await self.feed(bot, _text(5003, uid, "/rankings"), 6003)

        self.assertEqual(self.prompts(bot), [], "a member was gated")
        self.assertTrue(
            replied.calls, "the leaderboard reply never reached the chain",
        )

    async def test_blocked_message_is_not_counted(self):
        from bot import database as D

        calls: list = []
        original = D.db.count_message

        def spy(*a, **k):
            calls.append(a)
            return original(*a, **k)

        D.db.count_message = spy
        try:
            uid = self.next_user()
            bot = self.bot(member=False, uid=uid)
            await self.feed(bot, _text(5004, uid, "count me"), 6004)
        finally:
            D.db.count_message = original

        self.assertEqual(calls, [], "a gated message still fed the counters")

    async def test_member_message_is_counted(self):
        from bot import database as D

        calls: list = []
        original = D.db.count_message

        def spy(*a, **k):
            calls.append(a)
            return original(*a, **k)

        D.db.count_message = spy
        try:
            uid = self.next_user()
            bot = self.bot(member=True, uid=uid)
            await self.feed(bot, _text(5005, uid, "count me too"), 6005)
        finally:
            D.db.count_message = original

        self.assertGreaterEqual(
            len(calls), 1, "a member's message never reached the counter",
        )

    async def test_member_message_is_left_alone(self):
        uid = self.next_user()
        bot = self.bot(member=True, uid=uid)

        await self.feed(bot, _text(5006, uid, "I joined already"), 6006)

        self.assertEqual(self.prompts(bot), [])
        self.assertEqual(self.user_deletes(bot), [])

    async def test_group_admin_bypasses_the_gate(self):
        uid = self.next_user()
        bot = self.bot(member=False, uid=uid, group_admin=True)

        await self.feed(bot, _text(5007, uid, "admin talks"), 6007)

        self.assertEqual(self.prompts(bot), [])
        self.assertEqual(self.user_deletes(bot), [])


# ════════════════════════════════════════════════════════════════════
# 2. Every message shape is gated
# ════════════════════════════════════════════════════════════════════

class TestEveryMessageShapeIsGated(_Base):

    async def test_media_stickers_gifs_documents_voice_and_dice(self):
        for i, (label, build) in enumerate(MEDIA_CASES.items()):
            with self.subTest(kind=label):
                uid = self.next_user()
                bot = self.bot(member=False, uid=uid)
                await self.feed(bot, build(5100 + i, uid), 6100 + i)
                self.assertTrue(
                    self.prompts(bot), f"{label} was not gated at all",
                )
                self.assertTrue(
                    self.user_deletes(bot), f"{label} was not deleted",
                )


# ════════════════════════════════════════════════════════════════════
# 3. The bot cannot delete -> say so, and still stop the chain
# ════════════════════════════════════════════════════════════════════

class TestMissingDeleteRight(_Base):

    async def test_prompt_is_sent_but_the_command_is_still_refused(self):
        uid = self.next_user()
        bot = self.bot(member=False, bot_can_delete=False, uid=uid)
        with _spy("bot.modules.chatstats", "reply_text") as replied:
            await self.feed(bot, _text(5010, uid, "/rankings"), 6010)

        self.assertTrue(self.prompts(bot), "no prompt when deletion fails")
        self.assertEqual(
            replied.calls, [],
            "a gated user was answered even though the message stayed",
        )


# ════════════════════════════════════════════════════════════════════
# 4. Welcome / goodbye on the real dispatcher
# ════════════════════════════════════════════════════════════════════

class TestWelcomeThroughTheRealDispatcher(_Base):

    def setUp(self) -> None:
        super().setUp()
        bdb.remove_binding(CHAT)          # welcome has nothing to do with bind

    async def asyncSetUp(self) -> None:
        """Start every case from the shipped default: welcome ON."""
        from bot import database as D

        D.db.set_welcome_enabled(CHAT, True)

    async def asyncTearDown(self) -> None:
        from bot import database as D

        D.db.set_welcome_enabled(CHAT, True)

    @staticmethod
    def _join(mid: int, uid: int, *, chat_id: int = CHAT) -> dict:
        return {
            "message_id": mid,
            "date": NOW.isoformat(),
            "chat": {"id": chat_id, "type": "supergroup", "title": "Gate Test"},
            "from_user": {"id": uid, "is_bot": False, "first_name": "Newbie"},
            "new_chat_members": [
                {"id": uid, "is_bot": False, "first_name": "Newbie",
                 "username": "newbie"},
            ],
        }

    @staticmethod
    def _leave(mid: int, uid: int, *, chat_id: int = CHAT) -> dict:
        return {
            "message_id": mid,
            "date": NOW.isoformat(),
            "chat": {"id": chat_id, "type": "supergroup", "title": "Gate Test"},
            "from_user": {"id": uid, "is_bot": False, "first_name": "Leaver"},
            "left_chat_member": {"id": uid, "is_bot": False,
                                 "first_name": "Leaver"},
        }

    async def test_join_sends_the_welcome(self):
        from bot import database as D

        uid = self.next_user()
        bot = self.bot(member=True, uid=uid)
        await self.feed(bot, self._join(5020, uid), 6020)

        texts = [str(s.get("text", "")) for s in bot.sent]
        self.assertTrue(
            any("welcome" in t.lower() for t in texts),
            f"no welcome was sent; bot sent {texts!r}",
        )
        # The follow-up bookkeeping must have run too - this is what the
        # clean-welcome toggle reads on the next join.
        settings = D.db.get_welcome_settings(CHAT)
        self.assertIsNotNone(
            settings.get("last_welcome_msg_id"),
            "the welcome went out but was never recorded",
        )

    async def test_disabled_welcome_is_not_sent(self):
        from bot import database as D

        D.db.set_welcome_enabled(CHAT, False)

        uid = self.next_user()
        bot = self.bot(member=True, uid=uid)
        await self.feed(bot, self._join(5021, uid), 6021)

        texts = [str(s.get("text", "")) for s in bot.sent]
        self.assertFalse(
            any("welcome" in t.lower() for t in texts),
            f"a disabled welcome was still sent: {texts!r}",
        )

    async def test_goodbye_is_sent(self):
        uid = self.next_user()
        bot = self.bot(member=True, uid=uid)
        await self.feed(bot, self._leave(5022, uid), 6022)

        texts = [str(s.get("text", "")) for s in bot.sent]
        self.assertTrue(
            any("leaving" in t.lower() or "bye" in t.lower() for t in texts),
            f"no goodbye was sent; bot sent {texts!r}",
        )

    async def test_join_with_force_join_binding_still_welcomes(self):
        """The reported bug: join -> bind prompt, no welcome.

        force_join=1, channel bound, and the joiner has NOT joined the
        channel yet.  The join service message must still reach
        ``new_member_handler`` (group 10): it is a StatusUpdate, so the
        gate must neither fire nor StopChain on it, and nothing must be
        prompted until that user actually says something.
        """
        from bot import database as D

        bdb.upsert_binding(
            CHAT, CHANNEL,
            channel_title="Gate Channel",
            channel_username=None,
            channel_link=None,
            bound_by=1,
        )
        bdb.update_field(CHAT, "force_join", 1)
        self.addCleanup(bdb.remove_binding, CHAT)

        uid = self.next_user()
        bot = self.bot(member=False, uid=uid)   # never joined the channel
        await self.feed(bot, self._join(5023, uid), 6023)

        texts = [str(s.get("text", "")) for s in bot.sent]
        self.assertTrue(
            any("welcome" in t.lower() for t in texts),
            f"join produced no welcome while force_join was on: {texts!r}",
        )
        self.assertFalse(
            any(PROMPT_MARKER in t.lower() for t in texts),
            "the bind prompt fired on JOIN - it must wait for real "
            f"activity in the group: {texts!r}",
        )
        D.db.get_welcome_settings(CHAT)  # touch: keep the DB honest

    async def test_join_message_itself_is_not_gated(self):
        """The join service message must survive the gate untouched."""
        bdb.upsert_binding(
            CHAT, CHANNEL,
            channel_title="Gate Channel",
            channel_username=None,
            channel_link=None,
            bound_by=1,
        )
        bdb.update_field(CHAT, "force_join", 1)
        self.addCleanup(bdb.remove_binding, CHAT)

        uid = self.next_user()
        bot = self.bot(member=False, uid=uid)
        await self.feed(bot, self._join(5024, uid), 6024)

        self.assertEqual(
            self.user_deletes(bot), [],
            "the gate deleted the join service message",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
