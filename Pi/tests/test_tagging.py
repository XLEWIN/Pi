"""Tests for the Mass Tagging module (spec §65 coverage).

Run from the Pi/Pi root:

    python tests/test_tagging.py
    python -m unittest discover -s tests

Environment isolation (BEFORE any bot import):
    * BOT_TOKEN is forced — bot.config exits without one.
    * LOCALAPPDATA points at a temp dir so bot.database creates a fresh,
      empty SQLite file (a placeholder file prevents the legacy-DB copy).
No network calls: handlers use fake bots/messages; presence is stubbed.
"""

from __future__ import annotations

import asyncio
import atexit
import os
import shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.error import BadRequest

# ── Environment isolation — must precede bot imports ──────────────
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["BOT_TOKEN"] = "1:TEST-TOKEN-FOR-UNIT-TESTS"
_TEST_DIR = tempfile.mkdtemp(prefix="pi_tagging_test_")
os.environ["LOCALAPPDATA"] = _TEST_DIR
# Pre-create an empty DB file so bot.database skips its legacy migration.
_PIBOT = Path(_TEST_DIR) / "PiBot"
_PIBOT.mkdir(parents=True, exist_ok=True)
(_PIBOT / "bot_database.db").touch()
atexit.register(shutil.rmtree, _TEST_DIR, ignore_errors=True)

# ── Imports (after env) ───────────────────────────────────────────
from bot.modules.tagging import (  # noqa: E402
    activity_tracker,
    batcher,
    config,
    database as tdb,
    member_registry,
    sender,
    session as sess_mod,
    settings as settings_mod,
    sorter,
)
from bot.modules.tagging.cancellation import CancelToken  # noqa: E402
from bot.modules.tagging.exceptions import (  # noqa: E402
    AlreadyRunningError,
    SessionCancelled,
)
from bot.modules.tagging.handler import (  # noqa: E402
    all_command,
    allsettings_command,
    tagabort_command,
    tagstats_command,
)
from bot.modules.tagging.mention_builder import build_mention, mentions_length  # noqa: E402
from bot.modules.tagging.models import RANK_ACTIVE, RANK_ONLINE, RANK_STALE, Candidate, TagSettings  # noqa: E402
from bot.modules.tagging.presence.activity import ActivityPresence  # noqa: E402
from bot.modules.tagging.presence.manager import get_manager  # noqa: E402
from bot.modules.tagging.utils import (  # noqa: E402
    fmt_duration,
    fmt_n,
    pct,
    progress_bar,
    utf16_len,
)

CHAT = -999123456
OTHER_CHAT = -999123999


def setUpModule():
    """Create tagging tables in the fresh temp DB."""
    tdb.ensure_tables()


def tearDownModule():
    """Remove all rows this suite created (temp DB is deleted at exit)."""
    for chat in (CHAT, OTHER_CHAT):
        for table in ("tag_settings", "tag_members", "tag_activity", "tag_sessions"):
            tdb._conn.execute(f"DELETE FROM {table} WHERE chat_id=?", (chat,))
    tdb._conn.commit()


class _DbCleanupMixin:
    async def asyncTearDown(self):
        sess_mod.reset()
        activity_tracker.reset()
        for table in ("tag_settings", "tag_members", "tag_activity", "tag_sessions"):
            tdb._conn.execute(f"DELETE FROM {table} WHERE chat_id=?", (CHAT,))
        tdb._conn.commit()
        await super().asyncTearDown()


# ═════════════════════════════════════════════════════════════════
# Pure functions
# ═════════════════════════════════════════════════════════════════

class TestMentionBuilder(unittest.TestCase):
    def test_format(self):
        m = build_mention(123456, "Alice")
        self.assertEqual(
            m, '👤 <a href="tg://user?id=123456">Alice</a>'
        )

    def test_escapes_html(self):
        m = build_mention(1, "<b>&evil</b>")
        self.assertIn("&lt;b&gt;&amp;evil&lt;/b&gt;", m)
        self.assertNotIn("<b>&evil", m)

    def test_truncates_long_name(self):
        m = build_mention(1, "X" * 200)
        # 32-char cap + ellipsis inside the anchor.
        inner = m.rsplit("</a>", 1)[0].rsplit(">", 1)[1]
        self.assertEqual(len(inner), config.NAME_MAX)
        self.assertTrue(inner.endswith("…"))

    def test_fallback_for_empty_name(self):
        m = build_mention(999, "")
        self.assertIn(">999</a>", m)

    def test_mentions_length_counts_utf16(self):
        # Plain ASCII anchor length + 1 newline between two mentions.
        a = build_mention(1, "A")
        b = build_mention(2, "B")
        self.assertEqual(
            mentions_length([a, b]),
            utf16_len(a) + utf16_len(b) + 1,
        )


