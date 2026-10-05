"""Rich Message (Bot API 10.1+) rendering for /help — bot/modules/help.py.

Run from the Pi/Pi root:

    python -m pytest tests/test_help_rich.py

Environment isolation (BEFORE any bot import):
    * BOT_TOKEN is forced — importing `bot` pulls bot.config, which
      exits without one.
    * LOCALAPPDATA points at a temp dir so any DB touch stays isolated.

What is under test
------------------
1. ``bot/rich.py`` — the raw ``sendRichMessage`` / ``editMessageText``
   declarations aiogram 3.21 does not ship, the block builders and the
   HTML→RichText converter.
2. The menu's rich renderers (``_rich_main_blocks`` /
   ``_rich_module_blocks``) produce valid, paginated blocks.
3. The send/edit path prefers rich and degrades to the legacy HTML menu
   (with its FULL keyboard) on any API-level rejection — rich is a strict
   upgrade, never a single point of failure.
"""

from __future__ import annotations

import atexit
import asyncio
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
_TEST_DIR = tempfile.mkdtemp(prefix="pi_help_rich_")
os.environ["LOCALAPPDATA"] = _TEST_DIR
atexit.register(shutil.rmtree, _TEST_DIR, ignore_errors=True)

# ── Imports (after env) ───────────────────────────────────────────
from bot.constants import HELP_MENU  # noqa: E402
from bot import rich as R  # noqa: E402
from bot.modules import help as help_mod  # noqa: E402

BOT_USERNAME = "PiModulerBot"


# ── Helpers ───────────────────────────────────────────────────────

def run(coro):
    return asyncio.run(coro)


def plain(node) -> str:
    """Flatten RichText (str | list | dict) to a comparable string."""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(plain(x) for x in node)
    if isinstance(node, dict):
        if "text" in node:
            return plain(node["text"])
        return node.get("alternative_text", "")
    return str(node)


def button_blocks(blocks):
    return [b for b in blocks if b.get("type") == "buttons"]


def table_blocks(blocks):
    return [b for b in blocks if b.get("type") == "table"]


def all_callback_data(blocks):
    return [
        btn["callback_data"]
        for blk in button_blocks(blocks)
        for btn in blk["buttons"]
        if "callback_data" in btn
    ]


def all_rich_buttons(method):
    """Every RichMessageButton inside a captured send/edit method."""
    payload = method.model_dump(warnings=False)["rich_message"]
    blocks = payload["blocks"] if isinstance(payload, dict) else payload["blocks"]
    return [btn for blk in button_blocks(blocks) for btn in blk["buttons"]]


# ── Fakes ─────────────────────────────────────────────────────────

class TimedOut(Exception):
    """Name matters — help.py checks type(e).__name__ == 'TimedOut'."""


class _RichBot:
    """Accepts raw TelegramMethod instances the way ``Bot.__call__`` does."""

    def __init__(self):
        self.calls = []   # methods that succeeded
        self.seen = []    # every method attempted, success or not
        self.attempts = 0
        self.fail_with = None  # raise this on every call
        self.fail_first = None  # raise this only on attempt #1 (style retry)

    async def __call__(self, method):
        self.attempts += 1
        self.seen.append(method)
        if self.fail_first is not None and self.attempts == 1:
            raise self.fail_first
        if self.fail_with is not None:
            raise self.fail_with
        self.calls.append(method)
        return SimpleNamespace(message_id=1, chat=SimpleNamespace(id=1))

    @property
    def api_methods(self):
        return [m.__api_method__ for m in self.calls]


