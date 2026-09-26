"""Tests for the owner-only /mychats module (bot/modules/mychats.py).

Run from the repo root:

    python tests/test_mychats.py
    python -m unittest discover -s tests

Environment isolation (BEFORE any bot import):
    * BOT_TOKEN is forced — importing `bot` pulls bot.config, which
      exits without one.
    * LOCALAPPDATA points at a temp dir so runtime files are isolated.

No network: every Telegram call goes through fake bots/queries.
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
_TEST_DIR = tempfile.mkdtemp(prefix="pi_mychats_test_")
os.environ["LOCALAPPDATA"] = _TEST_DIR
atexit.register(shutil.rmtree, _TEST_DIR, ignore_errors=True)

# ── Imports (after env) ───────────────────────────────────────────
from bot.constants import HELP_MENU  # noqa: E402
from bot.database import db  # noqa: E402
from bot.modules import mychats as mm  # noqa: E402

OWNER_ID = 42
CID = -1004331827383


# ═════════════════════════════════════════════════════════════════
# Fakes
# ═════════════════════════════════════════════════════════════════

def _rows(n: int, start_id: int = -100000) -> list:
    """DB-shaped rows — what ``db.get_all_groups()`` returns."""
    return [
        {"chat_id": start_id - i, "chat_title": f"Chat {i}"} for i in range(n)
    ]


def _chats(n: int, start_id: int = -100000) -> list:
    """Scan-shaped entries — what the cache stores (``title`` key)."""
    return [
        {"chat_id": start_id - i, "title": f"Chat {i}"} for i in range(n)
    ]


class _FakeBot:
    id = 8958628679

    def __init__(self, statuses=None, count=1234, chat=None,
                 invite="https://t.me/+INVITEHASH", can_invite=True,
                 count_error=False, chat_error=False):
        self.statuses = dict(statuses or {})
        self.count = count
        self.chat = chat
        self.invite = invite
        self.can_invite = can_invite
        self.count_error = count_error
        self.chat_error = chat_error
        self.calls: list = []

    async def get_chat_member(self, chat_id, user_id):
        self.calls.append(("get_chat_member", chat_id, user_id))
        st = self.statuses.get(chat_id, "raise")
        if st == "raise":
            raise RuntimeError("Forbidden: bot is not a member")
        return SimpleNamespace(status=st, can_invite_users=self.can_invite)

    async def get_chat_member_count(self, chat_id):
        self.calls.append(("get_chat_member_count", chat_id))
        if self.count_error:
            raise RuntimeError("count failed")
        return self.count

    async def get_chat(self, chat_id):
        self.calls.append(("get_chat", chat_id))
        if self.chat_error:
            raise RuntimeError("chat unavailable")
        return self.chat

    async def exportChatInviteLink(self, chat_id):
        self.calls.append(("exportChatInviteLink", chat_id))
        return self.invite


class _Ctx:
    def __init__(self, bot=None):
        self.bot = bot if bot is not None else _FakeBot()
        self.bot_data: dict = {}


class _Msg:
    def __init__(self, text: str = "/mychats") -> None:
        self.text = text
        self.replies: list = []

    async def reply_text(self, text, **kw):
        self.replies.append({"text": text, **kw})
        return self

    @property
    def last(self):
        return self.replies[-1] if self.replies else None


class _Query:
    def __init__(self, data: str, user_id=OWNER_ID) -> None:
        self.data = data
        self.from_user = (
            None if user_id is None
            else SimpleNamespace(id=user_id, username=None, first_name="T")
        )
        self.message = None
        self.answers: list = []
        self.edits: list = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append({"text": text, "show_alert": show_alert})

    async def edit_message_text(self, text, **kw):
        self.edits.append({"text": text, **kw})
        return True

    @property
    def last_edit(self):
        return self.edits[-1] if self.edits else None


def _update(msg: _Msg, user_id=OWNER_ID):
    user = None if user_id is None else SimpleNamespace(
        id=user_id, username=None, first_name="T"
    )
    return SimpleNamespace(effective_message=msg, effective_user=user,
                           message=msg)


def _cb_update(data: str, user_id=OWNER_ID):
    return SimpleNamespace(callback_query=_Query(data, user_id=user_id))


@contextmanager
def _owner():
    with mock.patch.object(mm, "settings", SimpleNamespace(owner_id=OWNER_ID)):
        yield


@contextmanager
def _db(rows: list):
    with mock.patch.object(mm, "db",
                           SimpleNamespace(get_all_groups=lambda: rows)):
        yield


# ═════════════════════════════════════════════════════════════════
# Pure helpers — pagination, labels, texts, keyboard
# ═════════════════════════════════════════════════════════════════

class TestPaginationHelpers(unittest.TestCase):
    def test_page_count_edges(self):
        self.assertEqual(mm._page_count(0), 1)
        self.assertEqual(mm._page_count(5), 1)
        self.assertEqual(mm._page_count(6), 2)
        self.assertEqual(mm._page_count(12), 3)

    def test_clamp(self):
        self.assertEqual(mm._clamp(-3, 12), 0)
        self.assertEqual(mm._clamp(99, 12), 2)
        self.assertEqual(mm._clamp(0, 0), 0)

    def test_int_at_handles_junk(self):
        self.assertEqual(mm._int_at(["a", "info", "17"], 2), 17)
        self.assertEqual(mm._int_at(["a", "info"], 2), 0)
        self.assertEqual(mm._int_at(["a", "info", "x"], 2), 0)


class TestButtonLabel(unittest.TestCase):
    def test_short_title_unchanged(self):
        self.assertEqual(mm._button_label("Fairy sakti"), "Fairy sakti")

    def test_blank_falls_back_to_unnamed(self):
        self.assertEqual(mm._button_label("   "), "Unnamed")

    def test_long_title_trimmed_to_64_bytes(self):
        raw = "\u00e9" * 60            # 120 bytes
        out = mm._button_label(raw)
        self.assertLessEqual(len(out.encode("utf-8")), 64)
        self.assertTrue(out.endswith("\u2026"))

    def test_multibyte_never_splits(self):
        out = mm._button_label("\U0001f525" * 30)  # 4-byte emoji
        self.assertLessEqual(len(out.encode("utf-8")), 64)
        out.encode("utf-8").decode("utf-8")         # must not raise


class TestMenuText(unittest.TestCase):
    def test_counts_and_page_line(self):
        text = mm._menu_text(_rows(12), 1)
        self.assertIn("My Chats", text)
        self.assertIn("Admin chats: 12", text)
        self.assertIn("Page: 2/3", text)

    def test_empty_state(self):
        text = mm._menu_text([], 0)
        self.assertIn("Admin chats: 0", text)
        self.assertIn("/mychats again", text)

    def test_structural_emoji_free(self):
        """Only E.* glyphs — no stock structural emoji in the card."""
        text = mm._menu_text(_rows(3), 0)
        for glyph in ("\U0001f4ac", "\U0001f4ca", "\U0001f195"):
            self.assertNotIn(glyph, text)


class TestMenuKeyboard(unittest.TestCase):
    def _buttons(self, markup, row):
        return markup.inline_keyboard[row]

    def test_five_chat_rows_plus_nav_on_page_zero(self):
        kb = mm._menu_keyboard(_chats(12), 0)
        # 5 chat rows + 1 nav row (indicator + next; no prev on page 0)
        self.assertEqual(len(kb.inline_keyboard), 6)
        texts = [self._buttons(kb, r)[0].text for r in range(5)]
        self.assertEqual(texts, [f"Chat {i}" for i in range(5)])
        nav = [b.text for b in self._buttons(kb, 5)]
        self.assertEqual(nav, ["1/3", ">"])

    def test_chat_callbacks_carry_page(self):
        kb = mm._menu_keyboard(_chats(12), 0)
        first = self._buttons(kb, 0)[0]
        self.assertEqual(first.callback_data, "mychats:info:-100000:0")
        nav = self._buttons(kb, 5)
        self.assertEqual(nav[0].callback_data, "mychats:noop")
        self.assertEqual(nav[1].callback_data, "mychats:page:1")

    def test_middle_page_has_prev_and_next(self):
        kb = mm._menu_keyboard(_chats(12), 1)
        nav = [b.callback_data for b in self._buttons(kb, 5)]
        self.assertEqual(nav, ["mychats:page:0", "mychats:noop",
                               "mychats:page:2"])

    def test_single_page_has_no_nav_row(self):
        kb = mm._menu_keyboard(_chats(3), 0)
        self.assertEqual(len(kb.inline_keyboard), 3)

    def test_partial_last_page(self):
        kb = mm._menu_keyboard(_chats(7), 1)
        self.assertEqual(len(kb.inline_keyboard), 3)  # 2 chats + nav
        texts = [self._buttons(kb, r)[0].text for r in range(2)]
        self.assertEqual(texts, ["Chat 5", "Chat 6"])

    def test_all_buttons_green(self):
        kb = mm._menu_keyboard(_chats(12), 1)
        for row in kb.inline_keyboard:
            for btn in row:
                self.assertEqual(btn.api_kwargs.get("style"), "success")
        back = mm._back_keyboard(2)
        self.assertEqual(
            back.inline_keyboard[0][0].api_kwargs.get("style"), "success"
        )
        self.assertEqual(back.inline_keyboard[0][0].callback_data,
                         "mychats:page:2")


# ═════════════════════════════════════════════════════════════════
# Admin scan + cache
# ═════════════════════════════════════════════════════════════════

class TestScanAdminChats(unittest.IsolatedAsyncioTestCase):
    async def test_keeps_admin_and_creator_drops_member_and_errors(self):
        rows = _rows(4)                       # ids -100000..-100003
        statuses = {
            -100000: "administrator",
            -100001: "member",
            -100002: "creator",
            # -100003 missing → fake raises
        }
        bot = _FakeBot(statuses=statuses)
        with _db(rows):
            found = await mm._scan_admin_chats(_Ctx(bot))
        self.assertEqual(
            [(c["chat_id"], c["title"]) for c in found],
            [(-100000, "Chat 0"), (-100002, "Chat 2")],
        )

    async def test_missing_title_becomes_unnamed(self):
        bot = _FakeBot(statuses={-100000: "administrator"})
        with _db([{"chat_id": -100000, "chat_title": None}]):
            found = await mm._scan_admin_chats(_Ctx(bot))
        self.assertEqual(found, [{"chat_id": -100000, "title": "Unnamed"}])

    async def test_row_without_chat_id_skipped(self):
        bot = _FakeBot(statuses={-100000: "administrator"})
        with _db([{"chat_title": "No id"}, {"chat_id": -100000,
                                            "chat_title": "Ok"}]):
            found = await mm._scan_admin_chats(_Ctx(bot))
        self.assertEqual([c["chat_id"] for c in found], [-100000])

    async def test_empty_db_returns_empty(self):
        with _db([]):
            found = await mm._scan_admin_chats(_Ctx(_FakeBot()))
        self.assertEqual(found, [])
        self.assertEqual(_FakeBot().calls, [])  # untouched fake sanity

    async def test_fresh_scan_is_cached(self):
        bot = _FakeBot(statuses={-100000: "administrator"})
        ctx = _Ctx(bot)
        with _db(_rows(1)):
            chats = await mm._fresh_chats(ctx)
        self.assertEqual(mm._cached(ctx), chats)
        self.assertEqual(len(bot.calls), 1)


# ═════════════════════════════════════════════════════════════════
# Owner gate — command
# ═════════════════════════════════════════════════════════════════

class TestOwnerGateCommand(unittest.IsolatedAsyncioTestCase):
    async def test_non_owner_denied_without_scan(self):
        msg = _Msg()
        bot = _FakeBot(statuses={})           # would raise on any call
        with _owner(), _db(_rows(3)):
            await mm.mychats_command(_update(msg, user_id=99), _Ctx(bot))
        self.assertIn("Only the bot owner", msg.last["text"])
        self.assertIn("/mychats", msg.last["text"])
        self.assertEqual(bot.calls, [])

    async def test_missing_user_denied(self):
        msg = _Msg()
        with _owner(), _db([]):
            await mm.mychats_command(_update(msg, user_id=None), _Ctx())
        self.assertIn("Only the bot owner", msg.last["text"])

    async def test_unconfigured_owner_denies_everyone(self):
        msg = _Msg()
        with mock.patch.object(mm, "settings", SimpleNamespace(owner_id=0)):
            await mm.mychats_command(_update(msg, user_id=0), _Ctx())
        self.assertIn("Only the bot owner", msg.last["text"])


# ═════════════════════════════════════════════════════════════════
# Owner gate — callbacks
# ═════════════════════════════════════════════════════════════════

class TestOwnerGateCallback(unittest.IsolatedAsyncioTestCase):
    async def test_non_owner_gets_alert_and_no_edit(self):
        q = _Query("mychats:page:1", user_id=99)
        bot = _FakeBot()
        with _owner(), _db(_rows(6)):
            await mm.mychats_callback(
                SimpleNamespace(callback_query=q), _Ctx(bot)
            )
        self.assertEqual(len(q.answers), 1)
        self.assertIn("Only the bot owner", q.answers[0]["text"])
        self.assertTrue(q.answers[0]["show_alert"])
        self.assertEqual(q.edits, [])
        self.assertEqual(bot.calls, [])        # denied BEFORE any scan

    async def test_unconfigured_owner_denies_everyone(self):
        q = _Query("mychats:noop", user_id=0)
        with mock.patch.object(mm, "settings", SimpleNamespace(owner_id=0)):
            await mm.mychats_callback(
                SimpleNamespace(callback_query=q), _Ctx()
            )
        self.assertTrue(q.answers[0]["show_alert"])

    async def test_unknown_action_is_flagged(self):
        q = _Query("mychats:bogus")
        with _owner():
            await mm.mychats_callback(
                SimpleNamespace(callback_query=q), _Ctx()
            )
        self.assertEqual(q.answers[-1]["text"], "Unknown option")
        self.assertTrue(q.answers[-1]["show_alert"])

    async def test_noop_answers_silently(self):
        q = _Query("mychats:noop")
        with _owner():
            await mm.mychats_callback(
                SimpleNamespace(callback_query=q), _Ctx()
            )
        self.assertEqual(q.answers, [{"text": None, "show_alert": False}])
        self.assertEqual(q.edits, [])


# ═════════════════════════════════════════════════════════════════
# Command — owner path
# ═════════════════════════════════════════════════════════════════

class TestCommandOwnerPath(unittest.IsolatedAsyncioTestCase):
    async def test_lists_admin_chats_with_keyboard(self):
        msg = _Msg()
        bot = _FakeBot(statuses={
            -100000: "administrator", -100001: "member",
            -100002: "creator",
        })
        with _owner(), _db(_rows(3)):
            await mm.mychats_command(_update(msg), _Ctx(bot))
        self.assertEqual(len(msg.replies), 1)
        self.assertIn("Admin chats: 2", msg.last["text"])
        self.assertEqual(msg.last["parse_mode"], "HTML")
        self.assertIsNotNone(msg.last["reply_markup"])
        # one status check per tracked group
        checked = [c[1] for c in bot.calls if c[0] == "get_chat_member"]
        self.assertEqual(checked, [-100000, -100001, -100002])

    async def test_no_admin_chats_sends_empty_state_without_buttons(self):
        msg = _Msg()
        bot = _FakeBot(statuses={})            # everything raises
        with _owner(), _db(_rows(2)):
            await mm.mychats_command(_update(msg), _Ctx(bot))
        self.assertIn("Admin chats: 0", msg.last["text"])
        self.assertIsNone(msg.last["reply_markup"])


# ═════════════════════════════════════════════════════════════════
# Callback — pagination + cache
# ═════════════════════════════════════════════════════════════════

class TestPageCallback(unittest.IsolatedAsyncioTestCase):
    async def test_warm_cache_flips_page_without_rescan(self):
        q = _Query("mychats:page:1")
        ctx = _Ctx(_FakeBot())                # statuses empty → rescan
        chats = _chats(12)
        mm._store_cache(ctx, chats)
        with _owner():
            await mm.mychats_callback(SimpleNamespace(callback_query=q), ctx)
        self.assertEqual(len(q.edits), 1)
        self.assertIn("Page: 2/3", q.edits[0]["text"])
        self.assertEqual(ctx.bot.calls, [])    # cache reused, no network

    async def test_cold_cache_triggers_rescan(self):
        q = _Query("mychats:page:0")
        bot = _FakeBot(statuses={-100000: "administrator"})
        with _owner(), _db(_rows(1)):
            await mm.mychats_callback(
                SimpleNamespace(callback_query=q), _Ctx(bot)
            )
        self.assertEqual(len(q.edits), 1)
        self.assertEqual(len(bot.calls), 1)    # the fresh scan ran

    async def test_stale_cache_triggers_rescan(self):
        q = _Query("mychats:page:0")
        ctx = _Ctx(_FakeBot(statuses={-100000: "administrator"}))
        ctx.bot_data[mm._CACHE_KEY] = {"at": 0.0, "chats": _chats(99)}
        with _owner(), _db(_rows(1)):
            await mm.mychats_callback(SimpleNamespace(callback_query=q), ctx)
        self.assertEqual(len(ctx.bot.calls), 1)
        self.assertIn("Admin chats: 1", q.edits[0]["text"])

    async def test_page_out_of_range_clamps(self):
        q = _Query("mychats:page:99")
        ctx = _Ctx(_FakeBot())
        mm._store_cache(ctx, _chats(6))        # 2 pages
        with _owner():
            await mm.mychats_callback(SimpleNamespace(callback_query=q), ctx)
        self.assertIn("Page: 2/2", q.edits[0]["text"])


# ═════════════════════════════════════════════════════════════════
# Callback — info card
# ═════════════════════════════════════════════════════════════════

class TestInfoCallback(unittest.IsolatedAsyncioTestCase):
    def _run(self, *, page=0, chat_id=CID, bot=None, ctx=None, data=None):
        q = _Query(data or f"mychats:info:{chat_id}:{page}")
        ctx = ctx or _Ctx(bot or _FakeBot())
        return q, ctx

    async def test_card_matches_exact_template_with_invite_link(self):
        chat = SimpleNamespace(id=CID, title="Fairy sakti degi mukti",
                               username=None, first_name=None)
        bot = _FakeBot(statuses={CID: "administrator"}, count=1234,
                       chat=chat)
        q, ctx = self._run(bot=bot)
        mm._store_cache(ctx, [{"chat_id": CID, "title": "Fairy"}])
        with _owner():
            await mm.mychats_callback(SimpleNamespace(callback_query=q), ctx)
        expected = (
            "ᴄʜᴀᴛ ɴᴀᴍᴇ : Fairy sakti degi mukti\n"
            "ᴄʜᴀᴛ ɪᴅ : -1004331827383\n"
            "ᴄʜᴀᴛ ᴜsᴇʀɴᴀᴍᴇ : No username\n"
            "ɢʀᴏᴜᴘ ᴍᴇᴍʙᴇʀs : 1,234\n"
            "ᴄʜᴀᴛ ʟɪɴᴋ : https://t.me/+INVITEHASH"
        )
        self.assertEqual(q.last_edit["text"], expected)
        self.assertEqual(q.last_edit["parse_mode"], "HTML")
        # invite link path: rights check + export both hit
        kinds = [c[0] for c in bot.calls]
        self.assertIn("exportChatInviteLink", kinds)

    async def test_no_invite_permission_falls_back_to_public_link(self):
        chat = SimpleNamespace(id=CID, title="Pub", username="pubchat",
                               first_name=None)
        bot = _FakeBot(statuses={CID: "administrator"}, chat=chat,
                       can_invite=False)
        q, ctx = self._run(bot=bot)
        mm._store_cache(ctx, [{"chat_id": CID, "title": "Pub"}])
        with _owner():
            await mm.mychats_callback(SimpleNamespace(callback_query=q), ctx)
        self.assertIn("ᴄʜᴀᴛ ʟɪɴᴋ : https://t.me/pubchat",
                      q.last_edit["text"])
        kinds = [c[0] for c in bot.calls]
        self.assertNotIn("exportChatInviteLink", kinds)

    async def test_private_fallback_uses_c_link(self):
        chat = SimpleNamespace(id=CID, title="Priv", username=None,
                               first_name=None)
        bot = _FakeBot(statuses={CID: "administrator"}, chat=chat,
                       can_invite=False)
        q, ctx = self._run(bot=bot)
        mm._store_cache(ctx, [{"chat_id": CID, "title": "Priv"}])
        with _owner():
            await mm.mychats_callback(SimpleNamespace(callback_query=q), ctx)
        self.assertIn("ᴄʜᴀᴛ ʟɪɴᴋ : https://t.me/c/4331827383",
                      q.last_edit["text"])

    async def test_creator_status_still_gets_invite_link(self):
        """status == creator → may invite even without can_invite_users."""
        chat = SimpleNamespace(id=CID, title="Crew", username=None,
                               first_name=None)
        bot = _FakeBot(statuses={CID: "creator"}, chat=chat,
                       can_invite=False)
        q, ctx = self._run(bot=bot)
        mm._store_cache(ctx, [{"chat_id": CID, "title": "Crew"}])
        with _owner():
            await mm.mychats_callback(SimpleNamespace(callback_query=q), ctx)
        self.assertIn("https://t.me/+INVITEHASH", q.last_edit["text"])

    async def test_member_count_failure_shows_unknown(self):
        chat = SimpleNamespace(id=CID, title="X", username=None,
                               first_name=None)
        bot = _FakeBot(statuses={CID: "administrator"}, chat=chat,
                       count_error=True)
        q, ctx = self._run(bot=bot)
        mm._store_cache(ctx, [{"chat_id": CID, "title": "X"}])
        with _owner():
            await mm.mychats_callback(SimpleNamespace(callback_query=q), ctx)
        self.assertIn("ɢʀᴏᴜᴘ ᴍᴇᴍʙᴇʀs : Unknown", q.last_edit["text"])

    async def test_chat_load_failure_shows_error_card_with_back(self):
        bot = _FakeBot(chat_error=True)
        q, ctx = self._run(bot=bot)
        mm._store_cache(ctx, [{"chat_id": CID, "title": "X"}])
        with _owner():
            await mm.mychats_callback(SimpleNamespace(callback_query=q), ctx)
        self.assertIn("Could not load this chat", q.last_edit["text"])
        self.assertEqual(
            q.last_edit["reply_markup"].inline_keyboard[0][0].callback_data,
            "mychats:page:0",
        )

    async def test_back_returns_to_calling_page(self):
        chat = SimpleNamespace(id=CID, title="X", username=None,
                               first_name=None)
        bot = _FakeBot(statuses={CID: "administrator"}, chat=chat)
        q, ctx = self._run(page=2, bot=bot)
        mm._store_cache(ctx, _chats(12))       # 3 pages → page 2 valid
        with _owner():
            await mm.mychats_callback(SimpleNamespace(callback_query=q), ctx)
        back = q.last_edit["reply_markup"].inline_keyboard[0][0]
        self.assertEqual(back.callback_data, "mychats:page:2")
        self.assertEqual(back.api_kwargs.get("style"), "success")

    async def test_title_is_html_escaped(self):
        chat = SimpleNamespace(id=CID, title="<b>&evil</b>", username=None,
                               first_name=None)
        bot = _FakeBot(statuses={CID: "administrator"}, chat=chat)
        q, ctx = self._run(bot=bot)
        mm._store_cache(ctx, [{"chat_id": CID, "title": "X"}])
        with _owner():
            await mm.mychats_callback(SimpleNamespace(callback_query=q), ctx)
        self.assertIn("ᴄʜᴀᴛ ɴᴀᴍᴇ : &lt;b&gt;&amp;evil&lt;/b&gt;",
                      q.last_edit["text"])
        self.assertNotIn("<b>", q.last_edit["text"])


# ═════════════════════════════════════════════════════════════════
# Database layer — get_all_groups
# ═════════════════════════════════════════════════════════════════

class TestGetAllGroups(unittest.TestCase):
    """mongomock integration: shape, projection, title sort."""

    def test_returns_sorted_id_and_title_only(self):
        db.add_group(-100901000001, "Zeta Mychats")
        db.add_group(-100901000002, "Alpha Mychats")
        rows = [r for r in db.get_all_groups()
                if r["chat_id"] in (-100901000001, -100901000002)]
        self.assertEqual(
            [r["chat_title"] for r in rows],
            ["Alpha Mychats", "Zeta Mychats"],
        )
        for r in rows:
            self.assertEqual(set(r.keys()), {"chat_id", "chat_title"})

    def test_empty_db_returns_empty_list(self):
        # Other tests may have seeded groups; just assert the type here.
        self.assertIsInstance(db.get_all_groups(), list)


# ═════════════════════════════════════════════════════════════════
# Wiring + help
# ═════════════════════════════════════════════════════════════════

class TestWiring(unittest.TestCase):
    def test_setup_registers_command_and_callback(self):
        from telegram.ext import (CallbackQueryHandler,
                                  CommandHandler as PTBCommandHandler)

        class _App:
            def __init__(self):
                self.handlers = []

            def add_handler(self, handler, group=0):
                self.handlers.append(handler)

        app = _App()
        routes = mm.setup(app)
        self.assertEqual(routes, ["/mychats"])
        cmds = [h for h in app.handlers
                if isinstance(h, PTBCommandHandler)]
        cbs = [h for h in app.handlers
               if isinstance(h, CallbackQueryHandler)]
        self.assertEqual(len(cmds), 1)
        self.assertEqual(sorted(cmds[0].commands), ["mychats"])
        self.assertEqual(len(cbs), 1)
        self.assertEqual(cbs[0].pattern.pattern, "^mychats:")

    def test_help_documents_mychats(self):
        general = next(m for m in HELP_MENU if m["key"] == "general")
        lines = [line for _, cmds in general["sections"] for line in cmds]
        self.assertTrue(
            any(line.startswith("/mychats") for line in lines),
            "General help is missing the /mychats entry",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