class TestBatcher(unittest.TestCase):
    def _mentions(self, n, name="User"):
        return [build_mention(1000 + i, f"{name} {i}") for i in range(n)]

    def test_empty(self):
        self.assertEqual(batcher.make_batches([]), [])

    def test_all_mentions_present_in_order(self):
        mentions = self._mentions(37)
        batches = batcher.make_batches(mentions)
        joined = "\n".join(batches)
        positions = [joined.find(m) for m in mentions]
        self.assertTrue(all(p >= 0 for p in positions))
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(joined.count("tg://user?id="), 37)

    def test_limits_respected(self):
        mentions = self._mentions(500, name="A" * 30)
        batches = batcher.make_batches(mentions)
        self.assertGreater(len(batches), 1)
        for text in batches:
            self.assertLessEqual(utf16_len(text), config.HARD_LIMIT)
            self.assertLessEqual(
                batcher.count_mentions(text), config.MAX_MENTIONS_PER_BATCH
            )
            # Soft target unless a single mention alone exceeds it.
            if batcher.count_mentions(text) > 1:
                self.assertLessEqual(utf16_len(text), config.TARGET_LEN)

    def test_batch_size_option(self):
        mentions = self._mentions(20)
        batches = batcher.make_batches(mentions, target=200)
        for text in batches:
            if batcher.count_mentions(text) > 1:
                self.assertLessEqual(utf16_len(text), 200)


class TestSorter(unittest.TestCase):
    def _cands(self):
        return [
            Candidate(user_id=3, display_name="c", last_active_at=100),
            Candidate(user_id=1, display_name="a", last_active_at=50,
                      presence_rank=RANK_ACTIVE),
            Candidate(user_id=2, display_name="b", last_active_at=200),
        ]

    def test_online_first_orders_presence_then_activity(self):
        ordered = sorter.order(self._cands(), "online_first")
        # id=1 has presence rank active → first; then by activity desc.
        self.assertEqual([c.user_id for c in ordered], [1, 2, 3])

    def test_recent_ignores_presence(self):
        ordered = sorter.order(self._cands(), "recent")
        self.assertEqual([c.user_id for c in ordered], [2, 3, 1])

    def test_random_seed_deterministic(self):
        cands = self._cands()
        a = sorter.order(cands, "random", seed=7)
        b = sorter.order(cands, "random", seed=7)
        self.assertEqual([c.user_id for c in a], [c.user_id for c in b])
        self.assertEqual(
            sorted(c.user_id for c in a), sorted(c.user_id for c in cands)
        )

    def test_apply_limit(self):
        cands = self._cands()
        self.assertEqual(len(sorter.apply_limit(cands, 2)), 2)
        self.assertEqual(len(sorter.apply_limit(cands, 0)), 3)


class TestUtils(unittest.TestCase):
    def test_utf16_emoji_counts_two(self):
        self.assertEqual(utf16_len("😀"), 2)
        self.assertEqual(utf16_len("ab"), 2)

    def test_progress_bar(self):
        self.assertEqual(progress_bar(0, 10), "[░░░░░░░░░░]")
        self.assertEqual(progress_bar(10, 10), "[██████████]")
        self.assertEqual(progress_bar(5, 10), "[█████░░░░░]")
        self.assertEqual(progress_bar(1, 0), "[░░░░░░░░░░]")

    def test_pct(self):
        self.assertEqual(pct(0, 0), 0)
        self.assertEqual(pct(5, 10), 50)
        self.assertEqual(pct(20, 10), 100)

    def test_fmt(self):
        self.assertEqual(fmt_n(1234567), "1,234,567")
        self.assertEqual(fmt_duration(8), "8s")
        self.assertEqual(fmt_duration(75), "1m 15s")
        self.assertEqual(fmt_duration(3672), "1h 1m")


class TestFloodParsing(unittest.TestCase):
    def test_retry_after_attribute(self):
        from telegram.error import RetryAfter
        self.assertEqual(sender.flood_seconds(RetryAfter(7)), 7)

    def test_value_attribute_fallback(self):
        exc = SimpleNamespace(retry_after=None, value=9)
        self.assertEqual(sender.flood_seconds(exc), 9)

    def test_garbage_is_zero(self):
        self.assertEqual(sender.flood_seconds(SimpleNamespace()), 0)

    def test_flood_max_sane(self):
        self.assertEqual(config.FLOOD_MAX, 60)
        self.assertLess(config.FLOOD_MAX, 300)


class TestSpecStrings(unittest.TestCase):
    """Exact user-facing strings from the spec are asserted here."""

    def test_no_reply(self):
        self.assertEqual(config.MSG_NO_REPLY, "Reply to a message to use /all.")

    def test_no_session(self):
        self.assertEqual(config.MSG_NO_SESSION, "No active tagging process.")

    def test_running(self):
        self.assertTrue(
            config.MSG_RUNNING.startswith("A tagging process is already running")
        )
        self.assertIn("/tagabort", config.MSG_RUNNING)

    def test_stopped_format(self):
        self.assertEqual(
            config.MSG_STOPPED_FMT.format(tagged=12, total=30),
            "Tagging stopped. Tagged: 12 / 30 users",
        )

    def test_group_only(self):
        self.assertEqual(config.MSG_NOT_GROUP, "Groups only.")


# ═════════════════════════════════════════════════════════════════
# Database
# ═════════════════════════════════════════════════════════════════

