"""Regression tests: perf layer (read cache / write-behind / memos).

Run from the Pi/Pi root:

    python tests/test_perf_layer.py

The perf layer keeps group messages off the hot path:
  * read cache — hot getters (shield, blocklist, filters, watch words,
    spam block, sudo, welcome) answer from memory; writes invalidate
    exactly through the collection proxy (both db methods and raw
    ``db.collection(...)`` access).
  * write-behind buffers — count_message / bump_* / group title refresh
    land in Mongo on the next read (flush-on-read) or the background
    cadence, never on the message handler's critical path.
  * aggregate memos — day/user totals merge pending counts in memory.
  * identity fast-skip — repeated per-message upserts (users rows,
    activity logs, membership touches) are skipped for _SKIP_TTL;
    raw delete_many clears the skip so re-inserts still happen.
  * security admin cache — get_chat_member runs once per (chat, user)
    instead of once per message.

Environment isolation (BEFORE any bot import):
    * BOT_TOKEN is forced — bot.config exits without one.
    * unittest is already imported → bot.database always selects
      mongomock, so the suite never touches a real MongoDB server.
"""

from __future__ import annotations

import asyncio
import atexit
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

# ── Environment isolation — must precede bot imports ──────────────
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["BOT_TOKEN"] = "1:TEST-TOKEN-FOR-UNIT-TESTS"
_TEST_DIR = tempfile.mkdtemp(prefix="pi_perflayer_test_")
os.environ["LOCALAPPDATA"] = _TEST_DIR
atexit.register(shutil.rmtree, _TEST_DIR, ignore_errors=True)

# ── Imports (after env) ───────────────────────────────────────────
from bot.database import db  # noqa: E402

CHAT = -910001
CHAT2 = -910002
USER = 710001
TODAY = "2026-09-27"


class _PerfBase(unittest.TestCase):
    """Wipes every collection the perf layer touches."""

    _COLLS = (
        "groups", "daily_messages", "chat_daily_stats", "chat_hourly_stats",
        "filters", "blocklist", "blocklist_exemptions", "watch_words",
        "spam_protection", "sudo_users", "shield_settings",
        "welcome_settings", "welcome_messages",
        "users", "user_activity", "group_members",
    )

    def setUp(self):
        self._clean()

    def tearDown(self):
        self._clean()

    def _clean(self):
        for coll in self._COLLS:
            db.collection(coll).delete_many({})


class TestReadCacheInvalidation(_PerfBase):
    def test_shield_set_then_get_is_fresh(self):
        self.assertEqual(db.get_shield_settings(CHAT)["msg_limit"], 10)
        db.set_shield_settings(CHAT, msg_limit=42)
        self.assertEqual(db.get_shield_settings(CHAT)["msg_limit"], 42)

    def test_filters_write_and_raw_delete_invalidate(self):
        self.assertEqual(db.get_filters(CHAT), [])
        db.add_filter(CHAT, "hi", "hello")
        self.assertEqual(len(db.get_filters(CHAT)), 1)
        db.add_filter(CHAT, "yo", "hey")
        self.assertEqual(len(db.get_filters(CHAT)), 2)
        db.remove_filter(CHAT, "hi")
        self.assertEqual([f["trigger_word"] for f in db.get_filters(CHAT)], ["yo"])
        # Raw test-style delete must be visible too (proxy invalidation).
        db.collection("filters").delete_many({"chat_id": CHAT})
        self.assertEqual(db.get_filters(CHAT), [])

    def test_blocklist_and_exempt(self):
        self.assertEqual(db.get_blocklist(CHAT), [])
        db.add_blocklist_word(CHAT, "bad", action="delete", reason="r")
        self.assertEqual(db.get_blocklist(CHAT)[0]["word"], "bad")
        db.set_blocklist_action(CHAT, "mute")
        self.assertEqual(db.get_blocklist(CHAT)[0]["action"], "mute")

        self.assertFalse(db.is_blocklist_exempt(CHAT, USER))
        db.exempt_blocklist_user(CHAT, USER)
        self.assertTrue(db.is_blocklist_exempt(CHAT, USER))
        db.collection("blocklist_exemptions").delete_many({"chat_id": CHAT})
        self.assertFalse(db.is_blocklist_exempt(CHAT, USER))

    def test_spam_block_clear_immediate(self):
        self.assertFalse(db.is_spam_blocked(USER))
        db.spam_set_block(USER, "2999-01-01T00:00:00+00:00")
        self.assertTrue(db.is_spam_blocked(USER))
        db.spam_clear(USER)
        self.assertFalse(db.is_spam_blocked(USER))

    def test_expired_block_reads_false_despite_cache(self):
        db.spam_set_block(USER, "2000-01-01T00:00:00+00:00")
        self.assertFalse(db.is_spam_blocked(USER))

    def test_sudo_add_remove_immediate(self):
        self.assertFalse(db.is_sudo_user(USER))
        db.add_sudo_user(USER)
        self.assertTrue(db.is_sudo_user(USER))
        self.assertIn(USER, db.get_sudo_users())
        db.remove_sudo_user(USER)
        self.assertFalse(db.is_sudo_user(USER))

    def test_watch_words_and_mode(self):
        self.assertEqual(db.get_all_watch_words(CHAT), {})
        db.add_watch_word(CHAT, USER, "zzz")
        self.assertEqual(db.get_all_watch_words(CHAT), {USER: ["zzz"]})
        self.assertEqual(db.get_watch_mode(CHAT, USER), "copy")
        db.set_watch_mode(CHAT, USER, "forward")
        self.assertEqual(db.get_watch_mode(CHAT, USER), "forward")
        db.remove_watch_word(CHAT, USER, "zzz")
        self.assertEqual(db.get_all_watch_words(CHAT), {})

    def test_welcome_settings_and_message(self):
        self.assertEqual(db.get_welcome_settings(CHAT)["welcome_enabled"], 1)
        db.set_welcome_enabled(CHAT, False)
        self.assertEqual(db.get_welcome_settings(CHAT)["welcome_enabled"], 0)
        self.assertIn("welcome_text", db.get_welcome_message(CHAT))
        db.set_welcome_text(CHAT, "Custom welcome {first}!")
        self.assertEqual(db.get_welcome_message(CHAT)["welcome_text"],
                         "Custom welcome {first}!")

    def test_cached_dicts_are_copies(self):
        """Callers mutate their copy — the cache must not see it."""
        settings = db.get_shield_settings(CHAT)
        settings["msg_limit"] = 999
        self.assertEqual(db.get_shield_settings(CHAT)["msg_limit"], 10)


