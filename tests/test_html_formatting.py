"""The raw ``<tg-emoji …>`` markup leak — one fix, enforced everywhere.

Run from the repo root:

    python -m pytest -q tests/test_html_formatting.py

What this locks down
--------------------
``E.ERROR`` and friends expand to ``<tg-emoji emoji-id="…">`` HTML.  Two
places were showing that markup character-for-character instead of the
emoji:

1. **Messages sent without ``parse_mode``.**  ``reply_text(message,
   f"{E.INFO} You're already the group creator.")`` went out unparsed,
   so Telegram rendered the tag itself::

       <tg-emoji emoji-id="5904248647972820334">💭</tg-emoji> You're ...

   :func:`bot.reply._maybe_html` now promotes any payload carrying that
   marker to HTML.  ``plain_text`` callers are untouched.

2. **Callback toasts.**  ``answerCallbackQuery`` accepts NO
   ``parse_mode`` at all, so markup can never render there — the only
   correct answer is :func:`bot.emojis.plain`, which degrades the custom
   emoji to its stock fallback.

Part 1 is enforced by real sends; part 2 and the "forgot it everywhere
else" class are enforced by an AST scan of the whole ``bot`` package that
refuses any HTML payload reaching a sink without a way to parse it.

Environment isolation (BEFORE any bot import):
    * BOT_TOKEN is forced — importing `bot` pulls bot.config, which
      exits without one.
    * unittest is already imported -> bot.database always selects
      mongomock, so the suite never touches a real MongoDB server.
"""

from __future__ import annotations

import ast
import atexit
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

# ── Environment isolation ── must precede bot imports ────────────────
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["BOT_TOKEN"] = "1:TEST-TOKEN-FOR-UNIT-TESTS"
_TEST_DIR = tempfile.mkdtemp(prefix="pi_html_format_test_")
os.environ["LOCALAPPDATA"] = _TEST_DIR
atexit.register(shutil.rmtree, _TEST_DIR, ignore_errors=True)

# ── Imports (after env) ──────────────────────────────────────────────
from aiofakes import FakeMessage  # noqa: E402
from bot.emojis import E, custom_emoji, plain  # noqa: E402
from bot.reply import _HTML_MARKER, _maybe_html, reply_photo, reply_text  # noqa: E402

#: The exact message the user reported seeing raw.
RAW_REPORT = custom_emoji("💭", "5904248647972820334") + \
    " You're already the group creator."


# ════════════════════════════════════════════════════════════════════
# 1. bot.emojis.plain — the only correct answer for a parse_mode-less sink
# ════════════════════════════════════════════════════════════════════

class TestPlain(unittest.TestCase):
    """``plain`` scrubs HTML so toasts show words, not tags."""

    def test_custom_emoji_degrades_to_its_fallback(self):
        self.assertEqual(plain(RAW_REPORT), "💭 You're already the group creator.")

    def test_no_markup_survives(self):
        scrubbed = plain(f"{E.ERROR} <b>bold</b> <i>it</i> <code>x</code>")
        for tag in ("<tg-emoji", "</tg-emoji>", "<b>", "</b>", "<i>", "<code>"):
            self.assertNotIn(tag, scrubbed)

    def test_link_text_is_kept_and_the_url_dropped(self):
        self.assertEqual(plain('<a href="https://x.test">click here</a> now'),
                         "click here now")

    def test_html_entities_are_unescaped(self):
        self.assertEqual(plain("a &amp; b &lt; c"), "a & b < c")

    def test_plain_of_plain_is_identity(self):
        self.assertEqual(plain("nothing to strip"), "nothing to strip")

    def test_every_emoji_constant_is_renderable_as_plain(self):
        """Nothing in E may still carry a tag after plain()."""
        for name in dir(E):
            if name.startswith("_"):
                continue
            with self.subTest(constant=name):
                out = plain(getattr(E, name))
                self.assertNotIn("<", out, f"{name} leaked markup")
                self.assertNotIn(">", out, f"{name} leaked markup")