class TestDatabase(unittest.TestCase):
    def setUp(self):
        for table in ("tag_members", "tag_activity", "tag_sessions", "tag_settings"):
            tdb._conn.execute(f"DELETE FROM {table} WHERE chat_id=?", (CHAT,))
        tdb._conn.commit()

    def test_upsert_dedups(self):
        tdb.upsert_member(CHAT, 101, display_name="A", seen_at=1000.0)
        tdb.upsert_member(CHAT, 101, display_name="A2", seen_at=1001.0)
        self.assertEqual(tdb.count_members(CHAT), 1)

    def test_leave_excludes_and_rejoin_clears(self):
        tdb.upsert_member(CHAT, 102, display_name="B", seen_at=1000.0)
        tdb.mark_leave(CHAT, 102)
        self.assertEqual(tdb.count_members(CHAT), 0)
        tdb.mark_join(CHAT, 102, display_name="B")
        self.assertEqual(tdb.count_members(CHAT), 1)

    def test_settings_roundtrip(self):
        tdb.update_settings(CHAT, mode="random", window_hours=6)
        row = tdb.get_settings(CHAT)
        self.assertEqual(row["mode"], "random")
        self.assertEqual(row["window_hours"], 6)
        # Partial update keeps other columns.
        tdb.update_settings(CHAT, batch_size=3200)
        row = tdb.get_settings(CHAT)
        self.assertEqual(row["mode"], "random")
        self.assertEqual(row["batch_size"], 3200)

    def test_activity_flush(self):
        tdb.flush_activity([(CHAT, 501, 3, 5000.0)])
        tdb.flush_activity([(CHAT, 501, 2, 6000.0)])
        row = tdb._conn.execute(
            "SELECT message_count, last_message_at FROM tag_activity "
            "WHERE chat_id=? AND user_id=?",
            (CHAT, 501),
        ).fetchone()
        self.assertEqual(row["message_count"], 5)
        self.assertEqual(row["last_message_at"], 6000.0)

    def test_session_lifecycle_and_interrupted(self):
        sid = tdb.create_session(CHAT, 42, mode="all", window_hours=0)
        stats = tdb.session_stats(CHAT)
        self.assertEqual(stats["sessions"], 1)
        n = tdb.mark_interrupted()
        self.assertGreaterEqual(n, 1)
        stats = tdb.session_stats(CHAT)
        self.assertEqual(stats["stopped"], 1)
        tdb.finish_session(sid, "completed", total=10, tagged=10, messages_sent=3)
        stats = tdb.session_stats(CHAT)
        self.assertEqual(stats["completed"], 1)
        self.assertEqual(stats["tagged"], 10)

    def test_count_active(self):
        now = time.time()
        tdb.upsert_member(CHAT, 601, display_name="fresh",
                          seen_at=now - 5, active_at=now - 5)
        tdb.upsert_member(CHAT, 602, display_name="stale",
                          seen_at=now - 90000, active_at=now - 90000)
        self.assertEqual(tdb.count_active(CHAT, now - 60), 1)
        self.assertEqual(tdb.count_active(CHAT, now - 200000), 2)


