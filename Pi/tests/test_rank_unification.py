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
    # Refuse to touch anything but a temp-dir DB. In a shared discover
    # run another test file may have imported bot.database first (its
    # mkdtemp dir wins) — that is still a throwaway copy, including the
    # one-time legacy migration. The live DB (%LOCALAPPDATA%\PiBot) and
    # the project-folder legacy DB must never be wiped.
    db_path = str(db.db_path)
    assert (
        db_path.startswith(tempfile.gettempdir())
        and not db_path.startswith(str(ROOT))
    ), f"refusing to clean non-test DB: {db_path}"
    conn = db.connection
    # Whole-table wipe: global ranks/members count EVERY user, so
    # per-id deletes can't isolate the assertions.
    conn.execute("DELETE FROM daily_messages")
    for tbl, col, ids in (
        ("groups", "chat_id", (CHAT, CHAT2)),
        ("group_members", "chat_id", (CHAT, CHAT2)),
        ("group_members", "user_id", (U1, U2, U3)),
        ("users", "user_id", (U1, U2, U3)),
        ("spam_protection", "user_id", (U1, U2, U3)),
        ("user_level", "user_id", (U1, U2, U3)),
        ("user_chat_level", "chat_id", (CHAT, CHAT2)),
    ):
        conn.execute(
            f"DELETE FROM {tbl} WHERE {col} IN ({','.join('?' * len(ids))})",
            ids,
        )
    conn.commit()


def _seed(chat_id: int, user_id: int, n: int, day: str = TODAY) -> None:
    """Insert n messages straight into daily_messages."""
    db.connection.execute(
        """
        INSERT INTO daily_messages (chat_id, user_id, date, messages)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(chat_id, user_id, date)
        DO UPDATE SET messages = ?
        """,
        (chat_id, user_id, day, n, n),
    )
    db.connection.commit()


class _Msg:
    """Command message — records reply_text/reply_photo calls."""

    def __init__(self, text: str = "/rank") -> None:
        self.text = text
        self.reply_to_message = None
        self.replies: list = []

    async def reply_text(self, text, **kw):
        self.replies.append({"text": text, **kw})
        return SimpleNamespace(message_id=1)

    async def reply_photo(self, photo=None, **kw):
        self.replies.append({"photo": photo, **kw})
        return SimpleNamespace(message_id=1)

    async def delete(self):
        return None

    @property
    def last(self):
        return self.replies[-1] if self.replies else None


def _update(msg: _Msg, *, chat_id: int = CHAT, chat_type: str = "supergroup",
            user_id: int = U1, first_name: str = "U1"):
    chat = SimpleNamespace(id=chat_id, type=chat_type, title="Rank Group")
    user = SimpleNamespace(
        id=user_id, is_bot=False, first_name=first_name,
        username=None, last_name=None,
    )
    return SimpleNamespace(
        message=msg, effective_message=msg, effective_chat=chat,
        effective_user=user,
    )


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
        self.assertIn(info["template"], range(1, 7))


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

        n = db.connection.execute(
            "SELECT COUNT(*) FROM daily_messages WHERE user_id = ?", (U1,)
        ).fetchone()[0]
        self.assertEqual(n, 0, "XP path must not count messages")

        n = db.connection.execute(
            "SELECT COUNT(*) FROM user_chat_level WHERE user_id = ?", (U1,)
        ).fetchone()[0]
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
        msg = _Msg("the hundredth")
        await cs.count_message(_update(msg), None)

        self.assertEqual(len(msg.replies), 1, "one reply, not two")
        text = msg.last["text"]
        self.assertIn("Rank 2", text)                       # rank-up line
        self.assertIn("100 messages reached today!", text)   # milestone
        self.assertEqual(msg.last.get("parse_mode"), "HTML")

    async def test_rank_up_only_no_milestone(self):
        _seed(CHAT, U1, 199)
        msg = _Msg("two hundredth")
        await cs.count_message(_update(msg), None)

        self.assertEqual(len(msg.replies), 1)
        text = msg.last["text"]
        self.assertIn("Rank 3", text)  # 200//100 + 1
        self.assertNotIn("messages reached today", text)

    async def test_quiet_message_stays_silent(self):
        _seed(CHAT, U1, 151)
        msg = _Msg("quiet")
        await cs.count_message(_update(msg), None)
        self.assertEqual(msg.replies, [])


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
        msg = _Msg("/nextlevel")
        await leveling.nextlevel_command(_update(msg), None)

        text = msg.last["text"]
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
        msg = _Msg("/nextlevel")
        upd = _update(msg, chat_type="private", chat_id=U1)
        await leveling.nextlevel_command(upd, None)

        text = msg.last["text"]
        self.assertIn("Global: <b>Rank 2 → 3</b>", text)
        self.assertNotIn("Chat:", text)
        self.assertIn("chat rank counts per group", text)

    async def test_reply_carries_colored_nextlevel_button(self):
        msg = _Msg("/nextlevel")
        await leveling.nextlevel_command(_update(msg), None)

        markup = msg.last.get("reply_markup")
        self.assertIsNotNone(markup, "button must be attached")
        btn = markup.inline_keyboard[0][0]
        self.assertEqual(btn.text, "My Next Level")
        self.assertEqual(btn.callback_data, "nextlevel:me")
        self.assertEqual(btn.api_kwargs.get("style"), "primary")
        self.assertEqual(btn.api_kwargs.get("icon_custom_emoji_id"), EID.FIRE)


