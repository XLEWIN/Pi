"""Tests for chat message rankings (bot/modules/chatstats.py).

Run from the Pi/Pi root:

    python tests/test_chatstats.py
    python -m unittest discover -s tests

Environment isolation (BEFORE any bot import):
    * BOT_TOKEN is forced — importing `bot` pulls bot.config, which
      exits without one.
    * LOCALAPPDATA points at a temp dir so the SQLite DB is isolated.

No network: handlers run against fake messages/callbacks; the counter
runs its DB work on the in-process executor.
"""

from __future__ import annotations

import asyncio
import atexit
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

# ── Environment isolation — must precede bot imports ──────────────
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["BOT_TOKEN"] = "1:TEST-TOKEN-FOR-UNIT-TESTS"
_TEST_DIR = tempfile.mkdtemp(prefix="pi_chatstats_test_")
os.environ["LOCALAPPDATA"] = _TEST_DIR
atexit.register(shutil.rmtree, _TEST_DIR, ignore_errors=True)

# ── Imports (after env) ───────────────────────────────────────────
from aiogram.dispatcher.event.handler import FilterObject, HandlerObject  # noqa: E402

from aiofakes import FakeBot, call, command_filters, make_callback  # noqa: E402
from aiofakes import make_message  # noqa: E402
from bot import pipeline  # noqa: E402
from bot.constants import HELP_MENU  # noqa: E402
from bot.database import db  # noqa: E402
from bot.modules import chatstats as cs  # noqa: E402
from bot.timeutils import ist_date, ist_monday  # noqa: E402

# IST-based day boundaries (the counter stamps rows with these).
TODAY = ist_date()
YESTERDAY = (date.fromisoformat(TODAY) - timedelta(days=1)).isoformat()
OLD = (date.fromisoformat(TODAY) - timedelta(days=40)).isoformat()
MONDAY = ist_monday()
LAST_SUNDAY = (date.fromisoformat(MONDAY) - timedelta(days=1)).isoformat()

CHAT_A = -100555001
CHAT_B = -100555002
CHAT_C = -100555003
USERS = [99555001, 99555002, 99555003, 99555004]
USER_1 = USERS[0]
NEW_USER = 99555099     # never /start'ed — must auto-register on first message


# ═════════════════════════════════════════════════════════════════
# Fakes
# ═════════════════════════════════════════════════════════════════

def _msg(
    text: str | None = "/rankings",
    *,
    chat_id: int = CHAT_A,
    chat_type: str = "supergroup",
    title: str = "Test Group",
    user_id: int = USER_1,
    is_bot: bool = False,
    first_name: str = "Lewin",
    username: str | None = None,
    **kw,
):
    """Command message — bot.reply helpers record into .calls as
    ("reply", …) in group chats and ("answer", …) in private."""
    msg = make_message(
        text, chat_id=chat_id, chat_type=chat_type, title=title,
        user_id=user_id, is_bot=is_bot, first_name=first_name,
        username=username, **kw,
    )
    msg.from_user.last_name = None   # count_message registers the profile
    return msg


def _sent(msg):
    """Text of the last reply/answer recorded on ``msg``."""
    return msg.last[1] if msg.last else None


def _kw(msg):
    """Keyword args of the last reply/answer recorded on ``msg``."""
    return msg.last[2] if msg.last else {}


class _CBMsg:
    """Board message: chat + edit_text (aiogram puts edits on Message)."""

    def __init__(self, chat_id: int, chat_type: str = "supergroup") -> None:
        self.chat = SimpleNamespace(id=chat_id, type=chat_type)
        self.chat_id = chat_id
        self.edits: list = []

    async def edit_text(self, text, **kw):
        self.edits.append({"text": text, **kw})
        return self


def _seed(chat_id: int, user_id: int, n: int, day: str = TODAY) -> None:
    """Insert n messages straight into daily_messages."""
    db.collection("daily_messages").update_one(
        {"chat_id": chat_id, "user_id": user_id, "date": day},
        {"$inc": {"messages": n}},
        upsert=True,
    )