class TestWriteBehindBuffers(_PerfBase):
    def test_count_message_not_written_until_read(self):
        # Hold the perf lock so the 5s background flusher can't race
        # the "still pending" assertion.
        with db._lock:
            db.count_message(CHAT, USER, TODAY, "Perf Chat")
            self.assertGreater(len(db._buf_msg), 0)
        # The raw read itself flushes (flush-on-read) — then it's there.
        raw = db.collection("daily_messages").count_documents(
            {"chat_id": CHAT}, limit=1
        )
        self.assertEqual(raw, 1)
        self.assertEqual(db._buf_msg, {})

    def test_day_total_exact_with_memo_and_pending(self):
        db.count_message(CHAT, USER, TODAY, "C")
        self.assertEqual(db.get_chat_day_total(CHAT, TODAY), 1)
        db.count_message(CHAT, USER, TODAY, "C")
        # Memo hit + pending merge — no flush needed for exactness.
        self.assertEqual(db.get_chat_day_total(CHAT, TODAY), 2)
        db.count_message(CHAT2, USER, TODAY, "C2")
        db.count_message(CHAT2, USER, TODAY, "C2")
        self.assertEqual(db.get_chat_day_total(CHAT2, TODAY), 2)
        self.assertEqual(db.get_chat_day_total(CHAT, TODAY), 2)

    def test_user_totals_exact(self):
        db.count_message(CHAT, USER, TODAY, "C")
        db.count_message(CHAT, USER, TODAY, "C")
        db.count_message(CHAT2, USER, TODAY, "C2")
        self.assertEqual(db.get_user_message_totals(CHAT, USER), (2, 3))
        self.assertEqual(db.get_user_messages(USER), 3)
        db.count_message(CHAT, USER, TODAY, "C")
        self.assertEqual(db.get_user_message_totals(CHAT, USER), (3, 4))

    def test_group_title_lands_on_flush(self):
        db.count_message(CHAT, USER, TODAY, "Fresh Title")
        db.flush_buffers()
        row = db.collection("groups").find_one({"chat_id": CHAT})
        self.assertIsNotNone(row)
        self.assertEqual(row["chat_title"], "Fresh Title")
        self.assertEqual(db._buf_groups, {})

    def test_bumps_visible_through_readers(self):
        db.bump_messages(CHAT, 3)
        db.bump_mod_actions(CHAT, 1)
        db.bump_new_members(CHAT, 2)
        stats = db.get_daily_stats(CHAT, days=1)
        self.assertEqual(stats["messages"], 3)
        self.assertEqual(stats["mod_actions"], 1)
        self.assertEqual(stats["new_members"], 2)

    def test_bump_hourly_visible(self):
        db.bump_hourly(CHAT, 9, 4)
        db.bump_hourly(CHAT, 9, 1)
        peak = db.get_peak_hours(CHAT, days=1, limit=3)
        self.assertEqual(peak[0]["hour"], 9)
        self.assertEqual(peak[0]["messages"], 5)

    def test_flush_is_idempotent_noop(self):
        db.count_message(CHAT, USER, TODAY, "C")
        db.flush_buffers()
        db.flush_buffers()  # nothing pending → no double count
        self.assertEqual(db.get_chat_day_total(CHAT, TODAY), 1)

    def test_concurrent_count_message_exact(self):
        """8 threads × 50 messages — buffered $inc must be lossless."""
        def worker():
            for _ in range(50):
                db.count_message(CHAT, USER, TODAY, "C")

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(db.get_chat_day_total(CHAT, TODAY), 400)
        self.assertEqual(
            db.collection("daily_messages").count_documents({"chat_id": CHAT}),
            1,
        )


