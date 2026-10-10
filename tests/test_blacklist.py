"""Blacklist module — bot/modules/blacklist.py.

Run from the Pi root:

    python -m pytest tests/test_blacklist.py

Environment isolation (BEFORE any bot import):
    * BOT_TOKEN is forced — importing `bot` pulls bot.config, which
      exits without one.
    * LOCALAPPDATA points at a temp dir so any DB touch stays isolated.

What is under test
------------------
1. ``matches_blacklist`` — boa's whole-word, case-insensitive matcher
   (boa's original ``\\b``-free pattern matched inside longer words).
2. The management menu: coloured keyboard (green = active mode) and the
   Rich/HTML twins of the body — the SAME keyboard on both paths.
3. DB CRUD for words, stickers and the per-chat mode.
4. The seven commands and the ``bl:*`` callback router (admin gate,
   exactly one callback answer).
5. ``blacklist_check`` — mode gate, private/service/bot/sender_chat
   skips, admin exemption, word + sticker + caption hits.
6. Dispatch registration: commands at group 0, auto-detect at
   :data:`bl.BLACKLIST_GROUP` (14), clear of every occupied group.
"""

from __future__ import annotations

import atexit
import asyncio
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
_TEST_DIR = tempfile.mkdtemp(prefix="pi_blacklist_")
os.environ["LOCALAPPDATA"] = _TEST_DIR
atexit.register(shutil.rmtree, _TEST_DIR, ignore_errors=True)

# ── Imports (after env) ───────────────────────────────────────────
from aiofakes import FakeBot, FakeMessage, call, make_callback  # noqa: E402,F401

from bot import pipeline  # noqa: E402
from bot import richsend as rs  # noqa: E402
from bot.async_bridge import adb  # noqa: E402
from bot.database import db  # noqa: E402
from bot.loader import load_modules  # noqa: E402
from bot.modules import blacklist as bl  # noqa: E402

# Distinct chat ids so nothing here collides with another suite's data.
CRUD = -1009911000001
MENU = -1009911000002
D_DEL = -1009911000003
D_OFF = -1009911000004
D_ADMIN = -1009911000005
D_STICKER = -1009911000006
D_CAPTION = -1009911000007
D_SERVICE = -1009911000008
D_CALLBACK = -1009911000010
D_PRIV = -1009911000020

USER_ID = 42
ADMIN_ID = 77


def run(coro):
    return asyncio.run(coro)


def set_words(chat_id, *words):
    run(adb(db.clear_blacklist_words(chat_id)))
    for w in words:
        run(adb(db.add_blacklist_word(chat_id, w)))


def set_stickers(chat_id, *ids):
    for old in run(adb(db.get_blacklist_stickers(chat_id))):
        run(adb(db.remove_blacklist_sticker(chat_id, old)))
    for s in ids:
        run(adb(db.add_blacklist_sticker(chat_id, s)))


def set_mode(chat_id, mode):
    run(adb(db.set_blacklist_mode(chat_id, mode, 0)))


def mode_of(chat_id) -> str:
    return str(run(adb(db.get_blacklist_mode(chat_id))).get("mode") or "off")


def words_of(chat_id):
    return run(adb(db.get_blacklist_words(chat_id)))


def stickers_of(chat_id):
    return run(adb(db.get_blacklist_stickers(chat_id)))