def _cleanup() -> None:
    for tbl, col, ids in (
        ("daily_messages", "chat_id", (CHAT_A, CHAT_B, CHAT_C)),
        ("daily_messages", "user_id", (*USERS, NEW_USER)),
        ("groups", "chat_id", (CHAT_A, CHAT_B, CHAT_C)),
        ("group_members", "chat_id", (CHAT_A, CHAT_B, CHAT_C)),
        ("group_members", "user_id", (*USERS, NEW_USER)),
        ("users", "user_id", (*USERS, NEW_USER)),
        ("spam_protection", "user_id", (*USERS, NEW_USER)),
    ):
        db.collection(tbl).delete_many({col: {"$in": list(ids)}})


# ═════════════════════════════════════════════════════════════════
# DB layer
# ═════════════════════════════════════════════════════════════════

class _DBTest(unittest.TestCase):
    def setUp(self):
        _cleanup()

    def tearDown(self):
        _cleanup()


class TestCountMessage(_DBTest):
    def test_increments_same_bucket(self):
        for _ in range(3):
            db.count_message(CHAT_A, USER_1, TODAY)
        rows = db.get_chat_top(CHAT_A)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["total_messages"], 3)
        self.assertEqual(db.get_chat_message_total(CHAT_A), 3)

    def test_separate_days_sum_but_stay_distinct(self):
        db.count_message(CHAT_A, USER_1, TODAY)
        db.count_message(CHAT_A, USER_1, YESTERDAY)
        total = db.get_chat_message_total(CHAT_A)
        self.assertEqual(total, 2)
        today_only = db.get_chat_top(CHAT_A, since=TODAY)
        self.assertEqual(today_only[0]["total_messages"], 1)

    def test_group_title_insert_and_refresh(self):
        db.count_message(CHAT_A, USER_1, TODAY, "Old Title")
        row = db.collection("groups").find_one(
            {"chat_id": CHAT_A}, {"chat_title": 1, "_id": 0}
        )
        self.assertEqual(row["chat_title"], "Old Title")
        db.count_message(CHAT_A, USER_1, TODAY, "New Title")
        row = db.collection("groups").find_one(
            {"chat_id": CHAT_A}, {"chat_title": 1, "_id": 0}
        )
        self.assertEqual(row["chat_title"], "New Title")


class TestAddMessageXpNoDoubleCount(_DBTest):
    def test_add_message_xp_no_longer_touches_daily_messages(self):
        db.add_message_xp(USER_1, CHAT_A)
        n = db.collection("daily_messages").count_documents(
            {"chat_id": CHAT_A, "user_id": USER_1}
        )
        self.assertEqual(n, 0, "XP cooldown path must not write daily_messages")