# ════════════════════════════════════════════════════════════════════
# 2. bot.reply — promote our own marker to HTML
# ════════════════════════════════════════════════════════════════════

class TestMaybeHtml(unittest.TestCase):

    def _kwargs(self, *args, **kw):
        _maybe_html(args, kw)
        return kw

    def test_marker_promotes(self):
        self.assertEqual(self._kwargs(f"{E.ERROR} nope")["parse_mode"], "HTML")

    def test_raw_tag_promotes(self):
        self.assertEqual(self._kwargs(RAW_REPORT)["parse_mode"], "HTML")

    def test_keyword_text_promotes(self):
        kw = self._kwargs(text=f"{E.INFO} hint")
        self.assertEqual(kw["parse_mode"], "HTML")

    def test_keyword_caption_promotes(self):
        kw = self._kwargs(caption=f"{E.INFO} hint")
        self.assertEqual(kw["parse_mode"], "HTML")

    def test_plain_payload_is_left_alone(self):
        """A stray '<' in user text must never be parsed as HTML."""
        kw = self._kwargs("5 < 6 and 7 > 4")
        self.assertNotIn("parse_mode", kw)

    def test_explicit_parse_mode_wins(self):
        kw = self._kwargs(f"{E.ERROR} nope", parse_mode="MarkdownV2")
        self.assertEqual(kw["parse_mode"], "MarkdownV2")

    def test_explicit_none_still_promotes(self):
        kw = self._kwargs(f"{E.ERROR} nope", parse_mode=None)
        self.assertEqual(kw["parse_mode"], "HTML")

    def test_the_marker_is_the_one_only_we_can_generate(self):
        # …not a general '<' sniff: user text may contain a stray one.
        self.assertIn("<tg-emoji", _HTML_MARKER)
        self.assertNotIn("<", _HTML_MARKER.replace("<tg-emoji", ""))


class TestReplyTextAutoHtml(unittest.IsolatedAsyncioTestCase):
    """The reported bug, end to end, through the real send helper."""

    def _msg(self, chat_type="supergroup") -> FakeMessage:
        return FakeMessage(chat_type=chat_type)

    async def test_the_reported_message_is_sent_as_html(self):
        msg = self._msg()
        await reply_text(msg, f"{E.INFO} You're already the group creator.")

        kind, text, kw = msg.last
        self.assertEqual(kind, "reply", "a group message is quoted")
        self.assertEqual(kw.get("parse_mode"), "HTML")
        self.assertIn(_HTML_MARKER, text)

    async def test_plain_text_is_still_sent_untouched(self):
        msg = self._msg()
        await reply_text(msg, "just some words")

        kind, text, kw = msg.last
        self.assertEqual(text, "just some words")
        self.assertNotIn("parse_mode", kw,
                         "plain payloads must keep their old behaviour")

    async def test_explicit_parse_mode_is_respected(self):
        msg = self._msg()
        await reply_text(msg, f"{E.ERROR} nope", parse_mode="MarkdownV2")

        self.assertEqual(msg.last[2].get("parse_mode"), "MarkdownV2")

    async def test_private_chat_takes_the_answer_path_and_still_parses(self):
        msg = self._msg(chat_type="private")
        await reply_text(msg, f"{E.ERROR} nope")

        kind, _text, kw = msg.last
        self.assertEqual(kind, "answer")
        self.assertEqual(kw.get("parse_mode"), "HTML")

    async def test_explicit_reply_to_still_parses(self):
        msg = self._msg()
        await reply_text(msg, f"{E.ERROR} nope", reply_to_message_id=99)

        kind, _text, kw = msg.last
        self.assertEqual(kind, "answer")
        self.assertEqual(kw.get("parse_mode"), "HTML")

    async def test_photo_caption_promotes_too(self):
        msg = self._msg()
        await reply_photo(msg, "file.jpg", caption=f"{E.INFO} a photo")

        kind, _media, kw = msg.last
        self.assertEqual(kind, "answer_photo")
        self.assertEqual(kw.get("parse_mode"), "HTML")