class TestSeedFromHistory(unittest.TestCase):
    """Registry backfill from the shared history tables.

    seed_from_history() exists so /all works right after a restart:
    user_activity (UTC), daily_messages (local date) and group_members
    (joined_at) are merged per user, identity comes from `users`, and
    bulk_observe() upserts with MAX semantics (no regressions, no
    un-leaving members).
    """

    UID = 730001
    UID2 = 730002
    UID_BOT = 730003

    def setUp(self):
        self._wipe()

    def tearDown(self):
        self._wipe()

    def _wipe(self):
        c = tdb._conn
        c.execute("DELETE FROM user_activity WHERE chat_id=?", (CHAT,))
        c.execute("DELETE FROM daily_messages WHERE chat_id=?", (CHAT,))
        c.execute("DELETE FROM group_members WHERE chat_id=?", (CHAT,))
        c.execute(
            "DELETE FROM users WHERE user_id IN (?, ?, ?)",
            (self.UID, self.UID2, self.UID_BOT),
        )
        c.execute("DELETE FROM tag_members WHERE chat_id=?", (CHAT,))
        c.commit()

    def _insert_user(self, uid, first, last=None, username=None, bot=0):
        tdb._conn.execute(
            "INSERT INTO users (user_id, username, first_name, last_name, is_bot) "
            "VALUES (?, ?, ?, ?, ?)",
            (uid, username, first, last, bot),
        )
        tdb._conn.commit()

    @staticmethod
    def _utc(text: str) -> float:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        ).timestamp()

    def test_empty_history_returns_zero(self):
        self.assertEqual(tdb.seed_from_history(CHAT), 0)
        self.assertEqual(tdb.count_members(CHAT), 0)

    def test_seeds_activity_with_identity(self):
        self._insert_user(self.UID, "Alice", "Smith", username="alice")
        tdb._conn.execute(
            "INSERT INTO user_activity (user_id, action, chat_id, timestamp) "
            "VALUES (?, 'sent message', ?, '2026-09-24 18:22:01')",
            (self.UID, CHAT),
        )
        tdb._conn.commit()
        self.assertEqual(tdb.seed_from_history(CHAT), 1)
        rows = tdb.fetch_members(CHAT)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["user_id"], self.UID)
        self.assertEqual(row["username"], "alice")
        self.assertEqual(row["display_name"], "Alice Smith")
        self.assertAlmostEqual(
            row["last_active_at"], self._utc("2026-09-24 18:22:01"),
            places=3,
        )

    def test_daily_date_parses_local_midnight(self):
        self._insert_user(self.UID2, "Bob")
        tdb._conn.execute(
            "INSERT INTO daily_messages (chat_id, user_id, date, messages) "
            "VALUES (?, ?, '2026-09-24', 10)",
            (CHAT, self.UID2),
        )
        tdb._conn.commit()
        self.assertEqual(tdb.seed_from_history(CHAT), 1)
        row = tdb.fetch_members(CHAT)[0]
        expected = datetime(2026, 9, 24).timestamp()  # local midnight
        self.assertAlmostEqual(row["last_active_at"], expected, places=3)

    def test_merges_sources_newest_wins_and_idempotent(self):
        # Older daily rollup must lose to the newer activity row.
        self._insert_user(self.UID, "Alice")
        tdb._conn.execute(
            "INSERT INTO daily_messages (chat_id, user_id, date, messages) "
            "VALUES (?, ?, '2026-01-05', 4)",
            (CHAT, self.UID),
        )
        tdb._conn.execute(
            "INSERT INTO user_activity (user_id, action, chat_id, timestamp) "
            "VALUES (?, 'sent message', ?, '2026-09-24 18:22:01')",
            (self.UID, CHAT),
        )
        tdb._conn.commit()
        self.assertEqual(tdb.seed_from_history(CHAT), 1)
        first = tdb.fetch_members(CHAT)[0]
        # Second run must not change anything (MAX semantics).
        self.assertEqual(tdb.seed_from_history(CHAT), 1)
        second = tdb.fetch_members(CHAT)[0]
        self.assertEqual(first["last_active_at"], second["last_active_at"])
        self.assertEqual(first["first_seen"], second["first_seen"])
        self.assertAlmostEqual(
            second["last_active_at"], self._utc("2026-09-24 18:22:01"),
            places=3,
        )

    def test_never_regresses_live_rows_and_keeps_leave(self):
        now = time.time()
        # Live registry row, much fresher than the 2020 history stub.
        tdb.upsert_member(CHAT, self.UID, display_name="Live",
                          seen_at=now, active_at=now)
        # Left member: history predates the leave → left_at must stay.
        tdb.upsert_member(CHAT, self.UID2, display_name="Gone",
                          seen_at=now, active_at=now)
        tdb.mark_leave(CHAT, self.UID2)
        for uid in (self.UID, self.UID2):
            tdb._conn.execute(
                "INSERT INTO user_activity (user_id, action, chat_id, timestamp) "
                "VALUES (?, 'sent message', ?, '2020-01-01 00:00:00')",
                (uid, CHAT),
            )
        tdb._conn.commit()

        tdb.seed_from_history(CHAT)

        rows = {r["user_id"]: r for r in tdb.fetch_members(CHAT)}
        self.assertIn(self.UID, rows)  # fresh row untouched
        self.assertAlmostEqual(rows[self.UID]["last_active_at"], now, places=1)
        self.assertNotIn(self.UID2, rows)  # still excluded via left_at
        left = tdb._conn.execute(
            "SELECT left_at FROM tag_members WHERE chat_id=? AND user_id=?",
            (CHAT, self.UID2),
        ).fetchone()
        self.assertIsNotNone(left["left_at"])

    def test_bot_flags_from_users_and_group_members(self):
        self._insert_user(self.UID_BOT, "PiBot", bot=1)
        self._insert_user(self.UID, "Carol")
        tdb._conn.execute(
            "INSERT INTO group_members (chat_id, user_id, role, joined_at) "
            "VALUES (?, ?, 'member', '2026-09-20 10:00:00'), "
            "(?, ?, 'bot', '2026-09-20 10:00:00')",
            (CHAT, self.UID, CHAT, self.UID_BOT),
        )
        tdb._conn.commit()
        self.assertEqual(tdb.seed_from_history(CHAT), 2)
        flags = {
            r["user_id"]: bool(r["is_bot"])
            for r in tdb._conn.execute(
                "SELECT user_id, is_bot FROM tag_members WHERE chat_id=?",
                (CHAT,),
            ).fetchall()
        }
        self.assertTrue(flags[self.UID_BOT])
        self.assertFalse(flags[self.UID])

    def test_parse_ts_formats(self):
        self.assertAlmostEqual(
            tdb._parse_ts("2026-09-24 18:22:01"),
            self._utc("2026-09-24 18:22:01"), places=3,
        )  # SQLite CURRENT_TIMESTAMP → UTC
        local = datetime(2026, 9, 24, 18, 22, 1, 500000).timestamp()
        self.assertAlmostEqual(
            tdb._parse_ts("2026-09-24T18:22:01.500000"), local, places=3,
        )  # Python isoformat → local wall-clock
        self.assertAlmostEqual(
            tdb._parse_ts("2026-09-24"),
            datetime(2026, 9, 24).timestamp(), places=3,
        )  # bare date → local midnight
        self.assertEqual(tdb._parse_ts(None), 0.0)
        self.assertEqual(tdb._parse_ts("garbage"), 0.0)
        self.assertEqual(tdb._parse_ts(""), 0.0)


# ═════════════════════════════════════════════════════════════════
# Settings
# ═════════════════════════════════════════════════════════════════

