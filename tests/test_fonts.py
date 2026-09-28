"""Font module tests (bot/modules/fonts.py - the boa port).

Run from the Pi/Pi root:

    python tests/test_fonts.py
    python -m unittest discover -s tests

Covers: /font payload + quote, the usage card, blue style buttons /
green Next-Back navigation, both page flips, style transforms via the
reply-to-command mechanism, unknown-style safety, and loader wiring.

Environment isolation (BEFORE any bot import):
    * BOT_TOKEN is forced - importing `bot` pulls bot.config, which
      exits without one.
    * LOCALAPPDATA points at a temp dir (unittest -> mongomock).
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

# ── Environment isolation - must precede bot imports ──────────────
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["BOT_TOKEN"] = "1:TEST-TOKEN-FOR-UNIT-TESTS"
_TEST_DIR = tempfile.mkdtemp(prefix="pi_fonts_test_")
os.environ["LOCALAPPDATA"] = _TEST_DIR
atexit.register(shutil.rmtree, _TEST_DIR, ignore_errors=True)

# ── Imports (after env) ───────────────────────────────────────────
from aiofakes import call, command_filters, make_callback, make_message  # noqa: E402
from bot import pipeline  # noqa: E402
from bot.loader import load_modules  # noqa: E402
from bot.modules import fonts as fm  # noqa: E402

CHAT_ID = -100999001


def _cmd(text="/font hello world", chat_type="supergroup"):
    return make_message(text, chat_id=CHAT_ID, chat_type=chat_type,
                        user_id=42, first_name="Lewin")


def _payload(text="hello world", src_text="/font hello world", with_markup=True):
    """The bot's sent message (quotes the command) - style target."""
    kw = {}
    if with_markup:
        kw["reply_markup"] = object()  # marker: preserved across edits
    msg = make_message(text, chat_id=CHAT_ID, chat_type="supergroup",
                       user_id=1, **kw)
    if src_text is not None:
        msg.reply_to_message = make_message(src_text, chat_id=CHAT_ID,
                                            chat_type="supergroup", user_id=42)
    else:
        msg.reply_to_message = None
    return msg


# ── /font command ─────────────────────────────────────────────────
class TestFontCommand(unittest.IsolatedAsyncioTestCase):
    async def test_payload_quoted_even_in_private(self):
        """quote=True is the whole mechanism - private chats would
        otherwise answer() without a reply_to and break style buttons."""
        msg = _cmd("/font hello world", chat_type="private")
        await call(fm.font_command, msg)
        kind, text, kw = msg.last
        self.assertEqual(kind, "reply")           # quote=True forced reply
        self.assertEqual(text, "hello world")     # literal payload, no card
        self.assertNotIn("parse_mode", kw)        # plain text, not HTML
        rows = kw["reply_markup"].inline_keyboard
        self.assertEqual(len(rows), 8)            # 7 style rows + Next
        for row in rows[:-1]:
            self.assertEqual(len(row), 3)
            for btn in row:
                self.assertEqual(btn.style, "primary")   # blue style buttons
                self.assertTrue(btn.callback_data.startswith("style+"))
        nav = rows[-1][0]
        self.assertEqual(len(rows[-1]), 1)
        self.assertEqual(nav.style, "success")    # green navigation
        self.assertEqual(nav.callback_data, "nxt")
        self.assertEqual(nav.text, "\u0274\u1d07x\u1d1b \u27bb")  # ɴᴇxᴛ ➻

    async def test_alias_and_prefixes(self):
        for text in (".fonts styled text", "!font hi", "#fonts abc"):
            msg = _cmd(text)
            await call(fm.font_command, msg)
            self.assertEqual(msg.last[1], text.split(None, 1)[1], text)

    async def test_missing_text_shows_usage_without_buttons(self):
        msg = _cmd("/font")
        await call(fm.font_command, msg)
        kind, text, kw = msg.last
        self.assertEqual(kind, "reply")
        self.assertIn("Usage", text)
        self.assertNotIn("reply_markup", kw)