# ════════════════════════════════════════════════════════════════════
# 3. Static scan — no HTML payload may reach a sink that cannot parse it
# ════════════════════════════════════════════════════════════════════

HTML_LITERALS = (
    "<tg-emoji", "<b>", "</b>", "<i>", "</i>", "<code>", "</code>",
    "<a href=", "<blockquote", "<u>", "<s>", "<pre>", "<tg-spoiler>",
    "<strong>", "<em>", "<mark>", "</mark>", "<span", "</span>",
)

#: Helpers that return HTML.  Their names carry the markup.
HTML_HELPERS = {
    "plain_error", "plain_ok", "failed", "bot_rights_error",
    "action_card", "success_card", "error_card", "mention",
    "user_label", "actor_label",
}
#: …of which these embed an ``E.*`` tag, so the reply layer's marker fires.
MARKER_HELPERS = {
    "plain_error", "plain_ok", "failed", "bot_rights_error",
    "action_card", "success_card", "error_card",
}
#: Callers that scrub markup on the way out — nothing inside them is HTML.
NEUTRALIZERS = {"plain", "_strip_custom_emoji", "strip_custom_emoji"}

#: Sent through ``bot.reply``, which promotes a marker-bearing payload.
AUTO_SINKS = {
    "reply_text", "reply_photo", "reply_document", "reply_animation",
    "reply_video", "reply_sticker", "reply_voice", "reply_audio",
}
#: Sets ``parse_mode`` itself — out of scope for this scan.
SAFE_SINKS = {"reply_card"}

#: sink name -> positional index of the payload (``text`` / ``caption``).
TEXT_INDEX = {
    "reply_text": 1, "send_message": 1, "reply_photo": 1, "reply_document": 1,
    "reply_animation": 1, "reply_video": 1, "reply_sticker": 1,
    "reply_voice": 1, "reply_audio": 1, "send_photo": 1, "send_document": 1,
    "send_caption": 1,
    "answer": 0, "reply": 0, "edit_text": 0, "edit": 0, "edit_caption": 0,
    # module-local wrappers around CallbackQuery.answer
    "_safe_answer": 1, "_answer": 1,
}


def _callee(call: ast.Call) -> str:
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    if isinstance(call.func, ast.Name):
        return call.func.id
    return ""


def _active(node):
    """Every node the payload actually sends, stopping at a scrubber."""
    out = []
    stack = [node]
    while stack:
        cur = stack.pop()
        out.append(cur)
        if isinstance(cur, ast.Call) and _callee(cur) in NEUTRALIZERS:
            continue                      # markup inside is stripped
        stack.extend(ast.iter_child_nodes(cur))
    return out


def _is_e_attr(node) -> bool:
    if not (isinstance(node, ast.Attribute) and node.attr.isupper()):
        return False
    value = node.value
    return (isinstance(value, ast.Name) and value.id == "E") or (
        isinstance(value, ast.Attribute) and value.attr == "E"
    )


def payload_is_html(node) -> bool:
    nodes = _active(node)
    for n in nodes:
        if isinstance(n, ast.Constant) and isinstance(n.value, str) \
                and any(t in n.value for t in HTML_LITERALS):
            return True
    if any(_is_e_attr(n) for n in nodes):
        return True
    return any(isinstance(n, ast.Call) and _callee(n) in HTML_HELPERS
               for n in nodes)


def payload_carries_marker(node) -> bool:
    nodes = _active(node)
    for n in nodes:
        if isinstance(n, ast.Constant) and isinstance(n.value, str) \
                and "<tg-emoji" in n.value:
            return True
    if any(_is_e_attr(n) for n in nodes):
        return True
    return any(isinstance(n, ast.Call) and _callee(n) in MARKER_HELPERS
               for n in nodes)


