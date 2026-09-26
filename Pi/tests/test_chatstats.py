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

import atexit
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
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
from telegram import Chat, Message, Update  # noqa: E402
from telegram.ext import CallbackQueryHandler as PTBCallbackQueryHandler  # noqa: E402
from telegram.ext import MessageHandler as PTBMessageHandler  # noqa: E402

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

class _Msg:
    """Command message — records reply_text calls."""

    def __init__(self, text: str = "/rankings") -> None:
        self.text = text
        self.reply_to_message = None
        self.replies: list = []

    async def reply_text(self, text, **kw):
        self.replies.append({"text": text, **kw})
        return SimpleNamespace(message_id=1)

    @property
    def last(self):
        return self.replies[-1] if self.replies else None


class _CBMsg:
    """Mirrors real telegram.Message: it has chat/edit_text — NOT
    edit_message_text (that lives on CallbackQuery), so calling the
    wrong one fails loudly."""

    def __init__(self, chat_id: int, chat_type: str = "supergroup") -> None:
        self.chat = SimpleNamespace(id=chat_id, type=chat_type)
        self.chat_id = chat_id
        self.edits: list = []

    async def edit_text(self, text, **kw):
        self.edits.append({"text": text, **kw})
        return self


class _CBQuery:
    def __init__(self, data: str, from_user, msg: _CBMsg) -> None:
        self.data = data
        self.from_user = from_user
        self.message = msg
        self.answers: list = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append({"text": text, "show_alert": show_alert})

    async def edit_message_text(self, text, **kw):
        # PTB puts the edit method on CallbackQuery, not on Message —
        # mirror that here so a wrong call would fail loudly.
        self.message.edits.append({"text": text, **kw})
        return self.message


def _update(
    text: str = "hello",
    *,
    chat_id: int = CHAT_A,
    chat_type: str = "supergroup",
    title: str = "Test Group",
    user_id: int = USER_1,
    is_bot: bool = False,
    first_name: str = "Lewin",
    username: str | None = None,
    last_name: str | None = None,
    message=None,
    chat=None,
    user=None,
):
    msg = message if message is not None else SimpleNamespace(text=text)
    chat = chat if chat is not None else SimpleNamespace(
        id=chat_id, type=chat_type, title=title
    )
    user = user if user is not None else SimpleNamespace(
        id=user_id, is_bot=is_bot, first_name=first_name,
        username=username, last_name=last_name,
    )
    return SimpleNamespace(
        message=msg, effective_message=msg, effective_chat=chat, effective_user=user
    )


def _cmd_update(text: str, *, chat_id=CHAT_A, chat_type="supergroup",
                title="Test Group", user_id=USER_1):
    return _update(
        text, chat_id=chat_id, chat_type=chat_type, title=title,
        user_id=user_id, message=_Msg(text),  # reply-capable message
    )


def _real_update(text: str, chat_id: int = CHAT_A,
                 chat_type: str = "supergroup") -> Update:
    """A real telegram.Update — used for filter (check_update) tests."""
    msg = Message(
        message_id=1,
        date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        chat=Chat(id=chat_id, type=chat_type),
        text=text,
    )
    msg._bot = SimpleNamespace(username="PiModulerBot")
    return Update(update_id=1, message=msg)


def _seed(chat_id: int, user_id: int, n: int, day: str = TODAY) -> None:
    """Insert n messages straight into daily_messages."""
    db.connection.execute(
        """
        INSERT INTO daily_messages (chat_id, user_id, date, messages)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(chat_id, user_id, date)
        DO UPDATE SET messages = messages + excluded.messages
        """,
        (chat_id, user_id, day, n),
    )
    db.connection.commit()