class _Msg:
    """Minimal Message duck-type for send/edit paths."""

    def __init__(self, chat_id=1, chat_type="private", message_id=100,
                 is_photo=False):
        self.chat = SimpleNamespace(id=chat_id, type=chat_type)
        self.message_id = message_id
        self.photo = [SimpleNamespace()] if is_photo else []
        self.replies = []
        self.edits = []

    async def answer(self, text, **kw):
        self.replies.append({"text": text, "markup": kw.get("reply_markup")})
        return self

    async def reply(self, text, **kw):
        self.replies.append({"text": text, "markup": kw.get("reply_markup")})
        return self

    async def edit_text(self, text, **kw):
        self.edits.append({"text": text, "markup": kw.get("reply_markup")})
        return self

    async def edit_caption(self, caption=None, **kw):
        self.edits.append({"caption": caption, "markup": kw.get("reply_markup")})
        return self

    async def edit_reply_markup(self, reply_markup=None, **kw):
        return self

    async def delete(self):
        return None


class _Query:
    def __init__(self, data, message=None):
        self.data = data
        self.from_user = SimpleNamespace(id=42, is_bot=False, first_name="T",
                                         username="tester")
        self.message = message if message is not None else _Msg()
        self.answers = []

    async def answer(self, text=None, show_alert=False, **kw):
        self.answers.append({"text": text, "show_alert": show_alert})

    @property
    def edits(self):
        return self.message.edits

    @property
    def replies(self):
        return self.message.replies


def _capture_reply_text():
    """Swap help_mod.reply_text for a recorder; returns (list, restore)."""
    sent = []

    async def fake(target, text, **kw):
        sent.append({"text": text, "markup": kw.get("reply_markup")})
        return None

    original = help_mod.reply_text
    help_mod.reply_text = fake

    def restore():
        help_mod.reply_text = original

    return sent, restore


# ═════════════════════════════════════════════════════════════════
# bot/rich.py — raw method declarations
# ═════════════════════════════════════════════════════════════════

class TestRawMethods(unittest.TestCase):
    def test_api_method_names(self):
        self.assertEqual(R.SendRichMessage.__api_method__, "sendRichMessage")
        self.assertEqual(R.EditRichMessage.__api_method__, "editMessageText")

    def test_send_returns_message(self):
        from aiogram.types import Message
        self.assertIs(R.SendRichMessage.__returning__, Message)

    def test_send_model_dump_keeps_rich_message(self):
        m = R.SendRichMessage(
            chat_id=-100,
            rich_message={"blocks": [R.divider()], "skip_entity_detection": True},
        )
        dumped = m.model_dump(warnings=False)
        self.assertIn("rich_message", dumped)
        self.assertIsInstance(dumped["rich_message"], dict)
        self.assertEqual(dumped["chat_id"], -100)
        # aiogram has no rich method of its own — ours must be the only one
        import aiogram.methods as am
        self.assertFalse(hasattr(am, "SendRichMessage"))

    def test_edit_model_dump_keeps_message_id(self):
        e = R.EditRichMessage(chat_id=1, message_id=7,
                              rich_message={"blocks": [R.divider()]})
        dumped = e.model_dump(warnings=False)
        self.assertEqual(dumped["message_id"], 7)
        self.assertIn("rich_message", dumped)

    def test_optional_fields_are_dropped_by_prepare_value(self):
        from aiogram.client.session.base import BaseSession

        class _Session(BaseSession):
            async def close(self):
                pass

            async def stream_content(self, *a, **k):
                pass

            async def make_request(self, bot, method, timeout=None):
                return None

        m = R.SendRichMessage(chat_id=1, rich_message={"blocks": [R.divider()]})
        prepared = _Session().prepare_value(
            m.model_dump(warnings=False), None, {}
        )
        self.assertIn("rich_message", prepared)
        self.assertNotIn("reply_markup", prepared)  # None is omitted


# ═════════════════════════════════════════════════════════════════
# bot/rich.py — HTML → RichText
# ═════════════════════════════════════════════════════════════════

