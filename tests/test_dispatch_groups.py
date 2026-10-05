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
handler — reproducing "all groups run".  A handler may raise
``pipeline.StopChain`` instead, which _wrap converts into a normal
return so the update ends right there (used by the force-join gate, which
must run before group 0).  Groups are dispatched in NUMERIC order, never
in registration order.  This test loads the real modules through
bot.loader (production order) and replays dispatch over fake messages.
If anyone puts two message pipelines back into the same group, or breaks
the chain semantics, it fails.

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


# ── _wrap contract + real-Dispatcher chain ─────────────────────────
class TestWrapChain(unittest.TestCase):
    """_wrap raises SkipHandler, except when the handler says STOP.

    aiogram's observer stops at the first handler that returns normally
    and only continues on SkipHandler. If _wrap returned after a
    successful run, the first matching observer (a no-filter hook like
    adminbox_message) would starve every later group — counting, flood
    watch, trackers — while commands kept working. Success and error
    paths therefore skip, PTB "all groups run" style.

    The one deliberate exception is ``pipeline.StopChain``: a handler
    that raises it must come back as a normal return so the observer
    ends the update for good. That is what lets the force-join gate
    (group -1) refuse a non-member before group 0 commands ever run.
    """

    def test_wrap_success_still_skips(self):
        from aiogram.dispatcher.event.bases import SkipHandler

        from bot.pipeline import _wrap

        ran = []

        async def ok_handler():
            ran.append("ran")

        w = _wrap(ok_handler)
        with self.assertRaises(SkipHandler):
            asyncio_run(w())
        self.assertEqual(ran, ["ran"], "handler body must execute first")

    def test_wrap_stop_chain_returns_normally(self):
        """StopChain must become a plain return, NOT a SkipHandler."""
        from aiogram.dispatcher.event.bases import SkipHandler

        from bot.pipeline import StopChain, _wrap

        ran = []

        async def gate_handler():
            ran.append("gate")
            raise StopChain

        w = _wrap(gate_handler)
        try:
            result = asyncio_run(w())
        except SkipHandler:                      # pragma: no cover
            self.fail(
                "StopChain was turned back into SkipHandler - the update "
                "would keep running through every later group",
            )
        self.assertEqual(ran, ["gate"], "handler body must execute first")
        self.assertIsNone(
            result, "_wrap must swallow StopChain and return None",
        )

    def test_wrap_error_reports_and_skips(self):
        from aiogram.dispatcher.event.bases import SkipHandler

        from bot.pipeline import _wrap

        async def boom(_event=None):
            raise RuntimeError("explodes")

        w = _wrap(boom)
        with self.assertRaises(SkipHandler):
            asyncio_run(w(make_message("x")))

    def test_wrap_preserves_signature(self):
        import inspect

        from bot.pipeline import _wrap

        async def handler(message, bot, args):
            """doc"""

        w = _wrap(handler)
        self.assertEqual(
            set(inspect.signature(w).parameters), {"message", "bot", "args"},
        )
        self.assertEqual(w.__doc__, "doc")


class TestRealDispatcherChain(unittest.TestCase):
    """Feed a real Update through the real Dispatcher.

    Earlier observers (no-filter adminbox hook, analytics, antispam …)
    match a plain group text BEFORE chatstats in dispatch order; the
    chain must still reach count_message and write to the DB.
    """

    @classmethod
    def setUpClass(cls) -> None:
        from aiogram import Dispatcher

        pipeline.clear()
        load_modules()
        cls.dp = Dispatcher()
        pipeline.install(cls.dp)
        cls.matched = None  # filled lazily per test to keep imports local

    def _update(self, text, *, chat_id=-1005550001112, chat_type="supergroup",
                update_id=1, message_id=501):
        from datetime import datetime, timezone

        from aiogram.types import Chat, Message, Update, User

        chat_kw = {"id": chat_id, "type": chat_type}
        if chat_type != "private":
            chat_kw["title"] = "Count Test"
        return Update(
            update_id=update_id,
            message=Message(
                message_id=message_id,
                date=datetime.now(timezone.utc),
                chat=Chat(**chat_kw),
                from_user=User(id=424242, is_bot=False, first_name="Tester"),
                text=text,
            ),
        )

    def test_plain_text_reaches_count_message(self):
        from bot import database as D

        calls = []
        orig = D.db.count_message

        def spy(*a, **k):
            calls.append(a)
            return orig(*a, **k)

        D.db.count_message = spy
        try:
            class Bot:
                id = 777000111
            asyncio_run(self.dp.feed_update(
                bot=Bot(), update=self._update("hello counting world"),
            ))
        finally:
            D.db.count_message = orig
        self.assertGreaterEqual(
            len(calls), 1,
            "count_message never ran — chain stopped at an earlier observer",
        )
        chat_id, user_id, date, title = calls[0][:4]
        self.assertEqual(chat_id, -1005550001112)
        self.assertEqual(user_id, 424242)

    def test_command_and_private_not_counted(self):
        from bot import database as D

        calls = []
        orig = D.db.count_message

        def spy(*a, **k):
            calls.append(a)
            return orig(*a, **k)

        D.db.count_message = spy
        try:
            class Bot:
                id = 777000111
            asyncio_run(self.dp.feed_update(
                bot=Bot(),
                update=self._update("/rankings", update_id=2, message_id=502),
            ))
            asyncio_run(self.dp.feed_update(
                bot=Bot(),
                update=self._update(
                    "private hello", chat_id=42, chat_type="private",
                    update_id=3, message_id=503,
                ),
            ))
        finally:
            D.db.count_message = orig
        self.assertEqual(calls, [], "commands/private must not be counted")


if __name__ == "__main__":
    unittest.main(verbosity=2)