class TestSettings(unittest.TestCase):
    def tearDown(self):
        tdb._conn.execute("DELETE FROM tag_settings WHERE chat_id=?", (CHAT,))
        tdb._conn.commit()

    def test_defaults(self):
        st = settings_mod.get(CHAT)
        self.assertEqual(st.mode, "online_first")
        self.assertEqual(st.window_hours, 24)
        self.assertEqual(st.batch_size, 3600)
        self.assertEqual(st.send_mode, "normal")
        self.assertEqual(st.registry_mode, "hybrid")

    def test_cycle_window_wraps(self):
        # WINDOWS_H = (1, 6, 12, 24, 72, 168, 0); default 24.
        st = settings_mod.cycle(CHAT, "window_hours")
        self.assertEqual(st.window_hours, 72)
        st = settings_mod.cycle(CHAT, "window_hours")
        self.assertEqual(st.window_hours, 168)
        st = settings_mod.cycle(CHAT, "window_hours")
        self.assertEqual(st.window_hours, 0)   # off
        st = settings_mod.cycle(CHAT, "window_hours")
        self.assertEqual(st.window_hours, 1)   # wraps to start

    def test_cycle_unknown_key(self):
        with self.assertRaises(ValueError):
            settings_mod.cycle(CHAT, "nope")

    def test_apply_arg_aliases(self):
        st = settings_mod.apply_arg(CHAT, "mode", "online")
        self.assertEqual(st.mode, "online_first")
        st = settings_mod.apply_arg(CHAT, "window", "12h")
        self.assertEqual(st.window_hours, 12)
        st = settings_mod.apply_arg(CHAT, "window", "off")
        self.assertEqual(st.window_hours, 0)
        st = settings_mod.apply_arg(CHAT, "max", "all")
        self.assertEqual(st.max_mentions, 0)
        st = settings_mod.apply_arg(CHAT, "send", "throttled")
        self.assertEqual(st.send_mode, "throttled")

    def test_apply_arg_validation(self):
        with self.assertRaises(ValueError) as ctx:
            settings_mod.apply_arg(CHAT, "mode", "nope")
        self.assertIn("Unknown mode", str(ctx.exception))
        with self.assertRaises(ValueError):
            settings_mod.apply_arg(CHAT, "batch", "999999")
        with self.assertRaises(ValueError):
            settings_mod.apply_arg(CHAT, "window", "-5")


# ═════════════════════════════════════════════════════════════════
# Async: cancellation, sessions, presence, assembly
# ═════════════════════════════════════════════════════════════════

class TestCancellation(unittest.IsolatedAsyncioTestCase):
    async def test_sleep_cancel_interrupts(self):
        token = CancelToken()

        async def canceller():
            await asyncio.sleep(0.05)
            token.cancel()

        task = asyncio.create_task(canceller())
        start = time.monotonic()
        with self.assertRaises(SessionCancelled):
            await token.sleep(5.0)
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 1.0)
        await task

    async def test_sleep_completes_when_not_cancelled(self):
        token = CancelToken()
        await token.sleep(0.01)  # must not raise

    async def test_check_raises_after_cancel(self):
        token = CancelToken()
        token.check()
        token.cancel()
        self.assertTrue(token.cancelled)
        with self.assertRaises(SessionCancelled):
            token.check()


def _fake_source(chat_id=CHAT, message_id=77):
    return SimpleNamespace(
        message_id=message_id,
        chat=SimpleNamespace(id=chat_id),
        message_thread_id=None,
    )


def _fake_status():
    msg = SimpleNamespace(deleted=False, edits=[], replies=[])

    async def edit_message_text(text, parse_mode=None, reply_markup=None):
        msg.edits.append(text)

    async def delete():
        msg.deleted = True

    async def reply_text(text, parse_mode=None, reply_markup=None):
        msg.replies.append(text)

    msg.edit_message_text = edit_message_text
    msg.delete = delete
    msg.reply_text = reply_text
    return msg


def _failing_status():
    """Status message whose edits always fail (stale/deleted card)."""
    msg = _fake_status()

    async def boom(text, parse_mode=None, reply_markup=None):
        raise BadRequest("message to edit not found")

    msg.edit_message_text = boom
    return msg


class TestSessions(_DbCleanupMixin, unittest.IsolatedAsyncioTestCase):
    async def test_one_session_per_chat(self):
        st = settings_mod.get(CHAT)
        s = sess_mod.create(
            chat_id=CHAT,
            session_id=1,
            invoker_id=42,
            source_message=_fake_source(),
            status_message=_fake_status(),
            settings=st,
            admin_ids=set(),
        )
        self.assertTrue(sess_mod.is_running(CHAT))
        with self.assertRaises(AlreadyRunningError):
            sess_mod.create(
                chat_id=CHAT,
                session_id=2,
                invoker_id=42,
                source_message=_fake_source(),
                status_message=_fake_status(),
                settings=st,
                admin_ids=set(),
            )
        # After finish+discard, a new session may start.
        sess_mod.finish(s, "completed")
        sess_mod.discard(s)
        self.assertFalse(sess_mod.is_running(CHAT))
        s2 = sess_mod.create(
            chat_id=CHAT,
            session_id=3,
            invoker_id=42,
            source_message=_fake_source(),
            status_message=_fake_status(),
            settings=st,
            admin_ids=set(),
        )
        self.assertIsNot(s2, s)