class TestHtmlToRich(unittest.TestCase):
    def test_plain_text_is_returned_as_is(self):
        self.assertEqual(R.html_to_rich("no tags here"), "no tags here")

    def test_entities_are_unescaped(self):
        self.assertEqual(R.html_to_rich("A &amp; B &lt;C&gt;"), "A & B <C>")

    def test_bold_span_only_covers_its_own_text(self):
        out = R.html_to_rich("<b>Anti-flood:</b> 5 messages")
        self.assertEqual(out[0], {"type": "bold", "text": "Anti-flood:"})
        self.assertEqual(out[1], " 5 messages")

    def test_nested_tags(self):
        out = R.html_to_rich("<i>a</i> plain <b><code>c</code></b>")
        self.assertEqual(out[0], {"type": "italic", "text": "a"})
        self.assertEqual(out[1], " plain ")
        self.assertEqual(out[2]["type"], "bold")
        self.assertEqual(out[2]["text"], {"type": "code", "text": "c"})

    def test_link_becomes_url_node(self):
        out = R.html_to_rich('see <a href="https://x.io">this</a> now')
        self.assertEqual(out[1],
                         {"type": "url", "text": "this", "url": "https://x.io"})

    def test_custom_emoji_node(self):
        out = R.html_to_rich(
            '<tg-emoji emoji-id="5368324170671202286">🔥</tg-emoji> Hi'
        )
        self.assertEqual(out[0]["type"], "custom_emoji")
        self.assertEqual(out[0]["custom_emoji_id"], "5368324170671202286")
        self.assertEqual(out[1], " Hi")

    def test_unknown_tag_is_unwrapped(self):
        self.assertEqual(R.html_to_rich("<mark>x</mark> y"), ["x", " y"])

    def test_strip_html(self):
        self.assertEqual(R.strip_html("<b>A</b> &amp; <code>B</code>"), "A & B")


# ═════════════════════════════════════════════════════════════════
# bot/rich.py — builders and hard limits
# ═════════════════════════════════════════════════════════════════

class TestBuilders(unittest.TestCase):
    def test_table_rejects_too_many_columns(self):
        with self.assertRaises(ValueError):
            R.table([[R.cell(str(i)) for i in range(R.MAX_TABLE_COLUMNS + 1)]])

    def test_buttons_block_rejects_more_than_eight(self):
        with self.assertRaises(ValueError):
            R.buttons_block(
                [R.button(str(i), callback_data=f"k:{i}")
                 for i in range(R.MAX_BUTTONS_PER_BLOCK + 1)]
            )

    def test_button_rejects_over_long_callback(self):
        with self.assertRaises(ValueError):
            R.button("x", callback_data="z" * 65)

    def test_button_requires_an_action(self):
        with self.assertRaises(ValueError):
            R.button("x")

    def test_validate_rejects_too_many_blocks(self):
        with self.assertRaises(ValueError):
            R.validate([R.divider()] * (R.MAX_BLOCKS + 1))

    def test_table_cells_carry_required_alignment(self):
        # The Bot API marks align/valign required on RichBlockTableCell.
        cell = R.cell("x")
        self.assertEqual(cell["align"], "left")
        self.assertEqual(cell["valign"], "middle")

    def test_unstyle_strips_only_button_styles(self):
        blocks = [
            R.buttons_block([R.button("A", callback_data="a", style="primary")]),
            R.heading("T", 1),
        ]
        stripped = R.unstyle(blocks)
        self.assertNotIn("style", stripped[0]["buttons"][0])
        self.assertEqual(stripped[1], blocks[1])
        self.assertTrue(R.has_styled_buttons(blocks))
        self.assertFalse(R.has_styled_buttons(stripped))

    def test_icon_rich_returns_none_without_custom_emoji(self):
        self.assertIsNone(R.icon_rich("ℹ️"))
        node = R.icon_rich('<tg-emoji emoji-id="42">🔥</tg-emoji>')
        self.assertEqual(node["custom_emoji_id"], "42")


# ═════════════════════════════════════════════════════════════════
# Menu rich renderers
# ═════════════════════════════════════════════════════════════════

