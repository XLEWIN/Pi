"""Tests for the admin panel module (bot/modules/adminbox.py).

Run from the Pi/Pi root:

    python tests/test_adminbox.py
    python -m unittest discover -s tests

Covers:
    * setup wiring (command + abox:* callbacks + unique group 21 hook)
    * admin gating on the command AND on every callback
    * the remove-admin flow's protections (owner / bot / self / stale
      list / missing bot promote-rights) plus the happy path
    * waiting-input flows (name, bio, broadcast): prompt, success,
      validation keeping state, expiry, command-cancels-waiting
    * panel views (main, admins, invite, info, perms, bot, tools,
      settings, dperms) and actions (pin/unpin/delete, purge, close,
      join-request link)

Environment isolation (BEFORE any bot import):
    * BOT_TOKEN is forced — importing `bot` pulls bot.config, which
      exits without one.
    * LOCALAPPDATA points at a temp dir so any DB touch stays isolated.
No network: handlers run against fakes that record every API call.
"""

from __future__ import annotations

import asyncio
import atexit
import os
import shutil
import sys
import tempfile
import time
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace

# ── Environment isolation — must precede bot imports ──────────────
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["BOT_TOKEN"] = "1:TEST-TOKEN-FOR-UNIT-TESTS"
_TEST_DIR = tempfile.mkdtemp(prefix="pi_adminbox_test_")
os.environ["LOCALAPPDATA"] = _TEST_DIR
atexit.register(shutil.rmtree, _TEST_DIR, ignore_errors=True)

# ── Imports (after env) ───────────────────────────────────────────
from aiogram.dispatcher.event.handler import FilterObject, HandlerObject  # noqa: E402
from aiogram.exceptions import TelegramBadRequest  # noqa: E402

from aiofakes import call, command_filters, make_callback, make_message  # noqa: E402
from bot import pipeline  # noqa: E402
from bot.modules import adminbox as ab  # noqa: E402

CHAT_ID = -100555001
BOT_ID = 999888777
OWNER_ID = 111
PRESSER_ID = 222       # the admin pressing buttons
OTHER_ID = 333         # demotable admin
MEMBER_ID = 444


# ── Fakes ─────────────────────────────────────────────────────────

def _user(uid: int, name: str = "Some User", is_bot: bool = False):
    parts = name.split()
    return SimpleNamespace(
        id=uid,
        first_name=parts[0],
        last_name=" ".join(parts[1:]),
        full_name=name,
        username=None,
        is_bot=is_bot,
    )


def _cm(status: str, uid: int, name: str | None = None, is_bot: bool = False,
        **rights):
    """ChatMember-ish: status + user + every can_* flag (default True)."""
    base = dict(
        can_manage_chat=True,
        can_change_info=True,
        can_invite_users=True,
        can_pin_messages=True,
        can_delete_messages=True,
        can_restrict_members=True,
        can_promote_members=True,
    )
    base.update(rights)
    return SimpleNamespace(
        status=status,
        user=_user(uid, name or f"User {uid}", is_bot=is_bot),
        **base,
    )


