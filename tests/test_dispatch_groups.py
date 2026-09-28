"""Handler-group dispatch regression (PTB one-handler-per-group).

Run from the Pi/Pi root:

    python tests/test_dispatch_groups.py
    python -m unittest discover -s tests

Background — the production incident this locks down
----------------------------------------------------
PTB's ``Application.process_update`` iterates handler groups and runs
AT MOST ONE handler per group, then moves to the next group.
antispam.flood_watch, chatstats.count_message and users.track_message
were once all registered in group 0, so flood_watch shadowed the
counters entirely.

The bot now runs on aiogram through bot.pipeline: registrations are
queued with PTB's group numbers and dispatch order, and every handler
wrapper raises SkipHandler so the chain continues to the next matching
handler — reproducing "all groups run".  This test loads the real
modules through bot.loader (production order) and replays dispatch over
fake messages.  If anyone puts two message pipelines back into the same
group, or breaks the chain semantics, it fails.

Environment isolation (BEFORE any bot import):
    * BOT_TOKEN is forced — importing `bot` pulls bot.config, which
      exits without one.
    * LOCALAPPDATA points at a temp dir so bind's table init (and any
      other DB use at import) is isolated.
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
_TEST_DIR = tempfile.mkdtemp(prefix="pi_dispatch_test_")
os.environ["LOCALAPPDATA"] = _TEST_DIR
atexit.register(shutil.rmtree, _TEST_DIR, ignore_errors=True)

# ── Imports (after env) ───────────────────────────────────────────
from aiogram.dispatcher.event.handler import FilterObject, HandlerObject  # noqa: E402

from bot import pipeline  # noqa: E402
from bot.loader import load_modules  # noqa: E402
from aiofakes import FakeBot, make_message  # noqa: E402

CHAT_ID = -100777001


async def _dispatch(pairs, update, data) -> list:
    """Replay pipeline dispatch: filters checked in order; a match does
    NOT stop later handlers (SkipHandler chain) — exactly what
    production does."""
    fired = []
    for entry in pairs:
        flts = [f for f in (entry.flt,) if f is not None]
        handler = HandlerObject(callback=entry.fn,
                                filters=[FilterObject(f) for f in flts])
        ok, _ = await handler.check(update, **data)
        if ok:
            fired.append(entry)
    return fired


def _key(entry) -> str:
    return entry.key


# ── Tests ─────────────────────────────────────────────────────────
class TestDispatch(unittest.TestCase):
    """Every message pipeline must survive a full loader registration."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.loaded = load_modules()
        cls.entries = pipeline.snapshot()

    def _fired(self, text, *, chat_type="supergroup", chat_id=CHAT_ID):
        msg = make_message(text, chat_id=chat_id, chat_type=chat_type)
        bot = FakeBot()
        return asyncio_run(_dispatch(self.entries, msg, {"bot": bot}))

    # ── Smoke ─────────────────────────────────────────────────────

    def test_loader_registered_modules(self):
        self.assertGreater(self.loaded, 5, "loader found almost no modules")

    # ── The incident: plain group text must reach ALL pipelines ───

    def test_plain_group_text_reaches_every_pipeline(self):
        keys = [_key(e) for e in self._fired("hello there")]
        for expected in (
            "bot.modules.adminbox.adminbox_message",
            "bot.modules.antispam.flood_watch",
            "bot.modules.chatstats.count_message",
            "bot.modules.users.track_message",
            "bot.modules.leveling.track_message",
            "bot.modules.bind.handlers.gate_message_handler",
            "bot.modules.bind.handlers.waiting_text_handler",
        ):
            self.assertIn(
                expected, keys,
                f"{expected} was shadowed in its group — two handlers "
                f"share a group? fired={keys}",
            )

    def test_flood_runs_before_counter(self):
        """Block must be set before the counter sees the message."""
        keys = [_key(e) for e in self._fired("one two three")]
        self.assertLess(
            keys.index("bot.modules.antispam.flood_watch"),
            keys.index("bot.modules.chatstats.count_message"),
            "flood_watch must dispatch before count_message",
        )

    def test_pipelines_use_distinct_groups(self):
        want = {
            "bot.modules.adminbox.adminbox_message",
            "bot.modules.antispam.flood_watch",
            "bot.modules.chatstats.count_message",
            "bot.modules.users.track_message",
            "bot.modules.leveling.track_message",
            "bot.modules.bind.handlers.waiting_text_handler",
        }
        seen: dict = {}
        for entry in self.entries:
            if entry.key in want:
                seen.setdefault(entry.key, entry.group)
        self.assertEqual(set(seen), want, f"missing registrations: {seen}")
        self.assertEqual(
            len(set(seen.values())), len(seen),
            f"two pipelines share a group: {seen}",
        )

    # ── Commands must keep working ────────────────────────────────

    def test_command_reaches_its_handler(self):
        keys = [_key(e) for e in self._fired("/rankings")]
        self.assertIn("bot.modules.chatstats.rankings_command", keys)
        self.assertNotIn(
            "bot.modules.antispam.flood_watch", keys,
            "flood_watch must not fire on commands",
        )
        self.assertNotIn(
            "bot.modules.chatstats.count_message", keys,
            "the counter must not count commands",
        )

    def test_private_text_not_counted(self):
        keys = [_key(e) for e in self._fired("dm message", chat_type="private",
                                              chat_id=42)]
        self.assertNotIn("bot.modules.chatstats.count_message", keys)
        self.assertNotIn("bot.modules.antispam.flood_watch", keys)
        # users.track_message has no group restriction — by design.
        self.assertIn("bot.modules.users.track_message", keys)

    # ── Non-text still registers (but never counts) ───────────────

    def test_sticker_registers_but_does_not_count(self):
        keys = [_key(e) for e in self._fired(None)]  # no text → not TEXT
        self.assertIn("bot.modules.users.track_message", keys)
        self.assertNotIn("bot.modules.chatstats.count_message", keys)
        self.assertNotIn("bot.modules.antispam.flood_watch", keys)


def asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)


if __name__ == "__main__":
    unittest.main(verbosity=2)