class TestQueries(_DBTest):
    def test_chat_top_orders_and_limits(self):
        for i, uid in enumerate(USERS):
            _seed(CHAT_A, uid, (i + 1) * 5)
        rows = db.get_chat_top(CHAT_A, limit=3)
        self.assertEqual([r["user_id"] for r in rows], USERS[::-1][:3])
        self.assertEqual(rows[0]["total_messages"], 20)

    def test_chat_top_all_time_includes_old_rows(self):
        _seed(CHAT_A, USER_1, 10, day=OLD)
        _seed(CHAT_A, USER_1, 2, day=TODAY)
        all_time = db.get_chat_top(CHAT_A)
        self.assertEqual(all_time[0]["total_messages"], 12)
        today = db.get_chat_top(CHAT_A, since=TODAY)
        self.assertEqual(today[0]["total_messages"], 2)

    def test_today_window_is_the_ist_date(self):
        """Today scope = current IST date only (refreshes at IST midnight)."""
        self.assertEqual(ist_date(), TODAY)
        _seed(CHAT_A, USER_1, 4, day=TODAY)
        _seed(CHAT_A, USER_1, 6, day=YESTERDAY)
        today = db.get_chat_top(CHAT_A, since=ist_date())
        self.assertEqual(today[0]["total_messages"], 4,
                         "yesterday is outside the Today window")

    def test_week_window_starts_monday(self):
        """Weekly = current week, Monday inclusive; last Sunday excluded."""
        self.assertEqual(
            date.fromisoformat(ist_monday()).weekday(), 0, "week must start Monday"
        )
        _seed(CHAT_A, USER_1, 5, day=MONDAY)
        _seed(CHAT_A, USER_1, 7, day=LAST_SUNDAY)
        week = db.get_chat_top(CHAT_A, since=ist_monday())
        self.assertEqual(week[0]["total_messages"], 5,
                         "last Sunday belongs to the previous week")

    def test_chat_total_matches_top_sum(self):
        _seed(CHAT_A, USER_1, 4)
        _seed(CHAT_A, USERS[1], 6, day=YESTERDAY)
        total = db.get_chat_message_total(CHAT_A)
        week = db.get_chat_message_total(CHAT_A, since=YESTERDAY)
        self.assertEqual(total, 10)
        self.assertEqual(week, 10)
        self.assertEqual(db.get_chat_message_total(CHAT_A, since=TODAY), 4)

    def test_user_top_groups_orders_by_messages(self):
        _seed(CHAT_A, USER_1, 3)
        _seed(CHAT_B, USER_1, 30)
        _seed(CHAT_C, USER_1, 15, day=YESTERDAY)
        rows = db.get_user_top_groups(USER_1)
        self.assertEqual([r["chat_id"] for r in rows], [CHAT_B, CHAT_C, CHAT_A])
        self.assertEqual(rows[0]["total_messages"], 30)

    def test_user_top_groups_joins_title(self):
        db.count_message(CHAT_A, USER_1, TODAY, "Nice Title")
        rows = db.get_user_top_groups(USER_1)
        self.assertEqual(rows[0]["chat_title"], "Nice Title")


# ═════════════════════════════════════════════════════════════════
# Counter handler
# ═════════════════════════════════════════════════════════════════