def scan_source(src: str, filename: str = "<snippet>") -> list:
    """Every line in ``src`` whose HTML payload cannot be parsed."""
    hits = []
    tree = ast.parse(src, filename=filename)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _callee(node)
        if name not in TEXT_INDEX or name in SAFE_SINKS:
            continue
        if any(kw.arg == "parse_mode" for kw in node.keywords):
            continue
        idx = TEXT_INDEX[name]
        txt = node.args[idx] if len(node.args) > idx else None
        if txt is None:
            for kw in node.keywords:
                if kw.arg in ("text", "caption"):
                    txt = kw.value
                    break
        if txt is None or not payload_is_html(txt):
            continue
        if name in AUTO_SINKS and payload_carries_marker(txt):
            continue                      # bot.reply will promote it
        hits.append((filename, node.lineno, name))
    return hits


def scan_tree(root: Path) -> list:
    hits = []
    for path in sorted(root.rglob("*.py")):
        if ".git" in path.parts or "__pycache__" in path.parts:
            continue
        if path.is_relative_to(Path(__file__).resolve().parent):
            continue                      # never scan the test fixtures
        src = path.read_text(encoding="utf-8")
        hits.extend(scan_source(src, filename=str(path.relative_to(root))))
    return hits


class TestScannerItself(unittest.TestCase):
    """The scan must actually catch the bug — otherwise it proves nothing."""

    def _hits(self, src: str) -> list:
        return scan_source(src, filename="fixture.py")

    def test_catches_a_markup_bearing_toast(self):
        src = 'query.answer(f"{E.ERROR} Groups only.", show_alert=True)'
        self.assertEqual(self._hits(src), [("fixture.py", 1, "answer")])

    def test_catches_a_literal_marker_on_a_sink_that_cannot_promote(self):
        src = ('send_message(chat_id, '
               '\'<tg-emoji emoji-id="1">\u274c</tg-emoji> Already done.\')')
        self.assertEqual(len(self._hits(src)), 1)

    def test_accepts_a_literal_marker_on_a_reply_sink(self):
        """bot.reply will promote it — that is exactly the reported leak."""
        src = ('reply_text(message, '
               '\'<tg-emoji emoji-id="1">\u274c</tg-emoji> Already done.\')')
        self.assertEqual(self._hits(src), [])

    def test_catches_html_sent_without_parse_mode(self):
        src = 'reply_text(message, "<b>bold</b> only")'
        self.assertEqual(len(self._hits(src)), 1)

    def test_catches_send_message_without_parse_mode(self):
        src = 'await bot.send_message(chat_id, "<b>bold</b>")'
        self.assertEqual(len(self._hits(src)), 1)

    def test_accepts_an_explicit_parse_mode(self):
        src = ('send_message(chat_id, "<b>bold</b>", '
               'parse_mode=ParseMode.HTML)')
        self.assertEqual(self._hits(src), [])

    def test_accepts_a_plained_toast(self):
        src = 'query.answer(plain(f"{E.CHECK} Nightmode enabled."))'
        self.assertEqual(self._hits(src), [])

    def test_accepts_the_auto_promoted_reply(self):
        src = 'reply_text(message, f"{E.INFO} You are already the owner.")'
        self.assertEqual(self._hits(src), [])

    def test_accepts_a_plain_payload(self):
        src = 'reply_text(message, "nothing to parse")'
        self.assertEqual(self._hits(src), [])

    def test_never_looks_inside_a_scrubber(self):
        src = 'send_message(chat_id, plain(f"{E.ERROR} <b>bold</b>"))'
        self.assertEqual(self._hits(src), [])


class TestWholePackage(unittest.TestCase):
    """No raw-markup send anywhere in the shipped code."""

    def test_no_html_payload_reaches_an_unparseable_sink(self):
        hits = scan_tree(ROOT / "bot")
        formatted = "\n".join(
            f"  {filename}:{lineno} [{name}]"
            for filename, lineno, name in hits
        )
        self.assertEqual(
            hits, [],
            "HTML payload without a way to parse it — Telegram will show "
            "the tags literally:\n" + formatted,
        )


if __name__ == "__main__":
    unittest.main()