class _FakeBot:
    def __init__(self, presser_status: str = "administrator",
                 bot_status: str = "administrator",
                 bot_can_promote: bool = True):
        self.id = BOT_ID
        self.username = "PiModulerBot"
        self.presser_status = presser_status
        self.bot_status = bot_status
        self.bot_can_promote = bot_can_promote
        self.calls: list[tuple] = []       # (method, ...) for mutations
        self.edits: list[dict] = []        # revert-panel edits
        self.sent: list[tuple] = []        # chat_id, text
        # uid → ChatMember | None (None raises, simulating missing user)
        self.members: dict[int, object] = {}

    # ── reads ─────────────────────────────────────────────────────
    async def get_chat_member(self, chat_id, user_id):
        if user_id in self.members:
            member = self.members[user_id]
            if member is None:
                raise TelegramBadRequest(None, "user not found")
            return member
        if user_id == BOT_ID:
            return _cm(
                self.bot_status, BOT_ID, "Phi Bot", is_bot=True,
                can_promote_members=self.bot_can_promote,
            )
        if user_id == OWNER_ID:
            return _cm("creator", OWNER_ID, "Owner Name")
        if user_id == PRESSER_ID:
            return _cm(self.presser_status, PRESSER_ID, "Presser User")
        if user_id == OTHER_ID:
            return _cm("administrator", OTHER_ID, "Other Admin")
        return _cm("member", MEMBER_ID, "Plain Member")

    async def get_chat_administrators(self, chat_id):
        return [
            _cm("creator", OWNER_ID, "Owner Name"),
            _cm("administrator", PRESSER_ID, "Presser User"),
            _cm("administrator", OTHER_ID, "Other Admin"),
            _cm("administrator", BOT_ID, "Phi Bot", is_bot=True),
        ]

    async def get_chat_member_count(self, chat_id):
        return 1284

    async def get_chat(self, chat_id):
        return SimpleNamespace(
            title="Shadow Community",
            invite_link="https://t.me/+PRIMARY",
            slow_mode_delay=0,
            permissions=SimpleNamespace(
                can_send_messages=True,
                can_send_polls=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
                can_change_info=False,
                can_invite_users=True,
                can_pin_messages=True,
                can_manage_topics=True,
            ),
        )

    # ── mutations (recorded) ──────────────────────────────────────
    async def set_chat_title(self, chat_id, title):
        self.calls.append(("set_chat_title", chat_id, title))

    async def set_chat_description(self, chat_id, description):
        self.calls.append(("set_chat_description", chat_id, description))

    async def send_message(self, chat_id, text, **kw):
        self.calls.append(("send_message", chat_id, text))
        self.sent.append((chat_id, text))

    async def promote_chat_member(self, chat_id, user_id, **kw):
        self.calls.append(("promote_chat_member", chat_id, user_id, kw))

    async def create_chat_invite_link(self, chat_id, **kw):
        self.calls.append(("create_chat_invite_link", chat_id, kw))
        if kw.get("creates_join_request"):
            return SimpleNamespace(invite_link="https://t.me/+JOINREQ")
        return SimpleNamespace(invite_link="https://t.me/+NEWLINK")

    async def pin_chat_message(self, chat_id, message_id, **kw):
        self.calls.append(("pin_chat_message", chat_id, message_id))

    async def unpin_chat_message(self, chat_id, message_id=None):
        self.calls.append(("unpin_chat_message", chat_id, message_id))

    async def delete_message(self, chat_id, message_id):
        self.calls.append(("delete_message", chat_id, message_id))

    async def delete_messages(self, chat_id, message_ids):
        self.calls.append(("delete_messages", chat_id, list(message_ids)))

    async def edit_message_text(self, chat_id=None, message_id=None, text=None,
                                parse_mode=None, reply_markup=None, **kw):
        self.edits.append(
            {"chat_id": chat_id, "message_id": message_id,
             "text": text, "markup": reply_markup}
        )


def _calls(bot, name):
    return [c for c in bot.calls if c[0] == name]


def _panel_msg(message_id: int = 50):
    """The panel message a callback acts on."""
    return make_message(chat_id=CHAT_ID, chat_type="supergroup",
                        title="Shadow Community", message_id=message_id)


def _cmd_msg(chat_type: str = "supergroup", reply_mid: int | None = None,
             user=None):
    """The /adminbox command message (group unless told otherwise)."""
    msg = make_message("/adminbox", chat_id=CHAT_ID, chat_type=chat_type,
                       title="Shadow Community")
    msg.from_user = user if user is not None else _user(PRESSER_ID, "Presser User")
    if reply_mid is not None:
        msg.reply_to_message = SimpleNamespace(message_id=reply_mid)
    return msg


def _hook_msg(text: str | None, message_id: int, sender):
    """A group message arriving at the message hook."""
    msg = make_message(text, chat_id=CHAT_ID, chat_type="supergroup",
                       title="Shadow Community", message_id=message_id)
    msg.from_user = sender
    return msg


# ── Recording views over FakeMessage.calls / CallbackQuery.answers ──

def _sent(msg) -> list[dict]:
    """reply/answer payloads as {"text", "markup"} dicts."""
    return [{"text": t, "markup": kw.get("reply_markup")}
            for (k, t, kw) in msg.calls if k in ("answer", "reply")]


def _last(msg) -> dict | None:
    sent = _sent(msg)
    return sent[-1] if sent else None


def _edits(query) -> list[dict]:
    """edit_text payloads on the callback's message."""
    return [{"text": t, "markup": kw.get("reply_markup")}
            for (k, t, kw) in query.message.calls if k == "edit_text"]


def _last_edit(query) -> dict | None:
    edits = _edits(query)
    return edits[-1] if edits else None


def _deleted(query) -> bool:
    return any(k == "delete" for (k, _, _) in query.message.calls)


def _cb(data: str, bot: _FakeBot | None = None, user=None,
        chat_data: dict | None = None, message=None):
    bot = bot if bot is not None else _FakeBot()
    panel = message if message is not None else _panel_msg()
    query = make_callback(data, message=panel, user_id=PRESSER_ID)
    query.from_user = user if user is not None else _user(PRESSER_ID, "Presser User")
    return query, bot, (chat_data if chat_data is not None else {})


