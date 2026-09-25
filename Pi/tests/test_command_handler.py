"""Tests for multi-prefix command handling (bot/command_handler.py).

Run from the Pi/Pi root:

    python tests/test_command_handler.py
    python -m unittest discover -s tests

Environment isolation (BEFORE any bot import):
    * BOT_TOKEN is forced — importing `bot` pulls bot.config, which
      exits without one.
    * LOCALAPPDATA points at a temp dir so any DB touch stays isolated.
No network: updates are built locally and bound to a fake bot username.
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

# ── Environment isolation — must precede bot imports ──────────────
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["BOT_TOKEN"] = "1:TEST-TOKEN-FOR-UNIT-TESTS"
_TEST_DIR = tempfile.mkdtemp(prefix="pi_cmdprefix_test_")
os.environ["LOCALAPPDATA"] = _TEST_DIR
atexit.register(shutil.rmtree, _TEST_DIR, ignore_errors=True)

# ── Imports (after env) ───────────────────────────────────────────
from telegram import Chat, Message, Update  # noqa: E402
from telegram.ext import CommandHandler as PTBCommandHandler  # noqa: E402
from telegram.ext import filters as ptb_filters  # noqa: E402

from bot.command_handler import (  # noqa: E402
    COMMAND,
    COMMAND_PREFIXES,
    CommandHandler,
    parse_command,
)

BOT_USERNAME = "PiModulerBot"


async def _noop(update, context):  # noqa: ANN001, ANN201
    return None


def _update(text: str) -> Update:
    """Build a text message update bound to a fake bot identity."""
    msg = Message(
        message_id=1,
        date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        chat=Chat(id=-100123, type="supergroup"),
        text=text,
    )
    msg._bot = SimpleNamespace(username=BOT_USERNAME)
    return Update(update_id=1, message=msg)


class TestParseCommand(unittest.TestCase):
    def test_prefixes_constant(self):
        self.assertEqual(
            COMMAND_PREFIXES, ("/", "!", ".", "#", "$", "%", "&", "?")
        )

    def test_every_prefix_parses(self):
        for p in COMMAND_PREFIXES:
            parsed = parse_command(p + "help")
            self.assertIsNotNone(parsed, f"prefix {p!r} not recognized")
            self.assertEqual(parsed[0], "help")
            self.assertEqual(parsed[1], [])

    def test_args_split(self):
        cmd, args = parse_command(".all mode all")
        self.assertEqual(cmd, "all")
        self.assertEqual(args, ["mode", "all"])

    def test_rejects_plain_text(self):
        self.assertIsNone(parse_command("help me"))
        self.assertIsNone(parse_command(""))
        self.assertIsNone(parse_command(None))

    def test_rejects_leading_space(self):
        self.assertIsNone(parse_command(" /help"))

    def test_rejects_bare_prefix(self):
        for p in COMMAND_PREFIXES:
            self.assertIsNone(parse_command(p), f"bare {p!r}")
            self.assertIsNone(parse_command(p + "   "), f"bare {p!r} + spaces")

    def test_rejects_mid_message(self):
        self.assertIsNone(parse_command("say !help"))


class TestCommandHandler(unittest.TestCase):
    def setUp(self):
        self.h = CommandHandler("help", _noop)

    def _match(self, text):
        return self.h.check_update(_update(text))

    def test_all_prefixes_match(self):
        for p in COMMAND_PREFIXES:
            res = self._match(p + "help")
            self.assertIsInstance(res, tuple, f"prefix {p!r} did not match")
            self.assertEqual(res[0], [], f"prefix {p!r} args wrong")

    def test_args_reached_handler(self):
        res = self._match("!help me now")
        self.assertIsInstance(res, tuple)
        self.assertEqual(res[0], ["me", "now"])

    def test_case_insensitive(self):
        self.assertIsInstance(self._match("!HELP"), tuple)
        self.assertIsInstance(self._match("/Help"), tuple)

    def test_at_self_accepted(self):
        self.assertIsInstance(self._match(f"!help@{BOT_USERNAME}"), tuple)

    def test_at_other_bot_rejected(self):
        self.assertIsNone(self._match("!help@OtherBot"))

    def test_unregistered_rejected(self):
        self.assertIsNone(self._match("!nope"))
        self.assertIsNone(self._match("/nope"))

    def test_plain_text_rejected(self):
        self.assertIsNone(self._match("just talking about help"))

    def test_entity_parity_tails(self):
        # PTB entity parity: trailing punctuation/dashes are not part of
        # the command word — "/help!" and "!help-me" still trigger help.
        self.assertIsInstance(self._match("/help!"), tuple)
        self.assertIsInstance(self._match("!help-me"), tuple)

    def test_has_args_still_works(self):
        h = CommandHandler("go", _noop, has_args=True)
        self.assertIsNone(h.check_update(_update("!go")))
        self.assertIsInstance(h.check_update(_update("!go now")), tuple)

    def test_is_ptb_subclass(self):
        # Application.add_handler accepts it exactly like PTB's class.
        self.assertIsInstance(self.h, PTBCommandHandler)


class TestCommandFilter(unittest.TestCase):
    def test_matches_all_prefixes(self):
        for p in COMMAND_PREFIXES:
            self.assertTrue(
                COMMAND.check_update(_update(p + "anything")),
                f"filter missed prefix {p!r}",
            )

    def test_rejects_plain_text(self):
        self.assertFalse(COMMAND.check_update(_update("hello there")))

    def test_negation_works(self):
        neg = ~COMMAND
        self.assertFalse(neg.check_update(_update(".trigger")))
        self.assertTrue(neg.check_update(_update("normal chat")))

    def test_combined_like_text_pipelines(self):
        combo = ptb_filters.TEXT & ~COMMAND
        self.assertFalse(combo.check_update(_update("!cmd arg")))
        self.assertTrue(combo.check_update(_update("plain words")))


class TestModuleWiring(unittest.TestCase):
    """load_modules() smoke: every registered command handler is ours."""

    def test_all_modules_register_multi_prefix_handler(self):
        from bot.loader import load_modules

        handlers = []

        class _AppStub:
            bot_data = {}

            def add_handler(self, handler, group=0):  # noqa: ANN001, ANN202
                handlers.append((group, handler))

        loaded = load_modules(_AppStub())
        self.assertGreater(loaded, 5, "loader found almost no modules")

        cmd_handlers = [
            h for (_, h) in handlers if isinstance(h, PTBCommandHandler)
        ]
        foreign = [h for h in cmd_handlers if not isinstance(h, CommandHandler)]
        self.assertEqual(
            foreign, [],
            "modules still register plain telegram.ext.CommandHandler: "
            f"{[type(f).__module__ + '.' + type(f).__name__ for f in foreign]}",
        )
        self.assertGreaterEqual(
            len(cmd_handlers), 40, "expected dozens of registered commands"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
