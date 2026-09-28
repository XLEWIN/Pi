"""Tests for the #BOT_ADDED / #BOT_REMOVED own-membership logs.

Run from the repo root:

    python tests/test_bot_logs.py
    python -m unittest discover -s tests

Environment isolation (BEFORE any bot import):
    * BOT_TOKEN is forced — importing `bot` pulls bot.config, which
      exits without one.
    * LOCALAPPDATA points at a temp dir so runtime files are isolated.

No network: the handler runs against a duck ChatMemberUpdated; send_log
is patched.
"""

from __future__ import annotations

import atexit
import asyncio
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

# ── Environment isolation — must precede bot imports ──────────────
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["BOT_TOKEN"] = "1:TEST-TOKEN-FOR-UNIT-TESTS"
_TEST_DIR = tempfile.mkdtemp(prefix="pi_botlogs_test_")
os.environ["LOCALAPPDATA"] = _TEST_DIR
atexit.register(shutil.rmtree, _TEST_DIR, ignore_errors=True)

# ── Imports (after env) ───────────────────────────────────────────
from aiofakes import call  # noqa: E402

from bot.modules import users as um  # noqa: E402


def _chat(cid=-1004331827383, title="Fairy sakti degi mukti", username=None):
    return SimpleNamespace(id=cid, title=title, first_name=None, username=username)


def _actor(uid=7544264351, name="_fex_r_", username="NATSU123477"):
    return SimpleNamespace(
        id=uid, first_name=name, last_name=None, full_name=name, username=username
    )


class TestFormatBotLog(unittest.TestCase):
    """Line-for-line checks against the owner's sample templates."""

    def _fixed(self, when: datetime):
        return mock.patch.object(
            um, "datetime", SimpleNamespace(now=lambda: when)
        )

    def test_added_matches_sample(self):
        with self._fixed(datetime(2026, 9, 26, 1, 53, 24)):
            got = um.format_bot_log("added", _chat(), _actor(), "22")
        expected = (
            "#BOT_ADDED\n"
            "\n"
            "ᴄʜᴀᴛ ɴᴀᴍᴇ : Fairy sakti degi mukti\n"
            "ᴄʜᴀᴛ ɪᴅ : -1004331827383\n"
            "ᴄʜᴀᴛ ᴜsᴇʀɴᴀᴍᴇ : No username\n"
            "ɢʀᴏᴜᴘ ᴍᴇᴍʙᴇʀs : 22\n"
            "\n"
            "ᴀᴅᴅᴇᴅ ʙʏ : _fex_r_\n"
            "ᴀᴅᴅᴇʀ ᴜsᴇʀɴᴀᴍᴇ : @NATSU123477\n"
            "ᴀᴅᴅᴇʀ ɪᴅ : 7544264351\n"
            "\n"
            "ᴄʜᴀᴛ ʟɪɴᴋ : https://t.me/c/4331827383\n"
            "ᴛɪᴍᴇ : 2026-09-26 01:53:24 AM"
        )
        self.assertEqual(got, expected)

    def test_removed_matches_sample(self):
        title = "✨⚘𝛵𝜢𝛦 𝜢𝜜𝑽𝜩𝐿𝜤ཀ 💗✨"
        actor = SimpleNamespace(
            id=8758820466, first_name="- ꧊᱂", last_name="𝛆 ⲛ !!",
            full_name="- ꧊᱂ 𝛆 ⲛ !!", username="Hiiqt_ego",
        )
        with self._fixed(datetime(2026, 9, 25, 11, 50, 35)):
            got = um.format_bot_log(
                "removed",
                _chat(cid=-1004403437091, title=title),
                actor,
                "Unknown",
            )
        expected = (
            "#BOT_REMOVED\n"
            "\n"
            f"ᴄʜᴀᴛ ɴᴀᴍᴇ : {title}\n"
            "ᴄʜᴀᴛ ɪᴅ : -1004403437091\n"
            "ᴄʜᴀᴛ ᴜsᴇʀɴᴀᴍᴇ : No username\n"
            "ɢʀᴏᴜᴘ ᴍᴇᴍʙᴇʀs : Unknown\n"
            "\n"
            "ʀᴇᴍᴏᴠᴇᴅ ʙʏ : - ꧊᱂ 𝛆 ⲛ !!\n"
            "ʀᴇᴍᴏᴠᴇʀ ᴜsᴇʀɴᴀᴍᴇ : @Hiiqt_ego\n"
            "ʀᴇᴍᴏᴠᴇʀ ɪᴅ : 8758820466\n"
            "\n"
            "ᴄʜᴀᴛ ʟɪɴᴋ : https://t.me/c/4403437091\n"
            "ᴛɪᴍᴇ : 2026-09-25 11:50:35 AM"
        )
        self.assertEqual(got, expected)

    def test_public_chat_shows_username(self):
        got = um.format_bot_log(
            "added", _chat(cid=-100111, title="Pub", username="pubchat"),
            _actor(), "10",
        )
        self.assertIn("ᴄʜᴀᴛ ᴜsᴇʀɴᴀᴍᴇ : @pubchat", got)
        self.assertIn("ᴄʜᴀᴛ ʟɪɴᴋ : https://t.me/pubchat", got)

    def test_actor_without_username(self):
        got = um.format_bot_log(
            "added", _chat(), _actor(username=None), "5"
        )
        self.assertIn("ᴀᴅᴅᴇʀ ᴜsᴇʀɴᴀᴍᴇ : No username", got)

    def test_html_escaped_fields(self):
        got = um.format_bot_log(
            "added", _chat(title="<b>&evil</b>"), _actor(name="<i>x</i>"), "1"
        )
        self.assertIn("&lt;b&gt;&amp;evil&lt;/b&gt;", got)
        self.assertIn("&lt;i&gt;x&lt;/i&gt;", got)
        self.assertNotIn("<b>", got)