def _hook(text: str | None, chat_data: dict | None = None,
          message_id: int = 1, uid: int = PRESSER_ID,
          bot: _FakeBot | None = None):
    sender = _user(uid, "Presser User" if uid == PRESSER_ID else "Other User")
    msg = _hook_msg(text, message_id, sender)
    return msg, (bot if bot is not None else _FakeBot()), \
        (chat_data if chat_data is not None else {})


def _flat(markup):
    return [b for row in markup.inline_keyboard for b in row]


def _datas(markup):
    return [getattr(b, "callback_data", None) or "" for b in _flat(markup)]


async def _matches(entry, event) -> bool:
    """Run the registered filter against an event (production path)."""
    handler = HandlerObject(callback=entry.fn,
                            filters=[FilterObject(entry.flt)])
    ok, _ = await handler.check(event)
    return ok


# ═════════════════════════════════════════════════════════════════
# Wiring
# ═════════════════════════════════════════════════════════════════

class TestWiring(unittest.TestCase):
    def test_setup_registers_all_three(self):
        pipeline.clear()
        routes = ab.setup()
        entries = pipeline.snapshot()
        self.assertEqual(len(entries), 3)
        self.assertEqual([e.group for e in entries], [0, 0, ab.MESSAGE_GROUP])
        self.assertEqual([e.event for e in entries],
                         ["message", "callback_query", "message"])
        self.assertEqual(
            [e.key for e in entries],
            ["bot.modules.adminbox.adminbox_command",
             "bot.modules.adminbox.adminbox_callback",
             "bot.modules.adminbox.adminbox_message"],
        )
        # The command entry carries the /adminbox CommandFilter.
        self.assertEqual(
            sorted(command_filters(entries[0].flt)[0].commands), ["adminbox"]
        )
        self.assertTrue(any("/adminbox" in r for r in routes))

    def test_message_hook_group_is_unique(self):
        """21 must stay out of every other module's groups (0..20)."""
        taken = {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 18, 19, 20}
        self.assertNotIn(ab.MESSAGE_GROUP, taken)
        self.assertEqual(ab.MESSAGE_GROUP, 21)

    def test_callback_pattern(self):
        pipeline.clear()
        ab.setup()
        entry = next(e for e in pipeline.snapshot()
                     if e.key == "bot.modules.adminbox.adminbox_callback")
        good = make_callback("abox:main:0", message=_panel_msg(),
                             user_id=PRESSER_ID)
        bad = make_callback("template:1", message=_panel_msg(),
                            user_id=PRESSER_ID)
        self.assertTrue(asyncio.run(_matches(entry, good)))
        self.assertFalse(asyncio.run(_matches(entry, bad)))


# ═════════════════════════════════════════════════════════════════
# Command gating + main panel
# ═════════════════════════════════════════════════════════════════

class TestCommand(unittest.IsolatedAsyncioTestCase):
    async def test_private_refused(self):
        msg = _cmd_msg(chat_type="private")
        await call(ab.adminbox_command, msg, bot=_FakeBot(), chat_data={})
        self.assertEqual(len(_sent(msg)), 1)
        self.assertIn("in a group", _last(msg)["text"])

    async def test_non_admin_refused(self):
        msg = _cmd_msg()
        await call(ab.adminbox_command, msg,
                   bot=_FakeBot(presser_status="member"), chat_data={})
        self.assertEqual(len(_sent(msg)), 0)

    async def test_opens_main_panel(self):
        msg = _cmd_msg()
        await call(ab.adminbox_command, msg, bot=_FakeBot(), chat_data={})
        self.assertEqual(len(_sent(msg)), 1)
        last = _last(msg)
        text, markup = last["text"], last["markup"]
        self.assertIn("Admin Panel", text)
        self.assertIn("Shadow Community", text)
        self.assertIn("1,284", text)
        self.assertIn("Administrator", text)
        data = _datas(markup)
        for expected in (
            "abox:admins:0", "abox:name:0", "abox:bio:0",
            "abox:invite:0", "abox:info:0", "abox:perms:0",
            "abox:bot:0", "abox:tools:0", "abox:settings:0",
            "abox:main:0", "abox:close",
        ):
            self.assertIn(expected, data, f"missing button {expected}")

    async def test_reply_bakes_target_mid(self):
        msg = _cmd_msg(reply_mid=9)
        await call(ab.adminbox_command, msg, bot=_FakeBot(), chat_data={})
        data = _datas(_last(msg)["markup"])
        self.assertIn("abox:tools:9", data)
        # The tools card carries the reply target into every op button.
        query, bot, chat_data = _cb("abox:tools:9")
        await call(ab.adminbox_callback, query, bot=bot, chat_data=chat_data)
        data2 = _datas(_last_edit(query)["markup"])
        for expected in ("abox:pin:9", "abox:unpin:9", "abox:del:9",
                         "abox:purge:9", "abox:bcast:9"):
            self.assertIn(expected, data2)