class TestMemoInvalidation(_PerfBase):
    def test_raw_delete_zeroes_day_total(self):
        db.count_message(CHAT, USER, TODAY, "C")
        self.assertEqual(db.get_chat_day_total(CHAT, TODAY), 1)
        db.collection("daily_messages").delete_many({"chat_id": CHAT})
        self.assertEqual(db.get_chat_day_total(CHAT, TODAY), 0)

    def test_raw_inc_is_seen(self):
        """tests/test_chatstats.py does raw $inc — must bypass stale memos."""
        db.count_message(CHAT, USER, TODAY, "C")
        self.assertEqual(db.get_chat_day_total(CHAT, TODAY), 1)
        db.collection("daily_messages").update_one(
            {"chat_id": CHAT, "user_id": USER, "date": TODAY},
            {"$inc": {"messages": 5}},
        )
        self.assertEqual(db.get_chat_day_total(CHAT, TODAY), 6)
        self.assertEqual(db.get_user_messages(USER), 6)


class _FakeMember:
    def __init__(self, status):
        self.status = status


class _FakeBot:
    """Counts get_chat_member calls; can be told to fail."""

    def __init__(self, status="member", fail=False):
        self.status = status
        self.fail = fail
        self.calls = 0

    async def get_chat_member(self, chat_id, user_id):
        self.calls += 1
        if self.fail:
            raise RuntimeError("api down")
        return _FakeMember(self.status)


