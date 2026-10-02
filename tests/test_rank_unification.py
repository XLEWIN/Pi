"""Rank unification — thresholds, derived readers, announcements, cards.

Every rank surface must derive from daily_messages + the two ladders in
bot/constants.py (100 messages per chat rank, 250 per global rank).

Run from the Pi/Pi root:

    python tests/test_rank_unification.py
    python -m unittest discover -s tests

Environment isolation (BEFORE any bot import):
    * BOT_TOKEN is forced — importing `bot` pulls bot.config, which
      exits without one.
    * LOCALAPPDATA points at a temp dir so any DB touch stays isolated.
No network: handlers run against fakes that record replies.
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

# ── Environment isolation — must precede bot imports ──────────────
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["BOT_TOKEN"] = "1:TEST-TOKEN-FOR-UNIT-TESTS"
_TEST_DIR = tempfile.mkdtemp(prefix="pi_rank_test_")
os.environ["LOCALAPPDATA"] = _TEST_DIR
atexit.register(shutil.rmtree, _TEST_DIR, ignore_errors=True)

# ── Imports (after env) ───────────────────────────────────────────
from aiogram.dispatcher.event.handler import FilterObject, HandlerObject  # noqa: E402

from aiofakes import FakeBot, call, make_callback, make_message  # noqa: E402
from bot import pipeline  # noqa: E402
from bot.constants import (  # noqa: E402
    CHAT_RANK_MESSAGES,
    GLOBAL_RANK_MESSAGES,
    HELP_MENU,
)
from bot.database import db  # noqa: E402
from bot.emojis import E, EID  # noqa: E402
from bot.modules import chatstats as cs  # noqa: E402
from bot.modules import leveling  # noqa: E402
from bot.timeutils import ist_date  # noqa: E402

TODAY = ist_date()
OLD_DAY = "2026-01-05"

CHAT = -100777001
CHAT2 = -100777002
U1, U2, U3 = 99777001, 99777002, 99777003


# ── Scaffolding ───────────────────────────────────────────────────

def _cleanup() -> None:
    # Refuse to touch anything but the in-memory test backend. Test
    # runs always select mongomock (unittest is imported before
    # bot.database), so a live cluster can never be wiped from here.
    assert "mongomock" in db.backend, (
        f"refusing to clean non-test DB: {db.backend}"
    )
    # Whole-table wipe: global ranks/members count EVERY user, so
    # per-id deletes can't isolate the assertions.
    db.collection("daily_messages").delete_many({})
    for tbl, col, ids in (
        ("groups", "chat_id", (CHAT, CHAT2)),
        ("group_members", "chat_id", (CHAT, CHAT2)),
        ("group_members", "user_id", (U1, U2, U3)),
        ("users", "user_id", (U1, U2, U3)),
        ("spam_protection", "user_id", (U1, U2, U3)),
        ("user_level", "user_id", (U1, U2, U3)),
        ("user_chat_level", "chat_id", (CHAT, CHAT2)),
    ):
        db.collection(tbl).delete_many({col: {"$in": list(ids)}})


def _seed(chat_id: int, user_id: int, n: int, day: str = TODAY) -> None:
    """Insert n messages straight into daily_messages."""
    db.collection("daily_messages").update_one(
        {"chat_id": chat_id, "user_id": user_id, "date": day},
        {"$set": {"messages": n}},
        upsert=True,
    )


def _msg(text: str = "/rank", *, chat_id: int = CHAT,
         chat_type: str = "supergroup", user_id: int = U1,
         first_name: str = "U1", username=None, **kw):
    """Command message — bot.reply helpers record into .calls as
    ("reply", …) in group chats and ("answer", …) in private."""
    msg = make_message(
        text, chat_id=chat_id, chat_type=chat_type, title="Rank Group",
        user_id=user_id, first_name=first_name, username=username, **kw,
    )
    msg.from_user.last_name = None   # count_message registers the profile
    return msg


def _edits(msg):
    """[{"text", **kw}] for every ("edit_text", …) record on msg."""
    return [{"text": t, **kw} for (k, t, kw) in msg.calls if k == "edit_text"]


def _photos(msg):
    """[{"photo", **kw}] for every ("answer_photo", …) record on msg."""
    return [{"photo": p, **kw} for (k, p, kw) in msg.calls
            if k == "answer_photo"]


def _captions(msg):
    """[{"caption", **kw}] for every ("edit_caption", …) record on msg."""
    return [{"caption": c, **kw} for (k, c, kw) in msg.calls
            if k == "edit_caption"]


def _deleted(msg) -> bool:
    return any(k == "delete" for (k, _, _) in msg.calls)


class _Base(unittest.TestCase):
    def setUp(self):
        _cleanup()

    def tearDown(self):
        _cleanup()


# ═════════════════════════════════════════════════════════════════
# Thresholds
# ═════════════════════════════════════════════════════════════════

class TestThresholds(_Base):
    def test_constants_match_request(self):
        self.assertEqual(CHAT_RANK_MESSAGES, 100)
        self.assertEqual(GLOBAL_RANK_MESSAGES, 250)

    def test_help_note_states_ladders(self):
        leveling_mod = next(m for m in HELP_MENU if m["key"] == "leveling")
        notes = " ".join(leveling_mod["notes"])
        self.assertIn("per 100 messages", notes)
        self.assertIn("per 250 messages", notes)

    def test_chat_rank_ladder(self):
        f = db.chat_rank_for
        self.assertEqual(f(0), 1)
        self.assertEqual(f(99), 1)
        self.assertEqual(f(100), 2)
        self.assertEqual(f(199), 2)
        self.assertEqual(f(200), 3)
        self.assertEqual(f(1000), 11)

    def test_global_rank_ladder(self):
        f = db.global_rank_for
        self.assertEqual(f(0), 1)
        self.assertEqual(f(249), 1)
        self.assertEqual(f(250), 2)
        self.assertEqual(f(499), 2)
        self.assertEqual(f(500), 3)
        self.assertEqual(f(2500), 11)

    def test_next_in_rank_math(self):
        n = leveling._next_in_rank
        self.assertEqual(n(0, 100), 100)
        self.assertEqual(n(99, 100), 1)
        self.assertEqual(n(100, 100), 100)   # just ranked up → full step
        self.assertEqual(n(180, 100), 20)
        self.assertEqual(n(250, 250), 250)
        self.assertEqual(n(251, 250), 249)


# ═════════════════════════════════════════════════════════════════
# get_user_rank_info — the unified reader
# ═════════════════════════════════════════════════════════════════

class TestRankInfo(_Base):
    def test_counts_come_from_daily_messages(self):
        _seed(CHAT, U1, 250)
        _seed(CHAT2, U1, 100)      # global total 350
        _seed(CHAT, U2, 100)

        info = db.get_user_rank_info(U1, CHAT)
        self.assertEqual(info["chat_messages"], 250)
        self.assertEqual(info["chat_rank"], 3)          # 250//100 + 1
        self.assertEqual(info["chat_position"], 1)
        self.assertEqual(info["chat_members"], 2)
        self.assertEqual(info["global_messages"], 350)
        self.assertEqual(info["global_rank"], 2)        # 350//250 + 1
        self.assertEqual(info["global_position"], 1)
        self.assertEqual(info["global_members"], 2)

    def test_unranked_user_has_none_positions(self):
        _seed(CHAT, U2, 100)
        info = db.get_user_rank_info(U3, CHAT)
        self.assertEqual(info["chat_messages"], 0)
        self.assertEqual(info["chat_rank"], 1)
        self.assertIsNone(info["chat_position"])
        self.assertEqual(info["chat_members"], 1)       # U2 is ranked
        self.assertIsNone(info["global_position"])
        self.assertEqual(info["global_members"], 1)

    def test_tie_breaks_by_user_id(self):
        _seed(CHAT, U1, 100)
        _seed(CHAT, U2, 100)
        first = db.get_user_rank_info(U1, CHAT)
        second = db.get_user_rank_info(U2, CHAT)
        self.assertEqual(first["chat_position"], 1)
        self.assertEqual(second["chat_position"], 2)

    def test_sums_multiple_days_like_rankings(self):
        _seed(CHAT, U1, 60, TODAY)
        _seed(CHAT, U1, 40, OLD_DAY)
        info = db.get_user_rank_info(U1, CHAT)
        self.assertEqual(info["chat_messages"], 100)
        self.assertEqual(info["chat_rank"], 2)
        # …identical to what /rankings reads:
        top = db.get_chat_top(CHAT)
        self.assertEqual(top[0]["total_messages"], 100)

    def test_dm_scope_skips_chat_fields(self):
        _seed(CHAT, U1, 150)
        info = db.get_user_rank_info(U1, None)
        self.assertEqual(info["chat_messages"], 0)
        self.assertIsNone(info["chat_position"])
        self.assertEqual(info["global_messages"], 150)
        self.assertEqual(info["global_rank"], 1)        # <250 → rank 1

    def test_streak_and_template_carried(self):
        _seed(CHAT, U1, 5)
        db.add_message_xp(U1, CHAT)
        info = db.get_user_rank_info(U1, CHAT)
        self.assertEqual(info["streak_current"], 1)
        self.assertGreater(info["global_xp"], 0)
        self.assertIn(info["template"], range(1, 20))


# ═════════════════════════════════════════════════════════════════
# /leaderboard — same counts/order as /rankings, plus derived rank
# ═════════════════════════════════════════════════════════════════

class TestLeaderboard(_Base):
    def test_orders_and_ranks_like_rankings(self):
        _seed(CHAT, U1, 200)
        _seed(CHAT, U2, 150)
        _seed(CHAT, U3, 100)

        lb = db.get_leaderboard(CHAT, limit=10)
        self.assertEqual([r["user_id"] for r in lb], [U1, U2, U3])
        self.assertEqual([r["messages"] for r in lb], [200, 150, 100])
        self.assertEqual([r["rank"] for r in lb], [3, 2, 2])

        # Same order as the /rankings reader (single source).
        top = db.get_chat_top(CHAT, limit=10)
        self.assertEqual(
            [r["user_id"] for r in top], [r["user_id"] for r in lb]
        )

    def test_empty_chat_gives_empty_board(self):
        self.assertEqual(db.get_leaderboard(CHAT), [])


# ═════════════════════════════════════════════════════════════════
# XP path writes no counters (daily_messages / user_chat_level / cols)
# ═════════════════════════════════════════════════════════════════

class TestXpPath(_Base):
    def test_add_message_xp_writes_no_counters(self):
        result = db.add_message_xp(U1, CHAT)
        self.assertEqual(result, (0, 0, False))

        n = db.collection("daily_messages").count_documents({"user_id": U1})
        self.assertEqual(n, 0, "XP path must not count messages")

        n = db.collection("user_chat_level").count_documents({"user_id": U1})
        self.assertEqual(n, 0, "legacy chat-level table must stay untouched")

        lvl = db.get_user_level(U1)
        self.assertEqual(lvl.get("global_messages", 0), 0,
                         "legacy global_messages column must stay frozen")
        self.assertGreater(lvl.get("global_xp", 0), 0, "XP still accrues")

    def test_streak_uses_ist_today(self):
        db.add_message_xp(U1, CHAT)
        lvl = db.get_user_level(U1)
        self.assertEqual(lvl.get("streak_current"), 1)
        self.assertEqual(lvl.get("last_message_date"), TODAY)


# ═════════════════════════════════════════════════════════════════
# Rank-up announcements (100 chat / 250 global)
# ═════════════════════════════════════════════════════════════════

class TestRankUpLines(_Base):
    M = '<a href="tg://user?id=1">U1</a>'

    def test_chat_threshold(self):
        lines = cs._rank_up_lines(100, 100, self.M)
        self.assertEqual(len(lines), 1)
        self.assertIn("Rank 2", lines[0])
        self.assertIn("in this group", lines[0])
        self.assertNotIn("Global", lines[0])

    def test_global_threshold(self):
        lines = cs._rank_up_lines(101, 250, self.M)
        self.assertEqual(len(lines), 1)
        self.assertIn("Global Rank 2", lines[0])

    def test_both_thresholds_in_one_call(self):
        # 500 is a multiple of both ladders → both lines in one reply.
        lines = cs._rank_up_lines(500, 500, self.M)
        self.assertEqual(len(lines), 2)
        self.assertIn("Rank 6", lines[0])            # 500//100 + 1
        self.assertIn("Global Rank 3", lines[1])     # 500//250 + 1

    def test_below_thresholds_announce_nothing(self):
        self.assertEqual(cs._rank_up_lines(99, 249, self.M), [])
        self.assertEqual(cs._rank_up_lines(101, 251, self.M), [])

    def test_zero_never_announces(self):
        self.assertEqual(cs._rank_up_lines(0, 0, self.M), [])


# ═════════════════════════════════════════════════════════════════
# Handler: milestone + rank-up land in ONE combined reply
# ═════════════════════════════════════════════════════════════════

class TestCombinedAnnouncement(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _cleanup()
        db.add_user(U1, "u1", "U1", None)  # pre-registered → no log path

    def tearDown(self):
        _cleanup()

    async def test_milestone_and_rank_up_share_one_reply(self):
        _seed(CHAT, U1, 99)
        msg = _msg("the hundredth")
        await call(cs.count_message, msg)

        self.assertEqual(len(msg.sent_texts), 1, "one reply, not two")
        self.assertEqual(msg.last[0], "reply")
        text = msg.last[1]
        self.assertIn("Rank 2", text)                       # rank-up line
        self.assertIn("100 messages reached today!", text)   # milestone
        self.assertEqual(msg.last[2].get("parse_mode"), "HTML")

    async def test_rank_up_only_no_milestone(self):
        _seed(CHAT, U1, 199)
        msg = _msg("two hundredth")
        await call(cs.count_message, msg)

        self.assertEqual(len(msg.sent_texts), 1)
        text = msg.last[1]
        self.assertIn("Rank 3", text)  # 200//100 + 1
        self.assertNotIn("messages reached today", text)

    async def test_quiet_message_stays_silent(self):
        _seed(CHAT, U1, 151)
        msg = _msg("quiet")
        await call(cs.count_message, msg)
        self.assertEqual(msg.sent_texts, [])


# ═════════════════════════════════════════════════════════════════
# Commands — same info everywhere
# ═════════════════════════════════════════════════════════════════

class TestNextLevelCommand(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _cleanup()

    def tearDown(self):
        _cleanup()

    async def test_group_shows_both_ladders(self):
        _seed(CHAT, U1, 180)
        msg = _msg("/nextlevel")
        await call(leveling.nextlevel_command, msg)

        text = msg.last[1]
        self.assertIn("Rank Progress", text)
        # Chat ladder: 180//100+1 = 2 → 3, 20 msgs to go, 80/100 bar
        self.assertIn("Chat: <b>Rank 2 → 3</b>", text)
        self.assertIn("<b>20</b> messages to go", text)
        self.assertIn("80/100", text)
        self.assertIn("▰" * 8 + "▱" * 2, text)
        self.assertIn("(80%)", text)
        # Global ladder: 180 < 250 → rank 1 → 2, 70 msgs to go, 180/250
        self.assertIn("Global: <b>Rank 1 → 2</b>", text)
        self.assertIn("<b>70</b> messages to go", text)
        self.assertIn("180/250", text)
        self.assertIn("(72%)", text)
        self.assertIn("180 in this group · 180 total", text)

    async def test_dm_shows_global_only(self):
        _seed(CHAT2, U1, 250)
        msg = _msg("/nextlevel", chat_type="private", chat_id=U1)
        await call(leveling.nextlevel_command, msg)

        text = msg.last[1]
        self.assertIn("Global: <b>Rank 2 → 3</b>", text)
        self.assertNotIn("Chat:", text)
        self.assertIn("chat rank counts per group", text)

    async def test_reply_carries_colored_nextlevel_button(self):
        msg = _msg("/nextlevel")
        await call(leveling.nextlevel_command, msg)

        markup = msg.last[2].get("reply_markup")
        self.assertIsNotNone(markup, "button must be attached")
        btn = markup.inline_keyboard[0][0]
        self.assertEqual(btn.text, "My Next Level")
        self.assertEqual(btn.callback_data, "nextlevel:me")
        self.assertEqual(btn.style, "primary")
        self.assertEqual(btn.icon_custom_emoji_id, EID.FIRE)


class TestLeaderboardCommand(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _cleanup()

    def tearDown(self):
        _cleanup()

    async def test_lines_match_rankings_counts(self):
        _seed(CHAT, U1, 200)
        _seed(CHAT, U2, 150)
        msg = _msg("/leaderboard")
        await call(leveling.leaderboard_command, msg)

        lines = msg.last[1].splitlines()
        ranked = [ln for ln in lines if "msgs" in ln]
        self.assertEqual(len(ranked), 2)
        self.assertIn("Rank 3 · 200 msgs", ranked[0])
        self.assertIn("Rank 2 · 150 msgs", ranked[1])


class TestMytopHeader(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _cleanup()

    def tearDown(self):
        _cleanup()

    async def test_header_is_name_plus_board_only(self):
        _seed(CHAT, U1, 150)
        _seed(CHAT2, U1, 100)

        user = SimpleNamespace(id=U1, username="u1", first_name="U1")
        text, _markup = cs._mytop_board(user, "overall")

        self.assertIn(cs._big("Top Groups"), text)
        # Rank lines were removed from /mytop — ranks live on /rank,
        # /profile and /info.
        self.assertNotIn("Chat: Rank", text)
        self.assertNotIn("Global: Rank", text)

    async def test_empty_state_has_no_rank_lines(self):
        user = SimpleNamespace(id=U3, username=None, first_name="Nobody")
        text, markup = cs._mytop_board(user, "overall")
        self.assertNotIn("Chat: Rank", text)
        self.assertNotIn("Global: Rank", text)
        self.assertIn("No messages found", text)
        self.assertIsNone(markup)


class TestRankValueFormatter(_Base):
    def test_rank_value_format(self):
        from bot.responses import rank_value

        self.assertEqual(rank_value(5, 3, 57, 480),
                         "Rank 5 · #3/57 · 480 msgs")
        self.assertEqual(rank_value(1, None, 0, 0),
                         "Rank 1 · #—/0 · 0 msgs")


# ═════════════════════════════════════════════════════════════════
# /info and /profile — same unified rank fields
# ═════════════════════════════════════════════════════════════════

class _InfoBot:
    async def get_chat(self, chat_id):
        raise RuntimeError("no chat info in tests")

    async def get_user_profile_photos(self, user_id, limit=1, **kw):
        return SimpleNamespace(total_count=0)


def _target(user_id: int = U1):
    return SimpleNamespace(
        id=user_id, first_name="U1", last_name=None,
        username=None, is_bot=False, full_name="U1",
    )


class TestInfoCard(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _cleanup()

    def tearDown(self):
        _cleanup()

    async def test_group_card_has_messages_and_both_ranks(self):
        from bot.modules import users as users_mod

        _seed(CHAT, U1, 150)
        text = await users_mod._build_info_text(_InfoBot(), _target(), chat_id=CHAT)

        self.assertIn("Messages:", text)
        self.assertIn("Chat Rank:", text)
        self.assertIn("Global Rank:", text)
        self.assertIn("Rank 2 · #1/1 · 150 msgs", text)

    async def test_dm_card_omits_chat_rank(self):
        from bot.modules import users as users_mod

        _seed(CHAT, U1, 150)
        text = await users_mod._build_info_text(_InfoBot(), _target(), chat_id=None)

        self.assertNotIn("Chat Rank:", text)
        self.assertIn("Global Rank:", text)
        self.assertIn("Messages:", text)


class TestProfileCommand(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _cleanup()

    def tearDown(self):
        _cleanup()

    async def test_profile_shows_unified_ranks(self):
        from bot.modules import profile as profile_mod

        _seed(CHAT, U1, 150)
        msg = _msg("/profile")
        await call(profile_mod.profile_command, msg, bot=_InfoBot(), args=[])

        text = msg.last[1]
        self.assertIn("User Profile", text)
        self.assertIn("Reputation:", text)
        self.assertIn("Messages:", text)
        self.assertIn("Chat Rank:", text)
        self.assertIn("Global Rank:", text)
        self.assertIn("Rank 2 · #1/1 · 150 msgs", text)

    async def test_profile_in_dm_skips_chat_rank(self):
        from bot.modules import profile as profile_mod

        _seed(CHAT, U1, 150)
        msg = _msg("/profile", chat_type="private", chat_id=U1)
        await call(profile_mod.profile_command, msg, bot=_InfoBot(), args=[])

        text = msg.last[1]
        self.assertNotIn("Chat Rank:", text)
        self.assertIn("Global Rank:", text)


# ═════════════════════════════════════════════════════════════════
# Rank-card caption + See Your Rank / My Next Level buttons
# ═════════════════════════════════════════════════════════════════

class TestRankCardCaption(unittest.TestCase):
    def test_header_line_only(self):
        cap = leveling._rank_caption("Lewin")
        self.assertEqual(cap, f"{E.CROWN} <b>Rank card for Lewin</b>")
        self.assertNotIn("Chat:", cap)
        self.assertNotIn("Global:", cap)

    def test_name_is_html_escaped(self):
        cap = leveling._rank_caption('A<B & "co"')
        self.assertIn("Rank card for A&lt;B &amp; &quot;co&quot;", cap)


class TestSeeRankButton(unittest.TestCase):
    def test_deep_link_opens_dm_and_starts_bot(self):
        markup = leveling._see_rank_keyboard(
            None, {"username": "PiModulerBot"}
        )
        btn = markup.inline_keyboard[0][0]
        self.assertEqual(btn.text, "See Your Rank")
        self.assertEqual(btn.url, "https://t.me/PiModulerBot?start=rank")
        self.assertEqual(btn.style, "primary")
        self.assertEqual(btn.icon_custom_emoji_id, EID.CROWN)

    def test_username_falls_back_to_bot(self):
        bot = SimpleNamespace(username="FallbackBot")
        markup = leveling._see_rank_keyboard(bot, {})
        self.assertIn("t.me/FallbackBot", markup.inline_keyboard[0][0].url)


class TestProgressBar(unittest.TestCase):
    def test_bar_and_percent(self):
        self.assertEqual(leveling._bar(0, 100), ("▱" * 10, 0))
        self.assertEqual(leveling._bar(80, 100), ("▰" * 8 + "▱" * 2, 80))
        self.assertEqual(leveling._bar(100, 100), ("▰" * 10, 100))
        self.assertEqual(leveling._bar(180, 250), ("▰" * 7 + "▱" * 3, 72))


class TestNextLevelButton(unittest.IsolatedAsyncioTestCase):
    """Clicking 'My Next Level' sends a fresh card as a NEW message."""

    def setUp(self):
        _cleanup()

    def tearDown(self):
        _cleanup()

    def _click(self, *, chat_id: int = CHAT, chat_type: str = "supergroup"):
        original = _msg(            # the bot message the button sits on
            "/nextlevel", chat_id=chat_id, chat_type=chat_type
        )
        query = SimpleNamespace(
            message=original,
            from_user=SimpleNamespace(id=U1, first_name="U1"),
            answered=False,
        )

        async def _answer(*a, **kw):
            query.answered = True

        query.answer = _answer
        return query, original

    async def test_click_replies_with_fresh_group_card(self):
        _seed(CHAT, U1, 180)
        query, original = self._click()
        await call(leveling.nextlevel_callback, query)

        self.assertTrue(query.answered)
        self.assertEqual(len(original.sent_texts), 1)
        reply = original.last[1]
        self.assertIn("Rank Progress", reply)
        self.assertIn("Chat: <b>Rank 2 → 3</b>", reply)
        markup = original.last[2].get("reply_markup")
        self.assertEqual(markup.inline_keyboard[0][0].callback_data,
                         "nextlevel:me")

    async def test_click_in_dm_shows_global_only(self):
        _seed(CHAT2, U1, 250)
        query, original = self._click(
            chat_id=U1, chat_type="private"
        )
        await call(leveling.nextlevel_callback, query)

        text = original.last[1]
        self.assertIn("Global: <b>Rank 2 → 3</b>", text)
        self.assertNotIn("Chat:", text)


class TestLevelingWiring(unittest.TestCase):
    def test_nextlevel_callback_registered_with_pattern(self):
        pipeline.clear()
        leveling.setup()

        entries = [
            e for e in pipeline.snapshot()
            if e.fn is leveling.nextlevel_callback
        ]
        self.assertEqual(len(entries), 1, "button handler must be registered")

        def matches(data):
            handler = HandlerObject(
                callback=entries[0].fn, filters=[FilterObject(entries[0].flt)]
            )
            ok, _ = asyncio.run(handler.check(make_callback(data)))
            return ok

        self.assertTrue(matches("nextlevel:me"))
        self.assertFalse(matches("nextlevel:other"))
        self.assertFalse(matches("info:me"))


# ═════════════════════════════════════════════════════════════════
# /template — preview generation + branded reply
# ═════════════════════════════════════════════════════════════════

class TestTemplatePreview(unittest.TestCase):
    def test_preview_returns_png_bytes(self):
        """Regression: generate_profile_card returns BytesIO — the old
        code called .resize() on it directly, so every /template run
        failed with "'_io.BytesIO' object has no attribute 'resize'"
        and fell back to the plain text reply.
        """
        from bot.profile_templates import generate_template_preview

        out = generate_template_preview(
            name="U1", username="u1", level=1, rank="#1",
            chat_messages="10", global_messages="20",
        )
        data = out.read()
        out.close()
        self.assertTrue(data.startswith(b"\x89PNG"), "must be a real PNG")
        self.assertGreater(len(data), 1000)


class TestTemplateCommand(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _cleanup()
        db.add_user(U1, "u1", "U1", None)

    def tearDown(self):
        _cleanup()

    async def test_dm_reply_is_main_screen_with_section_buttons(self):
        from bot.modules import template as template_mod

        _seed(CHAT, U1, 150)
        msg = _msg("/template", chat_type="private", chat_id=U1)
        await call(template_mod.template_command, msg)

        text = msg.last[1]
        self.assertIn("Rank Templates", text)
        self.assertIn("Active:", text)             # current selection
        self.assertIn("Pick a category below", text)
        self.assertIn("Usage:", text)
        self.assertIn("/template &lt;number&gt;", text)
        self.assertNotIn("photo", msg.last[2],
                         "/template must not send a picture")

        markup = msg.last[2]["reply_markup"]
        flat = [b for row in markup.inline_keyboard for b in row]
        self.assertEqual(len(flat), 2, "main screen = two section buttons")
        self.assertEqual(flat[0].callback_data, "template:sec:free")
        self.assertEqual(flat[0].style, "primary")
        self.assertIn("Free (6)", flat[0].text)
        self.assertEqual(flat[1].callback_data, "template:sec:fictional")
        self.assertEqual(flat[1].style, "success")
        self.assertIn("Fictional (11)", flat[1].text)

    async def test_free_section_lists_templates_with_lock_state(self):
        from bot.modules import template as template_mod

        _seed(CHAT, U1, 150)  # 150 global messages: #1 needs 1,000 → locked
        msg = _msg("original", chat_type="private", chat_id=U1)
        query = make_callback("template:sec:free", message=msg, user_id=U1)
        await call(template_mod.template_callback, query)

        photos = _photos(msg)
        self.assertEqual(len(photos), 1,
                         "section screen must send the preview photo")
        self.assertEqual(_edits(msg), [], "photo replaces the text, no edit")
        self.assertTrue(_deleted(msg), "old text message must be removed")
        media = photos[0]["photo"]
        self.assertTrue(str(getattr(media, "path", "")).endswith(
            os.path.join("assets", "preview_free.jpg")),
            "Free section uses its collage preview")
        self.assertTrue(os.path.exists(template_mod.PREVIEWS["free"]),
                        "preview asset ships with the bot")

        text = photos[0]["caption"]
        self.assertIn("Rank Templates — Free", text)
        self.assertIn("1. AMBER GLOW", text)
        self.assertIn("6. NEON GREEN", text)
        self.assertNotIn("WINGED", text,
                         "#7 was removed from the free section")
        self.assertNotIn("7.", text, "no 7th entry remains in free")
        self.assertIn("1,000 GM", text)       # milestone hint on locked #1
        self.assertIn("active", text)

        flat = [b for row in photos[0]["reply_markup"].inline_keyboard
                for b in row]
        self.assertEqual(len(flat), 7, "6 templates + Back")
        self.assertEqual(flat[0].callback_data, "template:1")
        self.assertEqual(flat[0].icon_custom_emoji_id, EID.LOCK)
        btn3 = next(b for b in flat if b.callback_data == "template:3")
        self.assertEqual(btn3.icon_custom_emoji_id, EID.SPARKLE)
        self.assertEqual(flat[-1].callback_data, "template:sec:main")

    async def test_fictional_section_stays_locked_with_lock_icons(self):
        from bot.modules import template as template_mod

        msg = _msg("original", chat_type="private", chat_id=U1)
        query = make_callback("template:sec:fictional", message=msg,
                              user_id=U1)
        await call(template_mod.template_callback, query)

        photos = _photos(msg)
        self.assertEqual(len(photos), 1)
        self.assertTrue(_deleted(msg), "old text message must be removed")
        media = photos[0]["photo"]
        self.assertTrue(str(getattr(media, "path", "")).endswith(
            os.path.join("assets", "preview_fictional.jpg")),
            "Fictional section uses its collage preview")

        text = photos[0]["caption"]
        self.assertIn("Rank Templates — Fictional", text)
        self.assertIn("8. CRIMSON FEATHER", text)
        self.assertIn("18. SAMURAI INK", text)
        self.assertIn("group", text)   # #8's requirement mark
        self.assertIn("bio", text)     # bio-tagged templates
        self.assertIn("name", text)    # name-tagged templates

        flat = [b for row in photos[0]["reply_markup"].inline_keyboard
                for b in row]
        self.assertEqual(len(flat), 12, "11 templates + Back")
        for btn in flat[:-1]:
            self.assertEqual(btn.icon_custom_emoji_id, EID.LOCK)

    async def test_back_from_photo_screen_sends_main_text(self):
        """Back on a photo section deletes the collage and posts main."""
        from bot.modules import template as template_mod

        _seed(CHAT, U1, 150)
        msg = _msg("collage", chat_type="private", chat_id=U1,
                   photo=[{"file_id": "x"}])
        query = make_callback("template:sec:main", message=msg, user_id=U1)
        await call(template_mod.template_callback, query)

        self.assertTrue(_deleted(msg), "photo screen must be removed")
        self.assertEqual(_photos(msg), [], "main stays a text screen")
        answers = [t for (k, t, kw) in msg.calls if k == "answer"]
        self.assertEqual(len(answers), 1, "main screen sent as new text")
        self.assertIn("Rank Templates", answers[0])
        self.assertIn("Pick a category below", answers[0])
        ans = next(kw for (k, t, kw) in msg.calls if k == "answer")
        flat = [b for row in ans["reply_markup"].inline_keyboard for b in row]
        self.assertEqual(len(flat), 2)

    async def test_direct_number_sets_unlocked_template(self):
        from bot.modules import template as template_mod

        msg = _msg("/template", chat_type="private", chat_id=U1)
        await call(template_mod.template_command, msg, args=["3"])

        text = msg.last[1]
        self.assertIn("Template Selected", text)
        self.assertIn("PURPLE NEON", text)
        self.assertEqual(db.get_user_rank_info(U1)["template"], 3)

    async def test_direct_number_refuses_locked_template(self):
        from bot.modules import template as template_mod

        msg = _msg("/template", chat_type="private", chat_id=U1)
        await call(template_mod.template_command, msg, args=["1"])

        text = msg.last[1]
        self.assertIn("locked", text)
        self.assertIn("1,000", text)
        self.assertEqual(db.get_user_rank_info(U1)["template"], 3,
                         "locked template must not be equipped")

    async def test_group_is_redirected_to_dm(self):
        from bot.modules import template as template_mod

        msg = _msg("/template", chat_type="supergroup", chat_id=CHAT)
        await call(template_mod.template_command, msg)

        text = msg.last[1]
        self.assertIn("DM", text)
        self.assertNotIn("photo", msg.last[2])

    async def test_removed_number_is_unknown(self):
        """#7 (WINGED ARCHER) was dropped — the number no longer resolves."""
        from bot.modules import template as template_mod

        msg = _msg("/template", chat_type="private", chat_id=U1)
        await call(template_mod.template_command, msg, args=["7"])

        text = msg.last[1]
        self.assertIn("Unknown template", text)
        self.assertIn("pick 1-18", text)
        self.assertEqual(db.get_user_rank_info(U1)["template"], 3,
                         "removed template must not be equipped")


