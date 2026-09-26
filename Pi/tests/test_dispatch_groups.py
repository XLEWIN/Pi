"""Handler-group dispatch regression (PTB one-handler-per-group).

Run from the Pi/Pi root:

    python tests/test_dispatch_groups.py
    python -m unittest discover -s tests

Background — the production incident this locks down
----------------------------------------------------
PTB's ``Application.process_update`` iterates handler groups and runs
AT MOST ONE handler per group (source comment: "break — Only a max of
1 handler per group is handled"), then moves to the next group.

antispam.flood_watch, chatstats.count_message and users.track_message
were all registered in the default group 0. The loader registers
modules alphabetically (antispam < chatstats < users), so flood_watch
was first, matched every plain group message — and the counter and
both registration trackers NEVER ran. Symptom: users "not registering"
even with BotFather privacy mode disabled.

This test loads the real modules through bot.loader (production
order) and replays PTB's dispatch loop over fake updates. If anyone
puts two message pipelines back into the same group, it fails.

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
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

# ── Environment isolation — must precede bot imports ──────────────
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["BOT_TOKEN"] = "1:TEST-TOKEN-FOR-UNIT-TESTS"
_TEST_DIR = tempfile.mkdtemp(prefix="pi_dispatch_test_")
os.environ["LOCALAPPDATA"] = _TEST_DIR
atexit.register(shutil.rmtree, _TEST_DIR, ignore_errors=True)

# ── Imports (after env) ───────────────────────────────────────────
from telegram import Chat, Message, Update, User  # noqa: E402

from bot.loader import load_modules  # noqa: E402

CHAT_ID = -100777001
BOT_USERNAME = "PiModulerBot"


# ── Fakes ─────────────────────────────────────────────────────────

class _AppStub:
    """Mirrors Application.add_handler: records (group, handler) pairs."""

    bot_data = {}

    def __init__(self) -> None:
        self.pairs: list[tuple[int, object]] = []

    def add_handler(self, handler, group=0):  # noqa: ANN001, ANN202
        self.pairs.append((group, handler))


def _make_update(text: str | None, message_id: int = 1) -> Update:
    kwargs = {}
    if text is not None:
        kwargs["text"] = text
    msg = Message(
        message_id=message_id,
        date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        chat=Chat(id=CHAT_ID, type="supergroup"),
        from_user=User(id=42, first_name="Tester", is_bot=False),
        **kwargs,
    )
    msg._bot = SimpleNamespace(username=BOT_USERNAME)
    return Update(update_id=1, message=msg)


def _dispatch(pairs: list[tuple[int, object]], update: Update) -> list:
    """Replay PTB Application.process_update group semantics exactly.

    Groups are visited in first-registration order (dict insertion);
    within a group the FIRST matching handler fires and the group is
    left (``break — Only a max of 1 handler per group is handled``).
    Returns the fired handlers in dispatch order.
    """
    groups: dict[int, list] = {}
    for group, handler in pairs:
        groups.setdefault(group, []).append(handler)
    fired: list = []
    for group in groups:
        for handler in groups[group]:
            check = handler.check_update(update)
            if check is None or check is False:
                continue
            fired.append(handler)
            break
    return fired


def _key(handler) -> str:
    cb = handler.callback
    return f"{cb.__module__}.{cb.__name__}"


# ── Tests ─────────────────────────────────────────────────────────

class TestDispatch(unittest.TestCase):
    """Every message pipeline must survive a full loader registration."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = _AppStub()
        cls.loaded = load_modules(cls.app)

    def _fired(self, update: Update) -> list:
        return _dispatch(self.app.pairs, update)

    # ── Smoke ─────────────────────────────────────────────────────

    def test_loader_registered_modules(self):
        self.assertGreater(self.loaded, 5, "loader found almost no modules")

    # ── The incident: plain group text must reach ALL pipelines ───

    def test_plain_group_text_reaches_every_pipeline(self):
        fired = self._fired(_make_update("hello there"))
        keys = [_key(h) for h in fired]
        for expected in (
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
        keys = [_key(h) for h in self._fired(_make_update("one two three"))]
        self.assertLess(
            keys.index("bot.modules.antispam.flood_watch"),
            keys.index("bot.modules.chatstats.count_message"),
            "flood_watch must dispatch before count_message",
        )

    def test_pipelines_use_distinct_groups(self):
        want = {
            "bot.modules.antispam.flood_watch",
            "bot.modules.chatstats.count_message",
            "bot.modules.users.track_message",
            "bot.modules.leveling.track_message",
            "bot.modules.bind.handlers.waiting_text_handler",
        }
        seen: dict[str, int] = {}
        for group, handler in self.app.pairs:
            key = _key(handler)
            if key in want:
                seen.setdefault(key, group)
        self.assertEqual(set(seen), want, f"missing registrations: {seen}")
        self.assertEqual(
            len(set(seen.values())), len(seen),
            f"two pipelines share a group: {seen}",
        )

    # ── Commands must keep working ────────────────────────────────

    def test_command_reaches_its_handler(self):
        fired = self._fired(_make_update("/rankings"))
        keys = [_key(h) for h in fired]
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
        msg = Message(
            message_id=2,
            date=datetime(2026, 1, 1, tzinfo=timezone.utc),
            chat=Chat(id=42, type="private"),
            from_user=User(id=42, first_name="Tester", is_bot=False),
            text="dm message",
        )
        msg._bot = SimpleNamespace(username=BOT_USERNAME)
        fired = self._fired(Update(update_id=2, message=msg))
        keys = [_key(h) for h in fired]
        self.assertNotIn("bot.modules.chatstats.count_message", keys)
        self.assertNotIn("bot.modules.antispam.flood_watch", keys)
        # users.track_message has no group restriction — by design.
        self.assertIn("bot.modules.users.track_message", keys)

    # ── Non-text still registers (but never counts) ───────────────

    def test_sticker_registers_but_does_not_count(self):
        fired = self._fired(_make_update(None))  # no text → not TEXT
        keys = [_key(h) for h in fired]
        self.assertIn("bot.modules.users.track_message", keys)
        self.assertNotIn("bot.modules.chatstats.count_message", keys)
        self.assertNotIn("bot.modules.antispam.flood_watch", keys)


if __name__ == "__main__":
    unittest.main(verbosity=2)
