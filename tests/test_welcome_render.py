"""Welcome rendering + channel-reference regression tests.

Two bugs fixed together because they share the same root cause class —
"data was silently re-encoded instead of being replayed":

1. ``format_welcome`` ran ``str.format()`` over the WHOLE template, so one
   unknown/stray brace (decorated welcome frames are full of them) aborted
   the substitution and the raw text — ``{username}`` and all — was posted.

2. Welcome re-sent the source text through ``parse_mode=HTML``, which
   drops ``custom_emoji`` entities: every premium emoji degraded to its
   fallback glyph.  The source entities are now stored and replayed with
   ``entities=``, which is what makes the message look like a forward.

3. ``/bind 1002807915293`` answered "chat not found" because Telegram
   only accepts the signed ``-100xxxxxxxxxx`` form.  The sign is restored
   before giving up (and ``t.me/c/…`` links are parsed properly).

Environment isolation (BEFORE any bot import):
    * BOT_TOKEN is forced — bot.config exits without one.
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

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["BOT_TOKEN"] = "1:TEST-TOKEN-FOR-UNIT-TESTS"
_TEST_DIR = tempfile.mkdtemp(prefix="pi_welcome_render_test_")
os.environ["LOCALAPPDATA"] = _TEST_DIR
atexit.register(shutil.rmtree, _TEST_DIR, ignore_errors=True)

from aiogram.types import MessageEntity  # noqa: E402

from bot.modules.bind.handlers import _resolve_channel  # noqa: E402
from bot.modules.bind.utils import parse_channel_ref  # noqa: E402
from bot.modules.welcome import (  # noqa: E402
    _command_body,
    _format_entities,
    _send_template,
    _slice_entities,
    _utf16_len,
    _values,
    format_welcome,
)


class _User:
    id = 42
    first_name = "FakeD3v"
    last_name = None
    username = "FakeD3v"
    is_bot = False

    @property
    def full_name(self):
        return self.first_name


class _Chat:
    type = "supergroup"
    title = "SHADOW HUB"


class FormatWelcomeTest(unittest.TestCase):
    def test_known_placeholders_fill_even_with_unknown_ones_present(self):
        tpl = ") SHADOW HUB (\nWelcome, {username}!\n{unknown} { }"
        out = format_welcome(tpl, _User(), _Chat())
        self.assertIn("Welcome, @FakeD3v!", out)
        # Unknown token and stray braces survive byte-for-byte — the old
        # str.format() path returned the whole template untouched here.
        self.assertIn("{unknown}", out)
        self.assertIn("{ }", out)

    def test_html_mention_and_escape(self):
        user = _User()
        user.first_name = "A&B"
        out = format_welcome("{mention} {first}", user, _Chat())
        self.assertIn("tg://user?id=42", out)
        # Values are HTML-escaped; the template itself is trusted markup.
        self.assertIn("A&amp;B", out)


class EntityReplayTest(unittest.TestCase):
    def test_custom_emoji_survives_a_longer_substitution(self):
        # 😀 is a surrogate pair: naive len() would skew every offset after it.
        tpl = "X\U0001F600 {username} end"
        entities = [
            {"type": "custom_emoji", "offset": 0, "length": 1,
             "custom_emoji_id": "5368324170671202286"},
        ]
        text, out = _format_entities(tpl, _values(_User(), _Chat(), html=False),
                                     entities, mention_user=_User())

        self.assertEqual(text, "X\U0001F600 @FakeD3v end")
        emoji = [e for e in out if e["type"] == "custom_emoji"]
        self.assertEqual(len(emoji), 1)
        self.assertEqual((emoji[0]["offset"], emoji[0]["length"]), (0, 1))

    def test_entity_covering_a_placeholder_expands_over_the_replacement(self):
        tpl = "aaaa {username} zzzz"
        entities = [{"type": "bold", "offset": 0, "length": _utf16_len(tpl)}]
        text, out = _format_entities(tpl, _values(_User(), _Chat(), html=False),
                                     entities)
        self.assertEqual(text, "aaaa @FakeD3v zzzz")
        self.assertEqual(out[0]["length"], _utf16_len(text))

    def test_mention_placeholder_gets_a_text_mention_entity(self):
        text, out = _format_entities("{mention} joined",
                                     _values(_User(), _Chat(), html=False), [],
                                     mention_user=_User())
        self.assertEqual(text, "FakeD3v joined")
        self.assertEqual(out[0]["type"], "text_mention")
        self.assertEqual(out[0]["user"]["id"], 42)

    def test_command_arguments_rebase_onto_the_body(self):
        full = "/setwelcome Hello \U0001F600 {username}"
        body, base = _command_body(SimpleNamespace(text=full))
        self.assertEqual(body, "Hello \U0001F600 {username}")
        self.assertEqual(base, len("/setwelcome "))

        kept = _slice_entities([
            MessageEntity(type="bot_command", offset=0, length=len("/setwelcome")),
            MessageEntity(type="bold", offset=base,
                          length=_utf16_len("Hello \U0001F600")),
        ], base, _utf16_len(body))
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0], {"type": "bold", "offset": 0,
                                   "length": _utf16_len("Hello \U0001F600")})


class _SendBot:
    """Stands in for aiogram's Bot; records send_message kwargs."""

    def __init__(self, reject_entities: bool = False):
        self.calls = []
        self.reject_entities = reject_entities

    async def send_message(self, **kwargs):
        if kwargs.get("entities") and self.reject_entities:
            raise RuntimeError("Bad Request: ENTITY_OFFSET_INVALID")
        self.calls.append(kwargs)
        return SimpleNamespace(message_id=99)