class TestChatLink(unittest.TestCase):
    def test_private_group_uses_c_form(self):
        self.assertEqual(um._chat_link(_chat()), "https://t.me/c/4331827383")

    def test_public_uses_username(self):
        self.assertEqual(
            um._chat_link(_chat(username="mygroup")), "https://t.me/mygroup"
        )

    def test_no_username_no_prefix(self):
        self.assertEqual(
            um._chat_link(_chat(cid=12345, title="DM")), "No link"
        )


def _cmu(old, new, actor, chat=None):
    """Duck ChatMemberUpdated — statuses are Telegram's plain strings."""
    return SimpleNamespace(
        chat=chat or _chat(),
        from_user=actor,
        old_chat_member=SimpleNamespace(status=old),
        new_chat_member=SimpleNamespace(status=new),
    )


class _FakeBot:
    def __init__(self, count=22, fail=False):
        self._count = count
        self._fail = fail

    async def get_chat_member_count(self, chat_id):
        if self._fail:
            raise RuntimeError("forbidden")
        return self._count


class TestHandleBotMembership(unittest.IsolatedAsyncioTestCase):
    async def _run(self, event, count=22, fail=False):
        captured = []

        async def fake_send_log(bot, message):
            captured.append(message)

        with mock.patch.object(um, "send_log", fake_send_log):
            await call(um.handle_bot_membership, event,
                       bot=_FakeBot(count=count, fail=fail))
            # let the created send_log task run
            await asyncio.sleep(0.01)
        return captured

    async def test_left_to_member_sends_added(self):
        cmu = _cmu("left", "member", _actor())
        captured = await self._run(cmu)
        self.assertEqual(len(captured), 1)
        self.assertTrue(captured[0].startswith("#BOT_ADDED\n"))
        self.assertIn("ɢʀᴏᴜᴘ ᴍᴇᴍʙᴇʀs : 22", captured[0])

    async def test_member_to_banned_sends_removed_unknown(self):
        cmu = _cmu("member", "kicked", _actor())
        captured = await self._run(cmu)
        self.assertEqual(len(captured), 1)
        self.assertTrue(captured[0].startswith("#BOT_REMOVED\n"))
        self.assertIn("ɢʀᴏᴜᴘ ᴍᴇᴍʙᴇʀs : Unknown", captured[0])

    async def test_promotion_is_not_an_add(self):
        cmu = _cmu("member", "administrator", _actor())
        captured = await self._run(cmu)
        self.assertEqual(captured, [])

    async def test_demotion_is_not_a_remove(self):
        cmu = _cmu("administrator", "member", _actor())
        captured = await self._run(cmu)
        self.assertEqual(captured, [])

    async def test_count_failure_falls_back_to_unknown(self):
        cmu = _cmu("left", "administrator", _actor())
        captured = await self._run(cmu, fail=True)
        self.assertEqual(len(captured), 1)
        self.assertIn("ɢʀᴏᴜᴘ ᴍᴇᴍʙᴇʀs : Unknown", captured[0])

    async def test_missing_actor_skips(self):
        cmu = _cmu("left", "member", None)
        captured = await self._run(cmu)
        self.assertEqual(captured, [])

    async def test_no_my_chat_member_is_ignored(self):
        captured = await self._run(None)
        self.assertEqual(captured, [])


if __name__ == "__main__":
    unittest.main()
