"""Tests for the Boa-style tagall port (Yumeko → Pi).

Run from the Pi/Pi root:

    python tests/test_tagall.py
    python -m unittest discover -s tests

Environment isolation (BEFORE any bot import):
    * BOT_TOKEN is forced — bot.config exits without one.
    * LOCALAPPDATA points at a temp dir so bot.database creates a fresh,
      empty SQLite file (a placeholder file prevents the legacy-DB copy).
No network calls: handlers run against fake bots/messages.
"""

from __future__ import annotations

import atexit
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

# ── Environment isolation — must precede bot imports ──────────────
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["BOT_TOKEN"] = "1:TEST-TOKEN-FOR-UNIT-TESTS"
_TEST_DIR = tempfile.mkdtemp(prefix="pi_tagall_test_")
os.environ["LOCALAPPDATA"] = _TEST_DIR
_PIBOT = Path(_TEST_DIR) / "PiBot"
_PIBOT.mkdir(parents=True, exist_ok=True)
(_PIBOT / "bot_database.db").touch()
atexit.register(shutil.rmtree, _TEST_DIR, ignore_errors=True)

# ── Imports (after env) ───────────────────────────────────────────
from bot.modules.tagging import (  # noqa: E402
    config,
    database as tdb,
    session as sess_mod,
    settings as settings_mod,
    tagall as tagall_mod,
)
from bot.modules.tagging.tagall import (  # noqa: E402
    EMOJI_POOL,
    at_trigger,
    etagall_command,
    parse_input,
    tagall_command,
)
from bot.modules.tagging.models import TagSettings  # noqa: E402

CHAT = -999888777
_AT_RE = re.compile(r"^@(all|eall)(?:\s|$)")
_ANCHOR_RE = re.compile(r'tg://user\?id=(\d+)">([^<]+)</a>')


def setUpModule():
    """Create tagging tables in the fresh temp DB."""
    tdb.ensure_tables()


def tearDownModule():
    """Remove all rows this suite created (temp DB is deleted at exit)."""
    for table in ("tag_settings", "tag_members", "tag_activity", "tag_sessions"):
        tdb._conn.execute(f"DELETE FROM {table} WHERE chat_id=?", (CHAT,))
    tdb._conn.commit()


class _DbCleanupMixin:
    async def asyncTearDown(self):
        sess_mod.reset()
        for table in ("tag_settings", "tag_members", "tag_activity", "tag_sessions"):
            tdb._conn.execute(f"DELETE FROM {table} WHERE chat_id=?", (CHAT,))
        tdb._conn.commit()
        await super().asyncTearDown()


# ═════════════════════════════════════════════════════════════════
# Fakes (mirror tests/test_tagging.py)
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
    def __init__(self, *, chat_id=CHAT, reply_to=None, text=None):
        self.chat = SimpleNamespace(id=chat_id, type="supergroup")
        self.reply_to_message = reply_to
        self.text = text
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


def _fake_update(msg, user_id=42):
    return SimpleNamespace(
        effective_message=msg,
        effective_chat=msg.chat,
        effective_user=SimpleNamespace(id=user_id),
    )


def _fake_context(bot=None):
    return SimpleNamespace(bot=bot or _FakeBot())


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