# ═════════════════════════════════════════════════════════════════
# Callback gating (every press re-verifies)
# ═════════════════════════════════════════════════════════════════

class TestCallbackGate(unittest.IsolatedAsyncioTestCase):
    async def test_non_admin_gets_silent_ack_and_nothing_happens(self):
        for data in ("abox:main:0", "abox:admins:0", "abox:close",
                     "abox:demote:333:0", "abox:name:0"):
            query, bot, chat_data = _cb(data,
                                        bot=_FakeBot(presser_status="member"))
            await call(ab.adminbox_callback, query, bot=bot, chat_data=chat_data)
            self.assertTrue(query.answers, f"no answer for {data}")
            self.assertEqual(query.answers[-1]["text"], "", data)
            self.assertFalse(query.answers[-1]["show_alert"], data)
            self.assertEqual(_edits(query), [], data)
            self.assertEqual(bot.calls, [], data)
            self.assertFalse(_deleted(query), data)

    async def test_valid_prefix_only(self):
        query, bot, chat_data = _cb("other:x")
        await call(ab.adminbox_callback, query, bot=bot, chat_data=chat_data)
        self.assertEqual(query.answers, [])


# ═════════════════════════════════════════════════════════════════
# Main refresh + close
# ═════════════════════════════════════════════════════════════════