class TestLeaderboardCommand(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _cleanup()

    def tearDown(self):
        _cleanup()

    async def test_lines_match_rankings_counts(self):
        _seed(CHAT, U1, 200)
        _seed(CHAT, U2, 150)
        msg = _Msg("/leaderboard")
        await leveling.leaderboard_command(_update(msg), None)

        lines = msg.last["text"].splitlines()
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

        self.assertIn("Top Groups", text)
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
        msg = _Msg("/profile")
        ctx = SimpleNamespace(args=[], bot=_InfoBot())
        await profile_mod.profile_command(_update(msg), ctx)

        text = msg.last["text"]
        self.assertIn("User Profile", text)
        self.assertIn("Reputation:", text)
        self.assertIn("Messages:", text)
        self.assertIn("Chat Rank:", text)
        self.assertIn("Global Rank:", text)
        self.assertIn("Rank 2 · #1/1 · 150 msgs", text)

    async def test_profile_in_dm_skips_chat_rank(self):
        from bot.modules import profile as profile_mod

        _seed(CHAT, U1, 150)
        msg = _Msg("/profile")
        ctx = SimpleNamespace(args=[], bot=_InfoBot())
        upd = _update(msg, chat_type="private", chat_id=U1)
        await profile_mod.profile_command(upd, ctx)

        text = msg.last["text"]
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
        ctx = SimpleNamespace(
            bot_data={"username": "PiModulerBot"}, bot=None
        )
        markup = leveling._see_rank_keyboard(ctx)
        btn = markup.inline_keyboard[0][0]
        self.assertEqual(btn.text, "See Your Rank")
        self.assertEqual(btn.url, "https://t.me/PiModulerBot?start=rank")
        self.assertEqual(btn.api_kwargs.get("style"), "primary")
        self.assertEqual(btn.api_kwargs.get("icon_custom_emoji_id"), EID.CROWN)

    def test_username_falls_back_to_bot(self):
        ctx = SimpleNamespace(
            bot_data={}, bot=SimpleNamespace(username="FallbackBot")
        )
        markup = leveling._see_rank_keyboard(ctx)
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
        original = _Msg()          # the bot message the button sits on
        original.chat = SimpleNamespace(id=chat_id, type=chat_type)
        query = SimpleNamespace(
            message=original,
            from_user=SimpleNamespace(id=U1, first_name="U1"),
            answered=False,
        )

        async def _answer():
            query.answered = True

        query.answer = _answer
        update = SimpleNamespace(callback_query=query)
        return update, query, original

    async def test_click_replies_with_fresh_group_card(self):
        _seed(CHAT, U1, 180)
        update, query, original = self._click()
        await leveling.nextlevel_callback(update, None)

        self.assertTrue(query.answered)
        self.assertEqual(len(original.replies), 1)
        reply = original.replies[0]
        self.assertIn("Rank Progress", reply["text"])
        self.assertIn("Chat: <b>Rank 2 → 3</b>", reply["text"])
        markup = reply.get("reply_markup")
        self.assertEqual(markup.inline_keyboard[0][0].callback_data,
                         "nextlevel:me")

    async def test_click_in_dm_shows_global_only(self):
        _seed(CHAT2, U1, 250)
        update, query, original = self._click(
            chat_id=U1, chat_type="private"
        )
        await leveling.nextlevel_callback(update, None)

        text = original.replies[0]["text"]
        self.assertIn("Global: <b>Rank 2 → 3</b>", text)
        self.assertNotIn("Chat:", text)


class TestLevelingWiring(unittest.TestCase):
    def test_nextlevel_callback_registered_with_pattern(self):
        handlers = []
        app = SimpleNamespace(
            add_handler=lambda h, group=0: handlers.append(h)
        )
        leveling.setup(app)

        matches = [
            h for h in handlers
            if getattr(h, "callback", None) is leveling.nextlevel_callback
        ]
        self.assertEqual(len(matches), 1, "button handler must be registered")
        pattern = matches[0].pattern
        self.assertIsNotNone(pattern)
        self.assertTrue(pattern.search("nextlevel:me"))
        self.assertIsNone(pattern.search("nextlevel:other"))
        self.assertIsNone(pattern.search("info:me"))


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

    def tearDown(self):
        _cleanup()

    async def test_dm_reply_is_text_with_list_and_buttons(self):
        from bot.modules import template as template_mod

        _seed(CHAT, U1, 150)
        msg = _Msg("/template")
        upd = _update(msg, chat_type="private", chat_id=U1)
        await template_mod.template_command(upd, None)

        text = msg.last["text"]
        self.assertIn("Rank Templates", text)
        self.assertIn("Active:", text)             # current selection
        self.assertIn("1. NEON CYBERPUNK", text)
        self.assertIn("6. PIRATE", text)
        self.assertIn("Usage:", text)
        self.assertIn("/template &lt;number&gt;", text)
        self.assertNotIn("photo", msg.last, "/template must not send a picture")

        markup = msg.last["reply_markup"]
        flat = [b for row in markup.inline_keyboard for b in row]
        self.assertEqual(len(flat), 6)
        self.assertEqual(flat[0].callback_data, "template:1")
        self.assertEqual(flat[0].api_kwargs.get("style"), "primary")
        self.assertEqual(flat[1].api_kwargs.get("style"), "success")
        self.assertEqual(flat[2].api_kwargs.get("style"), "danger")
        for btn in flat:
            self.assertEqual(btn.api_kwargs.get("icon_custom_emoji_id"),
                             EID.SPARKLE)

    async def test_group_is_redirected_to_dm(self):
        from bot.modules import template as template_mod

        msg = _Msg("/template")
        upd = _update(msg, chat_type="supergroup", chat_id=CHAT)
        await template_mod.template_command(upd, None)

        text = msg.last["text"]
        self.assertIn("DM", text)
        self.assertNotIn("photo", msg.last)


class TestTemplateCallback(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _cleanup()
        db.add_user(U1, "u1", "U1", None)

    def tearDown(self):
        _cleanup()

    async def test_selection_sets_template_and_styled_caption(self):
        from bot.modules import template as template_mod

        edited = {}
        answered = {}

        async def _answer(text=None, show_alert=False):
            answered["text"] = text

        async def _edit(text=None, parse_mode=None):
            edited["text"] = text

        query = SimpleNamespace(
            data="template:3",
            from_user=SimpleNamespace(id=U1, first_name="U1"),
            answer=_answer,
            edit_message_text=_edit,
        )
        await template_mod.template_callback(
            SimpleNamespace(callback_query=query), None
        )

        self.assertEqual(db.get_user_rank_info(U1)["template"], 3)
        self.assertIn("ICE FANTASY", answered["text"] or "")
        self.assertIn("Template Selected", edited["text"])
        self.assertIn("ICE FANTASY", edited["text"])
        self.assertIn("Change anytime with /template", edited["text"])


if __name__ == "__main__":
    unittest.main()