class TestMainBlocks(unittest.TestCase):
    def setUp(self):
        self.blocks = help_mod._rich_main_blocks(0)

    def test_opens_with_h1_heading(self):
        self.assertEqual(self.blocks[0]["type"], "heading")
        self.assertEqual(self.blocks[0]["size"], 1)
        self.assertIn("Help Menu", plain(self.blocks[0]["text"]))

    def test_has_divider_heading_and_footer(self):
        types = [b["type"] for b in self.blocks]
        self.assertIn("divider", types)
        self.assertIn("footer", types)
        self.assertIn("paragraph", types)
        headings = [b for b in self.blocks if b["type"] == "heading"]
        # H1 title + the "Modules" H2 that labels the grid
        self.assertTrue(any(b["size"] == 2 for b in headings))

    def test_module_grid_is_three_wide_and_within_limits(self):
        grids = button_blocks(self.blocks)
        self.assertTrue(grids)
        for blk in grids:
            self.assertLessEqual(len(blk["buttons"]), 8)
            self.assertLessEqual(len(blk["buttons"]), help_mod.COLS)
        self.assertEqual(
            sum(len(blk["buttons"]) for blk in grids),
            min(len(HELP_MENU), help_mod.PAGE_SIZE),
        )

    def test_grid_callback_data_is_valid(self):
        for data in all_callback_data(self.blocks):
            self.assertLessEqual(len(data), 64, data)
            self.assertTrue(data.startswith("help:open:"), data)

    def test_every_page_of_the_grid_validates(self):
        for page in range(help_mod._page_count()):
            with self.subTest(page=page):
                R.validate(help_mod._rich_main_blocks(page))

    def test_second_page_reports_its_own_footer(self):
        if help_mod._page_count() < 2:
            self.skipTest("HELP_MENU fits one page")
        blocks = help_mod._rich_main_blocks(1)
        footers = [b for b in blocks if b["type"] == "footer"]
        self.assertIn("Page 2/", plain(footers[0]["text"]))


class TestModuleBlocks(unittest.TestCase):
    def test_every_module_page_builds_valid_blocks(self):
        for mod in HELP_MENU:
            for sub in range(help_mod._module_page_count(mod)):
                with self.subTest(key=mod["key"], sub=sub):
                    R.validate(help_mod._rich_module_blocks(mod, sub))

    def test_opens_with_h1_heading_with_icon(self):
        mod = help_mod._module("general")
        head = help_mod._rich_module_blocks(mod, 0)[0]
        self.assertEqual(head["type"], "heading")
        self.assertEqual(head["size"], 1)
        self.assertIn(mod["title"], plain(head["text"]))

    def test_grid_is_a_real_table_with_a_header_row(self):
        blocks = help_mod._rich_module_blocks(help_mod._module("general"), 0)
        tables = table_blocks(blocks)
        self.assertTrue(tables)
        header = tables[0]["cells"][0]
        self.assertEqual([plain(c["text"]) for c in header],
                         ["Command", "Description"])
        self.assertTrue(all(c.get("is_header") for c in header))
        # command column is monospace, description is plain prose
        first = tables[0]["cells"][1]
        self.assertEqual(first[0]["text"]["type"], "code")
        self.assertIsInstance(first[1]["text"], str)

    def test_command_cells_are_unescaped_plain_text(self):
        for mod in HELP_MENU:
            blocks = help_mod._rich_module_blocks(mod, 0)
            for tbl in table_blocks(blocks):
                for row in tbl["cells"]:
                    for cell in row:
                        text = plain(cell["text"])
                        for entity in ("&amp;", "&lt;", "&gt;", "<code>", "</code>"):
                            self.assertNotIn(entity, text, (mod["key"], text))

    def test_section_headings_are_h3_and_match_the_html_page(self):
        for mod in HELP_MENU:
            pages = help_mod._module_chunks(mod)
            self.assertEqual(len(pages), help_mod._module_page_count(mod))
            for sub, page in enumerate(pages):
                with self.subTest(key=mod["key"], sub=sub):
                    expect = [h for h, _b, _r, kind in page
                              if kind == "section" and h]
                    got = [
                        plain(b["text"])
                        for b in help_mod._rich_module_blocks(mod, sub)
                        if b["type"] == "heading" and b["size"] == 3
                    ]
                    self.assertEqual(got, expect)

    def test_notes_survive_on_the_rich_path(self):
        for mod in HELP_MENU:
            if not mod["notes"]:
                continue
            for note in mod["notes"]:
                with self.subTest(key=mod["key"], note=note[:24]):
                    # A note is prose: no tags and no leftover entities.
                    text = plain(R.html_to_rich(note))
                    self.assertNotIn("<b>", text)
                    self.assertNotIn("&amp;", text)
                    self.assertEqual(text, R.strip_html(note))

    def test_grid_never_needs_nbsp_padding(self):
        # The table aligns columns itself — the HTML NBSP hack must not
        # leak into the rich cells.
        blocks = help_mod._rich_module_blocks(help_mod._module("general"), 0)
        for tbl in table_blocks(blocks):
            for row in tbl["cells"]:
                for cell in row:
                    self.assertNotIn("\u00a0", plain(cell["text"]))

    def test_footers_close_every_page(self):
        for mod in HELP_MENU:
            for sub in range(help_mod._module_page_count(mod)):
                blocks = help_mod._rich_module_blocks(mod, sub)
                self.assertEqual(blocks[-1]["type"], "footer", (mod["key"], sub))