class TestPresence(unittest.IsolatedAsyncioTestCase):
    async def test_activity_ranks(self):
        now = time.time()
        p = ActivityPresence()
        fresh = Candidate(user_id=1, display_name="a", last_active_at=now - 3)
        day = Candidate(user_id=2, display_name="b", last_active_at=now - 3600)
        stale = Candidate(user_id=3, display_name="c", last_active_at=now - 90000)
        none = Candidate(user_id=4, display_name="d")
        await p.enrich(CHAT, [fresh, day, stale, none], now)
        self.assertEqual(fresh.presence_rank, RANK_ACTIVE)
        self.assertEqual(day.presence_rank, 2)
        self.assertEqual(stale.presence_rank, RANK_STALE)
        self.assertEqual(none.presence_rank, RANK_STALE)

    async def test_fresh_mtproto_presence_earns_online(self):
        now = time.time()
        p = ActivityPresence()
        c = Candidate(user_id=1, display_name="a", last_active_at=now - 90000,
                      presence_at=now - 2)
        await p.enrich(CHAT, [c], now)
        self.assertEqual(c.presence_rank, RANK_ONLINE)

    async def test_manager_label_honest_without_mtproto(self):
        mgr = get_manager()
        self.assertFalse(mgr.mtproto.available)
        self.assertEqual(mgr.source_label, "Activity (MTProto off)")

    async def test_refresh_once_resolves_known_chat_ids(self):
        """Regression: the refresh pass must find the MODULE helper.

        `presence/__init__` re-exports the singleton instance as
        `manager`, shadowing the submodule — a `from . import manager`
        in the loop used to raise
        "'PresenceManager' object has no attribute 'known_chat_ids'"
        on every tick. Calling _refresh_once() directly fails loudly
        if that import path ever regresses.
        """
        from bot.modules.tagging.presence.mtproto import MtprotoPresence

        tdb.upsert_member(CHAT, 424242, display_name="Ref")
        try:
            prov = MtprotoPresence()
            synced = []

            async def _fake_sync(chat_id):
                synced.append(chat_id)
                return 0

            prov.sync_chat = _fake_sync
            await prov._refresh_once()          # no AttributeError
            self.assertIn(CHAT, synced)          # registry chat was visited
        finally:
            tdb._conn.execute(
                "DELETE FROM tag_members WHERE chat_id=? AND user_id=?",
                (CHAT, 424242),
            )
            tdb._conn.commit()


class _StubPresence:
    """No-op presence: leaves ranks at defaults, records calls."""

    def __init__(self):
        self.syncs = []

    async def sync_members(self, chat_id, registry_mode):
        self.syncs.append((chat_id, registry_mode))
        return 0

    async def enrich(self, chat_id, candidates, now, *, registry_mode="hybrid"):
        return None


class TestAssembly(_DbCleanupMixin, unittest.IsolatedAsyncioTestCase):
    async def _seed(self):
        now = time.time()
        tdb._conn.execute("DELETE FROM tag_members WHERE chat_id=?", (CHAT,))
        tdb._conn.commit()
        # active member (1h ago)
        tdb.upsert_member(CHAT, 201, display_name="active",
                          seen_at=now - 3600, active_at=now - 3600)
        # fresh joiner (no messages yet)
        tdb.upsert_member(CHAT, 202, display_name="joiner", seen_at=now)
        # stale member (25h ago)
        tdb.upsert_member(CHAT, 203, display_name="stale",
                          seen_at=now - 90000, active_at=now - 90000)
        # bot
        tdb.upsert_member(CHAT, 204, display_name="bot", is_bot=True,
                          seen_at=now, active_at=now)
        # left member
        tdb.upsert_member(CHAT, 205, display_name="gone",
                          seen_at=now, active_at=now)
        tdb.mark_leave(CHAT, 205)
        return now

    async def test_exclusions_and_window(self):
        now = await self._seed()
        st = TagSettings(chat_id=CHAT, mode="online_first", window_hours=24)
        stub = _StubPresence()
        out = await member_registry.assemble_candidates(
            CHAT, st, admin_ids={201}, presence=stub, now=now
        )
        ids = [c.user_id for c in out]
        # 201 admin-excluded, 204 bot, 205 left, 203 outside 24h window.
        self.assertEqual(ids, [202])
        self.assertEqual(stub.syncs, [(CHAT, "hybrid")])

    async def test_mode_all_ignores_window(self):
        now = await self._seed()
        st = TagSettings(chat_id=CHAT, mode="all", window_hours=24)
        out = await member_registry.assemble_candidates(
            CHAT, st, admin_ids={201}, presence=_StubPresence(), now=now
        )
        self.assertEqual(sorted(c.user_id for c in out), [202, 203])

    async def test_max_cap(self):
        now = await self._seed()
        st = TagSettings(chat_id=CHAT, mode="all", max_mentions=1)
        out = await member_registry.assemble_candidates(
            CHAT, st, admin_ids={201}, presence=_StubPresence(), now=now
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].user_id, 202)  # most recent activity first

    async def test_mention_list_renders(self):
        now = await self._seed()
        st = TagSettings(chat_id=CHAT, mode="all")
        out = await member_registry.assemble_candidates(
            CHAT, st, admin_ids={201}, presence=_StubPresence(), now=now
        )
        mentions = member_registry.mention_list(out)
        self.assertEqual(len(mentions), len(out))
        self.assertTrue(all(m.startswith("👤 <a href=") for m in mentions))


# ═════════════════════════════════════════════════════════════════
# Handler flows (fakes, no network)
# ═════════════════════════════════════════════════════════════════

class _FakeBot:
    def __init__(self, invoker_status="administrator", admin_ids=(42,)):
        self.invoker_status = invoker_status
        self.admin_ids = list(admin_ids)

    async def get_chat_member(self, chat_id, user_id):
        return SimpleNamespace(status=self.invoker_status)

    async def get_chat_administrators(self, chat_id):
        return [
            SimpleNamespace(user=SimpleNamespace(id=i)) for i in self.admin_ids
        ]


