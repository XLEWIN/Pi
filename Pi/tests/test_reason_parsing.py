"""Tests for target/reason argument parsing (bot/modules/moderation.py).

Covers the bug where a numeric-ID target (or the target token generally)
leaked into the action card's "Reason" field:

    .mute 8458275552          → Reason: "8458275552"   (WRONG)
    .mute 8458275552 1h spam  → Reason: "8458275552 spam", duration lost

Run from the Pi/Pi root:

    python tests/test_reason_parsing.py
    python -m unittest discover -s tests

Environment isolation (BEFORE any bot import):
    * BOT_TOKEN is forced — importing `bot` pulls bot.config, which
      exits without one.
    * LOCALAPPDATA points at a temp dir so any DB touch stays isolated.
No network: the helpers only inspect update/context objects.
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

# ── Environment isolation — must precede bot imports ──────────────
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["BOT_TOKEN"] = "1:TEST-TOKEN-FOR-UNIT-TESTS"
_TEST_DIR = tempfile.mkdtemp(prefix="pi_reason_test_")
os.environ["LOCALAPPDATA"] = _TEST_DIR
atexit.register(shutil.rmtree, _TEST_DIR, ignore_errors=True)

# ── Imports (after env) ───────────────────────────────────────────
from bot.modules.moderation import (  # noqa: E402
    DEFAULT_REASON,
    action_args,
    parse_duration_reason,
)


# ── Fakes ─────────────────────────────────────────────────────────

def _update(text: str, *, reply: bool = False, entity=None):
    """Minimal update: message text + optional reply/mention entity."""
    msg = SimpleNamespace(
        text=text,
        caption=None,
        reply_to_message=(
            SimpleNamespace(from_user=SimpleNamespace(id=7))
            if reply
            else None
        ),
        entities=[entity] if entity else [],
    )
    return SimpleNamespace(message=msg)


def _entity(offset: int, length: int, user_id: int = 7):
    return SimpleNamespace(
        type="text_mention",
        user=SimpleNamespace(id=user_id),
        offset=offset,
        length=length,
    )


def _context(args: list[str]):
    return SimpleNamespace(args=args)


# ═════════════════════════════════════════════════════════════════
# action_args — target token never reaches the reason
# ═════════════════════════════════════════════════════════════════

class TestActionArgs(unittest.TestCase):
    def test_no_args(self):
        self.assertEqual(action_args(_update("/mute"), _context([])), [])

    def test_reply_keeps_every_arg(self):
        # Reply supplies the target — duration/reason are ALL the args.
        upd = _update("/mute 1h spam", reply=True)
        self.assertEqual(action_args(upd, _context(["1h", "spam"])), ["1h", "spam"])
        upd = _update("/mute spam", reply=True)
        self.assertEqual(action_args(upd, _context(["spam"])), ["spam"])

    def test_at_mention_target_dropped(self):
        upd = _update("/mute @user 1h spam")
        self.assertEqual(
            action_args(upd, _context(["@user", "1h", "spam"])),
            ["1h", "spam"],
        )

    def test_at_mention_only(self):
        upd = _update("/mute @user")
        self.assertEqual(action_args(upd, _context(["@user"])), [])

    def test_numeric_id_target_dropped(self):
        # The core bug: the ID must not become the reason.
        upd = _update("/mute 8458275552")
        self.assertEqual(action_args(upd, _context(["8458275552"])), [])

    def test_numeric_id_target_with_duration_and_reason(self):
        upd = _update("/mute 8458275552 1h spam")
        self.assertEqual(
            action_args(upd, _context(["8458275552", "1h", "spam"])),
            ["1h", "spam"],
        )

    def test_text_mention_returns_text_after_entity(self):
        # "/mute John 1h spam" with John as text_mention (entity covers
        # the name) → duration/reason are what follows the name.
        upd = _update("/mute John 1h spam", entity=_entity(offset=6, length=4))
        self.assertEqual(
            action_args(upd, _context(["John", "1h", "spam"])),
            ["1h", "spam"],
        )

    def test_text_mention_nothing_after(self):
        upd = _update("/mute John", entity=_entity(offset=6, length=4))
        self.assertEqual(action_args(upd, _context(["John"])), [])

    def test_text_mention_utf16_offsets(self):
        # Entity offsets are UTF-16 code units — an astral-plane char in
        # the name must not shift the cut point.
        name = "\U0001F600\U0001F600"          # 2 codepoints, 4 UTF-16 units
        text = f"/mute {name} 1h spam"          # offset 6, length 4
        upd = _update(text, entity=_entity(offset=6, length=4))
        self.assertEqual(
            action_args(upd, _context([name, "1h", "spam"])),
            ["1h", "spam"],
        )

    def test_reply_wins_over_entity(self):
        # get_target_user resolves reply first; args then are pure reason.
        upd = _update("/mute John 1h", reply=True, entity=_entity(offset=6, length=4))
        self.assertEqual(
            action_args(upd, _context(["John", "1h"])),
            ["John", "1h"],
        )


# ═════════════════════════════════════════════════════════════════
# parse_duration_reason — duration/reason split with safe defaults
# ═════════════════════════════════════════════════════════════════

class TestParseDurationReason(unittest.TestCase):
    def test_empty_args_defaults(self):
        self.assertEqual(parse_duration_reason([]), (None, DEFAULT_REASON))
        self.assertEqual(parse_duration_reason(None), (None, DEFAULT_REASON))
        self.assertEqual(DEFAULT_REASON, "No reason provided")

    def test_duration_and_reason(self):
        duration, reason = parse_duration_reason(["1h", "spamming"])
        self.assertEqual(duration, timedelta(hours=1))
        self.assertEqual(reason, "spamming")

    def test_duration_only(self):
        duration, reason = parse_duration_reason(["30m"])
        self.assertEqual(duration, timedelta(minutes=30))
        self.assertEqual(reason, DEFAULT_REASON)

    def test_all_units(self):
        self.assertEqual(parse_duration_reason(["30s"])[0], timedelta(seconds=30))
        self.assertEqual(parse_duration_reason(["2h"])[0], timedelta(hours=2))
        self.assertEqual(parse_duration_reason(["2d"])[0], timedelta(days=2))
        self.assertEqual(parse_duration_reason(["1w"])[0], timedelta(weeks=1))

    def test_reason_words_only(self):
        duration, reason = parse_duration_reason(["bad", "behavior"])
        self.assertIsNone(duration)
        self.assertEqual(reason, "bad behavior")

    def test_bare_number_is_not_a_duration(self):
        # No unit suffix → not a duration; stays part of the reason
        # (the target-ID leak is prevented by action_args upstream).
        duration, reason = parse_duration_reason(["100"])
        self.assertIsNone(duration)
        self.assertEqual(reason, "100")

    def test_zero_duration_treated_as_reason(self):
        duration, reason = parse_duration_reason(["0s"])
        self.assertIsNone(duration)
        self.assertEqual(reason, "0s")

    def test_case_insensitive_duration(self):
        duration, _ = parse_duration_reason(["1H"])
        self.assertEqual(duration, timedelta(hours=1))


# ═════════════════════════════════════════════════════════════════
# End-to-end: the reported bug scenario
# ═════════════════════════════════════════════════════════════════

class TestReportedBug(unittest.TestCase):
    def test_numeric_id_target_never_becomes_reason(self):
        """.mute 8458275552 → reason 'No reason provided', not the ID."""
        upd = _update("/mute 8458275552")
        duration, reason = parse_duration_reason(
            action_args(upd, _context(["8458275552"]))
        )
        self.assertIsNone(duration)
        self.assertEqual(reason, "No reason provided")

    def test_numeric_id_with_duration_keeps_reason(self):
        upd = _update("/mute 8458275552 2h spamming")
        duration, reason = parse_duration_reason(
            action_args(upd, _context(["8458275552", "2h", "spamming"]))
        )
        self.assertEqual(duration, timedelta(hours=2))
        self.assertEqual(reason, "spamming")


if __name__ == "__main__":
    unittest.main(verbosity=2)