class TestSecurityAdminCache(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from bot.modules import security
        self.security = security
        security._admin_cache.clear()

    async def test_cached_second_call(self):
        bot = _FakeBot(status="administrator")
        first = await self.security._member_is_admin(bot, CHAT, USER)
        second = await self.security._member_is_admin(bot, CHAT, USER)
        self.assertTrue(first)
        self.assertTrue(second)
        self.assertEqual(bot.calls, 1)

    async def test_non_admin_cached_too(self):
        bot = _FakeBot(status="member")
        self.assertFalse(await self.security._member_is_admin(bot, CHAT, USER))
        self.assertFalse(await self.security._member_is_admin(bot, CHAT, USER))
        self.assertEqual(bot.calls, 1)

    async def test_errors_return_none_and_are_not_cached(self):
        bot = _FakeBot(fail=True)
        self.assertIsNone(await self.security._member_is_admin(bot, CHAT, USER))
        self.assertIsNone(await self.security._member_is_admin(bot, CHAT, USER))
        self.assertEqual(bot.calls, 2)
        self.assertEqual(len(self.security._admin_cache), 0)

    async def test_distinct_keys_fetch_separately(self):
        bot = _FakeBot(status="creator")
        await self.security._member_is_admin(bot, CHAT, USER)
        await self.security._member_is_admin(bot, CHAT, USER + 1)
        self.assertEqual(bot.calls, 2)


class TestIdentityFastSkip(_PerfBase):
    """Per-message identity/activity upserts skip within _SKIP_TTL.

    Every group message used to re-write identical users rows (2 round
    trips), insert one user_activity row, and touch group_members — the
    fast-skip makes repeat messages free while delete_many (tests,
    moderation wipes) still forces a real re-insert.
    """

    def test_register_user_reports_new_then_existing(self):
        self.assertTrue(db.register_user(USER, "u1", "U", None))
        self.assertFalse(db.register_user(USER, "u1", "U", None))
        # Row exists exactly once.
        self.assertEqual(
            db.collection("users").count_documents({"user_id": USER}), 1
        )

    def test_add_user_returns_true_and_keeps_row(self):
        self.assertTrue(db.add_user(USER, "u1", "U", None))
        self.assertTrue(db.add_user(USER, "u1", "U", None))
        self.assertEqual(
            db.collection("users").count_documents({"user_id": USER}), 1
        )

    def test_raw_delete_clears_skip_so_reinsert_happens(self):
        self.assertTrue(db.register_user(USER, "u1", "U", None))
        db.collection("users").delete_many({})
        # Skip was cleared by the delete — the row must come back.
        self.assertTrue(db.register_user(USER, "u1", "U", None))
        self.assertEqual(
            db.collection("users").count_documents({"user_id": USER}), 1
        )

    def test_activity_dedupe_inserts_once(self):
        db.update_user_activity(USER, "sent message", CHAT, "T", dedupe=True)
        db.update_user_activity(USER, "sent message", CHAT, "T", dedupe=True)
        self.assertEqual(
            db.collection("user_activity").count_documents(
                {"user_id": USER, "action": "sent message"}
            ),
            1,
        )
        # Event logs (dedupe off) keep writing every row.
        db.update_user_activity(USER, "started the bot (DM)", CHAT, "T")
        db.update_user_activity(USER, "started the bot (DM)", CHAT, "T")
        self.assertEqual(
            db.collection("user_activity").count_documents(
                {"user_id": USER, "action": "started the bot (DM)"}
            ),
            2,
        )

    def test_cache_group_member_new_once_then_skip(self):
        self.assertTrue(db.cache_group_member(CHAT, USER))
        self.assertFalse(db.cache_group_member(CHAT, USER))
        db.collection("group_members").delete_many({})
        self.assertTrue(db.cache_group_member(CHAT, USER))

    def test_prune_skips_drops_expired_keys(self):
        with db._lock:
            db._skip_reg[("stale",)] = 0.0  # already expired
            db._skip_reg[("fresh",)] = float("inf")
        db._prune_skips()
        with db._lock:
            self.assertNotIn(("stale",), db._skip_reg)
            self.assertIn(("fresh",), db._skip_reg)
            db._skip_reg.pop(("fresh",), None)


class TestFlushSingleFlight(unittest.IsolatedAsyncioTestCase):
    """A reader must never observe a half-written flush.

    The async conversion made ``flush_buffers`` await Mongo while holding
    the perf RLock.  RLocks are per-thread, so a *second coroutine on the
    same loop* re-enters it re-entrantly, sees the buffers already
    claimed (cleared) by the first flush, returns immediately, and then
    reads a Mongo that does not have the rows yet — memoizing 0 as the
    truth.  ``flush_buffers`` is therefore single-flight: anyone who
    arrives mid-write waits for it to land.
    """

    def setUp(self):
        super().setUp()
        self._clean()

    def _clean(self):
        for name in _PerfBase._COLLS:
            db.collection(name).delete_many({})
        db._read_cache.clear()
        db._memos.clear()
        with db._lock:
            db._buf_daily.clear()
            db._buf_hourly.clear()
            db._buf_msg.clear()
            db._buf_groups.clear()
            db._drop_msg_sums()

    async def test_reader_arriving_mid_flush_waits_for_the_write(self):
        raw = db._target                       # the real Database, not the facade
        db.count_message(CHAT, USER, TODAY, "Single Flight")
        self.assertGreater(len(db._buf_msg), 0)

        original = raw._apply_ops
        writing = asyncio.Event()

        async def slow_apply(coll, ops):
            writing.set()
            await asyncio.sleep(0.05)          # hold the flush open
            return await original(coll, ops)

        raw._apply_ops = slow_apply
        first = asyncio.create_task(raw.flush_buffers())
        try:
            await asyncio.wait_for(writing.wait(), 5)
            self.assertIsNotNone(raw._flush_inflight)   # claim taken

            # This is the read-your-writes guarantee: by the time this
            # returns, the rows must actually be in Mongo.  Without
            # single-flight it returns instantly and finds 0 rows.
            await asyncio.wait_for(raw.flush_buffers(), 5)
            self.assertIsNone(raw._flush_inflight)
            self.assertEqual(
                db.collection("daily_messages").count_documents(
                    {"chat_id": CHAT, "date": TODAY}
                ),
                1,
            )
        finally:
            raw.__dict__.pop("_apply_ops", None)
            await first

    async def test_concurrent_flushes_apply_the_increment_exactly_once(self):
        raw = db._target
        for _ in range(5):
            db.count_message(CHAT, USER, TODAY, "Single Flight")
        await asyncio.gather(raw.flush_buffers(), raw.flush_buffers())
        rows = list(db.collection("daily_messages").find(
            {"chat_id": CHAT, "date": TODAY}
        ))
        self.assertEqual(sum(int(r["messages"]) for r in rows), 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