class _FakeMessage:
    def __init__(self, *, chat_id=CHAT, reply_to=None, args=None):
        self.chat = SimpleNamespace(id=chat_id, type="supergroup")
        self.reply_to_message = reply_to
        self.replies = []
        self.message_id = 500
        self.message_thread_id = None

    async def reply_text(self, text, parse_mode=None, reply_markup=None):
        self.replies.append({"text": text, "markup": reply_markup})
        return SimpleNamespace(message_id=501, chat=self.chat,
                               message_thread_id=None)

    @property
    def last(self):
        return self.replies[-1]["text"] if self.replies else ""


def _fake_update(msg, user_id=42, chat_type="supergroup"):
    return SimpleNamespace(
        effective_message=msg,
        effective_chat=msg.chat if hasattr(msg, "chat")
        else SimpleNamespace(id=CHAT, type=chat_type),
        effective_user=SimpleNamespace(id=user_id),
    )


def _fake_context(bot=None, args=None):
    return SimpleNamespace(bot=bot or _FakeBot(), args=args or [])


class TestAllCommand(_DbCleanupMixin, unittest.IsolatedAsyncioTestCase):
    async def test_private_rejected(self):
        msg = _FakeMessage()
        upd = _fake_update(msg)
        upd.effective_chat = SimpleNamespace(id=1, type="private")
        await all_command(upd, _fake_context())
        self.assertIn(config.MSG_NOT_GROUP, msg.last)

    async def test_requires_reply(self):
        msg = _FakeMessage(reply_to=None)
        await all_command(_fake_update(msg), _fake_context())
        self.assertIn(config.MSG_NO_REPLY, msg.last)

    async def test_requires_admin(self):
        msg = _FakeMessage(reply_to=_fake_source())
        ctx = _fake_context(bot=_FakeBot(invoker_status="member"))
        await all_command(_fake_update(msg), ctx)
        self.assertIn(config.MSG_NOT_ADMIN, msg.last)

    async def test_admin_fetch_failure_is_fatal(self):
        msg = _FakeMessage(reply_to=_fake_source())
        bot = _FakeBot()

        async def boom(chat_id):
            raise RuntimeError("nope")

        bot.get_chat_administrators = boom
        await all_command(_fake_update(msg), _fake_context(bot=bot))
        self.assertIn(config.MSG_ADMIN_FETCH_FAIL, msg.last)

    async def test_admin_starts_session(self):
        msg = _FakeMessage(reply_to=_fake_source())
        with patch.object(sender, "run", new=AsyncMock()) as run_mock:
            await all_command(_fake_update(msg), _fake_context())
            self.assertIn("Tagging Started", msg.last)
            self.assertIsNotNone(sess_mod.get(CHAT))
            self.assertEqual(run_mock.call_count, 1)
            # Let the mocked task finish.
            s = sess_mod.get(CHAT)
            if s.task:
                await s.task


class TestTagabort(_DbCleanupMixin, unittest.IsolatedAsyncioTestCase):
    async def test_no_session(self):
        msg = _FakeMessage()
        await tagabort_command(_fake_update(msg), _fake_context())
        self.assertIn(config.MSG_NO_SESSION, msg.last)

    async def test_requires_admin(self):
        st = settings_mod.get(CHAT)
        sess_mod.create(
            chat_id=CHAT, session_id=1, invoker_id=42,
            source_message=_fake_source(), status_message=_fake_status(),
            settings=st, admin_ids=set(),
        )
        msg = _FakeMessage()
        ctx = _fake_context(bot=_FakeBot(invoker_status="member"))
        await tagabort_command(_fake_update(msg), ctx)
        self.assertIn(config.MSG_NOT_ADMIN, msg.last)

    async def test_abort_replies_stopped(self):
        st = settings_mod.get(CHAT)
        s = sess_mod.create(
            chat_id=CHAT, session_id=2, invoker_id=42,
            source_message=_fake_source(), status_message=_fake_status(),
            settings=st, admin_ids=set(),
        )
        s.total = 30
        s.tagged = 9
        # Simulate the sender finishing shortly after cancellation.
        asyncio.get_running_loop().call_later(0.05, s.done.set)
        msg = _FakeMessage()
        await tagabort_command(_fake_update(msg), _fake_context())
        self.assertIn(
            config.MSG_STOPPED_FMT.format(tagged=9, total=30), msg.last
        )


class TestAllsettingsAndStats(_DbCleanupMixin, unittest.IsolatedAsyncioTestCase):
    async def test_show_settings_card(self):
        msg = _FakeMessage()
        await allsettings_command(_fake_update(msg), _fake_context())
        self.assertIn("Mass Tag Settings", msg.last)
        self.assertIsNotNone(msg.replies[-1]["markup"])

    async def test_invalid_value(self):
        msg = _FakeMessage()
        ctx = _fake_context(args=["mode", "nope"])
        await allsettings_command(_fake_update(msg), ctx)
        self.assertIn("Unknown mode", msg.last)

    async def test_valid_value_updates(self):
        msg = _FakeMessage()
        ctx = _fake_context(args=["window", "6h"])
        await allsettings_command(_fake_update(msg), ctx)
        self.assertIn("Settings Updated", msg.last)
        self.assertEqual(settings_mod.get(CHAT).window_hours, 6)

    async def test_stats_card(self):
        msg = _FakeMessage()
        await tagstats_command(_fake_update(msg), _fake_context())
        self.assertIn("Mass Tag Stats", msg.last)
        self.assertIn("Presence source", msg.last)