class TestCountHandler(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _cleanup()

    def tearDown(self):
        _cleanup()

    async def test_counts_group_text(self):
        await call(cs.count_message, _msg("hello"))
        await call(cs.count_message, _msg("again"))
        rows = db.get_chat_top(CHAT_A)
        self.assertEqual(rows[0]["total_messages"], 2)

    async def test_skips_private(self):
        await call(cs.count_message, _msg("hi", chat_type="private", chat_id=777))
        self.assertEqual(db.get_chat_top(CHAT_A), [])

    async def test_skips_bots(self):
        await call(cs.count_message, _msg("bot says", is_bot=True))
        self.assertEqual(db.get_chat_top(CHAT_A), [])

    async def test_skips_non_text(self):
        await call(cs.count_message, _msg(None))
        self.assertEqual(db.get_chat_top(CHAT_A), [])

    async def test_blocked_user_is_not_counted(self):
        """Spam-blocked messages must not reach the leaderboard."""
        db.spam_set_block(USER_1, "2999-12-31T00:00:00+00:00")
        await call(cs.count_message, _msg("hello"))
        await call(cs.count_message, _msg("again"))
        self.assertEqual(db.get_chat_top(CHAT_A), [])
        # …but the sender is still auto-registered (registration ≠ counting)
        self.assertIsNotNone(db.get_user(USER_1))
        # once the block is gone, counting resumes
        db.collection("spam_protection").update_one(
            {"user_id": USER_1},
            {"$set": {"blocked_until": "2000-01-01T00:00:00+00:00"}},
        )
        await call(cs.count_message, _msg("back"))
        self.assertEqual(db.get_chat_top(CHAT_A)[0]["total_messages"], 1)

    async def test_new_sender_auto_registers_without_start(self):
        """First group message registers the user — /start is not required."""
        self.assertIsNone(db.get_user(NEW_USER))
        await call(
            cs.count_message,
            _msg(
                "hello everyone",
                user_id=NEW_USER,
                first_name="Newbie",
                username="newbie99",
            ),
        )
        row = db.get_user(NEW_USER)
        self.assertIsNotNone(row)
        self.assertEqual(row["first_name"], "Newbie")
        self.assertEqual(row["username"], "newbie99")
        # leaderboard join resolves the real name, not the "User <id>" fallback
        top = db.get_chat_top(CHAT_A)
        self.assertEqual(top[0]["user_id"], NEW_USER)
        self.assertEqual(cs._name_of(top[0]), "Newbie")

    async def test_registration_refreshes_renamed_profile(self):
        db.add_user(NEW_USER, "oldname", "OldName", None)
        await call(
            cs.count_message,
            _msg(
                "I renamed myself",
                user_id=NEW_USER,
                first_name="NewName",
                username="newname",
            ),
        )
        row = db.get_user(NEW_USER)
        self.assertEqual(row["first_name"], "NewName")
        self.assertEqual(row["username"], "newname")


# ═════════════════════════════════════════════════════════════════
# First contact: group cache + one-time #Newuser log
# ═════════════════════════════════════════════════════════════════

class TestNewUserRegistration(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _cleanup()

    def tearDown(self):
        _cleanup()

    async def test_first_message_registers_caches_and_logs(self):
        with mock.patch.object(cs, "send_newuser_log", new=mock.AsyncMock()) as log:
            await call(
                cs.count_message,
                _msg(
                    "hello everyone",
                    user_id=NEW_USER,
                    first_name="Newbie",
                    username="newbie99",
                ),
            )
        # registered + counted
        self.assertIsNotNone(db.get_user(NEW_USER))
        self.assertEqual(db.get_chat_top(CHAT_A)[0]["user_id"], NEW_USER)
        # group-members cache touched
        row = db.collection("group_members").find_one(
            {"chat_id": CHAT_A, "user_id": NEW_USER},
            {"role": 1, "_id": 0},
        )
        self.assertIsNotNone(row, "sender must be cached for this group")
        # one-time #Newuser log with (context, user, chat_title)
        log.assert_called_once()
        self.assertEqual(log.call_args[0][1].id, NEW_USER)
        self.assertEqual(log.call_args[0][2], "Test Group")

    async def test_log_fires_only_once_ever(self):
        with mock.patch.object(cs, "send_newuser_log", new=mock.AsyncMock()) as log:
            await call(
                cs.count_message,
                _msg("one", user_id=NEW_USER, first_name="Newbie"),
            )
            await call(
                cs.count_message,
                _msg("two", user_id=NEW_USER, first_name="Newbie"),
            )
        self.assertEqual(log.call_count, 1)

    async def test_existing_user_gets_no_log(self):
        db.add_user(NEW_USER, "veteran", "Vet", None)
        with mock.patch.object(cs, "send_newuser_log", new=mock.AsyncMock()) as log:
            await call(
                cs.count_message,
                _msg("hi again", user_id=NEW_USER, first_name="Vet"),
            )
        log.assert_not_called()
        self.assertEqual(db.get_chat_top(CHAT_A)[0]["total_messages"], 1)

    async def test_blocked_new_user_still_registers_and_logs(self):
        db.spam_set_block(NEW_USER, "2999-12-31T00:00:00+00:00")
        with mock.patch.object(cs, "send_newuser_log", new=mock.AsyncMock()) as log:
            await call(
                cs.count_message,
                _msg("flooding already", user_id=NEW_USER, first_name="Newbie"),
            )
        log.assert_called_once()          # first contact still logs
        self.assertIsNotNone(db.get_user(NEW_USER))   # and registers
        self.assertEqual(db.get_chat_top(CHAT_A), [])  # but not counted


class TestNewuserLogFormat(unittest.IsolatedAsyncioTestCase):
    """#Newuser text — Pi style, only the owner's emoji (rich + plain)."""

    def setUp(self):
        _cleanup()

    def tearDown(self):
        _cleanup()

    def _user(self, **kw):
        base = dict(id=NEW_USER, first_name="Z\u00eb\u0141\u03c3", username="hoxrr")
        base.update(kw)
        return SimpleNamespace(**base)

    def test_rich_format_shape(self):
        from bot.modules.start import format_newuser_log

        text = format_newuser_log(self._user(), "Test Group")
        self.assertIn("<b>#Newuser</b>", text)
        self.assertIn("<b>Name:</b> Z\u00eb\u0141\u03c3", text)
        self.assertIn(f"<code>{NEW_USER}</code>", text)
        self.assertIn("@hoxrr", text)
        self.assertIn("<b>Chat:</b> Test Group", text)
        self.assertIn("<tg-emoji", text)   # custom icons, not plain glyphs

    def test_plain_twin_strips_tags_keeps_structure(self):
        from bot.modules.start import _strip_tgemoji, format_newuser_log

        user, title = self._user(), "Test Group"
        rich = format_newuser_log(user, title)
        plain = format_newuser_log(user, title, plain=True)
        self.assertNotIn("<tg-emoji", plain)
        self.assertIn("<b>#Newuser</b>", plain)
        self.assertIn("<code>", plain)
        self.assertEqual(_strip_tgemoji(rich), plain, "plain = rich minus tags")

    def test_name_is_html_escaped(self):
        from bot.modules.start import format_newuser_log

        text = format_newuser_log(self._user(first_name="A<B>&C"))
        self.assertIn("A&lt;B&gt;&amp;C", text)
        self.assertNotIn("A<B>&C", text)

    def test_no_username_fallback(self):
        from bot.modules.start import format_newuser_log

        text = format_newuser_log(self._user(username=None), plain=True)
        self.assertIn("No username", text)

    async def test_milestone_at_100(self):
        _seed(CHAT_A, USER_1, 99)
        msg = _msg("the hundredth")
        await call(cs.count_message, msg)
        self.assertEqual(len(msg.sent_texts), 1)
        text = _sent(msg)
        self.assertIn("100 messages reached today!", text)
        self.assertIn("(", text)  # (HH:MM)
        self.assertEqual(_kw(msg).get("parse_mode"), "HTML")
        self.assertIn("<tg-emoji", text)   # owner's custom 🔥 icon

    async def test_milestone_at_500_not_between(self):
        _seed(CHAT_A, USER_1, 498)
        msg = _msg("four ninety nine")
        await call(cs.count_message, msg)
        self.assertEqual(msg.sent_texts, [], "499 is not a milestone")
        msg2 = _msg("five hundred")
        await call(cs.count_message, msg2)
        self.assertEqual(len(msg2.sent_texts), 1)
        self.assertIn("500 messages reached today!", _sent(msg2))

    async def test_milestone_fires_once(self):
        _seed(CHAT_A, USER_1, 99)
        msg1 = _msg("m")
        await call(cs.count_message, msg1)
        msg2 = _msg("m")
        await call(cs.count_message, msg2)
        self.assertEqual(len(msg1.sent_texts), 1)
        self.assertEqual(msg2.sent_texts, [], "101 must not re-announce 100")


# ═════════════════════════════════════════════════════════════════
# /rankings
# ═════════════════════════════════════════════════════════════════

class TestRankings(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _cleanup()
        db.add_user(USER_1, "lewin", "Lewin", None)
        db.add_user(USERS[1], None, "Samuel", None)
        _seed(CHAT_A, USER_1, 11797)
        _seed(CHAT_A, USERS[1], 2019)

    def tearDown(self):
        _cleanup()

    async def test_board_format_and_total(self):
        msg = _msg("/rankings")
        await call(cs.rankings_command, msg)
        text = _sent(msg)
        self.assertIn("Leaderboard", text)
        self.assertIn(f'href="tg://user?id={USER_1}"', text)
        self.assertIn("11,797", text)
        self.assertIn("2,019", text)
        self.assertIn("Total messages: 13,816", text)
        self.assertEqual(_kw(msg).get("parse_mode"), "HTML")
        # every icon must be the owner's custom set — no plain glyphs
        self.assertIn("<tg-emoji", text)
        self.assertNotIn("\U0001f4ca", text)  # plain 📊
        self.assertNotIn("\U0001f4ac", text)  # plain 💬

    async def test_links_prefer_first_name_over_username(self):
        """Hyperlink text is the first name; @username is only a fallback."""
        msg = _msg("/rankings")
        await call(cs.rankings_command, msg)
        text = _sent(msg)
        self.assertIn(">Lewin<", text)
        self.assertIn(">Samuel<", text)
        self.assertNotIn("lewin", text)  # username must not be the link

    async def test_order_and_rank_numbers(self):
        msg = _msg("/rankings")
        await call(cs.rankings_command, msg)
        text = _sent(msg)
        self.assertLess(text.index("1. "), text.index("2. "))
        self.assertLess(text.index("2. "), text.index("Total messages"))

    async def test_tab_buttons_layout(self):
        msg = _msg("/rankings")
        await call(cs.rankings_command, msg)
        markup = _kw(msg).get("reply_markup")
        self.assertIsNotNone(markup)
        rows = markup.inline_keyboard
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(rows[0]), 1)
        self.assertEqual(len(rows[1]), 2)  # Today, Weekly — no Monthly tab
        active = rows[0][0]
        self.assertEqual(active.text, "Overall \u2705")
        self.assertEqual(active.style, "success")
        self.assertEqual(active.callback_data, "cs:r:overall")
        self.assertEqual(
            active.icon_custom_emoji_id, cs.EID.WEB
        )
        self.assertEqual([b.text for b in rows[1]], ["Today", "Weekly"])
        for b in rows[1]:
            self.assertEqual(b.style, "primary")
        self.assertEqual(
            [b.icon_custom_emoji_id for b in rows[1]],
            [cs.EID.TIME, cs.EID.FIRE],
        )
        self.assertEqual(
            [b.callback_data for b in rows[1]],
            ["cs:r:today", "cs:r:week"],
        )
        self.assertNotIn("month", cs.SCOPES)

    async def test_scope_text_shown_in_header(self):
        """Header reflects whichever tab is selected."""
        for scope, label in cs._SCOPE_LABELS.items():
            text, _ = cs._rank_board(CHAT_A, scope)
            self.assertIn(f"<i>{label}</i>", text, scope)

    async def test_private_denied(self):
        msg = _msg("/rankings", chat_type="private", chat_id=42)
        await call(cs.rankings_command, msg)
        self.assertIn("only works in groups", _sent(msg))
        self.assertIsNone(_kw(msg).get("reply_markup"))

    async def test_empty_state(self):
        _cleanup()
        msg = _msg("/rankings")
        await call(cs.rankings_command, msg)
        self.assertIn("No messages counted yet", _sent(msg))
        self.assertIsNone(_kw(msg).get("reply_markup"))


# ═════════════════════════════════════════════════════════════════
# /mytop
# ═════════════════════════════════════════════════════════════════

class TestMytop(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _cleanup()
        db.add_user(USER_1, "lewin", "Lewin", None)
        _seed(CHAT_A, USER_1, 50, day=TODAY)
        _seed(CHAT_B, USER_1, 7107, day=YESTERDAY)
        _seed(CHAT_C, USER_1, 9, day=OLD)
        db.add_group(CHAT_A, "Alpha Chat")
        db.add_group(CHAT_B, "Smash Your Character")

    def tearDown(self):
        _cleanup()

    async def test_groups_ranked_by_user_messages(self):
        msg = _msg("/mytop")
        await call(cs.mytop_command, msg)
        text = _sent(msg)
        self.assertIn("Top Groups", text)
        self.assertIn(f'href="tg://user?id={USER_1}"', text)  # profile mention header
        self.assertIn("Smash Your Character", text)
        self.assertIn("7,107", text)
        self.assertIn("Alpha Chat", text)
        self.assertLess(text.index("1. "), text.index("2. "))
        self.assertEqual(_kw(msg).get("parse_mode"), "HTML")
        self.assertIn("<tg-emoji", text)
        self.assertNotIn("\U0001f4ca", text)  # header must be a custom emoji

    async def test_buttons_carry_user_id(self):
        msg = _msg("/mytop")
        await call(cs.mytop_command, msg)
        markup = _kw(msg).get("reply_markup")
        active = markup.inline_keyboard[0][0]
        self.assertEqual(active.callback_data, f"cs:m:{USER_1}:overall")
        self.assertEqual(active.style, "success")
        self.assertEqual(active.text, "Overall \u2705")
        self.assertEqual(
            active.icon_custom_emoji_id, cs.EID.WEB
        )

    async def test_mytop_scope_text_in_header(self):
        viewer = SimpleNamespace(id=USER_1, username="lewin", first_name="Lewin")
        for scope, label in cs._SCOPE_LABELS.items():
            text, _ = cs._mytop_board(viewer, scope)
            self.assertIn(f"<i>{label}</i>", text, scope)

    async def test_header_links_first_name_not_username(self):
        """The /mytop header hyperlink uses the first name (not @username)."""
        viewer = SimpleNamespace(id=USER_1, username="lewin", first_name="Lewin")
        text, _ = cs._mytop_board(viewer, "overall")
        self.assertIn(f'href="tg://user?id={USER_1}"', text)
        self.assertIn(">Lewin<", text)
        self.assertNotIn("lewin", text)

    async def test_works_in_private(self):
        msg = _msg("/mytop", chat_type="private", chat_id=USER_1)
        await call(cs.mytop_command, msg)
        self.assertIn("Top Groups", _sent(msg))

    async def test_title_fallback_when_group_unknown(self):
        _cleanup()
        _seed(CHAT_A, USER_1, 5)
        msg = _msg("/mytop")
        await call(cs.mytop_command, msg)
        self.assertIn(f"Chat {CHAT_A}", _sent(msg))

    async def test_empty_state(self):
        _cleanup()
        msg = _msg("/mytop")
        await call(cs.mytop_command, msg)
        self.assertIn("No messages found", _sent(msg))
        self.assertIsNone(_kw(msg).get("reply_markup"))


# ═════════════════════════════════════════════════════════════════
# Tab callbacks
# ═════════════════════════════════════════════════════════════════

class TestBoardCallback(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _cleanup()
        db.add_user(USER_1, "lewin", "Lewin", None)
        _seed(CHAT_A, USER_1, 100, day=OLD)
        _seed(CHAT_A, USER_1, 5, day=TODAY)

    def tearDown(self):
        _cleanup()

    async def test_rank_scope_switch_edits_board(self):
        msg = _CBMsg(CHAT_A)
        query = make_callback("cs:r:today", message=msg, user_id=USER_1)
        await call(cs.board_callback, query)
        self.assertEqual(len(msg.edits), 1)
        text = msg.edits[0]["text"]
        self.assertIn("5", text)          # today-only count
        self.assertNotIn("105", text)     # all-time total excluded
        self.assertIn("<i>Today</i>", text)  # scope text follows the tab
        markup = msg.edits[0]["reply_markup"]
        self.assertEqual(markup.inline_keyboard[0][0].text, "Today \u2705")
        self.assertEqual(
            markup.inline_keyboard[0][0]
            .icon_custom_emoji_id,
            cs.EID.TIME,
        )
        self.assertEqual(markup.inline_keyboard[0][0].callback_data, "cs:r:today")
        self.assertTrue(query.answers and query.answers[0]["text"] is None)

    async def test_mytop_foreign_presser_gets_alert(self):
        msg = _CBMsg(CHAT_A)
        other = make_callback(
            f"cs:m:{USER_1}:week", message=msg, user_id=999999
        )
        await call(cs.board_callback, other)
        self.assertEqual(msg.edits, [], "board must not switch for a stranger")
        self.assertTrue(other.answers[0]["show_alert"])
        self.assertIn("original user", other.answers[0]["text"])

    async def test_owner_presser_switches(self):
        msg = _CBMsg(CHAT_A, chat_type="private")
        query = make_callback(
            f"cs:m:{USER_1}:week", message=msg, user_id=USER_1,
            username="lewin",
        )
        await call(cs.board_callback, query)
        self.assertEqual(len(msg.edits), 1)
        self.assertIn("Top Groups", msg.edits[0]["text"])

    async def test_invalid_data_ignored(self):
        msg = _CBMsg(CHAT_A)
        for data in ("cs:x:today", "cs:r:nope", "cs:r:month",
                     "cs:m:notanint:today", "other:cb"):
            query = make_callback(data, message=msg, user_id=USER_1)
            await call(cs.board_callback, query)
        self.assertEqual(msg.edits, [])


# ═════════════════════════════════════════════════════════════════
# Wiring, filters, help
# ═════════════════════════════════════════════════════════════════

class TestWiring(unittest.TestCase):
    def test_setup_registers_everything(self):
        pipeline.clear()
        routes = cs.setup()
        self.assertIn("/rankings", routes)
        self.assertIn("/mytop", routes)

        cmd_names = set()
        n_msg = n_cb = 0
        for entry in pipeline.snapshot():
            if entry.event == "callback_query":
                n_cb += 1
                continue
            cmd_filters = command_filters(entry.flt)
            if cmd_filters:
                for flt in cmd_filters:
                    cmd_names.update(flt.commands)
            elif entry.event == "message":
                n_msg += 1
        self.assertEqual(cmd_names, {"rankings", "mytop"})
        self.assertEqual(n_msg, 1)
        self.assertEqual(n_cb, 1)

    def test_counting_filter_accepts_text_rejects_commands_and_private(self):
        pipeline.clear()
        cs.setup()
        entry = next(e for e in pipeline.snapshot() if e.fn is cs.count_message)
        handler = HandlerObject(
            callback=entry.fn, filters=[FilterObject(entry.flt)]
        )

        def check(event):
            ok, _ = asyncio.run(handler.check(event, bot=FakeBot()))
            return ok

        self.assertTrue(check(make_message("hello there")))
        self.assertFalse(check(make_message("/rankings")), "commands must not count")
        self.assertFalse(
            check(make_message("hi", chat_type="private", chat_id=7)),
            "private must not count",
        )

    def test_help_documents_both_commands(self):
        stats = next(m for m in HELP_MENU if m["key"] == "stats")
        lines = [line for _, cmds in stats["sections"] for line in cmds]
        self.assertTrue(any(l.startswith("/rankings") for l in lines))
        self.assertTrue(any(l.startswith("/mytop") for l in lines))


class TestFormatters(unittest.TestCase):
    def test_fmt_thousands(self):
        self.assertEqual(cs._fmt(80788), "80,788")
        self.assertEqual(cs._fmt(999), "999")

    def test_milestone_thresholds(self):
        for n in (100, 500, 1000, 1500):
            self.assertTrue(cs._is_milestone(n), n)
        for n in (1, 99, 101, 499, 501, 999, 1499):
            self.assertFalse(cs._is_milestone(n), n)

    def test_scope_since_windows(self):
        self.assertIsNone(cs._scope_since("overall"))
        self.assertEqual(cs._scope_since("today"), ist_date())
        self.assertEqual(cs._scope_since("week"), ist_monday())

    def test_clip_long_titles(self):
        self.assertEqual(len(cs._clip("x" * 100)), cs._TITLE_CLIP)
        self.assertTrue(cs._clip("x" * 100).endswith("\u2026"))
        self.assertEqual(cs._clip("Short"), "Short")

    def test_name_fallbacks(self):
        # First name is the board's hyperlink text - last name is NOT
        # appended (the owner wants first-name links, not full names).
        self.assertEqual(
            cs._name_of({"user_id": 5, "first_name": "Sam", "last_name": "Lee"}),
            "Sam",
        )
        self.assertEqual(
            cs._name_of({"user_id": 5, "username": "samlee"}), "samlee"
        )
        self.assertEqual(cs._name_of({"user_id": 5}), "User 5")


if __name__ == "__main__":
    unittest.main(verbosity=2)