# ═════════════════════════════════════════════════════════════════
# Send / edit path
# ═════════════════════════════════════════════════════════════════

class TestSendPath(unittest.TestCase):
    def test_send_menu_uses_rich(self):
        bot, msg = _RichBot(), _Msg()
        sent, restore = _capture_reply_text()
        try:
            self.assertTrue(run(help_mod._send_menu(msg, bot)))
        finally:
            restore()
        self.assertEqual(bot.api_methods, ["sendRichMessage"])
        self.assertEqual(sent, [], "HTML must not also be sent")
        self.assertEqual(msg.replies, [])

    def test_rich_failure_falls_back_to_the_full_html_menu(self):
        # Fail BOTH rich attempts (styled, then unstyled) → HTML menu.
        bot, msg = _RichBot(), _Msg()
        bot.fail_with = RuntimeError("Bad Request: unknown field")
        sent, restore = _capture_reply_text()
        try:
            self.assertTrue(run(help_mod._send_menu(msg, bot)))
        finally:
            restore()
        self.assertEqual(bot.calls, [])
        self.assertEqual(bot.attempts, 2, "styled then unstyled, then HTML")
        self.assertEqual(len(sent), 1)
        self.assertIn("Help Menu", sent[0]["text"])
        # the HTML fallback MUST still carry the module grid
        markup = sent[0]["markup"]
        data = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertTrue(any(d.startswith("help:open:") for d in data), data)

    def test_unstyled_retry_succeeds_before_html(self):
        bot, msg = _RichBot(), _Msg()
        bot.fail_first = RuntimeError("BUTTON_STYLE_UNSUPPORTED")
        sent, restore = _capture_reply_text()
        try:
            self.assertTrue(run(help_mod._send_menu(msg, bot)))
        finally:
            restore()
        self.assertEqual(bot.attempts, 2, "styled then unstyled")
        self.assertEqual(len(bot.calls), 1, "only the unstyled attempt lands")
        self.assertTrue(any("style" in b for b in all_rich_buttons(bot.seen[0])))
        self.assertTrue(all("style" not in b for b in all_rich_buttons(bot.seen[1])))
        self.assertEqual(sent, [])

    def test_network_error_never_retries(self):
        bot, msg = _RichBot(), _Msg()
        bot.fail_with = TimedOut("request timed out")
        sent, restore = _capture_reply_text()
        try:
            self.assertFalse(run(help_mod._send_menu(msg, bot)))
        finally:
            restore()
        self.assertEqual(bot.attempts, 1, "a transport failure must not retry")
        self.assertEqual(sent, [])

    def test_blocks_none_sends_html_immediately(self):
        msg = _Msg()
        sent, restore = _capture_reply_text()
        try:
            ok = run(help_mod._send_rich(
                _RichBot(), msg, None, None,
                help_mod._build_main_message(0),
                help_mod.main_menu_keyboard(0),
                help_mod.main_menu_keyboard(0, icons=False),
            ))
        finally:
            restore()
        self.assertTrue(ok)
        self.assertEqual(len(sent), 1)

    def test_flag_off_skips_rich_entirely(self):
        original = help_mod.HELP_RICH
        try:
            help_mod.HELP_RICH = False
            self.assertIsNone(help_mod._blocks(help_mod._rich_main_blocks, 0))
            bot, msg = _RichBot(), _Msg()
            sent, restore = _capture_reply_text()
            try:
                self.assertTrue(run(help_mod._send_menu(msg, bot)))
            finally:
                restore()
            self.assertEqual(bot.calls, [])
            self.assertEqual(len(sent), 1)
        finally:
            help_mod.HELP_RICH = original

    def test_group_stub_sends_rich_with_a_url_button(self):
        bot, msg = _RichBot(), _Msg(chat_type="supergroup")
        sent, restore = _capture_reply_text()
        try:
            run(help_mod.help_command(msg, bot, {"username": BOT_USERNAME}))
        finally:
            restore()
        self.assertEqual(bot.api_methods, ["sendRichMessage"])
        blocks = bot.calls[0].rich_message["blocks"]
        urls = [b["url"] for blk in button_blocks(blocks) for b in blk["buttons"]]
        self.assertEqual(urls, [f"https://t.me/{BOT_USERNAME}?start=help"])
        # rich markup is None: the body carries the button itself
        self.assertIsNone(bot.calls[0].reply_markup)
        self.assertEqual(sent, [])


