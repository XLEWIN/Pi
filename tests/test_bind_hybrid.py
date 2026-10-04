"""Bind module regressions — hybrid DB calls on the bot loop + button emoji.

Bug 1: ``bot/modules/bind/database.py`` exposed plain ``def``s that called
the async facade and then *forced* the returned box (``{**d, **raw}``,
``res.deleted_count``).  Forcing on the bot's own event loop is refused
with ``run_sync() called from the loop that must execute the coroutine``,
so on the live bot every read was swallowed as ``None`` and every write
was dropped as an un-awaited coroutine.  The visible symptom: the
force-join "I've Joined" button answered "Not bound." (or nothing) for a
group that was demonstrably bound.

This test drives the real scenario — the calls issued from a coroutine
running on a loop declared with ``bind_loop``, exactly like production.

Bug 2: bind's inline buttons carried stock Unicode (❌ ✅ ‼️) that is not
part of the owner's custom emoji pack (bot/emojis.py).  Every emoji on a
bind button must come from that pack: either the button's
``icon_custom_emoji_id`` or the pack's own fallback character.

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
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["BOT_TOKEN"] = "1:TEST-TOKEN-FOR-UNIT-TESTS"
_TEST_DIR = tempfile.mkdtemp(prefix="pi_bind_test_")
os.environ["LOCALAPPDATA"] = _TEST_DIR
atexit.register(shutil.rmtree, _TEST_DIR, ignore_errors=True)

from bot.async_bridge import adb, bind_loop, unbind_loop  # noqa: E402
from bot.database import db as _db  # noqa: E402
from bot.modules.bind import database as bdb  # noqa: E402
from bot.modules.bind.keyboards import (  # noqa: E402
    autodel_menu,
    bind_main_menu,
    custom_menu,
    force_join_keyboard,
    gates_menu,
    grace_menu,
    replace_confirm_menu,
    status_menu,
    unbind_confirm_menu,
)

CHAT = -1007711001
CHANNEL = -1007711002  # unique: sparse unique index on channel_id
USER = 424242


def _settings() -> Dict[str, Any]:
    return {
        "chat_id": CHAT,
        "channel_id": CHANNEL,
        "channel_username": "pi_bind_test",
        "channel_title": "Pi Bind Test",
        "force_join": 1,
        "admin_bypass": 1,
        "grace_minutes": 5,
        "auto_delete_seconds": 30,
        "gate_text": 1,
        "gate_media": 0,
        "gate_link": 1,
        "gate_document": 0,
        "gate_gif": 0,
        "gate_audio": 0,
        "gate_sticker": 0,
        "custom_message": None,
    }


class BindHybridOnBotLoopTest(unittest.TestCase):
    """The exact call shapes handlers.py / callbacks.py use, on the bot loop."""

    def _run(self, coro_factory):
        loop = asyncio.new_event_loop()
        bind_loop(loop)
        try:
            return loop.run_until_complete(coro_factory())
        finally:
            unbind_loop()
            loop.close()

    def tearDown(self):
        # Leave no bind row / join stamp behind for other modules' tests.
        bdb.remove_binding(CHAT)
        bdb.clear_join(CHAT, USER)

    def test_read_returns_the_binding_instead_of_none(self):
        async def scenario():
            await adb(bdb.upsert_binding(
                CHAT, CHANNEL,
                channel_username="pi_bind_test",
                channel_title="Pi Bind Test",
                bound_by=1,
            ))
            return await adb(bdb.get_settings(CHAT))

        row = self._run(scenario)
        self.assertIsNotNone(row, "get_settings returned None for a bound chat")
        self.assertEqual(row["channel_id"], CHANNEL)

    def test_write_is_persisted_not_dropped(self):
        async def scenario():
            await adb(bdb.record_join(CHAT, USER, joined_at=1_600_000_000.0))
            return await adb(bdb.get_join_time(CHAT, USER))

        ts = self._run(scenario)
        self.assertEqual(ts, 1_600_000_000.0, "record_join write never landed")

    def test_toggle_and_unbind_round_trip(self):
        async def scenario():
            await adb(bdb.upsert_binding(CHAT, CHANNEL, channel_username="pi_bind_test"))
            flipped = await adb(bdb.toggle_field(CHAT, "force_join"))
            after = await adb(bdb.get_settings(CHAT))
            removed = await adb(bdb.remove_binding(CHAT))
            gone = await adb(bdb.get_settings(CHAT))
            return flipped, after, removed, gone

        flipped, after, removed, gone = self._run(scenario)
        self.assertIsNotNone(flipped)
        self.assertEqual(int(after["force_join"]), 0, "toggle did not persist")
        self.assertTrue(removed, "remove_binding reported no row deleted")
        self.assertIsNone(gone)

    def test_find_other_binding_sees_a_second_group(self):
        async def scenario():
            await adb(bdb.upsert_binding(CHAT, CHANNEL, channel_username="pi_bind_test"))
            other = await adb(bdb.find_other_binding(CHANNEL, CHAT + 1))
            await adb(bdb.remove_binding(CHAT))
            return other

        other = self._run(scenario)
        self.assertIsNotNone(other, "find_other_binding did not see the row")
        self.assertEqual(other["chat_id"], CHAT)

    def test_worker_thread_shape_still_returns_the_value(self):
        """handlers.py runs _gate_state / _write_binding / _track_joins via
        to_thread: there box() must resolve to a plain value, not a box."""
        async def scenario():
            await adb(bdb.upsert_binding(CHAT, CHANNEL, channel_username="pi_bind_test"))
            row = await asyncio.to_thread(bdb.get_settings, CHAT)
            await asyncio.to_thread(bdb.record_join, CHAT, USER, 1_600_000_000.0)
            ts = await asyncio.to_thread(bdb.get_join_time, CHAT, USER)
            await asyncio.to_thread(bdb.remove_binding, CHAT)
            return row, ts

        row, ts = self._run(scenario)
        self.assertIsInstance(row, dict, "to_thread caller got a box, not a dict")
        self.assertEqual(row["channel_id"], CHANNEL)
        self.assertEqual(ts, 1_600_000_000.0)

    def test_warning_tracking_round_trip(self):
        """add_warning reads a sequence id — that read was never awaited."""
        async def scenario():
            await adb(bdb.add_warning(CHAT, 777, USER))
            row = await adb(_db.collection("bind_warnings").find_one(
                {"chat_id": CHAT, "message_id": 777}
            ))
            await adb(bdb.remove_warning(CHAT, 777))
            return row

        row = self._run(scenario)
        self.assertIsNotNone(row, "add_warning write never landed")
        self.assertEqual(row["user_id"], USER)


# ── button emoji ────────────────────────────────────────────────────

# Typographic punctuation a label may use. Anything else outside ASCII is
# an emoji — and a label emoji is drawn from Telegram's STOCK set, not the
# owner's pack, which is exactly what "only my emojis" forbids.
PLAIN_PUNCTUATION = set("\u2014\u2013\u2022\u2026")

# The owner's custom emoji, and the only emoji a bind button may show,
# comes from the button's icon_custom_emoji_id (EID.*).
_ICON_FIELD = "icon_custom_emoji_id"


def _label_chars(markup) -> List[str]:
    labels = []
    for row in markup.inline_keyboard:
        for button in row:
            labels.append(button.text)
    return labels


def _buttons(markup) -> List[Any]:
    return [b for row in markup.inline_keyboard for b in row]


class BindButtonEmojiTest(unittest.TestCase):
    """Only the owner's emoji are allowed on a bind inline button."""

    def _all_markups(self) -> List[Any]:
        s = _settings()
        return [
            bind_main_menu(None),
            bind_main_menu(s),
            gates_menu(s),
            grace_menu(s),
            autodel_menu(s),
            custom_menu(s),
            status_menu(s),
            unbind_confirm_menu(),
            replace_confirm_menu(),
            force_join_keyboard("https://t.me/pi_bind_test", "Pi Bind Test"),
            force_join_keyboard("", "Pi Bind Test"),
        ]

    def test_labels_are_plain_text_no_stock_emoji(self):
        """No Unicode emoji in a label — it would render from Telegram's pack."""
        offenders: List[str] = []
        for markup in self._all_markups():
            for label in _label_chars(markup):
                for ch in label:
                    if ord(ch) < 128 or ch in PLAIN_PUNCTUATION:
                        continue
                    offenders.append(f"{label!r} -> {ch!r} (U+{ord(ch):04X})")
        self.assertEqual(
            offenders, [],
            "bind button labels must be plain text (the owner's emoji only "
            "arrives via icon_custom_emoji_id): " + "; ".join(offenders),
        )

    def test_every_button_carries_the_owner_custom_emoji(self):
        """Stripping label emoji must not leave a button with no emoji at all."""
        missing: List[str] = []
        for markup in self._all_markups():
            for button in _buttons(markup):
                if not getattr(button, _ICON_FIELD, None):
                    missing.append(button.text)
        self.assertEqual(
            missing, [],
            "these bind buttons show no owner emoji (no icon_custom_emoji_id): "
            + "; ".join(missing),
        )

    def test_gate_prompt_has_both_buttons(self):
        markup = force_join_keyboard("https://t.me/x", "Chan")
        texts = [b.text for row in markup.inline_keyboard for b in row]
        self.assertEqual(len(texts), 2)
        self.assertIn("Join Channel", texts[0])
        self.assertIn("I've Joined", texts[1])


if __name__ == "__main__":
    unittest.main()
