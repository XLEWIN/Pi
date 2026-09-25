"""Tests for the interactive /help menu (bot/modules/help.py).

Run from the Pi/Pi root:

    python tests/test_help_menu.py
    python -m unittest discover -s tests

Environment isolation (BEFORE any bot import):
    * BOT_TOKEN is forced — importing `bot` pulls bot.config, which
      exits without one.
    * LOCALAPPDATA points at a temp dir so any DB touch stays isolated.
No network: handlers run against fakes that record replies/edits.

Production layout (private): [header text][Pi logo photo + buttons].
The header message id rides along in callbacks as ``t<id>``.
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
_TEST_DIR = tempfile.mkdtemp(prefix="pi_help_test_")
os.environ["LOCALAPPDATA"] = _TEST_DIR
atexit.register(shutil.rmtree, _TEST_DIR, ignore_errors=True)

# ── Imports (after env) ───────────────────────────────────────────
from bot.constants import BOT_DESCRIPTION, HELP_MENU  # noqa: E402
from bot.modules import help as help_mod  # noqa: E402
from bot.modules import start as start_mod  # noqa: E402

BOT_USERNAME = "PiModulerBot"

#: reply_text ids start here: first header message of a menu stack.
TEXT_MSG_ID = 600


# ── Fakes ─────────────────────────────────────────────────────────

class _SentText:
    """Return value of FakeMessage.reply_text — the header message."""

    def __init__(self, message_id, chat, text):
        self.message_id = message_id
        self.chat = chat
        self.text = text
        self.markup_edits = []

    async def edit_message_reply_markup(self, reply_markup=None, **kw):
        self.markup_edits.append(reply_markup)


class _FakeMessage:
    def __init__(self, chat_type: str = "private", chat_id: int = 1,
                 message_id: int = 100):
        self.chat = SimpleNamespace(id=chat_id, type=chat_type, title="Test Chat")
        self.message_id = message_id
        self.message_thread_id = None
        self.replies: list = []   # {"text", "markup"} dicts
        self.sent: list = []      # _SentText instances (header messages)
        self.photos: list = []    # {"photo", "markup"} dicts
        self.markup_edits: list = []
        self.deleted = False

    async def reply_text(self, text, parse_mode=None, reply_markup=None, **kw):
        self.replies.append({"text": text, "markup": reply_markup})
        sent = _SentText(TEXT_MSG_ID + len(self.sent), self.chat, text)
        self.sent.append(sent)
        return sent

    async def reply_photo(self, photo, caption=None, parse_mode=None,
                          reply_markup=None, **kw):
        self.photos.append({"photo": photo, "caption": caption,
                            "markup": reply_markup})
        return SimpleNamespace(message_id=self.message_id + 1, chat=self.chat)

    async def edit_message_reply_markup(self, reply_markup=None, **kw):
        self.markup_edits.append(reply_markup)

    async def delete(self):
        self.deleted = True

    @property
    def last(self):
        return self.replies[-1] if self.replies else None


class _DeleteFailsMessage(_FakeMessage):
    async def delete(self):
        raise RuntimeError("message to delete not found")


class _PhotoFailsMessage(_FakeMessage):
    async def reply_photo(self, *a, **kw):
        raise RuntimeError("PHOTO_FAIL")


class _FakeQuery:
    def __init__(self, data: str, message: _FakeMessage | None = None):
        self.data = data
        self.answers: list = []
        self.edits: list = []
        self.markup_edits: list = []
        self.markup_stripped = False
        self.message = message or _FakeMessage()

    async def answer(self, text=None, show_alert=False, **kw):
        self.answers.append({"text": text, "show_alert": show_alert})

    async def edit_message_text(self, text, parse_mode=None, reply_markup=None, **kw):
        self.edits.append({"text": text, "markup": reply_markup})

    async def edit_message_reply_markup(self, reply_markup=None, **kw):
        self.markup_edits.append(reply_markup)
        if reply_markup is None:
            self.markup_stripped = True

    @property
    def last_edit(self):
        return self.edits[-1] if self.edits else None


class _FakeBot:
    """context.bot — records cross-message edits/deletes for the stack."""

    def __init__(self):
        self.username = BOT_USERNAME
        self.edits: list = []     # edit_message_text kwargs
        self.deletes: list = []   # (chat_id, message_id)

    async def edit_message_text(self, **kw):
        self.edits.append(kw)

    async def delete_message(self, chat_id, message_id, **kw):
        self.deletes.append((chat_id, message_id))


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(
        bot_data={"username": BOT_USERNAME},
        bot=_FakeBot(),
        args=[],
    )


def _cmd_update(chat_type: str = "private"):
    msg = _FakeMessage(chat_type, chat_id=-100 if chat_type != "private" else 1)
    return SimpleNamespace(
        message=msg,
        effective_message=msg,
        effective_chat=msg.chat,
        effective_user=SimpleNamespace(id=42),
    )


def _cb_update(data: str, message: _FakeMessage | None = None):
    return SimpleNamespace(callback_query=_FakeQuery(data, message=message))


def _style(btn):
    return (getattr(btn, "api_kwargs", None) or {}).get("style")


def _flat(markup):
    return [b for row in markup.inline_keyboard for b in row]


def _by_data(markup, data: str):
    for b in _flat(markup):
        if b.callback_data == data:
            return b
    return None


# ═════════════════════════════════════════════════════════════════
# Menu data integrity
# ═════════════════════════════════════════════════════════════════

class TestMenuData(unittest.TestCase):
    def test_enough_modules(self):
        self.assertGreaterEqual(len(HELP_MENU), 15)

    def test_unique_keys(self):
        keys = [m["key"] for m in HELP_MENU]
        self.assertEqual(len(keys), len(set(keys)))

    def test_entries_complete(self):
        for mod in HELP_MENU:
            with self.subTest(key=mod.get("key")):
                self.assertTrue(mod.get("icon"))
                self.assertTrue(mod.get("title"))
                self.assertTrue(mod.get("sections"))
                self.assertIsInstance(mod.get("notes"), list)

    def test_commands_are_commands(self):
        for mod in HELP_MENU:
            for _, cmds in mod["sections"]:
                for line in cmds:
                    with self.subTest(key=mod["key"], line=line):
                        self.assertTrue(line.startswith("/"), line)

    def test_pagination_covers_all(self):
        pages = (len(HELP_MENU) + help_mod.PAGE_SIZE - 1) // help_mod.PAGE_SIZE
        self.assertEqual(pages, help_mod._page_count())
        self.assertGreater(pages, 1)  # the design shows a ">" page arrow

    def test_logo_asset_exists(self):
        self.assertTrue(help_mod._logo_path(), "bot/assets/help_logo.png missing")


# ═════════════════════════════════════════════════════════════════
# /help in groups → DM redirect
# ═════════════════════════════════════════════════════════════════

class TestGroupRedirect(unittest.IsolatedAsyncioTestCase):
    async def test_group_gets_dm_button(self):
        upd = _cmd_update("supergroup")
        await help_mod.help_command(upd, _ctx())
        reply = upd.message.last
        self.assertIsNotNone(reply)
        self.assertIn("Help Menu", reply["text"])
        self.assertIn("DM", reply["text"])
        buttons = _flat(reply["markup"])
        self.assertEqual(len(buttons), 1)
        self.assertEqual(buttons[0].url, f"https://t.me/{BOT_USERNAME}")

    async def test_private_menu_has_no_dm_button(self):
        upd = _cmd_update("private")
        await help_mod.help_command(upd, _ctx())
        self.assertTrue(upd.message.photos)
        buttons = _flat(upd.message.photos[0]["markup"])
        self.assertFalse(any(getattr(b, "url", None) for b in buttons))


# ═════════════════════════════════════════════════════════════════
# Private stack: [text][logo photo + buttons]
# ═════════════════════════════════════════════════════════════════

class TestPrivateStack(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.upd = _cmd_update("private")
        await help_mod.help_command(self.upd, _ctx())
        self.ctx = _ctx()
        self.reply = self.upd.message.last
        self.photo = self.upd.message.photos[0] if self.upd.message.photos else None

    def test_header_text_sent(self):
        text = self.reply["text"]
        self.assertIn("Help Menu", text)
        self.assertIn(f"Page 1/{help_mod._page_count()}", text)
        self.assertIn("&amp;", text)  # HTML-safe '&' in the prefix line
        self.assertIn("!", text)

    def test_logo_photo_carries_buttons(self):
        self.assertIsNotNone(self.photo, "logo photo not sent")
        self.assertTrue(self.photo["photo"].endswith("help_logo.png"))
        self.assertIsNone(self.photo["caption"])  # text lives above, not on photo

    def test_grid_three_by_three(self):
        rows = self.photo["markup"].inline_keyboard
        grid = rows[:3]
        self.assertEqual([len(r) for r in grid], [3, 3, 3])
        for b in _flat(SimpleNamespace(inline_keyboard=grid)):
            self.assertTrue(b.callback_data.startswith("help:open:"))
            self.assertIn(":0:t", b.callback_data)  # page 0 + header id
            self.assertEqual(_style(b), "primary")

    def test_grid_labels_are_plain_titles(self):
        # No literal emoji in labels — the custom icon already shows.
        grid = _flat(SimpleNamespace(
            inline_keyboard=self.photo["markup"].inline_keyboard[:3]
        ))
        expected = [m["title"] for m in HELP_MENU[: help_mod.PAGE_SIZE]]
        self.assertEqual([b.text for b in grid], expected)
        for b in grid:
            self.assertTrue(b.text.isascii(), f"emoji leaked into {b.text!r}")

    def test_grid_matches_page_order(self):
        grid = _flat(SimpleNamespace(
            inline_keyboard=self.photo["markup"].inline_keyboard[:3]
        ))
        expected = [m["key"] for m in HELP_MENU[: help_mod.PAGE_SIZE]]
        self.assertEqual([b.callback_data.split(":")[2] for b in grid], expected)

    def test_nav_row_single_next(self):
        nav = self.photo["markup"].inline_keyboard[3]
        self.assertEqual(len(nav), 1)
        self.assertEqual(nav[0].text, ">")
        self.assertEqual(nav[0].callback_data,
                         f"help:main:1:t{TEXT_MSG_ID}")

    def test_close_and_back_rows(self):
        rows = self.photo["markup"].inline_keyboard
        close_btn, back_btn = rows[4][0], rows[5][0]
        self.assertEqual(close_btn.text, "CLOSE")
        self.assertEqual(close_btn.callback_data, f"help:close:t{TEXT_MSG_ID}")
        self.assertEqual(_style(close_btn), "danger")
        self.assertEqual(back_btn.text, "BACK")
        self.assertEqual(back_btn.callback_data, f"help:start:t{TEXT_MSG_ID}")
        self.assertEqual(_style(back_btn), "primary")

    def test_module_icons_present(self):
        first = self.photo["markup"].inline_keyboard[0][0]
        icon = (getattr(first, "api_kwargs", None) or {}).get(
            "icon_custom_emoji_id"
        )
        self.assertTrue(icon)  # branded button icon from EID


# ═════════════════════════════════════════════════════════════════
# Production stack callbacks (t<id> suffix present)
# ═════════════════════════════════════════════════════════════════

class TestStackCallbacks(unittest.IsolatedAsyncioTestCase):
    async def test_drilldown_edits_text_and_markup(self):
        ctx = _ctx()
        photo_msg = _FakeMessage(message_id=100)
        cb = _cb_update(f"help:open:moderation:0:t{TEXT_MSG_ID}",
                        message=photo_msg)
        await help_mod.help_callback(cb, ctx)

        self.assertEqual(len(ctx.bot.edits), 1)
        edit = ctx.bot.edits[0]
        self.assertEqual(edit["message_id"], TEXT_MSG_ID)
        self.assertIn("/mute @user", edit["text"])
        # buttons updated on the clicked (photo) message
        rows = cb.callback_query.markup_edits[-1].inline_keyboard
        self.assertEqual(rows[0][0].callback_data, f"help:close:t{TEXT_MSG_ID}")
        self.assertEqual(rows[1][0].callback_data,
                         f"help:main:0:t{TEXT_MSG_ID}")

    async def test_pagination_edits_both(self):
        ctx = _ctx()
        cb = _cb_update(f"help:main:1:t{TEXT_MSG_ID}",
                        message=_FakeMessage(message_id=100))
        await help_mod.help_callback(cb, ctx)
        self.assertIn(f"Page 2/{help_mod._page_count()}",
                      ctx.bot.edits[0]["text"])
        nav = ctx.bot.edits[0]["reply_markup"].inline_keyboard[3]
        self.assertEqual(nav[0].callback_data, f"help:main:0:t{TEXT_MSG_ID}")

    async def test_close_deletes_both_messages(self):
        ctx = _ctx()
        photo_msg = _FakeMessage(message_id=100)
        cb = _cb_update(f"help:close:t{TEXT_MSG_ID}", message=photo_msg)
        await help_mod.help_callback(cb, ctx)
        self.assertTrue(photo_msg.deleted)
        self.assertEqual(ctx.bot.deletes, [(photo_msg.chat.id, TEXT_MSG_ID)])

    async def test_close_skips_when_same_message(self):
        ctx = _ctx()
        photo_msg = _FakeMessage(message_id=TEXT_MSG_ID)
        cb = _cb_update(f"help:close:t{TEXT_MSG_ID}", message=photo_msg)
        await help_mod.help_callback(cb, ctx)
        self.assertTrue(photo_msg.deleted)
        self.assertEqual(ctx.bot.deletes, [])

    async def test_back_renders_start_and_removes_logo(self):
        ctx = _ctx()
        photo_msg = _FakeMessage(message_id=100)
        cb = _cb_update(f"help:start:t{TEXT_MSG_ID}", message=photo_msg)
        await help_mod.help_callback(cb, ctx)
        edit = ctx.bot.edits[0]
        self.assertIn(BOT_DESCRIPTION, edit["text"])
        self.assertIsNotNone(_by_data(edit["reply_markup"], "start:help"))
        self.assertTrue(photo_msg.deleted)


# ═════════════════════════════════════════════════════════════════
# Photo failure → buttons fall back onto the text message
# ═════════════════════════════════════════════════════════════════

class TestPhotoFallback(unittest.IsolatedAsyncioTestCase):
    async def test_buttons_attach_to_text_when_photo_fails(self):
        upd = _cmd_update("private")
        upd.message = _PhotoFailsMessage()
        upd.effective_message = upd.message
        await help_mod.help_command(upd, _ctx())
        self.assertEqual(upd.message.photos, [])
        self.assertEqual(len(upd.message.sent), 1)
        header = upd.message.sent[0]
        self.assertEqual(len(header.markup_edits), 1)
        rows = header.markup_edits[0].inline_keyboard
        self.assertEqual(rows[0][0].callback_data,
                         f"help:open:{HELP_MENU[0]['key']}:0:t{header.message_id}")


# ═════════════════════════════════════════════════════════════════
# Pagination via callbacks WITHOUT t-id (text-origin / legacy)
# ═════════════════════════════════════════════════════════════════

class TestPagination(unittest.IsolatedAsyncioTestCase):
    async def test_second_page(self):
        cb = _cb_update("help:main:1")
        await help_mod.help_callback(cb, _ctx())
        edit = cb.callback_query.last_edit
        self.assertIsNotNone(edit)
        self.assertIn(f"Page 2/{help_mod._page_count()}", edit["text"])
        rows = edit["markup"].inline_keyboard
        grid = rows[:3]  # 17 modules → 8 on page 2 → 3 + 3 + 2
        self.assertEqual([len(r) for r in grid], [3, 3, 2])
        nav = rows[3]
        self.assertEqual(len(nav), 1)
        self.assertEqual(nav[0].text, "<")
        self.assertEqual(nav[0].callback_data, "help:main:0")

    async def test_out_of_range_page_clamps(self):
        cb = _cb_update("help:main:99")
        await help_mod.help_callback(cb, _ctx())
        edit = cb.callback_query.last_edit
        self.assertIsNotNone(edit)
        self.assertIn(
            f"Page {help_mod._page_count()}/{help_mod._page_count()}",
            edit["text"],
        )

    async def test_callback_is_answered(self):
        cb = _cb_update("help:main:1")
        await help_mod.help_callback(cb, _ctx())
        self.assertEqual(len(cb.callback_query.answers), 1)


# ═════════════════════════════════════════════════════════════════
# Module drill-down pages (text-origin)
# ═════════════════════════════════════════════════════════════════

class TestModulePage(unittest.IsolatedAsyncioTestCase):
    async def test_moderation_page(self):
        cb = _cb_update("help:open:moderation:0")
        await help_mod.help_callback(cb, _ctx())
        edit = cb.callback_query.last_edit
        self.assertIsNotNone(edit)
        text = edit["text"]
        self.assertIn("Moderation", text)
        self.assertIn("Mute Commands", text)
        self.assertIn("/mute @user", text)
        self.assertIn("Duration Formats", text)
        self.assertIn("e.g. !mute", text)
        rows = edit["markup"].inline_keyboard
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][0].callback_data, "help:close")
        self.assertEqual(rows[1][0].callback_data, "help:main:0")

    async def test_back_keeps_calling_page(self):
        cb = _cb_update("help:open:fun:1")
        await help_mod.help_callback(cb, _ctx())
        rows = cb.callback_query.last_edit["markup"].inline_keyboard
        self.assertEqual(rows[1][0].callback_data, "help:main:1")

    async def test_ampersand_title_is_escaped(self):
        cb = _cb_update("help:open:gban:0")
        await help_mod.help_callback(cb, _ctx())
        self.assertIn("Gban &amp; Sudo", cb.callback_query.last_edit["text"])

    async def test_general_has_testcolors(self):
        cb = _cb_update("help:open:general:0")
        await help_mod.help_callback(cb, _ctx())
        self.assertIn("/testcolors", cb.callback_query.last_edit["text"])

    async def test_unknown_module_no_edit(self):
        cb = _cb_update("help:open:doesnotexist:0")
        await help_mod.help_callback(cb, _ctx())
        self.assertEqual(cb.callback_query.edits, [])
        self.assertEqual(len(cb.callback_query.answers), 1)


# ═════════════════════════════════════════════════════════════════
# Close / Back / unknown routes (text-origin)
# ═════════════════════════════════════════════════════════════════

class TestCloseAndBack(unittest.IsolatedAsyncioTestCase):
    async def test_close_deletes_message(self):
        msg = _FakeMessage()
        cb = _cb_update("help:close", message=msg)
        await help_mod.help_callback(cb, _ctx())
        self.assertTrue(msg.deleted)

    async def test_close_falls_back_to_stripping_buttons(self):
        msg = _DeleteFailsMessage()
        cb = _cb_update("help:close", message=msg)
        await help_mod.help_callback(cb, _ctx())
        self.assertFalse(msg.deleted)
        self.assertTrue(cb.callback_query.markup_stripped)

    async def test_back_renders_start_screen(self):
        cb = _cb_update("help:start")
        await help_mod.help_callback(cb, _ctx())
        edit = cb.callback_query.last_edit
        self.assertIsNotNone(edit)
        self.assertIn(BOT_DESCRIPTION, edit["text"])
        self.assertIsNotNone(_by_data(edit["markup"], "start:help"))

    async def test_unknown_route_alerts(self):
        cb = _cb_update("help:bogus")
        await help_mod.help_callback(cb, _ctx())
        self.assertEqual(cb.callback_query.edits, [])
        answers = cb.callback_query.answers
        self.assertEqual(len(answers), 1)
        self.assertTrue(answers[0]["show_alert"])

    async def test_foreign_callback_ignored(self):
        cb = _cb_update("tag:whatever")
        await help_mod.help_callback(cb, _ctx())
        self.assertEqual(cb.callback_query.answers, [])


# ═════════════════════════════════════════════════════════════════
# /start ⇄ /help loop
# ═════════════════════════════════════════════════════════════════

class TestStartHelpLoop(unittest.IsolatedAsyncioTestCase):
    async def test_start_help_button_swaps_to_menu_stack(self):
        start_msg = _FakeMessage()
        cb = _cb_update("start:help", message=start_msg)
        await start_mod.start_callback(cb, _ctx())
        # start screen replaced by [menu text][logo + buttons]
        self.assertTrue(start_msg.deleted)
        self.assertEqual(len(start_msg.replies), 1)
        self.assertIn("Help Menu", start_msg.replies[0]["text"])
        self.assertEqual(len(start_msg.photos), 1)
        markup = start_msg.photos[0]["markup"]
        self.assertIsNotNone(_by_data(markup, f"help:open:{HELP_MENU[0]['key']}:0:t{TEXT_MSG_ID}"))

    async def test_start_dashboard_still_coming_soon(self):
        cb = _cb_update("start:dashboard")
        await start_mod.start_callback(cb, _ctx())
        self.assertEqual(cb.callback_query.edits, [])
        self.assertEqual(cb.callback_query.answers[0]["text"], "Coming soon!")


if __name__ == "__main__":
    unittest.main(verbosity=2)
