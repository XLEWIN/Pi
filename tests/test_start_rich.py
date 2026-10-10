"""Rich Message (Bot API 10.1+) rendering for /start — bot/modules/start.py.

Run from the Pi root:

    python -m pytest tests/test_start_rich.py

Environment isolation (BEFORE any bot import):
    * BOT_TOKEN is forced — importing `bot` pulls bot.config, which
      exits without one.
    * LOCALAPPDATA points at a temp dir so any DB touch stays isolated.

What is under test
------------------
1. ``bot/constants.py`` — ``START_TEXT`` is *derived* from
   ``START_TITLE_LINE`` + ``START_HINT_LINE``, so the HTML body and the
   rich body cannot drift apart.
2. ``build_start_blocks`` produces a valid three-block Rich Message.
3. The send path prefers ``sendRichMessage`` and falls back to the
   legacy HTML reply on rejection / disabled / invalid blocks.
4. A network timeout is terminal — never a second full RTT.
5. The keyboard is IDENTICAL on both paths (colours + custom-emoji
   icons), because it lives in ``reply_markup`` rather than in-body.
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
_TEST_DIR = tempfile.mkdtemp(prefix="pi_start_rich_")
os.environ["LOCALAPPDATA"] = _TEST_DIR
atexit.register(shutil.rmtree, _TEST_DIR, ignore_errors=True)

# ── Imports (after env) ───────────────────────────────────────────
from aiofakes import FakeMessage, call  # noqa: E402

from bot import rich as R  # noqa: E402
from bot import richsend as rs  # noqa: E402
from bot.constants import (  # noqa: E402
    BOT_DESCRIPTION,
    START_HINT_LINE,
    START_TEXT,
    START_TITLE_LINE,
)
from bot.modules import start as start_mod  # noqa: E402

USERNAME = "PiBot"


def run(coro):
    return asyncio.run(coro)


def flat(node) -> str:
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, (list, tuple)):
        return "".join(flat(x) for x in node)
    if isinstance(node, dict):
        if "text" in node:
            return flat(node["text"])
        return node.get("alternative_text", "")
    return str(node)


def markup_pairs(markup):
    return [
        (b.text,
         b.callback_data or b.url,
         getattr(b, "style", None),
         getattr(b, "icon_custom_emoji_id", None))
        for b in (x for row in markup.inline_keyboard for x in row)
    ]


class _RichBot:
    """Accepts raw TelegramMethod instances the way ``Bot.__call__`` does."""

    def __init__(self, fail=None, fail_first=None, username=USERNAME):
        self.username = username
        self.id = 1
        self.me = SimpleNamespace(id=1, username=username, is_bot=True)
        self.calls = []
        self.seen = []
        self.attempts = 0
        self.fail = fail
        self.fail_first = fail_first
        self.sent: list = []

    async def get_me(self):
        return self.me

    async def __call__(self, method):
        self.attempts += 1
        self.seen.append(method)
        if self.fail_first is not None and self.attempts == 1:
            raise self.fail_first
        if self.fail is not None:
            raise self.fail
        self.calls.append(method)
        return SimpleNamespace(message_id=1, chat=SimpleNamespace(id=1))

    async def send_message(self, chat_id, text, **kw):
        self.sent.append({"chat_id": chat_id, "text": text, **kw})
        return SimpleNamespace(message_id=len(self.sent),
                               chat=SimpleNamespace(id=chat_id))

    @property
    def methods(self):
        return [m.__api_method__ for m in self.calls]

    @property
    def rich(self):
        return self.calls[0] if self.calls else None


class _Spawned:
    """Stands in for start._spawn so background work is awaitable."""

    def __init__(self):
        self.pending = []

    def __call__(self, coro):
        self.pending.append(coro)

    async def drain(self):
        while self.pending:
            await asyncio.gather(*self.pending, return_exceptions=True)
            self.pending.clear()


class StartTestCase(unittest.TestCase):
    """Common plumbing: collect _spawn coroutines instead of leaking tasks."""

    def setUp(self):
        self.spawn = _Spawned()
        self._orig_spawn = start_mod._spawn
        start_mod._spawn = self.spawn
        self._orig_enabled = rs.RICH_ENABLED

    def tearDown(self):
        start_mod._spawn = self._orig_spawn
        rs.RICH_ENABLED = self._orig_enabled

    def _message(self, chat_id=-1009922000001, chat_type="supergroup",
                 user_id=42):
        return FakeMessage(text="/start", chat_id=chat_id,
                           chat_type=chat_type, user_id=user_id)

    def _start(self, msg, bot, args=None):
        out = run(call(start_mod.start_command, msg, bot=bot,
                       bot_data={"username": USERNAME},
                       args=(args if args is not None else [])))
        run(self.spawn.drain())
        return out

    def _rich_kb(self, bot):
        return bot.rich.reply_markup

    @staticmethod
    def _send_calls(msg):
        return [c for c in msg.calls if c[0] in ("answer", "reply")]

    def _html_kb(self, msg):
        return self._send_calls(msg)[-1][2]["reply_markup"]


class TestConstants(StartTestCase):
    def test_start_text_is_built_from_the_named_pieces(self):
        self.assertEqual(
            START_TEXT,
            START_TITLE_LINE + "\n" + "{description}" + "\n\n" + START_HINT_LINE,
            "START_TEXT must stay derived from the two named pieces",
        )

    def test_pieces_use_the_placeholders_the_renderer_expects(self):
        self.assertIn("{fire}", START_TITLE_LINE)
        self.assertIn("{username}", START_TITLE_LINE)
        self.assertIn("{arrow}", START_HINT_LINE)
        self.assertIn("/help for the full command list.", START_HINT_LINE)

    def test_formatted_html_matches_rich_wording(self):
        html = START_TEXT.format(
            fire="FIRE", username=f"@{USERNAME}",
            description=BOT_DESCRIPTION, arrow="ARROW",
        )
        lines = html.split("\n")
        self.assertEqual(lines[0], f"FIRE @{USERNAME}")
        self.assertEqual(lines[1], BOT_DESCRIPTION)
        self.assertEqual(lines[-1], "ARROW /help for the full command list.")


class TestBlocks(StartTestCase):
    def test_three_blocks_in_order(self):
        blocks = start_mod.build_start_blocks(USERNAME)
        self.assertEqual(len(blocks), 3)
        self.assertEqual([b["type"] for b in blocks],
                         ["heading", "paragraph", "paragraph"])
        self.assertEqual(blocks[0]["size"], 2)

    def test_blocks_validate(self):
        self.assertIsNotNone(rs.build_blocks(start_mod.build_start_blocks,
                                             USERNAME))

    def test_over_the_block_limit_is_rejected_locally(self):
        """``validate`` catches it before the API ever sees it."""
        too_many = [{"type": "paragraph", "text": "x"}] * (R.MAX_BLOCKS + 1)
        self.assertIsNone(rs.build_blocks(lambda u: too_many, USERNAME))

    def test_body_carries_username_description_and_help_hint(self):
        blocks = start_mod.build_start_blocks(USERNAME)
        text = flat(blocks)
        self.assertIn(f"@{USERNAME}", text)
        self.assertIn(BOT_DESCRIPTION, text)
        self.assertIn("/help for the full command list.", text)

    def test_title_keeps_the_fire_custom_emoji(self):
        title = start_mod.build_start_blocks(USERNAME)[0]["text"]
        if "tg-emoji" in start_mod.E.FIRE:
            self.assertIsInstance(title, list)
            emoji = [n for n in title
                     if isinstance(n, dict) and n.get("type") == "custom_emoji"]
            self.assertTrue(emoji, f"fire emoji dropped from the title: {title}")
        else:
            self.assertIn(start_mod.E.FIRE, flat(title))


class TestSendPath(StartTestCase):
    def test_rich_is_preferred(self):
        msg = self._message()
        bot = _RichBot()
        self._start(msg, bot)
        self.assertEqual(bot.methods, ["sendRichMessage"])
        self.assertEqual(self._send_calls(msg), [],
                         "rich must not also send an HTML copy")

    def test_rich_carries_the_start_keyboard(self):
        msg = self._message()
        bot = _RichBot()
        self._start(msg, bot)
        self.assertEqual(
            markup_pairs(self._rich_kb(bot)),
            markup_pairs(start_mod.build_start_keyboard()),
        )

    def test_rich_and_html_keyboards_are_identical(self):
        rich_msg = self._message()
        rich_bot = _RichBot()
        self._start(rich_msg, rich_bot)
        rich_pairs = markup_pairs(self._rich_kb(rich_bot))

        saved = rs.RICH_ENABLED
        rs.RICH_ENABLED = False
        try:
            html_msg = self._message()
            self._start(html_msg, _RichBot())
        finally:
            rs.RICH_ENABLED = saved

        self.assertEqual(markup_pairs(self._html_kb(html_msg)), rich_pairs,
                         "the two render paths must carry the same keyboard")

    def test_html_fallback_after_a_rich_rejection(self):
        msg = self._message()
        bot = _RichBot(fail=RuntimeError("style enum rejected"))
        self._start(msg, bot)
        self.assertEqual(self._send_calls(msg)[-1][0], "reply")  # group → quote
        self.assertIn(f"@{USERNAME}", msg.sent_texts[-1])
        self.assertIn("/help for the full command list.", msg.sent_texts[-1])
        self.assertIn(BOT_DESCRIPTION, msg.sent_texts[-1])
        self.assertEqual(
            markup_pairs(self._html_kb(msg)),
            markup_pairs(start_mod.build_start_keyboard()),
        )

    def test_html_fallback_answered_in_private_chats(self):
        msg = self._message(chat_type="private", chat_id=42)
        bot = _RichBot(fail=RuntimeError("nope"))
        self._start(msg, bot)
        self.assertEqual(self._send_calls(msg)[-1][0], "answer")  # no quote

    def test_timeout_is_terminal_no_second_attempt(self):
        msg = self._message()
        bot = _RichBot(fail=asyncio.TimeoutError())
        self._start(msg, bot)
        self.assertEqual(bot.attempts, 1, "a dead network must not be retried")
        self.assertEqual(msg.calls, [], "no HTML reply after a timeout")

    def test_disabled_switch_goes_straight_to_html(self):
        rs.RICH_ENABLED = False
        msg = self._message()
        bot = _RichBot()
        self._start(msg, bot)
        self.assertEqual(bot.seen, [], "rich must not be attempted at all")
        self.assertEqual(self._send_calls(msg)[-1][0], "reply")

    def test_builder_failure_goes_straight_to_html(self):
        """``build_blocks`` swallows a broken builder — HTML must run."""
        saved = start_mod.build_start_blocks

        def _boom(username):
            raise ValueError("the builder blew up")

        start_mod.build_start_blocks = _boom
        try:
            msg = self._message()
            bot = _RichBot()
            self._start(msg, bot)
            self.assertEqual(bot.seen, [], "no blocks must mean no rich send")
            self.assertEqual(self._send_calls(msg)[-1][0], "reply")
            self.assertIn(BOT_DESCRIPTION, msg.sent_texts[-1])
        finally:
            start_mod.build_start_blocks = saved

    def test_reply_is_still_background_work(self):
        """delete / DB / channel log only run after the reply is on its way."""
        msg = self._message()
        bot = _RichBot()
        order = []

        async def _finish(bot_, user, chat):
            order.append("finish")

        orig_finish = start_mod._finish_start
        orig_delete = start_mod._safe_delete

        async def _delete(m):
            order.append("delete")

        start_mod._finish_start = _finish
        start_mod._safe_delete = _delete
        try:
            self._start(msg, bot)
        finally:
            start_mod._finish_start = orig_finish
            start_mod._safe_delete = orig_delete
        self.assertEqual(order, ["delete", "finish"])
        self.assertEqual(len(self.spawn.pending), 0, "background work leaked")

    def test_start_help_deep_link_still_opens_help(self):
        from bot.modules import help as help_mod

        msg = self._message()
        bot = _RichBot()
        called = {}

        async def _help(message, bot_, bot_data):
            called["help"] = True

        orig = help_mod.help_command
        help_mod.help_command = _help
        try:
            self._start(msg, bot, args=["help"])
        finally:
            help_mod.help_command = orig
        self.assertTrue(called.get("help"))
        self.assertEqual(bot.seen, [], "/start help must not send the screen")


class TestKeyboard(StartTestCase):
    def test_rows_and_callbacks(self):
        kb = start_mod.build_start_keyboard()
        self.assertEqual([len(r) for r in kb.inline_keyboard], [2, 1, 2])
        data = [b.callback_data or b.url
                for r in kb.inline_keyboard for b in r]
        self.assertIn("start:help", data)
        self.assertEqual(
            sum(1 for r in kb.inline_keyboard for b in r if b.url), 4)

    def test_labels_are_plain_text(self):
        for r in start_mod.build_start_keyboard().inline_keyboard:
            for b in r:
                self.assertFalse(any(ord(ch) > 0x2122 for ch in b.text),
                                 f"emoji leaked into {b.text!r}")

    def test_icons_disabled_changes_only_the_icons(self):
        on = markup_pairs(start_mod.build_start_keyboard())
        off = markup_pairs(start_mod.build_start_keyboard(icons=False))
        self.assertEqual([p[:3] for p in on], [p[:3] for p in off],
                         "labels/urls/callbacks/colours must not change")
        self.assertTrue(all(p[3] for p in on))
        self.assertTrue(all(p[3] is None for p in off))


if __name__ == "__main__":
    unittest.main(verbosity=2)