class TestObservers(_DbCleanupMixin, unittest.IsolatedAsyncioTestCase):
    async def test_activity_observer_buffers(self):
        from bot.modules.tagging.handler import activity_observer

        msg = _FakeMessage()
        msg.from_user = SimpleNamespace(
            id=777, first_name="Buf", last_name="Fer",
            username="buffer", bot=False,
        )
        msg.sender_chat = None
        upd = _fake_update(msg)
        await activity_observer(upd, _fake_context())
        self.assertEqual(activity_tracker.pending(), 1)
        flushed = activity_tracker.flush_now()
        self.assertEqual(flushed, 1)
        row = tdb._conn.execute(
            "SELECT user_id FROM tag_members WHERE chat_id=? AND user_id=?",
            (CHAT, 777),
        ).fetchone()
        self.assertIsNotNone(row)

    async def test_anonymous_sender_ignored(self):
        from bot.modules.tagging.handler import activity_observer

        msg = _FakeMessage()
        msg.from_user = SimpleNamespace(id=778, first_name="Anon",
                                        last_name=None, username=None, bot=False)
        msg.sender_chat = SimpleNamespace(id=1)  # anonymous
        await activity_observer(_fake_update(msg), _fake_context())
        self.assertEqual(activity_tracker.pending(), 0)

    async def test_join_leave_registry(self):
        from bot.modules.tagging.handler import member_observer

        joiner = SimpleNamespace(
            id=888, first_name="New", last_name="Member",
            username=None, bot=False,
        )
        msg = _FakeMessage()
        msg.new_chat_members = [joiner]
        msg.left_chat_member = None
        await member_observer(_fake_update(msg), _fake_context())
        self.assertEqual(tdb.count_members(CHAT), 1)

        msg2 = _FakeMessage()
        msg2.new_chat_members = []
        msg2.left_chat_member = joiner
        await member_observer(_fake_update(msg2), _fake_context())
        self.assertEqual(tdb.count_members(CHAT), 0)


class _RunBot(_FakeBot):
    """Adds send/copy recorders so sender.run() can execute unmocked."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.sent = []
        self.copied = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace(message_id=900 + len(self.sent))

    async def copy_message(self, **kwargs):
        self.copied.append(kwargs)
        return SimpleNamespace(message_id=899)


def _run_session(status_message, st):
    """Real tag_sessions row + in-memory session, as all_command builds."""
    sid = tdb.create_session(
        CHAT, 42, mode=st.mode, window_hours=st.window_hours
    )
    return sess_mod.create(
        chat_id=CHAT, session_id=sid, invoker_id=42,
        source_message=_fake_source(), status_message=status_message,
        settings=st, admin_ids={42},
    )


class TestSenderRun(_DbCleanupMixin, unittest.IsolatedAsyncioTestCase):
    """Full sender.run() flows: nobody card, edit fallback, tagging."""

    async def test_run_nobody_card_and_db(self):
        st = settings_mod.get(CHAT)  # defaults: online_first / 24h
        status = _fake_status()
        s = _run_session(status, st)
        bot = _RunBot()

        await sender.run(s, _fake_context(bot=bot))

        self.assertTrue(s.done.is_set())
        self.assertIsNone(sess_mod.get(CHAT))  # discarded after finish
        self.assertTrue(status.edits)
        card = status.edits[-1]
        self.assertIn("No One to Tag", card)
        self.assertIn(config.MSG_NOBODY, card)
        self.assertIn("members known", card)
        self.assertIn("learned as they chat", card)
        row = tdb.last_session(CHAT)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error"], "nobody")
        self.assertEqual(bot.copied, [])  # nobody → no source copy
        self.assertEqual(bot.sent, [])

    async def test_edit_failure_falls_back_to_reply(self):
        st = settings_mod.get(CHAT)
        status = _failing_status()  # every edit raises BadRequest
        s = _run_session(status, st)

        await sender.run(s, _fake_context(bot=_RunBot()))

        self.assertTrue(status.replies)  # fresh reply landed instead
        self.assertIn(config.MSG_NOBODY, status.replies[-1])
        self.assertEqual(tdb.last_session(CHAT)["status"], "failed")

    async def test_run_tags_and_completes(self):
        now = time.time()
        tdb.upsert_member(CHAT, 201, display_name="Alpha",
                          seen_at=now - 60, active_at=now - 60)
        tdb.upsert_member(CHAT, 202, display_name="Beta",
                          seen_at=now - 60, active_at=now - 60)
        st = TagSettings(chat_id=CHAT, mode="all", window_hours=0)
        status = _fake_status()
        s = _run_session(status, st)
        bot = _RunBot()

        await sender.run(s, _fake_context(bot=bot))

        self.assertEqual(s.state, "completed")
        self.assertEqual(len(bot.copied), 1)  # source copied once
        self.assertEqual(len(bot.sent), 1)  # both mentions in one batch
        sent = bot.sent[0]
        self.assertEqual(sent["chat_id"], CHAT)
        self.assertEqual(sent["parse_mode"], "HTML")
        self.assertIn("tg://user?id=201", sent["text"])
        self.assertIn("tg://user?id=202", sent["text"])
        self.assertIn("Tagging Complete", status.edits[-1])
        self.assertEqual(s.tagged, 2)
        self.assertEqual(s.total, 2)
        row = tdb.last_session(CHAT)
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["total"], 2)
        self.assertEqual(row["tagged"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