class TestEditPath(unittest.TestCase):
    def test_main_callback_edits_rich(self):
        bot, q = _RichBot(), _Query("help:main:1")
        sent, restore = _capture_reply_text()
        try:
            run(help_mod.help_callback(q, bot, {"username": BOT_USERNAME}))
        finally:
            restore()
        self.assertEqual(bot.api_methods, ["editMessageText"])
        self.assertIn("rich_message", bot.calls[0].model_dump(warnings=False))
        self.assertEqual(q.message.edits, [])
        self.assertEqual(sent, [])

    def test_open_callback_edits_a_table(self):
        key = HELP_MENU[0]["key"]
        bot = _RichBot()
        q = _Query(f"help:open:{key}:0:0")
        run(help_mod.help_callback(q, bot, {"username": BOT_USERNAME}))
        self.assertEqual(bot.api_methods, ["editMessageText"])
        blocks = bot.calls[0].rich_message["blocks"]
        R.validate(blocks)
        self.assertTrue(table_blocks(blocks))

    def test_edit_failure_falls_back_to_html_edit(self):
        bot, q = _RichBot(), _Query("help:main:0")
        bot.fail_with = RuntimeError("Bad Request: BAD_RICH")
        run(help_mod.help_callback(q, bot, {"username": BOT_USERNAME}))
        self.assertEqual(bot.calls, [])
        # two HTML attempts (brand then plain) at most
        self.assertTrue(q.message.edits)
        markup = q.message.edits[0]["markup"]
        data = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertTrue(any(d.startswith("help:open:") for d in data), data)

    def test_start_screen_stays_html(self):
        # /start is not part of the rich menu — it keeps its HTML text.
        bot, q = _RichBot(), _Query("help:start")
        run(help_mod.help_callback(q, bot, {"username": BOT_USERNAME}))
        self.assertEqual(bot.calls, [])
        self.assertTrue(q.message.edits)


if __name__ == "__main__":
    unittest.main()
