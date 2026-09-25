"""Tests for the single-message /help menu (bot/modules/help.py).

Run from the Pi/Pi root:

    python tests/test_help_menu.py
    python -m unittest discover -s tests

Environment isolation (BEFORE any bot import):
    * BOT_TOKEN is forced — importing `bot` pulls bot.config, which
      exits without one.
    * LOCALAPPDATA points at a temp dir so any DB touch stays isolated.
No network: handlers run against fakes that record replies/edits.

Production layout: ONE plain text message — menu text with the buttons
attached (no photo/media). Callbacks edit that same message in place
(``edit_message_text``); menus sent by older versions as photo captions
keep working through the caption-edit compatibility branch.
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


# ── Fakes ─────────────────────────────────────────────────────────

class _FakeMessage:
    def __init__(self, chat_type: str = "private", chat_id: int = 1,
                 message_id: int = 100, is_photo: bool = False):
        self.chat = SimpleNamespace(id=chat_id, type=chat_type, title="Test Chat")
        self.message_id = message_id
        self.message_thread_id = None
        #: non-empty when this message carries media — legacy menus sent
        #: as photo captions take the caption-edit branch in _safe_edit.
        self.photo = [SimpleNamespace()] if is_photo else []
        self.replies: list = []   # {"text", "markup"} dicts
        self.photos: list = []    # {"photo", "caption", "markup"} dicts — must stay empty
        self.markup_edits: list = []
        self.deleted = False

    async def reply_text(self, text, parse_mode=None, reply_markup=None, **kw):
        self.replies.append({"text": text, "markup": reply_markup})
        return SimpleNamespace(message_id=self.message_id + 1, chat=self.chat)

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


class TimedOut(Exception):
    """Name matters — help.py checks type(e).__name__ == 'TimedOut'."""


class _TextTimeoutMessage(_FakeMessage):
    async def reply_text(self, *a, **kw):
        raise TimedOut("request timed out")


class _BrandFailsMessage(_FakeMessage):
    """Brand text (with <tg-emoji>) rejected → plain fallback must send."""

    async def reply_text(self, text, *a, **kw):
        if "<tg-emoji" in text:
            raise RuntimeError("CUSTOM_EMOJI_FAIL")
        return await super().reply_text(text, *a, **kw)


def _photo_msg(chat_type: str = "private", chat_id: int = 1) -> _FakeMessage:
    """A LEGACY menu message (photo + caption) — old messages still on screen."""
    return _FakeMessage(chat_type, chat_id, is_photo=True)


class _FakeQuery:
    def __init__(self, data: str, message: _FakeMessage | None = None):
        self.data = data
        self.answers: list = []
        self.edits: list = []            # edit_message_text kwargs
        self.caption_edits: list = []    # edit_message_caption kwargs
        self.markup_stripped = False
        self.message = message if message is not None else _FakeMessage()

    async def answer(self, text=None, show_alert=False, **kw):
        self.answers.append({"text": text, "show_alert": show_alert})

    async def edit_message_text(self, text, parse_mode=None, reply_markup=None, **kw):
        self.edits.append({"text": text, "markup": reply_markup})

    async def edit_message_caption(self, caption=None, parse_mode=None,
                                   reply_markup=None, **kw):
        self.caption_edits.append({"caption": caption, "markup": reply_markup})
        if caption is None:
            self.markup_stripped = True

    async def edit_message_reply_markup(self, reply_markup=None, **kw):
        if reply_markup is None:
            self.markup_stripped = True

    @property
    def last_caption(self):
        return self.caption_edits[-1] if self.caption_edits else None

    @property
    def last_edit(self):
        return self.edits[-1] if self.edits else None


class _FakeBot:
    """context.bot — the single-message design must never need it."""

    def __init__(self):
        self.username = BOT_USERNAME
        self.edits: list = []
        self.deletes: list = []

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


def _cmd_update(chat_type: str = "private", message: _FakeMessage | None = None):
    msg = message or _FakeMessage()
    msg.chat.type = chat_type
    msg.chat.id = -100 if chat_type != "private" else 1
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
# Menu data integrity + page limits
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

    def test_main_message_fits_page_limit(self):
        for page in range(help_mod._page_count()):
            self.assertLessEqual(
                help_mod._visible_len(help_mod._build_main_message(page)),
                help_mod.CAPTION_LIMIT,
            )

    def test_every_module_page_fits_page_limit(self):
        for mod in HELP_MENU:
            total = help_mod._module_page_count(mod)
            for sub in range(total):
                with self.subTest(key=mod["key"], sub=sub):
                    text = help_mod._build_module_message(mod, sub)
                    self.assertLessEqual(
                        help_mod._visible_len(text), help_mod.CAPTION_LIMIT
                    )

    def test_start_screen_fits_page_limit(self):
        text = help_mod._build_start_message(BOT_USERNAME)
        self.assertLessEqual(help_mod._visible_len(text), help_mod.CAPTION_LIMIT)


# ═════════════════════════════════════════════════════════════════
# Module content pagination (only big modules span pages)
# ═════════════════════════════════════════════════════════════════

class TestModulePagination(unittest.TestCase):
    def test_moderation_spans_two_pages(self):
        mod = help_mod._module("moderation")
        self.assertEqual(help_mod._module_page_count(mod), 2)

    def test_pages_split_all_sections(self):
        mod = help_mod._module("moderation")
        pages = help_mod._module_chunks(mod)
        flat = [header for page in pages for header, _ in page if header]
        all_headers = [h for h, _ in mod["sections"] if h]
        self.assertEqual(sorted(flat), sorted(all_headers))

    def test_small_modules_single_page(self):
        for key in ("general", "profile", "bind"):
            mod = help_mod._module(key)
            self.assertEqual(help_mod._module_page_count(mod), 1, key)

    def test_second_page_indicator(self):
        mod = help_mod._module("moderation")
        self.assertIn("Page 2/2", help_mod._build_module_message(mod, 1))


# ═════════════════════════════════════════════════════════════════
# /help in groups → DM redirect (still one message, no media)
# ═════════════════════════════════════════════════════════════════

class TestGroupRedirect(unittest.IsolatedAsyncioTestCase):
    async def test_group_gets_single_text_message(self):
        upd = _cmd_update("supergroup")
        await help_mod.help_command(upd, _ctx())
        self.assertEqual(upd.message.photos, [])  # never a photo
        self.assertEqual(len(upd.message.replies), 1)
        reply = upd.message.last
        self.assertIn("Help Menu", reply["text"])
        self.assertIn("DM", reply["text"])
        buttons = _flat(reply["markup"])
        self.assertEqual(len(buttons), 1)
        self.assertEqual(buttons[0].url, f"https://t.me/{BOT_USERNAME}")

    async def test_brand_emoji_failure_falls_back_to_plain(self):
        upd = _cmd_update("supergroup", message=_BrandFailsMessage())
        await help_mod.help_command(upd, _ctx())
        self.assertEqual(len(upd.message.replies), 1)  # exactly one message sent
        self.assertNotIn("<tg-emoji", upd.message.last["text"])
        buttons = _flat(upd.message.last["markup"])
        self.assertEqual(buttons[0].url, f"https://t.me/{BOT_USERNAME}")

    async def test_private_menu_has_no_dm_button(self):
        upd = _cmd_update("private")
        await help_mod.help_command(upd, _ctx())
        self.assertEqual(len(upd.message.replies), 1)
        buttons = _flat(upd.message.last["markup"])
        self.assertFalse(any(getattr(b, "url", None) for b in buttons))


# ═════════════════════════════════════════════════════════════════
# Private /help → ONE text message (menu + buttons)
# ═════════════════════════════════════════════════════════════════

class TestPrivateMenu(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.upd = _cmd_update("private")
        await help_mod.help_command(self.upd, _ctx())
        self.msg = self.upd.message.last

    def test_single_message_only(self):
        self.assertEqual(len(self.upd.message.replies), 1)
        self.assertEqual(self.upd.message.photos, [])  # no media ever

    def test_menu_text_content(self):
        self.assertIsNotNone(self.msg)
        text = self.msg["text"]
        self.assertIn("Help Menu", text)
        self.assertIn(f"Page 1/{help_mod._page_count()}", text)
        self.assertIn("&amp;", text)  # HTML-safe '&' in the prefix line
        self.assertLessEqual(
            help_mod._visible_len(text), help_mod.CAPTION_LIMIT
        )

    def test_grid_three_by_three(self):
        rows = self.msg["markup"].inline_keyboard
        grid = rows[:3]
        self.assertEqual([len(r) for r in grid], [3, 3, 3])
        for b in _flat(SimpleNamespace(inline_keyboard=grid)):
            self.assertTrue(b.callback_data.startswith("help:open:"))
            self.assertTrue(b.callback_data.endswith(":0:0"))  # page 0, sub 0
            self.assertEqual(_style(b), "primary")

    def test_grid_labels_are_plain_titles(self):
        # No literal emoji in labels — the custom icon already shows.
        grid = _flat(SimpleNamespace(
            inline_keyboard=self.msg["markup"].inline_keyboard[:3]
        ))
        expected = [m["title"] for m in HELP_MENU[: help_mod.PAGE_SIZE]]
        self.assertEqual([b.text for b in grid], expected)
        for b in grid:
            self.assertTrue(b.text.isascii(), f"emoji leaked into {b.text!r}")

    def test_grid_matches_page_order(self):
        grid = _flat(SimpleNamespace(
            inline_keyboard=self.msg["markup"].inline_keyboard[:3]
        ))
        expected = [m["key"] for m in HELP_MENU[: help_mod.PAGE_SIZE]]
        self.assertEqual([b.callback_data.split(":")[2] for b in grid], expected)

    def test_nav_row_single_next(self):
        nav = self.msg["markup"].inline_keyboard[3]
        self.assertEqual(len(nav), 1)
        self.assertEqual(nav[0].text, ">")
        self.assertEqual(nav[0].callback_data, "help:main:1")

    def test_close_and_back_rows(self):
        rows = self.msg["markup"].inline_keyboard
        close_btn, back_btn = rows[4][0], rows[5][0]
        self.assertEqual(close_btn.text, "CLOSE")
        self.assertEqual(close_btn.callback_data, "help:close")
        self.assertEqual(_style(close_btn), "danger")
        self.assertEqual(back_btn.text, "BACK")
        self.assertEqual(back_btn.callback_data, "help:start")
        self.assertEqual(_style(back_btn), "primary")

    def test_module_icons_present(self):
        first = self.msg["markup"].inline_keyboard[0][0]
        icon = (getattr(first, "api_kwargs", None) or {}).get(
            "icon_custom_emoji_id"
        )
        self.assertTrue(icon)  # branded button icon from EID


# ═════════════════════════════════════════════════════════════════
# Callbacks edit the SAME message in place (text messages)
# ═════════════════════════════════════════════════════════════════

class TestTextEdits(unittest.IsolatedAsyncioTestCase):
    async def test_drilldown_edits_text(self):
        ctx = _ctx()
        cb = _cb_update("help:open:moderation:0:0")  # plain text message
        await help_mod.help_callback(cb, ctx)

        self.assertEqual(len(cb.callback_query.edits), 1)
        self.assertEqual(cb.callback_query.caption_edits, [])  # not a photo
        edit = cb.callback_query.last_edit
        self.assertIn("/mute @user", edit["text"])
        rows = edit["markup"].inline_keyboard
        self.assertEqual(rows[0][0].callback_data, "help:open:moderation:0:1")
        self.assertEqual(rows[-2][0].callback_data, "help:close")
        self.assertEqual(rows[-1][0].callback_data, "help:main:0")
        self.assertEqual(ctx.bot.edits, [])  # no cross-message ops needed

    async def test_module_subpage_nav(self):
        cb = _cb_update("help:open:moderation:0:1")
        await help_mod.help_callback(cb, _ctx())
        edit = cb.callback_query.last_edit
        self.assertIn("Page 2/2", edit["text"])
        nav = edit["markup"].inline_keyboard[0]
        self.assertEqual(nav[0].callback_data, "help:open:moderation:0:0")

    async def test_module_subpages_cover_all_sections(self):
        mod = help_mod._module("moderation")
        seen = []
        for sub in range(help_mod._module_page_count(mod)):
            cb = _cb_update(f"help:open:moderation:0:{sub}")
            await help_mod.help_callback(cb, _ctx())
            seen.append(cb.callback_query.last_edit["text"])
        for header, _ in mod["sections"]:
            if header:
                self.assertIn(header, " ".join(seen))

    async def test_main_pagination_edits_text(self):
        cb = _cb_update("help:main:1")
        await help_mod.help_callback(cb, _ctx())
        edit = cb.callback_query.last_edit
        self.assertIn(f"Page 2/{help_mod._page_count()}", edit["text"])
        nav = edit["markup"].inline_keyboard[3]
        self.assertEqual(nav[0].callback_data, "help:main:0")

    async def test_close_deletes_the_single_message(self):
        ctx = _ctx()
        msg = _FakeMessage()
        cb = _cb_update("help:close", message=msg)
        await help_mod.help_callback(cb, ctx)
        self.assertTrue(msg.deleted)
        self.assertEqual(ctx.bot.deletes, [])  # nothing else exists

    async def test_back_edits_start_screen_in_place(self):
        ctx = _ctx()
        msg = _FakeMessage()
        cb = _cb_update("help:start", message=msg)
        await help_mod.help_callback(cb, ctx)
        edit = cb.callback_query.last_edit
        self.assertIn(BOT_DESCRIPTION, edit["text"])
        self.assertIsNotNone(_by_data(edit["markup"], "start:help"))
        self.assertFalse(msg.deleted)  # still ONE message

    async def test_callback_is_answered(self):
        cb = _cb_update("help:main:1")
        await help_mod.help_callback(cb, _ctx())
        self.assertEqual(len(cb.callback_query.answers), 1)


# ═════════════════════════════════════════════════════════════════
# Legacy photo menus (sent by older versions) keep working
# ═════════════════════════════════════════════════════════════════

class TestLegacyPhotoMessages(unittest.IsolatedAsyncioTestCase):
    async def test_drilldown_edits_caption(self):
        ctx = _ctx()
        cb = _cb_update("help:open:moderation:0:0", message=_photo_msg())
        await help_mod.help_callback(cb, ctx)

        self.assertEqual(len(cb.callback_query.caption_edits), 1)
        self.assertEqual(cb.callback_query.edits, [])  # photo → caption edit
        edit = cb.callback_query.last_caption
        self.assertIn("/mute @user", edit["caption"])
        rows = edit["markup"].inline_keyboard
        self.assertEqual(rows[0][0].callback_data, "help:open:moderation:0:1")
        self.assertEqual(rows[-2][0].callback_data, "help:close")
        self.assertEqual(rows[-1][0].callback_data, "help:main:0")

    async def test_close_deletes_legacy_photo_message(self):
        ctx = _ctx()
        msg = _photo_msg()
        cb = _cb_update("help:close", message=msg)
        await help_mod.help_callback(cb, ctx)
        self.assertTrue(msg.deleted)


# ═════════════════════════════════════════════════════════════════
# Send failure handling (brand → plain fallback; no retry on timeout)
# ═════════════════════════════════════════════════════════════════

class TestSendFailures(unittest.IsolatedAsyncioTestCase):
    async def test_brand_failure_sends_single_plain_message(self):
        upd = _cmd_update("private", message=_BrandFailsMessage())
        await help_mod.help_command(upd, _ctx())
        self.assertEqual(upd.message.photos, [])
        self.assertEqual(len(upd.message.replies), 1)  # still ONE message
        self.assertNotIn("<tg-emoji", upd.message.last["text"])
        self.assertIn("Help Menu", upd.message.last["text"])
        self.assertIsNotNone(
            _by_data(
                upd.message.last["markup"],
                f"help:open:{HELP_MENU[0]['key']}:0:0",
            )
        )

    async def test_timeout_never_retries(self):
        upd = _cmd_update("private", message=_TextTimeoutMessage())
        await help_mod.help_command(upd, _ctx())
        self.assertEqual(upd.message.photos, [])
        self.assertEqual(upd.message.replies, [])  # no duplicate send

    async def test_foreign_message_callbacks_edit_text(self):
        cb = _cb_update("help:main:1", message=_FakeMessage())  # photo=[]
        await help_mod.help_callback(cb, _ctx())
        self.assertEqual(cb.callback_query.caption_edits, [])
        self.assertIn(f"Page 2/{help_mod._page_count()}",
                      cb.callback_query.last_edit["text"])


# ═════════════════════════════════════════════════════════════════
# Close fallback / unknown routes
# ═════════════════════════════════════════════════════════════════

class TestCloseAndUnknown(unittest.IsolatedAsyncioTestCase):
    async def test_close_falls_back_to_stripping_buttons(self):
        msg = _DeleteFailsMessage()
        cb = _cb_update("help:close", message=msg)
        await help_mod.help_callback(cb, _ctx())
        self.assertFalse(msg.deleted)
        self.assertTrue(cb.callback_query.markup_stripped)

    async def test_unknown_module_alerts(self):
        cb = _cb_update("help:open:doesnotexist:0:0")
        await help_mod.help_callback(cb, _ctx())
        self.assertEqual(cb.callback_query.edits, [])
        self.assertEqual(cb.callback_query.caption_edits, [])
        answers = cb.callback_query.answers
        self.assertEqual(len(answers), 1)
        self.assertTrue(answers[0]["show_alert"])

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
    async def test_start_help_button_swaps_to_menu_message(self):
        start_msg = _FakeMessage()
        cb = _cb_update("start:help", message=start_msg)
        await start_mod.start_callback(cb, _ctx())
        # start screen replaced by ONE text menu message
        self.assertTrue(start_msg.deleted)
        self.assertEqual(start_msg.photos, [])
        self.assertEqual(len(start_msg.replies), 1)
        text = start_msg.last["text"]
        self.assertIn("Help Menu", text)
        markup = start_msg.last["markup"]
        self.assertIsNotNone(
            _by_data(markup, f"help:open:{HELP_MENU[0]['key']}:0:0")
        )

    async def test_start_dashboard_still_coming_soon(self):
        cb = _cb_update("start:dashboard")
        await start_mod.start_callback(cb, _ctx())
        self.assertEqual(cb.callback_query.edits, [])
        self.assertEqual(cb.callback_query.answers[0]["text"], "Coming soon!")


if __name__ == "__main__":
    unittest.main(verbosity=2)