def _cleanup() -> None:
    conn = db.connection
    for tbl, col, ids in (
        ("daily_messages", "chat_id", (CHAT_A, CHAT_B, CHAT_C)),
        ("daily_messages", "user_id", (*USERS, NEW_USER)),
        ("groups", "chat_id", (CHAT_A, CHAT_B, CHAT_C)),
        ("group_members", "chat_id", (CHAT_A, CHAT_B, CHAT_C)),
        ("group_members", "user_id", (*USERS, NEW_USER)),
        ("users", "user_id", (*USERS, NEW_USER)),
        ("spam_protection", "user_id", (*USERS, NEW_USER)),
    ):
        conn.execute(
            f"DELETE FROM {tbl} WHERE {col} IN "
            f"({','.join('?' * len(ids))})",
            ids,
        )
    conn.commit()


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
        row = db.connection.execute(
            "SELECT chat_title FROM groups WHERE chat_id = ?", (CHAT_A,)
        ).fetchone()
        self.assertEqual(row[0], "Old Title")
        db.count_message(CHAT_A, USER_1, TODAY, "New Title")
        row = db.connection.execute(
            "SELECT chat_title FROM groups WHERE chat_id = ?", (CHAT_A,)
        ).fetchone()
        self.assertEqual(row[0], "New Title")