def flat(node) -> str:
    """Flatten RichText / block JSON (str | list | dict) to plain text."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, (list, tuple)):
        return "".join(flat(x) for x in node)
    if isinstance(node, dict):
        out = []
        for key in ("text", "summary", "caption"):
            if key in node:
                out.append(flat(node[key]))
        for key in ("rows", "cells", "blocks", "items", "buttons"):
            if key in node:
                out.append(flat(node[key]))
        if not out and "alternative_text" in node:
            out.append(node["alternative_text"])
        return "".join(out)
    return str(node)


def buttons_of(markup):
    return [b for row in markup.inline_keyboard for b in row]


def styles(markup) -> dict:
    """callback_data → (text, style)."""
    return {
        b.callback_data: (b.text, getattr(b, "style", None))
        for b in buttons_of(markup)
        if b.callback_data
    }


def markup_pairs(markup):
    """(text, callback, style) tuples — for comparing two keyboards."""
    return [
        (b.text, b.callback_data, getattr(b, "style", None))
        for b in buttons_of(markup)
    ]


def admin_bot(chat_id, user_id=ADMIN_ID) -> FakeBot:
    bot = FakeBot()
    bot.chat_members[(chat_id, user_id)] = SimpleNamespace(
        user=SimpleNamespace(id=user_id, is_bot=False, first_name="A"),
        status="administrator",
    )
    return bot


class RichBot(FakeBot):
    """Accepts raw TelegramMethod instances the way ``Bot.__call__`` does."""

    def __init__(self, chat_id, user_id=ADMIN_ID):
        super().__init__()
        self.chat_members[(chat_id, user_id)] = SimpleNamespace(
            user=SimpleNamespace(id=user_id, is_bot=False, first_name="A"),
            status="administrator",
        )
        self.rich = None
        self.seen = []

    async def __call__(self, method):
        self.seen.append(method)
        self.rich = method
        return SimpleNamespace(message_id=1, chat=SimpleNamespace(id=1))


# ── The menu ──────────────────────────────────────────────────────

class TestMenu(unittest.TestCase):
    """Colours carry meaning and must not drift between renderers."""

    def test_shapes(self):
        kb = bl.build_menu_keyboard("off")
        rows = kb.inline_keyboard
        self.assertEqual(len(rows), 3)
        self.assertEqual([len(r) for r in rows], [3, 3, 3])
        self.assertEqual(len(buttons_of(kb)), 9)

    def test_active_mode_is_green_everything_else_blue(self):
        for active in bl.MODES:
            with self.subTest(active=active):
                got = styles(bl.build_menu_keyboard(active))
                for m in bl.MODES:
                    data = f"bl:mode:{m}"
                    self.assertIn(data, got)
                    text, style = got[data]
                    self.assertEqual(text, bl.MODE_LABELS[m])
                    self.assertEqual(
                        style, "success" if m == active else "primary",
                        f"{m} under active={active}",
                    )

    def test_utility_row_colours(self):
        got = styles(bl.build_menu_keyboard("off"))
        self.assertEqual(got["bl:refresh"][1], "primary")
        self.assertEqual(got["bl:clear"][1], "danger")
        self.assertEqual(got["bl:close"][1], None)

    def test_labels_are_plain_text_no_emoji(self):
        for b in buttons_of(bl.build_menu_keyboard("off")):
            self.assertFalse(
                any(ord(ch) > 0x2122 for ch in b.text),
                f"label must stay plain text: {b.text!r}",
            )

    def test_icons_present_by_default_absent_when_disabled(self):
        with_icons = buttons_of(bl.build_menu_keyboard("off"))
        without = buttons_of(bl.build_menu_keyboard("off", icons=False))
        self.assertTrue(all(getattr(b, "icon_custom_emoji_id", None)
                            for b in with_icons))
        self.assertTrue(all(getattr(b, "icon_custom_emoji_id", None) is None
                            for b in without))
        # Same labels, same callbacks — only the icon differs.
        self.assertEqual(
            [(b.text, b.callback_data) for b in with_icons],
            [(b.text, b.callback_data) for b in without],
        )

    def test_rich_body_validates(self):
        self.assertIsNotNone(
            rs.build_blocks(bl.build_menu_blocks, "My Group", ["noob"],
                            ["abc"], "ban")
        )

    def test_rich_body_sections(self):
        blocks = bl.build_menu_blocks("My Group", ["noob"], ["abc"], "ban")
        kinds = [b["type"] for b in blocks]
        for want in ("heading", "paragraph", "divider", "footer", "table"):
            self.assertIn(want, kinds)

    def test_rich_body_carries_the_mode_and_the_lists(self):
        blocks = bl.build_menu_blocks("My Group", ["noob"], ["stk1"], "ban")
        text = flat(blocks)
        self.assertIn("Blacklist", text)
        self.assertIn("My Group", text)
        self.assertIn("noob", text)
        self.assertIn("stk1", text)
        self.assertIn(bl.MODE_LABELS["ban"], text)

    def test_empty_lists_get_placeholders(self):
        text = flat(bl.build_menu_blocks("G", [], [], "off"))
        self.assertIn("No blacklisted words yet.", text)
        self.assertIn("No blacklisted stickers yet.", text)

    def test_table_truncates_with_caption(self):
        many = [f"w{i:03d}" for i in range(bl.MAX_MENU_ROWS + 5)]
        blocks = bl.build_menu_blocks("G", many, [], "off")
        tables = [b for b in blocks if b["type"] == "table"]
        self.assertEqual(len(tables), 1)
        # one header row + MAX_MENU_ROWS data rows
        self.assertEqual(len(tables[0]["cells"]), bl.MAX_MENU_ROWS + 1)
        caption = tables[0]["caption"]
        self.assertIn(str(bl.MAX_MENU_ROWS), caption)
        self.assertIn(str(len(many)), caption)

    def test_html_twin_matches_rich_payload(self):
        html = bl.build_menu_html("My Group", ["noob"], ["stk1"], "ban")
        self.assertIn("Blacklist", html)
        self.assertIn("My Group", html)
        self.assertIn("<code>noob</code>", html)
        self.assertIn("<code>stk1</code>", html)
        self.assertIn(bl.MODE_LABELS["ban"], html)
        # The usage line stays escaped (parse_mode=HTML).
        self.assertIn("&lt;word...&gt;", html)
        self.assertNotIn("<word...>", html)


# ── Matching ──────────────────────────────────────────────────────

class TestMatching(unittest.TestCase):
    def test_whole_word_only(self):
        self.assertEqual(bl.matches_blacklist("you are a noob", ["noob"]), "noob")
        self.assertIsNone(bl.matches_blacklist("noobby", ["noob"]))
        self.assertIsNone(bl.matches_blacklist("a noobish", ["noob"]))
        self.assertIsNone(bl.matches_blacklist("shamnoob", ["noob"]))

    def test_case_insensitive(self):
        self.assertEqual(bl.matches_blacklist("NOOB", ["noob"]), "noob")
        self.assertEqual(bl.matches_blacklist("noob", ["NOOB"]), "NOOB")

    def test_first_hit_wins_and_empty_is_none(self):
        # First entry of the WORD LIST that matches wins (list order).
        self.assertEqual(bl.matches_blacklist("spam and scam", ["scam", "spam"]),
                         "scam")
        self.assertEqual(bl.matches_blacklist("spam and scam", ["spam", "scam"]),
                         "spam")
        self.assertIsNone(bl.matches_blacklist("anything", []))
        self.assertIsNone(bl.matches_blacklist("", ["noob"]))
        self.assertIsNone(bl.matches_blacklist(None, ["noob"]))

    def test_pathological_word_does_not_raise(self):
        self.assertIsNone(bl.matches_blacklist("x", ["\\"]))


# ── DB CRUD ───────────────────────────────────────────────────────

class TestCrud(unittest.TestCase):
    def test_words_roundtrip(self):
        set_words(CRUD)
        self.assertEqual(words_of(CRUD), [])
        run(adb(db.add_blacklist_word(CRUD, "noob")))
        run(adb(db.add_blacklist_word(CRUD, "idiot")))
        self.assertEqual(sorted(words_of(CRUD)), ["idiot", "noob"])
        # idempotent insert
        self.assertFalse(run(adb(db.add_blacklist_word(CRUD, "noob"))))
        self.assertEqual(len(words_of(CRUD)), 2)
        self.assertTrue(run(adb(db.remove_blacklist_word(CRUD, "noob"))))
        self.assertFalse(run(adb(db.remove_blacklist_word(CRUD, "noob"))))
        self.assertEqual(words_of(CRUD), ["idiot"])
        run(adb(db.add_blacklist_word(CRUD, "fool")))
        self.assertEqual(run(adb(db.clear_blacklist_words(CRUD))), 2)
        self.assertEqual(words_of(CRUD), [])

    def test_stickers_roundtrip(self):
        set_stickers(CRUD)
        self.assertEqual(stickers_of(CRUD), [])
        run(adb(db.add_blacklist_sticker(CRUD, "AAA")))
        run(adb(db.add_blacklist_sticker(CRUD, "BBB")))
        self.assertEqual(sorted(stickers_of(CRUD)), ["AAA", "BBB"])
        self.assertTrue(run(adb(db.remove_blacklist_sticker(CRUD, "AAA"))))
        self.assertFalse(run(adb(db.remove_blacklist_sticker(CRUD, "AAA"))))
        self.assertEqual(stickers_of(CRUD), ["BBB"])

    def test_mode_default_and_roundtrip(self):
        set_mode(D_OFF, "off")
        self.assertEqual(mode_of(D_OFF), "off")
        set_mode(D_DEL, "ban")
        state = run(adb(db.get_blacklist_mode(D_DEL)))
        self.assertEqual(state["mode"], "ban")
        set_mode(D_DEL, "off")
        self.assertEqual(mode_of(D_DEL), "off")

    def test_lists_are_per_chat(self):
        set_words(CRUD, "alpha")
        set_words(D_OFF, "beta")
        self.assertEqual(words_of(CRUD), ["alpha"])
        self.assertEqual(words_of(D_OFF), ["beta"])


# ── Commands ──────────────────────────────────────────────────────

class TestCommands(unittest.TestCase):
    def setUp(self):
        set_words(MENU)
        set_stickers(MENU)
        set_mode(MENU, "off")

    def _admin_msg(self, chat_id=MENU, **kw):
        return FakeMessage(chat_id=chat_id, user_id=ADMIN_ID, **kw)

    def test_non_admin_gets_no_reply_and_no_write(self):
        msg = FakeMessage(chat_id=MENU, user_id=USER_ID)
        run(call(bl.add_blacklist, msg, bot=FakeBot(), args=["noob"]))
        self.assertEqual(msg.calls, [], "non-admins get no reply (blocklist parity)")
        self.assertEqual(words_of(MENU), [])

    def test_private_chat_rejected(self):
        msg = FakeMessage(chat_id=D_PRIV, chat_type="private", user_id=ADMIN_ID)
        run(call(bl.blacklist_command, msg, bot=admin_bot(D_PRIV)))
        self.assertTrue(msg.calls)
        self.assertIn("only works in groups", msg.sent_texts[0])

    def test_add_and_remove_words(self):
        msg = self._admin_msg()
        run(call(bl.add_blacklist, msg, bot=admin_bot(MENU),
                 args=["Noob", "noob", "idiot"]))
        self.assertIn("Added", msg.sent_texts[-1])
        self.assertEqual(sorted(words_of(MENU)), ["idiot", "noob"])

        # Second pass reports them as already blacklisted.
        again = self._admin_msg()
        run(call(bl.add_blacklist, again, bot=admin_bot(MENU), args=["noob"]))
        self.assertIn("Already blacklisted", again.sent_texts[-1])

        out = self._admin_msg()
        run(call(bl.remove_blacklist, out, bot=admin_bot(MENU),
                 args=["noob", "ghost"]))
        self.assertIn("Removed", out.sent_texts[-1])
        self.assertIn("Not blacklisted", out.sent_texts[-1])
        self.assertEqual(words_of(MENU), ["idiot"])

    def test_add_from_reply_message(self):
        reply = FakeMessage(text="damn this sucks", chat_id=MENU)
        msg = self._admin_msg(reply_to_message=reply)
        run(call(bl.add_blacklist, msg, bot=admin_bot(MENU), args=[]))
        self.assertEqual(sorted(words_of(MENU)), ["damn", "sucks", "this"])

    def test_add_without_words_prints_usage(self):
        msg = self._admin_msg()
        run(call(bl.add_blacklist, msg, bot=admin_bot(MENU), args=[]))
        self.assertIn("Usage", msg.sent_texts[-1])
        self.assertEqual(words_of(MENU), [])

    def test_mode_command_valid_and_invalid(self):
        msg = self._admin_msg()
        run(call(bl.blacklist_mode, msg, bot=admin_bot(MENU), args=["ban"]))
        self.assertEqual(mode_of(MENU), "ban")
        self.assertIn("Ban", msg.sent_texts[-1])

        bad = self._admin_msg()
        run(call(bl.blacklist_mode, bad, bot=admin_bot(MENU), args=["nuke"]))
        self.assertEqual(mode_of(MENU), "ban", "bad input must not change mode")
        usage = bad.sent_texts[-1]
        self.assertIn("Usage", usage)
        self.assertIn("&lt;off|del|warn|mute|kick|ban&gt;", usage)
        self.assertNotIn("<off|", usage)

    def test_menu_rich_and_html_paths_share_one_keyboard(self):
        set_words(MENU, "noob")
        rb = RichBot(MENU)
        msg = self._admin_msg()
        run(call(bl.blacklist_command, msg, bot=rb))
        self.assertIsNotNone(rb.rich, "rich path not attempted")
        self.assertEqual(msg.calls, [], "rich must not also send an HTML copy")

        saved = rs.RICH_ENABLED
        rs.RICH_ENABLED = False
        try:
            msg2 = self._admin_msg()
            run(call(bl.blacklist_command, msg2, bot=admin_bot(MENU)))
        finally:
            rs.RICH_ENABLED = saved

        self.assertEqual(
            markup_pairs(rb.rich.reply_markup),
            markup_pairs(msg2.calls[-1][2]["reply_markup"]),
            "the two render paths must carry the identical keyboard",
        )
        self.assertIn("noob", msg2.sent_texts[-1])

    def test_rich_failure_falls_back_to_html_with_the_same_keyboard(self):
        set_words(MENU, "noob")

        class RejectingBot(RichBot):
            async def __call__(self, method):
                self.seen.append(method)
                raise RuntimeError("style enum rejected")

        rb = RejectingBot(MENU)
        msg = self._admin_msg()
        run(call(bl.blacklist_command, msg, bot=rb))
        self.assertTrue(msg.calls, "the HTML fallback never ran")
        self.assertEqual(markup_pairs(msg.calls[-1][2]["reply_markup"]),
                         markup_pairs(bl.build_menu_keyboard("off")))

    def test_sticker_commands(self):
        sticker = SimpleNamespace(file_unique_id="UNIQ1")
        reply = FakeMessage(chat_id=MENU, sticker=sticker)
        msg = self._admin_msg(reply_to_message=reply)

        run(call(bl.add_blacklist_sticker, msg, bot=admin_bot(MENU)))
        self.assertEqual(stickers_of(MENU), ["UNIQ1"])
        self.assertIn("Sticker blacklisted", msg.sent_texts[-1])

        dup = self._admin_msg(reply_to_message=reply)
        run(call(bl.add_blacklist_sticker, dup, bot=admin_bot(MENU)))
        self.assertIn("already blacklisted", dup.sent_texts[-1])

        listing = self._admin_msg()
        run(call(bl.list_blacklist_stickers, listing, bot=admin_bot(MENU)))
        self.assertIn("UNIQ1", listing.sent_texts[-1])

        drop = self._admin_msg(reply_to_message=reply)
        run(call(bl.remove_blacklist_sticker, drop, bot=admin_bot(MENU), args=[]))
        self.assertEqual(stickers_of(MENU), [])
        self.assertIn("Sticker removed", drop.sent_texts[-1])

    def test_addblsticker_without_a_sticker_replies(self):
        msg = self._admin_msg()
        run(call(bl.add_blacklist_sticker, msg, bot=admin_bot(MENU)))
        self.assertIn("Reply to a sticker", msg.sent_texts[-1])

    def test_blsticker_empty_list(self):
        listing = self._admin_msg()
        run(call(bl.list_blacklist_stickers, listing, bot=admin_bot(MENU)))
        self.assertIn("No blacklisted stickers", listing.sent_texts[-1])


# ── Auto-detect ───────────────────────────────────────────────────

class TestCheck(unittest.TestCase):
    def setUp(self):
        for chat in (D_DEL, D_OFF, D_ADMIN, D_STICKER, D_CAPTION, D_SERVICE):
            set_words(chat, "noob")
            set_stickers(chat)
            set_mode(chat, "del")

    def _check(self, chat_id, **kw):
        msg = FakeMessage(chat_id=chat_id, user_id=USER_ID, **kw)
        run(call(bl.blacklist_check, msg, bot=FakeBot()))
        return msg

    @staticmethod
    def _deleted(msg) -> bool:
        return any(c[0] == "delete" for c in msg.calls)

    def test_private_chat_ignored(self):
        msg = FakeMessage(text="noob", chat_id=D_PRIV, chat_type="private",
                          user_id=USER_ID)
        run(call(bl.blacklist_check, msg, bot=FakeBot()))
        self.assertEqual(msg.calls, [])

    def test_service_update_ignored(self):
        msg = self._check(D_SERVICE, text="noob",
                          new_chat_members=[SimpleNamespace(id=9)])
        self.assertEqual(msg.calls, [])

    def test_bot_sender_ignored(self):
        msg = FakeMessage(text="noob", chat_id=D_DEL, user_id=1, is_bot=True)
        run(call(bl.blacklist_check, msg, bot=FakeBot()))
        self.assertEqual(msg.calls, [])

    def test_sender_chat_ignored(self):
        """Linked-channel posts and anonymous admins have no human to act on."""
        msg = self._check(D_DEL, text="noob",
                          sender_chat=SimpleNamespace(id=-1001, type="channel"))
        self.assertEqual(msg.calls, [])

    def test_mode_off_never_enforces(self):
        set_mode(D_OFF, "off")
        msg = self._check(D_OFF, text="noob")
        self.assertEqual(msg.calls, [])

    def test_mode_del_deletes_without_a_card(self):
        set_mode(D_DEL, "del")
        bot = FakeBot()
        msg = FakeMessage(text="what a noob", chat_id=D_DEL, user_id=USER_ID)
        run(call(bl.blacklist_check, msg, bot=bot))
        self._assert_deleted_and_no_card(msg, bot)

    def _assert_deleted_and_no_card(self, msg, bot):
        self.assertTrue(self._deleted(msg), f"not deleted: {msg.calls}")
        self.assertEqual(bot.sent, [], "mode=del must not post a card")

    def test_caption_hits_too(self):
        set_mode(D_CAPTION, "del")
        msg = self._check(D_CAPTION, text=None,
                          caption="caption says noob here")
        self._assert_deleted_and_no_card(msg, FakeBot())

    def test_no_hit_is_a_noop(self):
        set_mode(D_DEL, "del")
        msg = self._check(D_DEL, text="totally innocent")
        self.assertEqual(msg.calls, [])

    def test_admin_is_exempt(self):
        set_mode(D_ADMIN, "del")
        msg = FakeMessage(text="noob", chat_id=D_ADMIN, user_id=ADMIN_ID)
        run(call(bl.blacklist_check, msg, bot=admin_bot(D_ADMIN)))
        self.assertEqual(msg.calls, [],
                         "admins configure the list, they are not subject to it")

    def test_sticker_hit_by_unique_id(self):
        set_words(D_STICKER)
        set_stickers(D_STICKER, "UNIQ-X")
        set_mode(D_STICKER, "del")
        msg = self._check(D_STICKER, text=None,
                          sticker=SimpleNamespace(file_unique_id="UNIQ-X"))
        self._assert_deleted_and_no_card(msg, FakeBot())

        # A different sticker (even from the same pack) must not trip it.
        msg2 = self._check(D_STICKER, text=None,
                           sticker=SimpleNamespace(file_unique_id="UNIQ-Y"))
        self.assertEqual(msg2.calls, [])

    def test_ban_mode_deletes_and_reports(self):
        set_mode(D_DEL, "ban")
        bot = FakeBot()
        msg = FakeMessage(text="noob", chat_id=D_DEL, user_id=USER_ID)
        run(call(bl.blacklist_check, msg, bot=bot))
        self.assertTrue(self._deleted(msg))
        self.assertTrue(bot.sent, "an outcome card must be posted")
        self.assertIn("Blacklisted Content", bot.sent[0]["text"])

    def test_warn_mode_reuses_the_warn_counter(self):
        from bot.modules import moderation

        set_mode(D_DEL, "warn")
        moderation.warnings_db.pop(D_DEL, None)
        try:
            bot = FakeBot()
            msg = FakeMessage(text="noob", chat_id=D_DEL, user_id=USER_ID)
            run(call(bl.blacklist_check, msg, bot=bot))
            self.assertTrue(self._deleted(msg))
            self.assertTrue(bot.sent)
            self.assertIn("Warnings 1/", bot.sent[0]["text"])

            msg2 = FakeMessage(text="noob", chat_id=D_DEL, user_id=USER_ID,
                               message_id=2)
            run(call(bl.blacklist_check, msg2, bot=bot))
            self.assertIn("Warnings 2/", bot.sent[-1]["text"])
        finally:
            moderation.warnings_db.pop(D_DEL, None)


# ── Callback router ───────────────────────────────────────────────

class TestCallback(unittest.TestCase):
    def setUp(self):
        set_mode(D_CALLBACK, "off")

    def _cb(self, data, *, user_id=ADMIN_ID):
        msg = FakeMessage(text="menu", chat_id=D_CALLBACK, title="G")
        return make_callback(data, message=msg, user_id=user_id), msg

    def test_non_admin_is_rejected_with_one_alert(self):
        cb, msg = self._cb("bl:mode:ban", user_id=USER_ID)
        run(call(bl.blacklist_callback, cb, bot=FakeBot()))
        self.assertEqual(mode_of(D_CALLBACK), "off")
        self.assertEqual(len(cb.answers), 1, "exactly one callback answer")
        self.assertEqual(cb.answers[0]["text"], "Admins only.")
        self.assertTrue(cb.answers[0]["show_alert"])

    def test_mode_switch_persists_and_refreshes(self):
        cb, msg = self._cb("bl:mode:ban")
        run(call(bl.blacklist_callback, cb, bot=admin_bot(D_CALLBACK)))
        self.assertEqual(mode_of(D_CALLBACK), "ban")
        self.assertEqual(len(cb.answers), 1)
        self.assertTrue(msg.calls, "the menu must be re-rendered")
        self.assertEqual(msg.calls[-1][0], "edit_text")

    def test_unknown_mode_answers_once_and_changes_nothing(self):
        cb, msg = self._cb("bl:mode:nuke")
        run(call(bl.blacklist_callback, cb, bot=admin_bot(D_CALLBACK)))
        self.assertEqual(mode_of(D_CALLBACK), "off")
        self.assertEqual(len(cb.answers), 1,
                         "Telegram honours one answer — never two")
        self.assertEqual(cb.answers[0]["text"], "Unknown option.")
        self.assertTrue(cb.answers[0]["show_alert"])
        self.assertEqual(msg.calls, [], "an invalid option must not edit")

    def test_unknown_action_answers_once(self):
        cb, msg = self._cb("bl:what")
        run(call(bl.blacklist_callback, cb, bot=admin_bot(D_CALLBACK)))
        self.assertEqual(len(cb.answers), 1)
        self.assertEqual(cb.answers[0]["text"], "Unknown option.")

    def test_refresh_re_renders_without_changing_mode(self):
        set_mode(D_CALLBACK, "mute")
        cb, msg = self._cb("bl:refresh")
        run(call(bl.blacklist_callback, cb, bot=admin_bot(D_CALLBACK)))
        self.assertEqual(mode_of(D_CALLBACK), "mute")
        self.assertEqual(cb.answers[0], {"text": None, "show_alert": False})
        self.assertTrue(msg.calls)

    def test_clear_wipes_the_word_list(self):
        set_words(D_CALLBACK, "noob", "idiot")
        cb, msg = self._cb("bl:clear")
        run(call(bl.blacklist_callback, cb, bot=admin_bot(D_CALLBACK)))
        self.assertEqual(words_of(D_CALLBACK), [])
        self.assertTrue(msg.calls)

    def test_close_deletes_the_menu_message(self):
        cb, msg = self._cb("bl:close")
        run(call(bl.blacklist_callback, cb, bot=admin_bot(D_CALLBACK)))
        self.assertEqual(len(cb.answers), 1)
        self.assertTrue(any(c[0] == "delete" for c in msg.calls),
                        f"close did not delete: {msg.calls}")

    def test_foreign_data_is_ignored(self):
        cb, msg = self._cb("something:else")
        run(call(bl.blacklist_callback, cb, bot=admin_bot(D_CALLBACK)))
        self.assertEqual(cb.answers, [], "prefix guard must answer nothing")


# ── Registration ──────────────────────────────────────────────────

class TestRegistration(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        load_modules()
        cls.entries = pipeline.snapshot()

    def _by_qualname(self, qual):
        return [e for e in self.entries if e.fn.__qualname__ == qual]

    def test_commands_at_default_group(self):
        for qual in ("blacklist_command", "add_blacklist", "remove_blacklist",
                     "blacklist_mode", "list_blacklist_stickers",
                     "add_blacklist_sticker", "remove_blacklist_sticker"):
            with self.subTest(qual=qual):
                found = self._by_qualname(qual)
                self.assertEqual(len(found), 1, f"{qual} registered {len(found)}x")
                self.assertEqual(found[0].event, "message")
                self.assertEqual(found[0].group, 0)

    def test_callback_registered_once(self):
        found = self._by_qualname("blacklist_callback")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].event, "callback_query")

    def test_auto_detect_lives_in_a_free_group(self):
        found = self._by_qualname("blacklist_check")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].group, bl.BLACKLIST_GROUP)
        self.assertEqual(found[0].event, "message")
        self.assertIn(bl.BLACKLIST_GROUP, range(14, 18),
                      "auto-detect must stay in the documented free window")

    def test_filter_rejects_commands_and_private_chats(self):
        from aiogram.dispatcher.event.handler import FilterObject, HandlerObject

        entry = self._by_qualname("blacklist_check")[0]
        handler = HandlerObject(callback=entry.fn,
                                filters=[FilterObject(entry.flt)])
        bot = FakeBot()

        async def fires(text, **kw):
            msg = FakeMessage(text=text, **kw)
            ok, _ = await handler.check(msg, bot=bot)
            return ok

        # Plain group text reaches it…
        self.assertTrue(run(fires("hello there", chat_id=D_DEL)))
        # …a command does not, and neither does a private chat.
        self.assertFalse(run(fires("/blacklist", chat_id=D_DEL)))
        self.assertFalse(run(fires("hello", chat_id=D_PRIV,
                                   chat_type="private")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