class TestTemplateCallback(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _cleanup()
        db.add_user(U1, "u1", "U1", None)

    def tearDown(self):
        _cleanup()

    async def test_selection_sets_template_and_styled_caption(self):
        from bot.modules import template as template_mod

        msg = _msg("original")
        query = make_callback("template:3", message=msg, user_id=U1)
        await call(template_mod.template_callback, query)

        self.assertEqual(db.get_user_rank_info(U1)["template"], 3)
        self.assertIn("PURPLE NEON", query.answers[0]["text"] or "")
        edits = _edits(msg)
        self.assertEqual(len(edits), 1, "stale-tap-safe edit must be sent")
        self.assertIn("Template Selected", edits[0]["text"])
        self.assertIn("PURPLE NEON", edits[0]["text"])
        self.assertIn("Change anytime with /template", edits[0]["text"])

    async def test_selection_on_photo_screen_edits_caption(self):
        """Choosing a template on the preview photo edits its caption."""
        from bot.modules import template as template_mod

        msg = _msg("collage", photo=[{"file_id": "x"}])
        query = make_callback("template:3", message=msg, user_id=U1)
        await call(template_mod.template_callback, query)

        self.assertEqual(db.get_user_rank_info(U1)["template"], 3)
        self.assertIn("PURPLE NEON", query.answers[0]["text"] or "")
        caps = _captions(msg)
        self.assertEqual(len(caps), 1,
                         "photo confirmation must edit the caption")
        self.assertEqual(_edits(msg), [], "no edit_text on a photo message")
        self.assertIn("Template Selected", caps[0]["caption"])
        self.assertIn("PURPLE NEON", caps[0]["caption"])
        self.assertIn("Change anytime with /template", caps[0]["caption"])

    async def test_locked_milestone_tap_refused(self):
        from bot.modules import template as template_mod

        msg = _msg("original")
        query = make_callback("template:1", message=msg, user_id=U1)
        await call(template_mod.template_callback, query)

        ans = query.answers[0]
        self.assertTrue(ans["show_alert"], "lock reason must alert")
        self.assertIn("1,000", ans["text"])
        self.assertEqual(db.get_user_rank_info(U1)["template"], 3)
        self.assertEqual(_edits(msg), [], "no confirmation on refusal")

    async def test_group_requirement_refused_without_group(self):
        from bot.modules import template as template_mod

        msg = _msg("original")
        query = make_callback("template:8", message=msg, user_id=U1)
        await call(template_mod.template_callback, query)

        ans = query.answers[0]
        self.assertTrue(ans["show_alert"])
        self.assertIn("group", ans["text"])
        self.assertEqual(db.get_user_rank_info(U1)["template"], 3)

    async def test_wear_equips_owner_template(self):
        from unittest import mock

        from bot.modules import template as template_mod

        msg = _msg("/wear", chat_type="private", chat_id=U1)
        with mock.patch.object(template_mod, "settings",
                               SimpleNamespace(owner_id=U1)):
            await call(template_mod.wear_command, msg)

        self.assertEqual(db.get_user_rank_info(U1)["template"], 19)
        text = msg.last[1]
        self.assertIn("Template Worn", text)
        self.assertIn("PHANTOM RIDER", text)

    async def test_wear_refuses_non_owner(self):
        from unittest import mock

        from bot.modules import template as template_mod

        msg = _msg("/wear", chat_type="private", chat_id=U1)
        with mock.patch.object(template_mod, "settings",
                               SimpleNamespace(owner_id=0)):
            await call(template_mod.wear_command, msg)

        self.assertIn("owner exclusive", msg.last[1])
        self.assertEqual(db.get_user_rank_info(U1)["template"], 3,
                         "template must not change")


class TestFictionalRequirements(unittest.IsolatedAsyncioTestCase):
    """Live requirement detection for the fictional section.

    * #8  — user shares a group with the bot (added there)
    * #9, #11, #13–#18 — Telegram bio contains @PIModulerBot
    * #10, #12         — display name contains @PI
    """

    def setUp(self):
        _cleanup()
        db.add_user(U1, "u1", "U1", None)

    def tearDown(self):
        _cleanup()

    async def test_bio_does_not_unlock_name_template(self):
        from bot.modules import template as template_mod

        bot = FakeBot()
        bot.chats[U1] = SimpleNamespace(id=U1, type="private",
                                        bio="code @PIModulerBot bot")
        msg = _msg("original", chat_type="private", chat_id=U1)
        query = make_callback("template:10", message=msg, user_id=U1)
        await call(template_mod.template_callback, query, bot=bot)

        ans = query.answers[0]
        self.assertTrue(ans["show_alert"])
        self.assertIn("name", ans["text"])
        self.assertEqual(db.get_user_rank_info(U1)["template"], 3)

    async def test_bio_tag_unlocks_bio_template(self):
        from bot.modules import template as template_mod

        bot = FakeBot()
        bot.chats[U1] = SimpleNamespace(id=U1, type="private",
                                        bio="just a bio @pimodulerbot enjoy")
        msg = _msg("original", chat_type="private", chat_id=U1)
        query = make_callback("template:9", message=msg, user_id=U1)
        await call(template_mod.template_callback, query, bot=bot)

        self.assertEqual(db.get_user_rank_info(U1)["template"], 9)
        self.assertIn("MONO MANGA", query.answers[0]["text"] or "")

    async def test_group_member_unlocks_group_template(self):
        from bot.modules import template as template_mod

        db.collection("groups").update_one(
            {"chat_id": CHAT},
            {"$set": {"chat_id": CHAT, "chat_title": "Rank Group",
                      "is_active": 1}},
            upsert=True,
        )
        db.collection("group_members").update_one(
            {"chat_id": CHAT, "user_id": U1},
            {"$set": {"chat_id": CHAT, "user_id": U1, "role": "member"}},
            upsert=True,
        )
        bot = FakeBot()   # no bio — only the group requirement is met
        msg = _msg("original", chat_type="private", chat_id=U1)
        query = make_callback("template:8", message=msg, user_id=U1)
        await call(template_mod.template_callback, query, bot=bot)

        self.assertEqual(db.get_user_rank_info(U1)["template"], 8)
        self.assertIn("CRIMSON FEATHER", query.answers[0]["text"] or "")

    async def test_name_tag_unlocks_name_template(self):
        from bot.modules import template as template_mod

        msg = _msg("/template", chat_type="private", chat_id=U1,
                   first_name="@PI Tester")
        await call(template_mod.template_command, msg, args=["10"])

        text = msg.last[1]
        self.assertIn("Template Selected", text)
        self.assertIn("MONO HUD", text)
        self.assertEqual(db.get_user_rank_info(U1)["template"], 10)


if __name__ == "__main__":
    unittest.main()