class TestAddMessageXpNoDoubleCount(_DBTest):
    def test_add_message_xp_no_longer_touches_daily_messages(self):
        db.add_message_xp(USER_1, CHAT_A)
        n = db.connection.execute(
            "SELECT COUNT(*) FROM daily_messages WHERE chat_id = ? AND user_id = ?",
            (CHAT_A, USER_1),
        ).fetchone()[0]
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
        await cs.count_message(_update("hello"), None)
        await cs.count_message(_update("again"), None)
        rows = db.get_chat_top(CHAT_A)
        self.assertEqual(rows[0]["total_messages"], 2)

    async def test_skips_private(self):
        await cs.count_message(
            _update("hi", chat_type="private", chat_id=777), None
        )
        self.assertEqual(db.get_chat_top(CHAT_A), [])

    async def test_skips_bots(self):
        await cs.count_message(_update("bot says", is_bot=True), None)
        self.assertEqual(db.get_chat_top(CHAT_A), [])

    async def test_skips_non_text(self):
        await cs.count_message(_update(None), None)
        self.assertEqual(db.get_chat_top(CHAT_A), [])

    async def test_blocked_user_is_not_counted(self):
        """Spam-blocked messages must not reach the leaderboard."""
        db.spam_set_block(USER_1, "2999-12-31T00:00:00+00:00")
        await cs.count_message(_update("hello"), None)
        await cs.count_message(_update("again"), None)
        self.assertEqual(db.get_chat_top(CHAT_A), [])
        # …but the sender is still auto-registered (registration ≠ counting)
        self.assertIsNotNone(db.get_user(USER_1))
        # once the block is gone, counting resumes
        db.connection.execute(
            "UPDATE spam_protection SET blocked_until = '2000-01-01T00:00:00+00:00' "
            "WHERE user_id = ?",
            (USER_1,),
        )
        db.connection.commit()
        await cs.count_message(_update("back"), None)
        self.assertEqual(db.get_chat_top(CHAT_A)[0]["total_messages"], 1)

    async def test_new_sender_auto_registers_without_start(self):
        """First group message registers the user — /start is not required."""
        self.assertIsNone(db.get_user(NEW_USER))
        await cs.count_message(
            _update(
                "hello everyone",
                user_id=NEW_USER,
                first_name="Newbie",
                username="newbie99",
            ),
            None,
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
        await cs.count_message(
            _update(
                "I renamed myself",
                user_id=NEW_USER,
                first_name="NewName",
                username="newname",
            ),
            None,
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
            await cs.count_message(
                _update(
                    "hello everyone",
                    user_id=NEW_USER,
                    first_name="Newbie",
                    username="newbie99",
                ),
                None,
            )
        # registered + counted
        self.assertIsNotNone(db.get_user(NEW_USER))
        self.assertEqual(db.get_chat_top(CHAT_A)[0]["user_id"], NEW_USER)
        # group-members cache touched
        row = db.connection.execute(
            "SELECT role FROM group_members WHERE chat_id = ? AND user_id = ?",
            (CHAT_A, NEW_USER),
        ).fetchone()
        self.assertIsNotNone(row, "sender must be cached for this group")
        # one-time #Newuser log with (context, user, chat_title)
        log.assert_called_once()
        self.assertEqual(log.call_args[0][1].id, NEW_USER)
        self.assertEqual(log.call_args[0][2], "Test Group")

    async def test_log_fires_only_once_ever(self):
        with mock.patch.object(cs, "send_newuser_log", new=mock.AsyncMock()) as log:
            await cs.count_message(
                _update("one", user_id=NEW_USER, first_name="Newbie"), None
            )
            await cs.count_message(
                _update("two", user_id=NEW_USER, first_name="Newbie"), None
            )
        self.assertEqual(log.call_count, 1)

    async def test_existing_user_gets_no_log(self):
        db.add_user(NEW_USER, "veteran", "Vet", None)
        with mock.patch.object(cs, "send_newuser_log", new=mock.AsyncMock()) as log:
            await cs.count_message(
                _update("hi again", user_id=NEW_USER, first_name="Vet"), None
            )
        log.assert_not_called()
        self.assertEqual(db.get_chat_top(CHAT_A)[0]["total_messages"], 1)

    async def test_blocked_new_user_still_registers_and_logs(self):
        db.spam_set_block(NEW_USER, "2999-12-31T00:00:00+00:00")
        with mock.patch.object(cs, "send_newuser_log", new=mock.AsyncMock()) as log:
            await cs.count_message(
                _update("flooding already", user_id=NEW_USER, first_name="Newbie"),
                None,
            )
        log.assert_called_once()          # first contact still logs
        self.assertIsNotNone(db.get_user(NEW_USER))   # and registers
        self.assertEqual(db.get_chat_top(CHAT_A), [])  # but not counted


class TestNewuserLogFormat(unittest.TestCase):
    """#Newuser text — Pi style, only the owner's emoji (rich + plain)."""

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
        msg = _Msg("the hundredth")
        await cs.count_message(_update(message=msg), None)
        self.assertEqual(len(msg.replies), 1)
        text = msg.last["text"]
        self.assertIn("100 messages reached today!", text)
        self.assertIn("(", text)  # (HH:MM)
        self.assertEqual(msg.last.get("parse_mode"), "HTML")
        self.assertIn("<tg-emoji", text)   # owner's custom 🔥 icon

    async def test_milestone_at_500_not_between(self):
        _seed(CHAT_A, USER_1, 498)
        msg = _Msg("four ninety nine")
        await cs.count_message(_update(message=msg), None)
        self.assertEqual(msg.replies, [], "499 is not a milestone")
        msg2 = _Msg("five hundred")
        await cs.count_message(_update(message=msg2), None)
        self.assertEqual(len(msg2.replies), 1)
        self.assertIn("500 messages reached today!", msg2.last["text"])

    async def test_milestone_fires_once(self):
        _seed(CHAT_A, USER_1, 99)
        msg1 = _Msg("m")
        await cs.count_message(_update(message=msg1), None)
        msg2 = _Msg("m")
        await cs.count_message(_update(message=msg2), None)
        self.assertEqual(len(msg1.replies), 1)
        self.assertEqual(msg2.replies, [], "101 must not re-announce 100")


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
        msg = _Msg("/rankings")
        await cs.rankings_command(_update(message=msg), None)
        text = msg.last["text"]
        self.assertIn("Leaderboard", text)
        self.assertIn(f'href="tg://user?id={USER_1}"', text)
        self.assertIn("11,797", text)
        self.assertIn("2,019", text)
        self.assertIn("Total messages: 13,816", text)
        self.assertEqual(msg.last.get("parse_mode"), "HTML")
        # every icon must be the owner's custom set — no plain glyphs
        self.assertIn("<tg-emoji", text)
        self.assertNotIn("\U0001f4ca", text)  # plain 📊
        self.assertNotIn("\U0001f4ac", text)  # plain 💬

    async def test_order_and_rank_numbers(self):
        msg = _Msg("/rankings")
        await cs.rankings_command(_update(message=msg), None)
        text = msg.last["text"]
        self.assertLess(text.index("1. "), text.index("2. "))
        self.assertLess(text.index("2. "), text.index("Total messages"))

    async def test_tab_buttons_layout(self):
        msg = _Msg("/rankings")
        await cs.rankings_command(_update(message=msg), None)
        markup = msg.last.get("reply_markup")
        self.assertIsNotNone(markup)
        rows = markup.inline_keyboard
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(rows[0]), 1)
        self.assertEqual(len(rows[1]), 2)  # Today, Weekly — no Monthly tab
        active = rows[0][0]
        self.assertEqual(active.text, "Overall \u2705")
        self.assertEqual(active.api_kwargs.get("style"), "success")
        self.assertEqual(active.callback_data, "cs:r:overall")
        self.assertEqual(
            active.api_kwargs.get("icon_custom_emoji_id"), cs.EID.WEB
        )
        self.assertEqual([b.text for b in rows[1]], ["Today", "Weekly"])
        for b in rows[1]:
            self.assertEqual(b.api_kwargs.get("style"), "primary")
        self.assertEqual(
            [b.api_kwargs.get("icon_custom_emoji_id") for b in rows[1]],
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
        msg = _Msg("/rankings")
        await cs.rankings_command(
            _update(message=msg, chat_type="private", chat_id=42), None
        )
        self.assertIn("only works in groups", msg.last["text"])
        self.assertIsNone(msg.last.get("reply_markup"))

    async def test_empty_state(self):
        _cleanup()
        msg = _Msg("/rankings")
        await cs.rankings_command(_update(message=msg), None)
        self.assertIn("No messages counted yet", msg.last["text"])
        self.assertIsNone(msg.last.get("reply_markup"))


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
        msg = _Msg("/mytop")
        await cs.mytop_command(_update(message=msg), None)
        text = msg.last["text"]
        self.assertIn("Top Groups", text)
        self.assertIn(f'href="tg://user?id={USER_1}"', text)  # profile mention header
        self.assertIn("Smash Your Character", text)
        self.assertIn("7,107", text)
        self.assertIn("Alpha Chat", text)
        self.assertLess(text.index("1. "), text.index("2. "))
        self.assertEqual(msg.last.get("parse_mode"), "HTML")
        self.assertIn("<tg-emoji", text)
        self.assertNotIn("\U0001f4ca", text)  # header must be a custom emoji

    async def test_buttons_carry_user_id(self):
        msg = _Msg("/mytop")
        await cs.mytop_command(_update(message=msg), None)
        markup = msg.last.get("reply_markup")
        active = markup.inline_keyboard[0][0]
        self.assertEqual(active.callback_data, f"cs:m:{USER_1}:overall")
        self.assertEqual(active.api_kwargs.get("style"), "success")
        self.assertEqual(active.text, "Overall \u2705")
        self.assertEqual(
            active.api_kwargs.get("icon_custom_emoji_id"), cs.EID.WEB
        )

    async def test_mytop_scope_text_in_header(self):
        viewer = SimpleNamespace(id=USER_1, username="lewin", first_name="Lewin")
        for scope, label in cs._SCOPE_LABELS.items():
            text, _ = cs._mytop_board(viewer, scope)
            self.assertIn(f"<i>{label}</i>", text, scope)

    async def test_works_in_private(self):
        msg = _Msg("/mytop")
        await cs.mytop_command(
            _update(message=msg, chat_type="private", chat_id=USER_1), None
        )
        self.assertIn("Top Groups", msg.last["text"])

    async def test_title_fallback_when_group_unknown(self):
        _cleanup()
        _seed(CHAT_A, USER_1, 5)
        msg = _Msg("/mytop")
        await cs.mytop_command(_update(message=msg), None)
        self.assertIn(f"Chat {CHAT_A}", msg.last["text"])

    async def test_empty_state(self):
        _cleanup()
        msg = _Msg("/mytop")
        await cs.mytop_command(_update(message=msg), None)
        self.assertIn("No messages found", msg.last["text"])
        self.assertIsNone(msg.last.get("reply_markup"))


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
        query = _CBQuery("cs:r:today", SimpleNamespace(id=USER_1), msg)
        await cs.board_callback(SimpleNamespace(callback_query=query), None)
        self.assertEqual(len(msg.edits), 1)
        text = msg.edits[0]["text"]
        self.assertIn("5", text)          # today-only count
        self.assertNotIn("105", text)     # all-time total excluded
        self.assertIn("<i>Today</i>", text)  # scope text follows the tab
        markup = msg.edits[0]["reply_markup"]
        self.assertEqual(markup.inline_keyboard[0][0].text, "Today \u2705")
        self.assertEqual(
            markup.inline_keyboard[0][0]
            .api_kwargs.get("icon_custom_emoji_id"),
            cs.EID.TIME,
        )
        self.assertEqual(markup.inline_keyboard[0][0].callback_data, "cs:r:today")
        self.assertTrue(query.answers and query.answers[0]["text"] is None)

    async def test_mytop_foreign_presser_gets_alert(self):
        msg = _CBMsg(CHAT_A)
        other = _CBQuery(
            f"cs:m:{USER_1}:week", SimpleNamespace(id=999999), msg
        )
        await cs.board_callback(SimpleNamespace(callback_query=other), None)
        self.assertEqual(msg.edits, [], "board must not switch for a stranger")
        self.assertTrue(other.answers[0]["show_alert"])
        self.assertIn("original user", other.answers[0]["text"])

    async def test_owner_presser_switches(self):
        msg = _CBMsg(CHAT_A, chat_type="private")
        owner = SimpleNamespace(id=USER_1, username="lewin", first_name="Lewin")
        query = _CBQuery(f"cs:m:{USER_1}:week", owner, msg)
        await cs.board_callback(SimpleNamespace(callback_query=query), None)
        self.assertEqual(len(msg.edits), 1)
        self.assertIn("Top Groups", msg.edits[0]["text"])

    async def test_invalid_data_ignored(self):
        msg = _CBMsg(CHAT_A)
        for data in ("cs:x:today", "cs:r:nope", "cs:r:month",
                     "cs:m:notanint:today", "other:cb"):
            query = _CBQuery(data, SimpleNamespace(id=USER_1), msg)
            await cs.board_callback(SimpleNamespace(callback_query=query), None)
        self.assertEqual(msg.edits, [])


# ═════════════════════════════════════════════════════════════════
# Wiring, filters, help
# ═════════════════════════════════════════════════════════════════

class TestWiring(unittest.TestCase):
    def test_setup_registers_everything(self):
        from bot.command_handler import CommandHandler as PiCommandHandler

        class _App:
            def __init__(self):
                self.handlers = []

            def add_handler(self, handler, group=0):
                self.handlers.append(handler)

        app = _App()
        routes = cs.setup(app)
        self.assertIn("/rankings", routes)
        self.assertIn("/mytop", routes)

        cmd_names = set()
        n_msg = n_cb = 0
        for h in app.handlers:
            if isinstance(h, PiCommandHandler):
                cmd_names.update(h.commands)
            elif isinstance(h, PTBMessageHandler):
                n_msg += 1
            elif isinstance(h, PTBCallbackQueryHandler):
                n_cb += 1
        self.assertEqual(cmd_names, {"rankings", "mytop"})
        self.assertEqual(n_msg, 1)
        self.assertEqual(n_cb, 1)

    def test_counting_filter_accepts_text_rejects_commands_and_private(self):
        from bot.command_handler import CommandHandler as PiCommandHandler

        class _App:
            def __init__(self):
                self.handlers = []

            def add_handler(self, handler, group=0):
                self.handlers.append(handler)

        app = _App()
        cs.setup(app)
        mh = next(h for h in app.handlers if isinstance(h, PTBMessageHandler))
        check = mh.filters.check_update
        self.assertTrue(check(_real_update("hello there")))
        self.assertFalse(check(_real_update("/rankings")), "commands must not count")
        self.assertFalse(
            check(_real_update("hi", chat_type="private", chat_id=7)), "private must not count"
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
        self.assertEqual(
            cs._name_of({"user_id": 5, "first_name": "Sam", "last_name": "Lee"}),
            "Sam Lee",
        )
        self.assertEqual(
            cs._name_of({"user_id": 5, "username": "samlee"}), "samlee"
        )
        self.assertEqual(cs._name_of({"user_id": 5}), "User 5")


if __name__ == "__main__":
    unittest.main(verbosity=2)
