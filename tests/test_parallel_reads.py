"""Speed-path guarantees: parallel DB reads, cache warmup, /rank overlap.

Pins the latency work (surgical response-time changes) so a future
"optimization" cannot quietly regress them:

1. ``get_user_rank_info`` runs its global / chat / presentation scopes
   CONCURRENTLY — an interlock test that a sequential implementation
   would fail (each scope waits for a flag another scope sets).
2. ``/stats`` fires its four independent reads in one gather — the
   spies must all START before any of them FINISHES.
3. ``/rank`` starts the rank-info task before the avatar work and
   still renders the card from the awaited info.
4. ``main._warm_read_caches`` fills the hot per-chat caches at boot
   and survives any individual loader failing.

Environment isolation (BEFORE any bot import):
    * BOT_TOKEN is forced — bot.config exits without one.
    * LOCALAPPDATA points at a temp dir so runtime files are isolated.
    * unittest is already imported → bot.database selects mongomock,
      so importing main never touches a real MongoDB server.
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
from unittest import mock

# ── Environment isolation — must precede bot imports ──────────────
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["BOT_TOKEN"] = "1:TEST-TOKEN-FOR-UNIT-TESTS"
_TEST_DIR = tempfile.mkdtemp(prefix="pi_parallel_test_")
os.environ["LOCALAPPDATA"] = _TEST_DIR
atexit.register(shutil.rmtree, _TEST_DIR, ignore_errors=True)

# ── Imports (after env) ───────────────────────────────────────────
from aiofakes import FakeBot, FakeMessage, call  # noqa: E402

import main as main_mod  # noqa: E402
from bot.database import Database, db as real_db  # noqa: E402
from bot.modules import analytics as am  # noqa: E402
from bot.modules import leveling as lv  # noqa: E402


# ═════════════════════════════════════════════════════════════════
# 1. get_user_rank_info — scopes must run concurrently
# ═════════════════════════════════════════════════════════════════

def _rank_info_scenario(*, dm: bool = False, level_raises: bool = False):
    """Drive get_user_rank_info with interlocked fakes.

    ``fake_sum`` blocks until the presentation scope has run — a
    sequential implementation would wait for a flag that can never be
    set and die in wait_for's timeout.
    """
    async def _run():
        dbx = object.__new__(Database)
        reached = asyncio.Event()
        sum_calls = []

        async def fake_sum(match):
            sum_calls.append(dict(match))
            await asyncio.wait_for(reached.wait(), 2.0)
            return 50 if "chat_id" not in match else 40

        async def fake_distinct(match):
            return 2

        async def fake_grank(msgs):
            return 4

        async def fake_crank(msgs):
            return 5

        async def fake_pos(match, uid, msgs):
            return 9

        async def fake_level(uid):
            reached.set()
            if level_raises:
                raise RuntimeError("level store down")
            return {"template": 3, "global_xp": 11,
                    "streak_current": 2, "streak_best": 7}

        dbx._sum_msgs = fake_sum
        dbx._distinct_users = fake_distinct
        dbx.global_rank_for = fake_grank
        dbx.chat_rank_for = fake_crank
        dbx._rank_position = fake_pos
        dbx.get_user_level = fake_level

        info = await dbx.get_user_rank_info(7, None if dm else -1001)
        return info, sum_calls

    return asyncio.run(_run())


class TestRankInfoConcurrency(unittest.TestCase):
    def test_scopes_run_concurrently(self):
        info, _ = _rank_info_scenario()
        self.assertEqual(info["global_messages"], 50)
        self.assertEqual(info["chat_messages"], 40)
        self.assertEqual(info["global_members"], 2)
        self.assertEqual(info["chat_members"], 2)
        self.assertEqual(info["global_rank"], 4)
        self.assertEqual(info["chat_rank"], 5)
        self.assertEqual(info["global_position"], 9)
        self.assertEqual(info["chat_position"], 9)
        self.assertEqual(info["template"], 3)
        self.assertEqual(info["global_xp"], 11)
        self.assertEqual(info["streak_current"], 2)
        self.assertEqual(info["streak_best"], 7)

    def test_dm_scope_skips_chat_reads(self):
        info, sum_calls = _rank_info_scenario(dm=True)
        self.assertEqual(len(sum_calls), 1)          # global sum only
        self.assertEqual(info["chat_messages"], 0)
        self.assertIsNone(info["chat_position"])
        self.assertEqual(info["global_messages"], 50)

    def test_partial_info_when_one_scope_fails(self):
        # Old semantics kept: the failure is logged, every other scope
        # still lands its fields, defaults survive for the dead scope.
        info, _ = _rank_info_scenario(level_raises=True)
        self.assertEqual(info["global_messages"], 50)
        self.assertEqual(info["chat_messages"], 40)
        self.assertEqual(info["chat_position"], 9)
        self.assertEqual(info["template"], 3)        # presentation died
        self.assertEqual(info["global_xp"], 0)


# ═════════════════════════════════════════════════════════════════
# 2. /stats — four independent reads, one gather
# ═════════════════════════════════════════════════════════════════

def _stats_db(events, *, sum_fails=False, stats_fails=False):
    def _spy(name, result, fail=False):
        async def f(*a, **k):
            events.append(f"start:{name}")
            await asyncio.sleep(0.01)
            events.append(f"end:{name}")
            if fail:
                raise RuntimeError(f"{name} down")
            # sum_daily_messages: 2 args = current window, 3 = prior.
            if name == "sum" and len(a) >= 3:
                return result[1]
            return result[0] if isinstance(result, tuple) else result
        return f

    return SimpleNamespace(
        get_daily_stats=_spy(
            "stats",
            {"messages": 99, "new_members": 2, "left_members": 1,
             "mod_actions": 3, "spam_attempts": 4, "bind_fails": 0},
            fail=stats_fails,
        ),
        sum_daily_messages=_spy("sum", (5, 3), fail=sum_fails),
        get_active_member_count=_spy("active", 7),
    )


class TestStatsParallel(unittest.IsolatedAsyncioTestCase):
    def _admin_bot(self, chat_id=-1001, user_id=42):
        bot = FakeBot()
        bot.chat_members[(chat_id, user_id)] = SimpleNamespace(
            status="administrator"
        )
        return bot

    async def test_four_reads_start_before_any_finishes(self):
        events, sent = [], []
        msg = FakeMessage("/stats", chat_id=-1001, user_id=42)

        async def _capture(m, text, **kw):
            sent.append(text)

        with mock.patch.object(am, "db", _stats_db(events)), \
                mock.patch.object(am, "reply_text", _capture):
            await call(am.stats_command, msg, bot=self._admin_bot(), args=[])

        # gather starts every read first; serial awaits would interleave
        # start/end pairs.
        self.assertEqual(len(events), 8)
        self.assertTrue(all(e.startswith("start:") for e in events[:4]),
                        f"reads were not concurrent: {events}")
        text = sent[0]
        self.assertIn("5 (Day)", text)
        self.assertIn("▲ 66% vs prior", text)
        self.assertIn("Active Members", text)
        self.assertIn("7", text)

    async def test_volume_failure_falls_back_to_stats_messages(self):
        events, sent = [], []
        msg = FakeMessage("/stats", chat_id=-1001, user_id=42)

        async def _capture(m, text, **kw):
            sent.append(text)

        with mock.patch.object(am, "db", _stats_db(events, sum_fails=True)), \
                mock.patch.object(am, "reply_text", _capture):
            await call(am.stats_command, msg, bot=self._admin_bot(), args=[])

        text = sent[0]
        self.assertIn("99 (Day)", text)   # fallback to stats["messages"]
        self.assertNotIn("▲", text)       # prior window failed → "—"

    async def test_primary_read_failure_still_raises(self):
        events = []
        msg = FakeMessage("/stats", chat_id=-1001, user_id=42)
        with mock.patch.object(am, "db", _stats_db(events, stats_fails=True)):
            with self.assertRaises(RuntimeError):
                await call(am.stats_command, msg,
                           bot=self._admin_bot(), args=[])


# ═════════════════════════════════════════════════════════════════
# 3. /rank — info task overlaps the avatar path, card still renders
# ═════════════════════════════════════════════════════════════════

class TestRankCommandOverlap(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        lv._avatars.clear()
        self.addCleanup(lv._avatars.clear)

    async def test_card_renders_from_awaited_info(self):
        msg = FakeMessage("/rank", chat_id=-1001, user_id=42)
        bot = FakeBot()

        async def _no_photos(user_id, limit=1):
            return SimpleNamespace(photos=[])

        bot.get_user_profile_photos = _no_photos

        cards, photos, errors = [], [], []

        def _fake_card(**kw):
            cards.append(kw)
            Path(kw["output_path"]).write_bytes(b"png-bytes")
            return True

        async def _capture_photo(m, **kw):
            photos.append(kw)

        async def _capture_text(m, text, **kw):
            errors.append(text)

        with mock.patch.object(lv, "create_rank_card", _fake_card), \
                mock.patch.object(lv, "reply_photo", _capture_photo), \
                mock.patch.object(lv, "reply_text", _capture_text):
            await call(lv.rank_command, msg, bot=bot,
                       args=[], bot_data={})

        self.assertEqual(len(cards), 1, errors)
        self.assertEqual(len(photos), 1, errors)
        self.assertIn("Rank card for Tester", photos[0]["caption"])
        # Negative avatar cache was written — the avatar block ran while
        # the rank-info task was in flight.
        self.assertEqual(lv._avatars[42][1], "")


# ═════════════════════════════════════════════════════════════════
# 4. Boot cache warmup
# ═════════════════════════════════════════════════════════════════

_WARM_METHODS = (
    "get_sudo_users",
    "get_shield_settings",
    "get_blocklist",
    "get_filters",
    "get_welcome_settings",
    "get_welcome_message",
    "get_all_watch_words",
    "get_bind_settings",
)


class TestCacheWarmup(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        cache = real_db._target._read_cache
        cache.clear()
        self.addCleanup(cache.clear)

    async def test_warms_all_hot_entries(self):
        async def _fake_ids():
            return [-10042]

        with mock.patch.object(real_db._target, "get_all_chat_ids",
                               _fake_ids):
            await main_mod._warm_read_caches()

        warmed = {k[0] for k in real_db._target._read_cache}
        for method in _WARM_METHODS:
            self.assertIn(method, warmed,
                          f"{method} not warmed at boot")

    async def test_survives_chat_list_failure(self):
        async def _ok():
            return []

        async def _boom():
            raise RuntimeError("groups unreachable")

        fake = SimpleNamespace(get_sudo_users=_ok, get_all_chat_ids=_boom)
        with mock.patch("bot.database.db", fake):
            await main_mod._warm_read_caches()   # must not raise

    async def test_runs_clean_on_empty_database(self):
        await main_mod._warm_read_caches()       # no chats, no exception


if __name__ == "__main__":
    unittest.main()