class TestMainAndClose(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_renders_main(self):
        query, bot, chat_data = _cb("abox:main:7")
        await call(ab.adminbox_callback, query, bot=bot, chat_data=chat_data)
        self.assertEqual(len(_edits(query)), 1)
        self.assertIn("Admin Panel", _edits(query)[-1]["text"])
        self.assertIn("abox:tools:7", _datas(_last_edit(query)["markup"]))

    async def test_close_deletes_panel(self):
        query, bot, chat_data = _cb("abox:close")
        await call(ab.adminbox_callback, query, bot=bot, chat_data=chat_data)
        self.assertTrue(_deleted(query))
        self.assertEqual(_edits(query), [])

    async def test_backdel_deletes_success_card(self):
        query, bot, chat_data = _cb("abox:backdel")
        await call(ab.adminbox_callback, query, bot=bot, chat_data=chat_data)
        self.assertTrue(_deleted(query))

    async def test_noop_is_silent(self):
        query, bot, chat_data = _cb("abox:noop")
        await call(ab.adminbox_callback, query, bot=bot, chat_data=chat_data)
        self.assertEqual(len(query.answers), 1)
        self.assertFalse(query.answers[-1]["show_alert"])
        self.assertEqual(_edits(query), [])


# ═════════════════════════════════════════════════════════════════
# Admin list + demote protections
# ═════════════════════════════════════════════════════════════════

class TestAdminsView(unittest.IsolatedAsyncioTestCase):
    async def test_owner_bot_self_have_no_remove_button(self):
        query, bot, chat_data = _cb("abox:admins:0")
        await call(ab.adminbox_callback, query, bot=bot, chat_data=chat_data)
        self.assertEqual(len(_edits(query)), 1)
        text = _edits(query)[-1]["text"]
        self.assertIn("Owner Name", text)
        data = _datas(_edits(query)[-1]["markup"])
        demotes = [d for d in data if d.startswith("abox:demote:")]
        # Only OTHER_ADMIN_ID is removable — owner, self and the bot are not.
        self.assertEqual(demotes, [f"abox:demote:{OTHER_ID}:0"])
        self.assertIn("abox:main:0", data)


class TestDemoteFlow(unittest.IsolatedAsyncioTestCase):
    async def test_owner_blocked(self):
        query, bot, chat_data = _cb(f"abox:demote:{OWNER_ID}:0")
        await call(ab.adminbox_callback, query, bot=bot, chat_data=chat_data)
        self.assertIn("owner", query.answers[-1]["text"].lower())
        self.assertTrue(query.answers[-1]["show_alert"])
        self.assertEqual(_calls(bot, "promote_chat_member"), [])

    async def test_self_blocked(self):
        query, bot, chat_data = _cb(f"abox:demote:{PRESSER_ID}:0")
        await call(ab.adminbox_callback, query, bot=bot, chat_data=chat_data)
        self.assertIn("yourself", query.answers[-1]["text"].lower())
        self.assertEqual(_calls(bot, "promote_chat_member"), [])

    async def test_bot_blocked(self):
        query, bot, chat_data = _cb(f"abox:demote:{BOT_ID}:0")
        await call(ab.adminbox_callback, query, bot=bot, chat_data=chat_data)
        self.assertIn("can't demote myself", query.answers[-1]["text"].lower())
        self.assertEqual(_calls(bot, "promote_chat_member"), [])

    async def test_stale_target_blocked(self):
        bot = _FakeBot()
        bot.members[OTHER_ID] = _cm("member", OTHER_ID, "Other Admin")
        query, _, chat_data = _cb(f"abox:demote:{OTHER_ID}:0", bot=bot)
        await call(ab.adminbox_callback, query, bot=bot, chat_data=chat_data)
        self.assertIn("not an admin anymore", query.answers[-1]["text"].lower())
        self.assertEqual(_calls(bot, "promote_chat_member"), [])

    async def test_missing_bot_rights_blocked(self):
        bot = _FakeBot(bot_can_promote=False)
        query, _, chat_data = _cb(f"abox:demote:{OTHER_ID}:0", bot=bot)
        await call(ab.adminbox_callback, query, bot=bot, chat_data=chat_data)
        self.assertIn("promote members", query.answers[-1]["text"].lower())
        self.assertEqual(_calls(bot, "promote_chat_member"), [])

    async def test_unknown_member_blocked(self):
        bot = _FakeBot()
        bot.members[OTHER_ID] = None  # raises in get_chat_member
        query, _, chat_data = _cb(f"abox:demote:{OTHER_ID}:0", bot=bot)
        await call(ab.adminbox_callback, query, bot=bot, chat_data=chat_data)
        self.assertIn("could not find", query.answers[-1]["text"].lower())
        self.assertEqual(_calls(bot, "promote_chat_member"), [])

    async def test_happy_path_calls_promote_with_rights_off(self):
        query, bot, chat_data = _cb(f"abox:demote:{OTHER_ID}:0")
        await call(ab.adminbox_callback, query, bot=bot, chat_data=chat_data)
        calls = _calls(bot, "promote_chat_member")
        self.assertEqual(len(calls), 1)
        _, chat_id, uid, kwargs = calls[0]
        self.assertEqual((chat_id, uid), (CHAT_ID, OTHER_ID))
        # Every admin right must be revoked — this is a demotion.
        self.assertFalse(any(kwargs.get(k) for k in kwargs))
        self.assertIn("demoted", query.answers[-1]["text"].lower())
        # Panel refreshes to the admin list afterwards.
        self.assertEqual(len(_edits(query)), 1)
        self.assertIn("Admin List", _edits(query)[-1]["text"])

    async def test_api_error_surfaced(self):
        bot = _FakeBot()

        async def boom(*a, **kw):
            raise TelegramBadRequest(None, "not enough rights")

        bot.promote_chat_member = boom  # type: ignore[method-assign]
        query, _, chat_data = _cb(f"abox:demote:{OTHER_ID}:0", bot=bot)
        await call(ab.adminbox_callback, query, bot=bot, chat_data=chat_data)
        self.assertIn("could not demote", query.answers[-1]["text"].lower())
        self.assertTrue(query.answers[-1]["show_alert"])


# ═════════════════════════════════════════════════════════════════
# Waiting-input flows
# ═════════════════════════════════════════════════════════════════

class TestWaitingFlows(unittest.IsolatedAsyncioTestCase):
    async def _start(self, action: str, shared: dict, bot: _FakeBot | None = None):
        query, bot, _ = _cb(f"abox:{action}:7", bot=bot, chat_data=shared)
        await call(ab.adminbox_callback, query, bot=bot, chat_data=shared)
        return query, bot

    async def test_name_flow_end_to_end(self):
        shared: dict = {}
        query, bot = await self._start("name", shared)
        # Prompt shown, state stored.
        self.assertIn("Set Group Name", _last_edit(query)["text"])
        self.assertIn("abox:cancel", _datas(_last_edit(query)["markup"]))
        self.assertEqual(shared["abox_wait"][PRESSER_ID]["action"], "name")
        self.assertEqual(shared["abox_wait"][PRESSER_ID]["mid"], 7)
        self.assertEqual(shared["abox_wait"][PRESSER_ID]["panel_mid"], 50)

        # The admin sends the new name.
        hmsg, hbot, hdata = _hook("  New Group Title  ", chat_data=shared, bot=bot)
        await call(ab.adminbox_message, hmsg, bot=hbot, chat_data=hdata)

        self.assertEqual(
            _calls(bot, "set_chat_title"),
            [("set_chat_title", CHAT_ID, "New Group Title")],
        )
        # State retired, panel reverted, success card replied.
        self.assertEqual(shared.get("abox_wait", {}), {})
        self.assertEqual(len(bot.edits), 1)
        self.assertIn("Admin Panel", bot.edits[0]["text"])
        self.assertEqual(bot.edits[0]["message_id"], 50)
        self.assertIn("Group Name Updated", _last(hmsg)["text"])
        self.assertIn("New Group Title", _last(hmsg)["text"])
        self.assertIn("abox:backdel", _datas(_last(hmsg)["markup"]))

    async def test_invalid_name_keeps_state(self):
        shared: dict = {}
        _, bot = await self._start("name", shared)
        hmsg, hbot, hdata = _hook("   ", chat_data=shared, bot=bot)
        await call(ab.adminbox_message, hmsg, bot=hbot, chat_data=hdata)
        self.assertIn("1–128", _last(hmsg)["text"])
        self.assertIn(PRESSER_ID, shared["abox_wait"])  # still waiting
        self.assertEqual(_calls(bot, "set_chat_title"), [])

    async def test_bio_too_long_rejected(self):
        shared: dict = {}
        _, bot = await self._start("bio", shared)
        hmsg, hbot, hdata = _hook("x" * 300, chat_data=shared, bot=bot)
        await call(ab.adminbox_message, hmsg, bot=hbot, chat_data=hdata)
        self.assertIn("255", _last(hmsg)["text"])
        self.assertIn(PRESSER_ID, shared["abox_wait"])
        self.assertEqual(_calls(bot, "set_chat_description"), [])

    async def test_bio_success(self):
        shared: dict = {}
        _, bot = await self._start("bio", shared)
        hmsg, hbot, hdata = _hook("We build cool things.", chat_data=shared, bot=bot)
        await call(ab.adminbox_message, hmsg, bot=hbot, chat_data=hdata)
        self.assertEqual(
            _calls(bot, "set_chat_description"),
            [("set_chat_description", CHAT_ID, "We build cool things.")],
        )
        self.assertEqual(shared.get("abox_wait", {}), {})
        self.assertIn("Group Bio Updated", _last(hmsg)["text"])

    async def test_broadcast_success(self):
        shared: dict = {}
        _, bot = await self._start("bcast", shared)
        hmsg, hbot, hdata = _hook("Hello everyone!", chat_data=shared, bot=bot)
        await call(ab.adminbox_message, hmsg, bot=hbot, chat_data=hdata)
        self.assertEqual(
            _calls(bot, "send_message"),
            [("send_message", CHAT_ID, "Hello everyone!")],
        )
        self.assertEqual(shared.get("abox_wait", {}), {})
        self.assertIn("Broadcast Sent", _last(hmsg)["text"])

    async def test_expired_prompt_is_reverted_silently(self):
        shared: dict = {}
        _, bot = await self._start("name", shared)
        shared["abox_wait"][PRESSER_ID]["expires"] = time.time() - 1
        hmsg, hbot, hdata = _hook("New Title", chat_data=shared, bot=bot)
        await call(ab.adminbox_message, hmsg, bot=hbot, chat_data=hdata)
        self.assertEqual(shared.get("abox_wait", {}), {})
        self.assertEqual(_calls(bot, "set_chat_title"), [])
        self.assertEqual(hmsg.calls, [])             # no reply
        self.assertEqual(len(bot.edits), 1)          # panel restored
        self.assertIn("Admin Panel", bot.edits[0]["text"])

    async def test_command_while_waiting_cancels(self):
        shared: dict = {}
        _, bot = await self._start("name", shared)
        hmsg, hbot, hdata = _hook("/adminbox", chat_data=shared, bot=bot)
        await call(ab.adminbox_message, hmsg, bot=hbot, chat_data=hdata)
        self.assertEqual(shared.get("abox_wait", {}), {})
        self.assertEqual(_calls(bot, "set_chat_title"), [])
        self.assertEqual(hmsg.calls, [])

    async def test_demoted_admin_input_dropped(self):
        shared: dict = {}
        bot = _FakeBot()
        await self._start("name", shared, bot=bot)
        bot.presser_status = "member"  # demoted between press and input
        hmsg, hbot, hdata = _hook("Sneaky Title", chat_data=shared, bot=bot)
        await call(ab.adminbox_message, hmsg, bot=hbot, chat_data=hdata)
        self.assertEqual(shared.get("abox_wait", {}), {})
        self.assertEqual(_calls(bot, "set_chat_title"), [])
        self.assertEqual(hmsg.calls, [])

    async def test_cancel_returns_to_main(self):
        shared: dict = {}
        query, bot = await self._start("name", shared)
        query2, _, chat_data = _cb("abox:cancel", chat_data=shared,
                                   message=query.message)
        await call(ab.adminbox_callback, query2, bot=bot, chat_data=chat_data)
        self.assertEqual(shared.get("abox_wait", {}), {})
        self.assertIn("Admin Panel", _last_edit(query2)["text"])

    async def test_no_state_means_passthrough(self):
        hmsg, hbot, hdata = _hook("plain chatter", chat_data={})
        await call(ab.adminbox_message, hmsg, bot=hbot, chat_data=hdata)
        self.assertEqual(hmsg.calls, [])
        self.assertEqual(hbot.calls, [])
        self.assertNotIn("abox_wait", hdata)


# ═════════════════════════════════════════════════════════════════
# Views
# ═════════════════════════════════════════════════════════════════

class TestViews(unittest.IsolatedAsyncioTestCase):
    async def _view(self, data: str, bot: _FakeBot | None = None):
        query, bot, _ = _cb(data, bot=bot)
        await call(ab.adminbox_callback, query, bot=bot, chat_data={})
        self.assertEqual(len(_edits(query)), 1, f"{data} → {query.answers}")
        return _last_edit(query), bot, query

    async def test_info_view(self):
        edit, _, _ = await self._view("abox:info:0")
        text = edit["text"]
        self.assertIn("Group Info", text)
        self.assertIn("Shadow Community", text)
        self.assertIn(str(CHAT_ID), text)
        self.assertIn("1,284", text)
        self.assertIn("Owner Name", text)
        self.assertIn("abox:main:0", _datas(edit["markup"]))

    async def test_perms_view_admin(self):
        edit, _, _ = await self._view("abox:perms:0")
        text = edit["text"]
        self.assertIn("Your Permissions", text)
        self.assertIn("Administrator", text)
        self.assertIn("Allowed", text)
        self.assertNotIn("Denied", text)  # fake admin has every right

    async def test_perms_view_owner(self):
        query, bot, chat_data = _cb(
            "abox:perms:0", user=_user(OWNER_ID, "Owner Name")
        )
        await call(ab.adminbox_callback, query, bot=bot, chat_data=chat_data)
        text = _last_edit(query)["text"]
        self.assertIn("Owner", text)
        self.assertIn("you own this group", text)

    async def test_bot_status_view(self):
        edit, _, _ = await self._view("abox:bot:0")
        text = edit["text"]
        self.assertIn("Bot Status", text)
        self.assertIn("@PiModulerBot", text)
        self.assertIn("Online", text)
        self.assertIn("Yes", text)

    async def test_bot_status_not_admin(self):
        edit, _, _ = await self._view(
            "abox:bot:0", bot=_FakeBot(bot_status="member")
        )
        self.assertIn("promote me", edit["text"])

    async def test_invite_view_shows_primary_link(self):
        edit, _, _ = await self._view("abox:invite:0")
        text = edit["text"]
        self.assertIn("Invite Link", text)
        self.assertIn("+PRIMARY", text)
        data = _datas(edit["markup"])
        self.assertIn("abox:invitegen:0", data)

    async def test_invite_generate_creates_link(self):
        edit, bot, _ = await self._view("abox:invitegen:0")
        calls = _calls(bot, "create_chat_invite_link")
        self.assertEqual(len(calls), 1)
        self.assertNotIn("creates_join_request", calls[0][2])
        self.assertIn("+NEWLINK", edit["text"])

    async def test_tools_view(self):
        edit, _, _ = await self._view("abox:tools:9")
        text = edit["text"]
        self.assertIn("Message Tools", text)
        data = _datas(edit["markup"])
        for expected in ("abox:pin:9", "abox:unpin:9", "abox:del:9",
                         "abox:purge:9", "abox:bcast:9", "abox:main:9"):
            self.assertIn(expected, data)

    async def test_settings_view(self):
        edit, _, _ = await self._view("abox:settings:0")
        text = edit["text"]
        self.assertIn("Group Settings", text)
        self.assertIn("Off", text)                     # slow mode value
        self.assertIn("Telegram → Group Info", text)   # honest note
        self.assertIn("7/8 allowed", text)  # can_change_info=False in fake
        data = _datas(edit["markup"])
        self.assertIn("abox:joinlink:0", data)
        self.assertIn("abox:dperms:0", data)

    async def test_dperms_view(self):
        edit, _, _ = await self._view("abox:dperms:0")
        text = edit["text"]
        self.assertIn("Default Permissions", text)
        self.assertIn("Allowed", text)
        self.assertIn("Denied", text)  # can_change_info=False in the fake

    async def test_joinlink_creates_request_invite(self):
        edit, bot, query = await self._view("abox:joinlink:0")
        calls = _calls(bot, "create_chat_invite_link")
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0][2].get("creates_join_request"))
        self.assertIn("+JOINREQ", edit["text"])
        self.assertIn("wait for your approval", edit["text"])
        self.assertIn("Join-request link created.",
                      query.answers[-1]["text"])