class SendTemplateTest(unittest.TestCase):
    def test_premium_emoji_is_replayed_via_entities_not_parse_mode(self):
        bot = _SendBot()
        msg_id = asyncio.run(_send_template(
            bot, -1003808103293,
            "hey {username}",
            [{"type": "custom_emoji", "offset": 0, "length": 1,
              "custom_emoji_id": "5368324170671202286"}],
            _User(), _Chat(), fallback="bye"))
        self.assertEqual(msg_id, 99)
        sent = bot.calls[0]
        self.assertEqual(sent["text"], "hey @FakeD3v")
        self.assertEqual(sent["entities"][0]["type"], "custom_emoji")
        self.assertNotIn("parse_mode", sent)

    def test_falls_back_to_html_when_the_entity_send_is_rejected(self):
        bot = _SendBot(reject_entities=True)
        msg_id = asyncio.run(_send_template(
            bot, -1003808103293, "hey {username}",
            [{"type": "custom_emoji", "offset": 0, "length": 1,
              "custom_emoji_id": "1"}],
            _User(), _Chat(), fallback="bye"))
        self.assertEqual(msg_id, 99)
        sent = bot.calls[0]
        self.assertEqual(sent["parse_mode"], "HTML")
        self.assertIn("@FakeD3v", sent["text"])

    def test_empty_template_uses_the_default_fallback(self):
        bot = _SendBot()
        asyncio.run(_send_template(bot, 1, "", None, _User(), _Chat(),
                                   fallback="Hey {first}"))
        self.assertIn("Hey FakeD3v", bot.calls[0]["text"])


class _FakeBot:
    def __init__(self):
        self.calls = []

    async def get_chat(self, ref):
        self.calls.append(ref)
        if ref == -1002807915293:
            return SimpleNamespace(id=-1002807915293, type="channel",
                                   username="ShadowBotsHQ", title="Chan")
        raise Exception("Bad Request: chat not found")


class ChannelRefTest(unittest.TestCase):
    def test_bare_positive_id_is_signed_before_the_lookup_gives_up(self):
        bot = _FakeBot()
        chat = __import__("asyncio").run(_resolve_channel(bot, "1002807915293"))
        self.assertEqual(chat.id, -1002807915293)
        self.assertEqual(bot.calls, [1002807915293, -1002807915293])

    def test_private_invite_style_link_restores_the_100_prefix(self):
        self.assertEqual(parse_channel_ref("https://t.me/c/2807915293/5"),
                         (-1002807915293, None, None))
        self.assertEqual(parse_channel_ref("https://t.me/c/2807915293"),
                         (-1002807915293, None, None))

    def test_username_forms_still_parse(self):
        self.assertEqual(parse_channel_ref("@ShadowBotsHQ")[1], "ShadowBotsHQ")
        self.assertEqual(parse_channel_ref("-1002807915293")[0], -1002807915293)


if __name__ == "__main__":
    unittest.main()