# ── style+ callbacks ──────────────────────────────────────────────
class TestStyleCallback(unittest.IsolatedAsyncioTestCase):
    async def test_typewriter_rewrites_payload(self):
        payload = _payload("hello world")
        cb = make_callback("style+typewriter", message=payload)
        await call(fm.font_style_callback, cb)
        self.assertTrue(cb.answers, "callback must be answered")
        kind, text, kw = payload.last
        self.assertEqual(kind, "edit_text")
        self.assertEqual(text, fm.Fonts.typewriter("hello world"))
        self.assertIs(kw.get("reply_markup"), payload.reply_markup)
        self.assertEqual(kw["reply_markup"], payload.reply_markup)

    async def test_every_registered_style_transforms(self):
        for name, transform in fm.STYLES.items():
            # the source is always the quoted COMMAND text (reply_to),
            # not the payload text on screen
            payload = _payload("whatever", src_text="/font Abc 123")
            cb = make_callback(f"style+{name}", message=payload)
            await call(fm.font_style_callback, cb)
            kind, text, _ = payload.last
            self.assertEqual(kind, "edit_text", name)
            self.assertEqual(text, transform("Abc 123"), name)

    async def test_unknown_style_does_not_edit(self):
        payload = _payload()
        cb = make_callback("style+bogus_font", message=payload)
        await call(fm.font_style_callback, cb)
        self.assertTrue(cb.answers)
        self.assertEqual(payload.calls, [])

    async def test_missing_reply_does_not_crash(self):
        payload = _payload(src_text=None)
        cb = make_callback("style+tiny", message=payload)
        await call(fm.font_style_callback, cb)
        self.assertEqual(payload.calls, [])

    async def test_command_without_text_in_reply_is_silent(self):
        payload = _payload(text="orphan", src_text="/font")  # no payload text
        cb = make_callback("style+serif", message=payload)
        await call(fm.font_style_callback, cb)
        self.assertEqual(payload.calls, [])


# ── nxt / nxt+0 page callbacks ────────────────────────────────────
class TestPageCallback(unittest.IsolatedAsyncioTestCase):
    async def test_nxt_flips_to_page2(self):
        payload = _payload()
        cb = make_callback("nxt", message=payload)
        await call(fm.font_page_callback, cb)
        self.assertTrue(cb.answers)
        kind, markup, _ = payload.last
        self.assertEqual(kind, "edit_reply_markup")
        rows = markup.inline_keyboard
        self.assertEqual(len(rows), 7)            # 6 style rows + Back
        for row in rows[:-1]:
            for btn in row:
                self.assertEqual(btn.style, "primary")
        nav = rows[-1][0]
        self.assertEqual(nav.style, "success")
        self.assertEqual(nav.callback_data, "nxt+0")
        self.assertEqual(nav.text, "\u0299\u1d00\u1d04\u1d0b")  # ʙᴀᴄᴋ

    async def test_back_flips_to_page1(self):
        payload = _payload()
        cb = make_callback("nxt+0", message=payload)
        await call(fm.font_page_callback, cb)
        rows = payload.last[1].inline_keyboard
        self.assertEqual(len(rows), 8)
        nav = rows[-1][0]
        self.assertEqual(nav.callback_data, "nxt")
        self.assertEqual(nav.style, "success")


# ── Registry, wiring, help ────────────────────────────────────────
class TestWiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.loaded = load_modules()
        cls.entries = pipeline.snapshot()

    def test_loader_registered_fonts(self):
        entries = [e for e in self.entries
                   if e.key.startswith("bot.modules.fonts.")]
        self.assertEqual(len(entries), 3)  # command + 2 callbacks
        events = sorted(e.event for e in entries)
        self.assertEqual(events,
                         ["callback_query", "callback_query", "message"])

    def test_command_filter_covers_font_and_fonts(self):
        entry = next(e for e in self.entries
                     if e.key == "bot.modules.fonts.font_command")
        filters = command_filters(entry.flt)
        self.assertEqual(len(filters), 1)
        self.assertEqual(filters[0].commands, frozenset({"font", "fonts"}))

    def test_style_registry_is_complete(self):
        self.assertEqual(len(fm.STYLES), 39)
        self.assertTrue(all(callable(v) for v in fm.STYLES.values()))
        p1 = [p for row in fm._PAGE1_STYLES for p in row]
        p2 = [p for row in fm._PAGE2_STYLES for p in row]
        self.assertEqual(len(p1), 21)
        self.assertEqual(len(p2), 18)
        # every button style is registered (and vice versa)
        button_styles = {cb.split("+", 1)[1] for _, cb in p1 + p2}
        self.assertEqual(button_styles, set(fm.STYLES))

    def test_callback_data_fits_telegram_limit(self):
        flat = [p for row in fm._PAGE1_STYLES + fm._PAGE2_STYLES
                for p in row]
        for _, data in flat:
            self.assertLessEqual(len(data.encode("utf-8")), 64, data)

    def test_help_documents_font(self):
        from bot.constants import HELP_MENU
        fun = next(m for m in HELP_MENU if m["key"] == "fun")
        lines = [line for _, cmds in fun["sections"] for line in cmds]
        self.assertTrue(any(l.startswith("/font") for l in lines))


if __name__ == "__main__":
    unittest.main(verbosity=2)