# ═════════════════════════════════════════════════════════════════
# Message tools: pin / unpin / delete / purge
# ═════════════════════════════════════════════════════════════════

class TestMessageTools(unittest.IsolatedAsyncioTestCase):
    async def test_pin_without_reply_alerts(self):
        query, bot, chat_data = _cb("abox:pin:0")
        await call(ab.adminbox_callback, query, bot=bot, chat_data=chat_data)
        self.assertTrue(query.answers[-1]["show_alert"])
        self.assertIn("reply", query.answers[-1]["text"].lower())
        self.assertEqual(_calls(bot, "pin_chat_message"), [])

    async def test_pin_with_reply(self):
        query, bot, chat_data = _cb("abox:pin:9")
        await call(ab.adminbox_callback, query, bot=bot, chat_data=chat_data)
        self.assertEqual(
            _calls(bot, "pin_chat_message"), [("pin_chat_message", CHAT_ID, 9)]
        )
        self.assertIn("Pinned", query.answers[-1]["text"])

    async def test_unpin_with_reply(self):
        query, bot, chat_data = _cb("abox:unpin:9")
        await call(ab.adminbox_callback, query, bot=bot, chat_data=chat_data)
        self.assertEqual(
            _calls(bot, "unpin_chat_message"),
            [("unpin_chat_message", CHAT_ID, 9)],
        )

    async def test_delete_with_reply(self):
        query, bot, chat_data = _cb("abox:del:9")
        await call(ab.adminbox_callback, query, bot=bot, chat_data=chat_data)
        self.assertEqual(
            _calls(bot, "delete_message"), [("delete_message", CHAT_ID, 9)]
        )

    async def test_api_error_is_alerted(self):
        bot = _FakeBot()

        async def boom(*a, **kw):
            raise TelegramBadRequest(None, "message to pin not found")

        bot.pin_chat_message = boom  # type: ignore[method-assign]
        query, _, chat_data = _cb("abox:pin:9", bot=bot)
        await call(ab.adminbox_callback, query, bot=bot, chat_data=chat_data)
        self.assertIn("not found", query.answers[-1]["text"])
        self.assertTrue(query.answers[-1]["show_alert"])

    async def test_purge_deletes_tracked_ids(self):
        shared = {"abox_recent": deque([10, 11, 12])}
        query, bot, chat_data = _cb("abox:purge:0", chat_data=shared)
        await call(ab.adminbox_callback, query, bot=bot, chat_data=chat_data)
        self.assertEqual(
            _calls(bot, "delete_messages"),
            [("delete_messages", CHAT_ID, [10, 11, 12])],
        )
        self.assertIn("Deleted 3", query.answers[-1]["text"])

    async def test_purge_caps_at_purge_max(self):
        shared = {"abox_recent": deque(range(100, 150))}
        query, bot, chat_data = _cb("abox:purge:0", chat_data=shared)
        await call(ab.adminbox_callback, query, bot=bot, chat_data=chat_data)
        ids = _calls(bot, "delete_messages")[0][2]
        self.assertEqual(len(ids), ab.PURGE_MAX)
        self.assertEqual(ids[-1], 149)  # newest kept

    async def test_purge_empty_alerts(self):
        query, bot, chat_data = _cb("abox:purge:0")
        await call(ab.adminbox_callback, query, bot=bot, chat_data=chat_data)
        self.assertTrue(query.answers[-1]["show_alert"])
        self.assertEqual(_calls(bot, "delete_messages"), [])

    async def test_hook_tracks_deletable_messages(self):
        hmsg, hbot, hdata = _hook("count me", chat_data={}, message_id=41)
        await call(ab.adminbox_message, hmsg, bot=hbot, chat_data=hdata)
        self.assertEqual(list(hdata["abox_recent"]), [41])

    async def test_hook_skips_bot_and_service_messages(self):
        shared: dict = {}
        # A bot's own message — not tracked.
        hmsg, hbot, hdata = _hook("bot says hi", chat_data=shared, message_id=42)
        hmsg.from_user = _user(BOT_ID, "Phi Bot", is_bot=True)
        await call(ab.adminbox_message, hmsg, bot=hbot, chat_data=hdata)
        # A service message (pinned) — not tracked.
        hmsg2, hbot2, hdata2 = _hook("pinned!", chat_data=shared, message_id=43)
        hmsg2.pinned_message = SimpleNamespace(message_id=43)
        await call(ab.adminbox_message, hmsg2, bot=hbot2, chat_data=hdata2)
        # A normal user message — tracked.
        hmsg3, hbot3, hdata3 = _hook("count me", chat_data=shared, message_id=44)
        await call(ab.adminbox_message, hmsg3, bot=hbot3, chat_data=hdata3)
        self.assertEqual(list(shared["abox_recent"]), [44])


if __name__ == "__main__":
    unittest.main(verbosity=2)