class _RunBot(_FakeBot):
    """Adds a send_message recorder so tagall.run() can execute."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.sent = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace(message_id=900 + len(self.sent))


def _seed(uid, name, *, bot=False):
    now = time.time()
    tdb.upsert_member(CHAT, uid, display_name=name, is_bot=bot,
                      seen_at=now - 60, active_at=now - 60)


def _run_session(status_message, *, admin_ids=frozenset({42})):
    """Real tag_sessions row + in-memory session, as _start builds."""
    st = TagSettings(chat_id=CHAT, mode="all", window_hours=0)
    sid = tdb.create_session(CHAT, 42, mode=st.mode, window_hours=st.window_hours)
    return sess_mod.create(
        chat_id=CHAT, session_id=sid, invoker_id=42,
        source_message=_fake_source(), status_message=status_message,
        settings=st, admin_ids=set(admin_ids),
    )


def _anchors(sent):
    """[(user_id, label), …] across every recorded send."""
    out = []
    for kwargs in sent:
        out.extend(_ANCHOR_RE.findall(kwargs["text"]))
    return out


# ═════════════════════════════════════════════════════════════════
# Pure logic
# ═════════════════════════════════════════════════════════════════

class TestParseInput(unittest.TestCase):
    def test_text_only(self):
        msg = _FakeMessage(text="/tagall hello there")
        self.assertEqual(parse_input(msg), ("text", "hello there"))

    def test_reply_only(self):
        msg = _FakeMessage(text="/tagall", reply_to=_fake_source())
        self.assertEqual(parse_input(msg), ("reply", ""))

    def test_text_and_reply_is_error(self):
        msg = _FakeMessage(text="/tagall hi", reply_to=_fake_source())
        self.assertEqual(parse_input(msg), ("one_arg", ""))

    def test_neither_is_error(self):
        msg = _FakeMessage(text="/tagall")
        self.assertEqual(parse_input(msg), ("no_input", ""))

    def test_bot_suffix_command_parses(self):
        msg = _FakeMessage(text="/tagall@PiModulerBot party time")
        self.assertEqual(parse_input(msg), ("text", "party time"))

    def test_at_trigger_parses(self):
        msg = _FakeMessage(text="@eall lets go")
        self.assertEqual(parse_input(msg), ("text", "lets go"))


class TestAtPattern(unittest.TestCase):
    """The exact regex registered for @all/@eall in setup()."""

    def test_matches(self):
        for s in ("@all", "@all hi", "@eall", "@eall party"):
            with self.subTest(s=s):
                self.assertIsNotNone(_AT_RE.match(s))

    def test_rejects(self):
        for s in ("@alliance", "@everyone", "all", "@allx"):
            with self.subTest(s=s):
                self.assertIsNone(_AT_RE.match(s))


class TestSpecStrings(unittest.TestCase):
    def test_boabot_pacing(self):
        self.assertEqual(config.TAGALL_BATCH_SIZE, 5)
        self.assertEqual(config.TAGALL_BATCH_DELAY, 3.0)

    def test_input_errors(self):
        self.assertEqual(config.MSG_TAGALL_ONE_ARG, "Provide only one argument!")
        self.assertEqual(
            config.MSG_TAGALL_NO_INPUT,
            "Reply to a message or provide text to mention others!",
        )

    def test_emoji_pool_is_owner_set(self):
        # Pool drawn only from the owner's custom emoji set.
        from bot.emojis import EMOJI_MAP

        owner_glyphs = {
            fb for pairs in EMOJI_MAP.values() for fb, _cid in pairs
        }
        self.assertTrue(set(EMOJI_POOL) <= owner_glyphs)
        self.assertGreater(len(EMOJI_POOL), 10)


# ═════════════════════════════════════════════════════════════════
# Command validation (boabot's checks, Pi strings)
# ═════════════════════════════════════════════════════════════════

class TestTagallCommand(_DbCleanupMixin, unittest.IsolatedAsyncioTestCase):
    async def test_private_rejected(self):
        msg = _FakeMessage(text="/tagall hi")
        upd = _fake_update(msg)
        upd.effective_chat = SimpleNamespace(id=1, type="private")
        await tagall_command(upd, _fake_context())
        self.assertIn(config.MSG_NOT_GROUP, msg.last)

    async def test_text_and_reply_rejected(self):
        msg = _FakeMessage(text="/tagall hi", reply_to=_fake_source())
        await tagall_command(_fake_update(msg), _fake_context())
        self.assertIn(config.MSG_TAGALL_ONE_ARG, msg.last)

    async def test_no_input_rejected(self):
        msg = _FakeMessage(text="/tagall")
        await tagall_command(_fake_update(msg), _fake_context())
        self.assertIn(config.MSG_TAGALL_NO_INPUT, msg.last)

    async def test_requires_admin(self):
        msg = _FakeMessage(text="/tagall hi")
        ctx = _fake_context(bot=_FakeBot(invoker_status="member"))
        await tagall_command(_fake_update(msg), ctx)
        self.assertIn(config.MSG_NOT_ADMIN, msg.last)

    async def test_running_blocks_new_start(self):
        status = _fake_status()
        _run_session(status)
        msg = _FakeMessage(text="/tagall hi")
        await tagall_command(_fake_update(msg), _fake_context())
        self.assertIn(config.MSG_RUNNING, msg.last)

    async def test_admin_fetch_failure_is_fatal(self):
        msg = _FakeMessage(text="/tagall hi")
        bot = _FakeBot()

        async def boom(chat_id):
            raise RuntimeError("nope")

        bot.get_chat_administrators = boom
        await tagall_command(_fake_update(msg), _fake_context(bot=bot))
        self.assertIn(config.MSG_ADMIN_FETCH_FAIL, msg.last)

    async def test_admin_starts_windowless_session(self):
        # Stored settings carry a /allsettings cap — tagall must ignore
        # it (boabot tags everybody, not the first N).
        settings_mod.apply_arg(CHAT, "max", "50")
        msg = _FakeMessage(text="/tagall hello all")
        with patch.object(tagall_mod, "run", new=AsyncMock()) as run_mock:
            await tagall_command(_fake_update(msg), _fake_context())
            self.assertIsNotNone(sess_mod.get(CHAT))
            s = sess_mod.get(CHAT)
            self.assertEqual(s.settings.mode, "all")  # boabot: everybody
            self.assertEqual(s.settings.window_hours, 0)
            self.assertEqual(s.settings.max_mentions, 0)  # no cap
            # Fresh MTProto enumeration on every run (falls back to the
            # observed registry when MTProto is off — no-op either way).
            self.assertEqual(s.settings.registry_mode, "sync")
            if s.task:
                await s.task
            self.assertEqual(run_mock.call_count, 1)
            kwargs = run_mock.call_args.kwargs
            self.assertEqual(kwargs["text"], "hello all")
            self.assertFalse(kwargs["reply_mode"])
            self.assertFalse(kwargs["emoji_mode"])


class TestTriggers(_DbCleanupMixin, unittest.IsolatedAsyncioTestCase):
    async def test_etagall_sets_emoji_mode(self):
        msg = _FakeMessage(text="/etagall party")
        with patch.object(tagall_mod, "run", new=AsyncMock()) as run_mock:
            await etagall_command(_fake_update(msg), _fake_context())
            s = sess_mod.get(CHAT)
            self.assertIsNotNone(s)
            if s.task:
                await s.task
            self.assertTrue(run_mock.call_args.kwargs["emoji_mode"])
            self.assertEqual(run_mock.call_args.kwargs["text"], "party")

    async def test_at_all_trigger(self):
        msg = _FakeMessage(text="@all wake up")
        with patch.object(tagall_mod, "run", new=AsyncMock()) as run_mock:
            await at_trigger(_fake_update(msg), _fake_context())
            s = sess_mod.get(CHAT)
            self.assertIsNotNone(s)
            if s.task:
                await s.task
            kwargs = run_mock.call_args.kwargs
            self.assertFalse(kwargs["emoji_mode"])
            self.assertEqual(kwargs["text"], "wake up")

    async def test_at_eall_trigger(self):
        msg = _FakeMessage(text="@eall party")
        with patch.object(tagall_mod, "run", new=AsyncMock()) as run_mock:
            await at_trigger(_fake_update(msg), _fake_context())
            s = sess_mod.get(CHAT)
            if s.task:
                await s.task
            self.assertTrue(run_mock.call_args.kwargs["emoji_mode"])


# ═════════════════════════════════════════════════════════════════
# Run loop (boabot's batching on Pi's registry)
# ═════════════════════════════════════════════════════════════════

class TestTagallRun(_DbCleanupMixin, unittest.IsolatedAsyncioTestCase):
    async def _run(self, *, text="", reply_mode=False, emoji_mode=False,
                   bot=None):
        status = _fake_status()
        s = _run_session(status)
        bot = bot or _RunBot()
        with patch.object(config, "TAGALL_BATCH_DELAY", 0):
            await tagall_mod.run(
                s, _fake_context(bot=bot),
                text=text, reply_mode=reply_mode, emoji_mode=emoji_mode,
            )
        return s, bot, status

    async def test_text_mode_batches_of_five(self):
        for i in range(12):
            _seed(201 + i, f"Member{i:02d}")
        _seed(42, "The Boss")  # admin — tagall tags admins too (boabot parity)

        s, bot, status = await self._run(text="Hello <b>all</b>")

        self.assertEqual(s.state, "completed")
        # 13 mentions → 5 + 5 + 3 (boabot's batch size).
        self.assertEqual(len(bot.sent), 3)
        # Boabot: the provided text heads EVERY batch message.
        for kwargs in bot.sent:
            self.assertTrue(
                kwargs["text"].startswith("Hello &lt;b&gt;all&lt;/b&gt;\n")
            )
        for kwargs in bot.sent:
            self.assertEqual(kwargs["chat_id"], CHAT)
            self.assertEqual(kwargs["parse_mode"], "HTML")
            self.assertNotIn("reply_to_message_id", kwargs)  # text mode
        ids = [uid for uid, _label in _anchors(bot.sent)]
        self.assertEqual(len(ids), 13)
        self.assertIn("42", ids)  # admin included — boabot parity
        self.assertEqual(s.tagged, 13)
        self.assertEqual(s.total, 13)
        self.assertIn("Tagging Complete", status.replies[-1])
        row = tdb.last_session(CHAT)
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["tagged"], 13)

    async def test_reply_mode_quotes_source(self):
        _seed(201, "Alpha")
        _seed(202, "Beta")

        s, bot, status = await self._run(reply_mode=True)

        self.assertEqual(s.state, "completed")
        self.assertEqual(len(bot.sent), 1)
        self.assertEqual(bot.sent[0]["reply_to_message_id"], 77)
        self.assertNotIn("message_thread_id", bot.sent[0])
        self.assertNotIn("\n", bot.sent[0]["text"])  # no text prefix

    async def test_emoji_mode_uses_owner_pool(self):
        _seed(201, "Alpha")
        _seed(202, "Beta")
        _seed(203, "Gamma")

        s, bot, status = await self._run(emoji_mode=True)

        anchors = _anchors(bot.sent)
        self.assertEqual(len(anchors), 3)
        for _uid, label in anchors:
            self.assertIn(label, EMOJI_POOL)
        joined = " ".join(kw["text"] for kw in bot.sent)
        for name in ("Alpha", "Beta", "Gamma"):
            self.assertNotIn(name, joined)

    async def test_cancel_mid_run(self):
        for i in range(12):
            _seed(201 + i, f"Member{i:02d}")

        class _CancelAfterFirst(_RunBot):
            session = None

            async def send_message(self, **kwargs):
                await super().send_message(**kwargs)
                type(self).session.token.cancel()

        status = _fake_status()
        s = _run_session(status)
        bot = _CancelAfterFirst()
        type(bot).session = s

        with patch.object(config, "TAGALL_BATCH_DELAY", 0):
            await tagall_mod.run(s, _fake_context(bot=bot), text="go")

        self.assertEqual(s.state, "aborted")
        self.assertEqual(len(bot.sent), 1)  # stopped after first batch
        self.assertEqual(s.tagged, 5)
        self.assertIn(
            config.MSG_STOPPED_FMT.format(tagged=5, total=12),
            status.replies[-1],
        )
        self.assertEqual(tdb.last_session(CHAT)["status"], "aborted")
        self.assertTrue(s.done.is_set())
        self.assertIsNone(sess_mod.get(CHAT))  # discarded

    async def test_nobody_card(self):
        status = _fake_status()
        s = _run_session(status)
        bot = _RunBot()

        await tagall_mod.run(s, _fake_context(bot=bot), text="hi")

        self.assertEqual(s.state, "failed")
        self.assertEqual(bot.sent, [])
        self.assertIn("No One to Tag", status.replies[-1])
        self.assertIn(config.MSG_NOBODY, status.replies[-1])
        self.assertEqual(tdb.last_session(CHAT)["error"], "nobody")

    async def test_bots_included(self):
        # Boabot parity — /tagall mentions bots and admins alike.
        _seed(201, "Alpha")
        _seed(500, "PiHelper", bot=True)

        s, bot, status = await self._run(text="go")

        ids = [uid for uid, _label in _anchors(bot.sent)]
        self.assertEqual(sorted(ids), ["201", "500"])
        self.assertEqual(s.total, 2)
        self.assertEqual(s.tagged, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
