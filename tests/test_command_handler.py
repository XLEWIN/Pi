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
from pathlib import Path

# ── Environment isolation — must precede bot imports ──────────────
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["BOT_TOKEN"] = "1:TEST-TOKEN-FOR-UNIT-TESTS"
_TEST_DIR = tempfile.mkdtemp(prefix="pi_cmdprefix_test_")
os.environ["LOCALAPPDATA"] = _TEST_DIR
atexit.register(shutil.rmtree, _TEST_DIR, ignore_errors=True)

# ── Imports (after env) ───────────────────────────────────────────
from aiogram import F  # noqa: E402
from aiogram.filters import Command as AiogramCommand  # noqa: E402
from aiogram.filters import Filter, and_f  # noqa: E402
from aiogram.utils.magic_filter import MagicFilter  # noqa: E402

from bot import pipeline  # noqa: E402
from bot.command_handler import (  # noqa: E402
    COMMAND,
    COMMAND_PREFIXES,
    CommandFilter,
    CommandHandler,
    cmd,
    parse_command,
)
from bot.loader import load_modules  # noqa: E402
from aiofakes import FakeBot, make_message  # noqa: E402

BOT_USERNAME = "PiModulerBot"


def _msg(text: str):
    """Text-message event bound to a fake supergroup."""
    return make_message(text, chat_id=-100123, chat_type="supergroup")


def asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)


def _command_nodes(flt, out=None):
    """CommandFilter / aiogram-Command nodes inside any filter tree.

    Walks ``and_f``/``or_f`` targets, ``~invert`` targets and plain
    ``.filters`` containers; MagicFilter branches (``F.chat.type`` …)
    are not command registrations and are skipped.
    """
    if out is None:
        out = []
    if flt is None or isinstance(flt, MagicFilter):
        return out
    if isinstance(flt, (CommandFilter, AiogramCommand)):
        out.append(flt)
        return out
    if hasattr(flt, "targets"):
        for t in flt.targets:
            _command_nodes(getattr(t, "callback", t), out)
        return out
    if hasattr(flt, "target"):
        _command_nodes(getattr(flt.target, "callback", flt.target), out)
        return out
    for sub in getattr(flt, "filters", None) or []:
        if isinstance(sub, MagicFilter):
            continue
        _command_nodes(getattr(sub, "callback", sub), out)
    return out


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
        cmd_name, args = parse_command(".all mode all")
        self.assertEqual(cmd_name, "all")
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
        self.h = cmd("help")
        self.bot = FakeBot(username=BOT_USERNAME)

    def _match(self, text, bot=None):
        return asyncio_run(self.h(_msg(text), bot=bot or self.bot))

    def test_all_prefixes_match(self):
        for p in COMMAND_PREFIXES:
            res = self._match(p + "help")
            self.assertEqual(
                res, {"args": []}, f"prefix {p!r} did not match"
            )

    def test_args_reached_handler(self):
        res = self._match("!help me now")
        self.assertEqual(res, {"args": ["me", "now"]})

    def test_case_insensitive(self):
        self.assertEqual(self._match("!HELP"), {"args": []})
        self.assertEqual(self._match("/Help"), {"args": []})

    def test_at_self_accepted(self):
        self.assertEqual(
            self._match(f"!help@{BOT_USERNAME}"), {"args": []}
        )

    def test_at_other_bot_rejected(self):
        self.assertFalse(self._match("!help@OtherBot"))

    def test_unregistered_rejected(self):
        self.assertFalse(self._match("!nope"))
        self.assertFalse(self._match("/nope"))

    def test_plain_text_rejected(self):
        self.assertFalse(self._match("just talking about help"))

    def test_entity_parity_tails(self):
        # PTB entity parity: trailing punctuation/dashes are not part of
        # the command word — "/help!" and "!help-me" still trigger help.
        self.assertEqual(self._match("/help!"), {"args": []})
        self.assertEqual(self._match("!help-me"), {"args": []})

    def test_args_always_injected(self):
        # Match data always carries an args list — never None (the
        # aiogram stand-in for context.args).
        h = cmd("go")
        self.assertEqual(
            asyncio_run(h(_msg("!go"), bot=self.bot)), {"args": []}
        )
        self.assertEqual(
            asyncio_run(h(_msg("!go now"), bot=self.bot)), {"args": ["now"]}
        )

    def test_is_aiogram_filter(self):
        # The dispatcher registers it like any aiogram filter, and the
        # PTB-era CommandHandler alias still points at the same class.
        self.assertIsInstance(self.h, CommandFilter)
        self.assertIsInstance(self.h, Filter)
        self.assertIs(CommandHandler, CommandFilter)


class TestCommandFilter(unittest.TestCase):
    def _check(self, text):
        return asyncio_run(COMMAND(_msg(text)))

    def test_matches_all_prefixes(self):
        for p in COMMAND_PREFIXES:
            self.assertTrue(
                self._check(p + "anything"),
                f"filter missed prefix {p!r}",
            )

    def test_rejects_plain_text(self):
        self.assertFalse(self._check("hello there"))

    def test_negation_works(self):
        neg = ~COMMAND
        self.assertFalse(asyncio_run(neg(_msg(".trigger"))))
        self.assertTrue(asyncio_run(neg(_msg("normal chat"))))

    def test_combined_like_text_pipelines(self):
        combo = and_f(F.text, ~COMMAND)
        self.assertFalse(asyncio_run(combo(_msg("!cmd arg"))))
        self.assertTrue(asyncio_run(combo(_msg("plain words"))))


class TestModuleWiring(unittest.TestCase):
    """load_modules() smoke: every registered command is ours."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.loaded = load_modules()
        cls.entries = pipeline.snapshot()

    def test_all_modules_register_multi_prefix_handler(self):
        self.assertGreater(self.loaded, 5, "loader found almost no modules")

        ours, foreign = [], []
        for e in self.entries:
            for node in _command_nodes(e.flt):
                (ours if isinstance(node, CommandFilter) else foreign).append(
                    e.key
                )
        self.assertEqual(
            foreign, [],
            "modules still register aiogram's '/'-only Command filter: "
            f"{foreign}",
        )
        self.assertGreaterEqual(
            len(ours), 40, "expected dozens of registered commands"
        )

        by_key = {e.key: e for e in self.entries}
        info = _command_nodes(by_key["bot.modules.users.info_command"].flt)
        self.assertEqual(len(info), 1)
        self.assertEqual(info[0].commands, frozenset({"info"}))
        promo = _command_nodes(
            by_key["bot.modules.admin.promote_command"].flt
        )
        self.assertEqual(len(promo), 1)
        self.assertEqual(promo[0].commands, frozenset({"promote"}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
