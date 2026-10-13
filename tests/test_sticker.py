"""Tests for the sticker suite (bot/modules/sticker.py).

Run from the Pi/Pi root:

    python tests/test_sticker.py
    python -m unittest discover -s tests

Environment isolation (BEFORE any bot import):
    * BOT_TOKEN is forced — importing `bot` pulls bot.config, which
      exits without one.
    * LOCALAPPDATA points at a temp dir so any DB touch stays isolated.

No network: pure helpers are called directly; handlers run against
fakes and the Bot API sticker-set seam (``get_sticker_set`` /
``upload_sticker_file`` / ``create_new_sticker_set`` /
``add_sticker_to_set`` / ``delete_sticker_from_set``) is implemented by
``_WorkingBot``, so no Telegram, MTProto or ffmpeg call ever happens.
The one real dependency is PIL, used by the resize round-trip test.

Handlers are aiogram-era: ``await call(fn, event, bot=...)`` passes
only the deps each real signature declares.
"""

from __future__ import annotations

import atexit
import inspect
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
_TEST_DIR = tempfile.mkdtemp(prefix="pi_sticker_test_")
os.environ["LOCALAPPDATA"] = _TEST_DIR
atexit.register(shutil.rmtree, _TEST_DIR, ignore_errors=True)

# ── Imports (after env) ───────────────────────────────────────────
from aiofakes import call, command_filters  # noqa: E402
from aiogram.exceptions import TelegramBadRequest  # noqa: E402
from bot import pipeline  # noqa: E402
from bot.constants import HELP_MENU  # noqa: E402
from bot.modules import sticker as sm  # noqa: E402


USER_ID = 42


# ═════════════════════════════════════════════════════════════════
# Fakes
# ═════════════════════════════════════════════════════════════════

class _Prog:
    """Progress message returned by reply_text — records edit_text/delete."""

    def __init__(self) -> None:
        self.edits: list = []
        self.deleted = False

    async def edit_text(self, text, **kw):
        self.edits.append({"text": text, **kw})
        return self

    async def delete(self):
        self.deleted = True

    @property
    def last(self):
        return self.edits[-1] if self.edits else None


class _Msg:
    """Command message — records every reply_* call (bot.reply helpers)."""

    def __init__(self, text: str = "/kang", reply=None, entities=None,
                 user_id=USER_ID):
        self.text = text
        self.reply_to_message = reply
        self.entities = entities or []
        self.chat = SimpleNamespace(id=1, type="private")
        self.from_user = (
            None if user_id is None
            else SimpleNamespace(id=user_id, username=None, first_name="Lewin")
        )
        self.replies: list = []          # reply_text kwargs dicts
        self.prog = None               # last progress message (edits live here)
        self.documents: list = []        # reply_document kwargs
        self.animations: list = []
        self.videos: list = []
        self.stickers_sent: list = []

    # reply_text → answer (private chat) / reply (group) — bot.reply semantics
    async def _record_text(self, text, **kw):
        self.replies.append({"text": text, **kw})
        self.prog = _Prog()
        return self.prog

    async def answer(self, text, **kw):
        return await self._record_text(text, **kw)

    async def reply(self, text, **kw):
        return await self._record_text(text, **kw)

    async def answer_document(self, document=None, **kw):
        self.documents.append({"document": document, **kw})
        return SimpleNamespace(message_id=2)

    async def reply_document(self, document=None, **kw):
        return await self.answer_document(document, **kw)

    async def answer_animation(self, animation=None, **kw):
        self.animations.append({"animation": animation, **kw})
        return SimpleNamespace(message_id=2)

    async def reply_animation(self, animation=None, **kw):
        return await self.answer_animation(animation, **kw)

    async def answer_video(self, video=None, **kw):
        self.videos.append({"video": video, **kw})
        return SimpleNamespace(message_id=2)

    async def reply_video(self, video=None, **kw):
        return await self.answer_video(video, **kw)

    async def answer_sticker(self, sticker=None, **kw):
        self.stickers_sent.append({"sticker": sticker, **kw})
        return SimpleNamespace(message_id=2)

    async def reply_sticker(self, sticker=None, **kw):
        return await self.answer_sticker(sticker, **kw)

    @property
    def last(self):
        return self.replies[-1] if self.replies else None


class _FakeBot:
    """context.bot — download/network entry points must never fire."""

    def __init__(self) -> None:
        self.calls: list = []

    async def get_file(self, file_id):
        self.calls.append(("get_file", file_id))
        raise AssertionError("network download attempted in unit test")

    async def send_sticker(self, chat_id, sticker=None, **kw):
        self.calls.append(("send_sticker", chat_id))
        raise AssertionError("network send attempted in unit test")

    async def delete_message(self, chat_id, message_id, **kw):
        self.calls.append(("delete_message", chat_id, message_id))

    # ── sticker-set seam: a pack call here is an unexpected call ──
    async def get_sticker_set(self, name):
        self.calls.append(("get_sticker_set", name))
        raise AssertionError("pack lookup attempted in unit test")

    async def upload_sticker_file(self, **kw):
        self.calls.append(("upload_sticker_file", kw))
        raise AssertionError("sticker upload attempted in unit test")

    async def create_new_sticker_set(self, **kw):
        self.calls.append(("create_new_sticker_set", kw))
        raise AssertionError("pack create attempted in unit test")

    async def add_sticker_to_set(self, **kw):
        self.calls.append(("add_sticker_to_set", kw))
        raise AssertionError("pack add attempted in unit test")

    async def delete_sticker_from_set(self, sticker=None, **kw):
        self.calls.append(("delete_sticker_from_set", sticker))
        raise AssertionError("pack remove attempted in unit test")


def _sticker(file_id="STFILE", set_name="a_42_by_PiModulerBot", emoji="🔥",
             animated=False, video=False, file_name="sticker.webp",
             unique="UNIQUE1", date=None):
    return SimpleNamespace(
        file_id=file_id, set_name=set_name, emoji=emoji,
        is_animated=animated, is_video=video, file_name=file_name,
        file_unique_id=unique, date=date or datetime(2026, 1, 1, 12, 0),
    )


def _reply(sticker=None, photo=None, animation=None, video=None, document=None):
    return SimpleNamespace(
        sticker=sticker, photo=photo, animation=animation,
        video=video, document=document,
    )


def _bad_request(message: str) -> "TelegramBadRequest":
    """aiogram BadRequest shaped like the real one (needs a method)."""
    method = SimpleNamespace(__api_method__="getStickerSet")
    return TelegramBadRequest(method=method, message=message)


def _style(btn):
    return getattr(btn, "style", None)


# ═════════════════════════════════════════════════════════════════
# Pure helpers
# ═════════════════════════════════════════════════════════════════

class TestPackName(unittest.TestCase):
    def test_bare_pack_matches_boabot(self):
        self.assertEqual(
            sm.pack_name("a", 42, 0, "PiModulerBot"),
            "a_42_by_PiModulerBot",
        )

    def test_numbered_pack(self):
        self.assertEqual(
            sm.pack_name("a", 42, 2, "PiModulerBot"),
            "a2_42_by_PiModulerBot",
        )

    def test_prefixes(self):
        self.assertTrue(sm.pack_name("anim", 7, 0, "B").startswith("anim_7_"))
        self.assertTrue(sm.pack_name("vid", 7, 1, "B").startswith("vid1_7_"))

    def test_user_segment_strict_check_holds(self):
        name = sm.pack_name("a", 42, 0, "PiModulerBot")
        self.assertIn("_42_", name)


class TestArgsParsing(unittest.TestCase):
    def test_pack_number_popped(self):
        self.assertEqual(sm.split_pack_and_rest(["5", "🔥"]), (5, ["🔥"]))

    def test_no_number(self):
        self.assertEqual(sm.split_pack_and_rest(["🔥"]), (0, ["🔥"]))

    def test_zero_stays_in_rest(self):
        self.assertEqual(sm.split_pack_and_rest(["0", "🔥"]), (0, ["0", "🔥"]))

    def test_non_numeric_stays(self):
        self.assertEqual(sm.split_pack_and_rest(["abc"]), (0, ["abc"]))

    def test_empty(self):
        self.assertEqual(sm.split_pack_and_rest([]), (0, []))

    def test_strip_url_tokens(self):
        self.assertEqual(
            sm.strip_url_tokens(["https://x.co/a.png", "3", "🔥"]),
            ["3", "🔥"],
        )


class TestFirstEmoji(unittest.TestCase):
    def test_plain_text_returns_empty(self):
        self.assertEqual(sm.first_emoji("just words"), "")

    def test_simple_emoji(self):
        self.assertEqual(sm.first_emoji("hello 🔥 world"), "🔥")

    def test_emoji_among_digits(self):
        self.assertEqual(sm.first_emoji("5 😂"), "😂")

    def test_zwj_sequence_stays_whole(self):
        fam = "\U0001f468\u200d\U0001f469\u200d\U0001f467"
        self.assertEqual(sm.first_emoji(fam), fam)

    def test_adjacent_flags_truncate_to_first_pair(self):
        flags = "\U0001f1ee\U0001f1f3\U0001f1f5\U0001f1f0"  # 🇮🇳🇵🇰
        self.assertEqual(sm.first_emoji(flags), "\U0001f1ee\U0001f1f3")

    def test_empty(self):
        self.assertEqual(sm.first_emoji(""), "")


class TestClassifyMedia(unittest.TestCase):
    def test_none_reply(self):
        self.assertEqual(sm.classify_media(None), (None, None))

    def test_photo_uses_largest_size(self):
        sizes = [SimpleNamespace(file_id="small"), SimpleNamespace(file_id="big")]
        self.assertEqual(sm.classify_media(_reply(photo=sizes)), ("resize", "big"))

    def test_animation_converts(self):
        self.assertEqual(
            sm.classify_media(_reply(animation=SimpleNamespace(file_id="G"))),
            ("convert", "G"),
        )

    def test_video_converts(self):
        self.assertEqual(
            sm.classify_media(_reply(video=SimpleNamespace(file_id="V"))),
            ("convert", "V"),
        )

    def test_document_image_resizes(self):
        doc = SimpleNamespace(file_id="D", mime_type="image/webp", file_name="x.webp")
        self.assertEqual(sm.classify_media(_reply(document=doc)), ("resize", "D"))

    def test_document_tgs_passthrough(self):
        doc = SimpleNamespace(file_id="D", mime_type="application/x-tgsticker", file_name="s.tgs")
        self.assertEqual(sm.classify_media(_reply(document=doc)), ("animated", "D"))

    def test_document_video_converts(self):
        doc = SimpleNamespace(file_id="D", mime_type="video/mp4", file_name="v.mp4")
        self.assertEqual(sm.classify_media(_reply(document=doc)), ("convert", "D"))

    def test_document_unknown_mime_rejected(self):
        doc = SimpleNamespace(file_id="D", mime_type="application/zip", file_name="z.zip")
        self.assertEqual(sm.classify_media(_reply(document=doc)), (None, None))

    def test_static_sticker_resizes(self):
        self.assertEqual(
            sm.classify_media(_reply(sticker=_sticker())), ("resize", "STFILE")
        )

    def test_animated_sticker_passthrough(self):
        self.assertEqual(
            sm.classify_media(_reply(sticker=_sticker(animated=True))),
            ("animated", "STFILE"),
        )

    def test_video_sticker_passthrough(self):
        self.assertEqual(
            sm.classify_media(_reply(sticker=_sticker(video=True))),
            ("video", "STFILE"),
        )

    def test_plain_text_reply_rejected(self):
        self.assertEqual(
            sm.classify_media(_reply()), (None, None)
        )


class TestPackProfile(unittest.TestCase):
    def test_profiles(self):
        self.assertEqual(sm.pack_profile("resize"), ("a", 120, False))
        self.assertEqual(sm.pack_profile("convert"), ("vid", 50, True))
        self.assertEqual(sm.pack_profile("video"), ("vid", 50, False))
        self.assertEqual(sm.pack_profile("animated"), ("anim", 50, False))

    def test_unknown_kind_raises(self):
        with self.assertRaises(ValueError):
            sm.pack_profile("bogus")


class TestSourceSuffix(unittest.TestCase):
    def test_sticker_filename_wins(self):
        reply = _reply(sticker=_sticker(file_name="sticker.tgs", animated=True))
        self.assertEqual(sm.source_suffix("animated", reply), ".tgs")

    def test_kind_fallbacks(self):
        self.assertEqual(sm.source_suffix("animated", _reply()), ".tgs")
        self.assertEqual(sm.source_suffix("video", _reply()), ".webm")
        self.assertEqual(sm.source_suffix("convert", _reply()), ".mp4")
        self.assertEqual(sm.source_suffix("resize", _reply()), ".jpg")

    def test_document_filename_wins(self):
        doc = SimpleNamespace(file_id="D", mime_type="image/png", file_name="pic.PNG")
        self.assertEqual(sm.source_suffix("resize", _reply(document=doc)), ".png")


class TestMmfText(unittest.TestCase):
    def test_plain_text(self):
        self.assertEqual(sm.parse_mmf_text("hello world"), ("hello world", None))

    def test_back_color(self):
        self.assertEqual(sm.parse_mmf_text("hi -back red"), ("hi", "red"))

    def test_back_without_text(self):
        self.assertEqual(sm.parse_mmf_text("-back red"), ("", "red"))

    def test_back_without_color(self):
        self.assertEqual(sm.parse_mmf_text("hi -back"), ("hi", None))


class TestPackTitle(unittest.TestCase):
    def test_static(self):
        user = SimpleNamespace(first_name="Lewin")
        self.assertEqual(sm.pack_title(user, "a", 0), "Lewin's")

    def test_animated_numbered(self):
        user = SimpleNamespace(first_name="Lewin")
        self.assertEqual(sm.pack_title(user, "anim", 2), "Lewin's Animated-Pack v2")

    def test_video(self):
        user = SimpleNamespace(first_name="A")
        self.assertEqual(sm.pack_title(user, "vid", 0), "A's Video-Pack")

    def test_title_capped_at_64(self):
        user = SimpleNamespace(first_name="X" * 80)
        self.assertLessEqual(len(sm.pack_title(user, "a", 0)), 64)

    def test_missing_name_falls_back(self):
        user = SimpleNamespace(first_name=None)
        self.assertEqual(sm.pack_title(user, "a", 0), "User's")


class TestFindUrl(unittest.TestCase):
    def test_bare_token_fallback(self):
        msg = _Msg(text="/kang https://example.com/pic.png")
        self.assertEqual(sm.find_url(msg), "https://example.com/pic.png")

    def test_no_url(self):
        self.assertIsNone(sm.find_url(_Msg(text="/kang 2 🔥")))

    def test_text_link_entity(self):
        ent = SimpleNamespace(type="text_link", url="https://x.co/a.png", offset=5, length=4)
        msg = _Msg(text="/kang https://x.co/a.png", entities=[ent])
        self.assertEqual(sm.find_url(msg), "https://x.co/a.png")


# ═════════════════════════════════════════════════════════════════
# Real PIL: resize round-trip
# ═════════════════════════════════════════════════════════════════

class TestResizeImage(unittest.TestCase):
    def test_downscales_to_512_and_writes_webp(self):
        from PIL import Image

        tmp = tempfile.mkdtemp(prefix="pi_resize_")
        try:
            src = os.path.join(tmp, "src.jpg")
            Image.new("RGB", (1000, 500), (10, 20, 30)).save(src, "PNG")
            out = sm.resize_image(src)
            self.assertTrue(out.endswith(".webp"))
            self.assertTrue(os.path.isfile(out))
            self.assertFalse(os.path.exists(src))  # source removed
            with Image.open(out) as im:
                self.assertEqual(im.size, (512, 256))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_upscales_small_image_to_512(self):
        from PIL import Image

        tmp = tempfile.mkdtemp(prefix="pi_resize_")
        try:
            src = os.path.join(tmp, "tiny.png")
            Image.new("RGB", (100, 50), (1, 2, 3)).save(src, "PNG")
            out = sm.resize_image(src)
            with Image.open(out) as im:
                self.assertEqual(max(im.size), 512)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ═════════════════════════════════════════════════════════════════
# /kang validation paths
# ═════════════════════════════════════════════════════════════════

class TestKangValidation(unittest.IsolatedAsyncioTestCase):
    async def test_anonymous_user_refused(self):
        msg = _Msg(text="/kang", user_id=None)
        await call(sm.kang_command, msg, bot=_FakeBot())
        self.assertIn("anonymously", msg.last["text"])

    async def test_no_reply_no_url_shows_usage(self):
        msg = _Msg(text="/kang")
        await call(sm.kang_command, msg, bot=_FakeBot())
        self.assertIn("/kang", msg.last["text"])
        self.assertIn("reply", msg.last["text"].lower())

    async def test_non_media_reply_refused(self):
        msg = _Msg(text="/kang", reply=_reply())
        await call(sm.kang_command, msg, bot=_FakeBot())
        self.assertIn("Unable to kang", msg.last["text"])

    async def test_photo_reply_kangs_over_bot_api(self):
        msg = _Msg(text="/kang", reply=_reply(photo=[SimpleNamespace(file_id="P")]))
        bot = _WorkingBot()
        await call(sm.kang_command, msg, bot=bot)

        self.assertIn("Processing", msg.replies[0]["text"])
        # every step was edited onto the single progress message
        self.assertEqual(len(msg.replies), 1)
        self.assertIn("successfully", msg.prog.last["text"].lower())
        self.assertEqual(bot.created["user_id"], USER_ID)
        self.assertTrue(bot.created["name"].startswith("a_42_by_"))
        self.assertEqual([u["format"] for u in bot.uploads], ["static"])

    async def test_url_kang_uses_the_same_bot_api_path(self):
        msg = _Msg(text="/kang https://example.com/a.png")
        bot = _WorkingBot()

        async def _local(url, dest_base):
            path = dest_base + ".png"
            Image.new("RGB", (32, 32), (1, 2, 3)).save(path, "PNG")
            return path

        with mock.patch.object(sm, "_fetch_url", _local):
            await call(sm.kang_command, msg, bot=bot)
        self.assertIn("Processing", msg.replies[0]["text"])
        self.assertIsNotNone(bot.created, "the URL path must reach pack create")
        self.assertTrue(bot.created["name"].startswith("a_42_by_"))

    async def test_pack_number_and_emoji_parsed(self):
        # Exercise the pure pipeline the handler uses: URL + number + emoji
        tokens = sm.strip_url_tokens(["https://x.co/a.png", "3", "🔥"])
        num, rest = sm.split_pack_and_rest(tokens)
        self.assertEqual(num, 3)
        self.assertEqual(sm.first_emoji(" ".join(rest)), "🔥")
        self.assertEqual(sm.pack_name("a", USER_ID, num, "B"), "a3_42_by_B")


# ═════════════════════════════════════════════════════════════════
# /kang create path — owner resolution (regression: USER_IS_BOT)
# ═════════════════════════════════════════════════════════════════

from PIL import Image  # noqa: E402 — module-level: _WorkingBot writes real PNGs


class _WorkingBot(_FakeBot):
    """Bot API seam: downloads write a real image, pack ops are recorded.

    ``set_exists=False`` makes ``get_sticker_set`` raise the BadRequest
    the real API answers with for an unknown short name — the "create
    it" signal production keys off. ``full_then_missing=True`` answers
    one full pack and then "missing", which is the walk-to-the-next-
    number scenario. Every pack method also records into ``calls`` so
    tests can assert what was NOT touched.
    """

    def __init__(self, *, set_exists=False, stickers=(),
                 full_then_missing=False):
        super().__init__()
        self.set_exists = set_exists
        self.stickers = list(stickers)
        self.full_then_missing = full_then_missing
        self.set_lookups: list = []
        self.uploads: list = []
        self.created = None
        self.added: list = []
        self.deleted: list = []

    async def get_file(self, file_id):
        self.calls.append(("get_file", file_id))
        return SimpleNamespace(file_id=file_id, file_path=f"path/{file_id}")

    async def download_file(self, file_path, destination=None, *a, **k):
        self.calls.append(("download_file", file_path))
        if destination:
            Image.new("RGB", (64, 64), (10, 20, 30)).save(destination, "PNG")
        return b""

    async def get_sticker_set(self, name):
        self.set_lookups.append(name)
        if self.full_then_missing:
            if len(self.set_lookups) == 1:
                return SimpleNamespace(
                    name=name, stickers=[object()] * 120)  # "resize" cap
            raise _bad_request("Bad Request: STICKERSET_INVALID")
        if not self.set_exists:
            raise _bad_request("Bad Request: STICKERSET_INVALID")
        return SimpleNamespace(name=name, stickers=list(self.stickers))

    async def upload_sticker_file(self, user_id, sticker, sticker_format):
        self.uploads.append({
            "user_id": user_id, "format": sticker_format,
            "path": str(getattr(sticker, "path", "")),
        })
        return SimpleNamespace(file_id="UPLOADED_FILE_ID")

    async def create_new_sticker_set(self, user_id, name, title, stickers, **kw):
        self.created = {"user_id": user_id, "name": name, "title": title,
                        "stickers": list(stickers)}
        self.calls.append(("create_new_sticker_set", name))
        return True

    async def add_sticker_to_set(self, user_id, name, sticker):
        self.added.append({"user_id": user_id, "name": name, "sticker": sticker})
        self.calls.append(("add_sticker_to_set", name))
        return True

    async def delete_sticker_from_set(self, sticker=None, **kw):
        self.deleted.append(sticker)
        self.calls.append(("delete_sticker_from_set", sticker))
        return True


class TestKangPackFlow(unittest.IsolatedAsyncioTestCase):
    """Bot API create/add path — the owner is the requester's user id.

    Regression shield for USER_IS_BOT: the pack owner must be the
    requester, never the bot, and the Bot API takes it as a plain id so
    no @username (and no MTProto entity cache) is required — the fake
    message deliberately has ``username=None``.
    """

    async def test_create_uses_requester_owner_not_self(self):
        msg = _Msg(text="/kang", reply=_reply(
            photo=[SimpleNamespace(file_id="P")]))
        bot = _WorkingBot()
        await call(sm.kang_command, msg, bot=bot)

        self.assertIsNotNone(bot.created)
        self.assertEqual(bot.created["user_id"], USER_ID)   # requester…
        self.assertNotEqual(bot.created["user_id"], 1)       # …not the bot
        self.assertTrue(bot.created["name"].startswith("a_42_by_"))

        # one upload → file_id, format derived from the processed file
        self.assertEqual([u["format"] for u in bot.uploads], ["static"])
        item = bot.created["stickers"][0]
        self.assertEqual(item.sticker, "UPLOADED_FILE_ID")
        self.assertEqual(item.format, "static")
        self.assertEqual(item.emoji_list, ["\u2714\ufe0f"])  # DEFAULT_EMOJI

        # no log-channel minting / cleanup round trip
        self.assertFalse([c for c in bot.calls
                          if c[0] in ("send_sticker", "delete_message")])

        # success card was edited onto the single progress message
        self.assertEqual(len(msg.replies), 1)
        self.assertIn("successfully", msg.prog.last["text"].lower())
        self.assertEqual(msg.prog.last.get("parse_mode"), "HTML")
        markup = msg.prog.last.get("reply_markup")
        self.assertIsNotNone(markup)
        self.assertEqual(_style(markup.inline_keyboard[0][0]), "success")
        urls = [b.url for row in markup.inline_keyboard for b in row]
        self.assertIn(
            f"https://t.me/addstickers/{bot.created['name']}", urls)

    async def test_existing_pack_adds_instead_of_creating(self):
        msg = _Msg(text="/kang", reply=_reply(
            photo=[SimpleNamespace(file_id="P")]))
        bot = _WorkingBot(set_exists=True, stickers=[object()] * 3)
        await call(sm.kang_command, msg, bot=bot)

        self.assertIsNone(bot.created, "an existing pack must not be recreated")
        self.assertEqual(len(bot.added), 1)
        self.assertEqual(bot.added[0]["user_id"], USER_ID)
        self.assertEqual(bot.added[0]["name"], "a_42_by_PiModulerBot")
        self.assertEqual(bot.added[0]["sticker"].sticker, "UPLOADED_FILE_ID")
        seen = " ".join(str(e.get("text", "")).lower()
                        for e in msg.prog.edits)
        self.assertIn("existing pack", seen)

    async def test_full_pack_walks_to_the_next_number(self):
        msg = _Msg(text="/kang", reply=_reply(
            photo=[SimpleNamespace(file_id="P")]))
        bot = _WorkingBot(full_then_missing=True)
        await call(sm.kang_command, msg, bot=bot)

        self.assertEqual(bot.set_lookups,
                         ["a_42_by_PiModulerBot", "a1_42_by_PiModulerBot"])
        self.assertIsNotNone(bot.created)
        self.assertEqual(bot.created["name"], "a1_42_by_PiModulerBot")

    async def test_every_pack_full_is_a_clear_error(self):
        msg = _Msg(text="/kang", reply=_reply(
            photo=[SimpleNamespace(file_id="P")]))
        bot = _WorkingBot(set_exists=True, stickers=[object()] * 120)
        with mock.patch.object(sm, "PACK_SEARCH_LIMIT", 3):
            await call(sm.kang_command, msg, bot=bot)

        self.assertIsNone(bot.created)
        self.assertEqual(bot.uploads, [], "nothing should upload for a full walk")
        self.assertIn("numbered packs are full", msg.prog.last["text"])

    async def test_full_pack_on_add_is_a_clear_error(self):
        msg = _Msg(text="/kang", reply=_reply(
            photo=[SimpleNamespace(file_id="P")]))
        bot = _WorkingBot(set_exists=True, stickers=[object()] * 2)

        async def _full(**kw):
            raise _bad_request("Bad Request: STICKERS_TOO_MUCH")

        bot.add_sticker_to_set = _full
        await call(sm.kang_command, msg, bot=bot)

        self.assertIn("That pack is full", msg.prog.last["text"])
        self.assertIn("/kang 2", msg.prog.last["text"])

    async def test_other_bad_request_is_surfaced(self):
        msg = _Msg(text="/kang", reply=_reply(
            photo=[SimpleNamespace(file_id="P")]))
        bot = _WorkingBot()

        async def _boom(**kw):
            raise _bad_request("Bad Request: PEER_ID_INVALID")

        bot.create_new_sticker_set = _boom
        await call(sm.kang_command, msg, bot=bot)
        self.assertIn("Telegram rejected the sticker", msg.prog.last["text"])


class TestStickerFormat(unittest.TestCase):
    """uploadStickerFile/InputSticker format is derived from the file."""

    def test_processed_outputs(self):
        self.assertEqual(sm.sticker_format("x/webp_out.webp"), "static")
        self.assertEqual(sm.sticker_format("x/clip.webm"), "video")
        self.assertEqual(sm.sticker_format("x/sticker.tgs"), "animated")

    def test_source_kind_is_the_tie_breaker(self):
        # Passthrough downloads keep their kind even before conversion.
        self.assertEqual(sm.sticker_format("x/src.jpg", "resize"), "static")
        self.assertEqual(sm.sticker_format("x/src.mp4", "convert"), "video")
        self.assertEqual(sm.sticker_format("x/src.webm", "video"), "video")
        self.assertEqual(sm.sticker_format("x/src.tgs", "animated"), "animated")

    def test_unknown_extension_falls_back_to_the_kind(self):
        self.assertEqual(sm.sticker_format("x/noext", "convert"), "video")
        self.assertEqual(sm.sticker_format("x/noext", "resize"), "static")


class TestBadRequestParsing(unittest.IsolatedAsyncioTestCase):
    """Which BadRequests mean 'pack missing', 'pack full', 'other'."""

    def test_missing_set_signals_create(self):
        for text in ("Bad Request: STICKERSET_INVALID",
                     "Bad Request: STICKERS_INVALID",
                     "Bad Request: sticker set not found",
                     "Bad Request: invalid sticker set name"):
            with self.subTest(text=text):
                self.assertTrue(sm._set_missing(_bad_request(text)))

    def test_missing_set_ignores_unrelated_errors(self):
        for text in ("Bad Request: PEER_ID_INVALID",
                     "Bad Request: STICKERS_TOO_MUCH"):
            with self.subTest(text=text):
                self.assertFalse(sm._set_missing(_bad_request(text)))

    def test_pack_full_detection(self):
        for text in ("Bad Request: STICKERS_TOO_MUCH",
                     "Bad Request: pack is full"):
            with self.subTest(text=text):
                self.assertTrue(sm._pack_full(_bad_request(text)))
        self.assertFalse(sm._pack_full(_bad_request("Bad Request: PEER_ID_INVALID")))

    async def test_resolve_pack_missing_raises_create_signal(self):
        bot = _WorkingBot(set_exists=False)
        name, exists = await sm._resolve_pack(
            bot, "a", USER_ID, 0, "PiModulerBot", 120)
        self.assertEqual(name, "a_42_by_PiModulerBot")
        self.assertFalse(exists)

    async def test_resolve_pack_rejects_unrelated_bad_request(self):
        bot = _WorkingBot(set_exists=True)

        async def _boom(name):
            raise _bad_request("Bad Request: FLOOD_WAIT_4")

        bot.get_sticker_set = _boom
        with self.assertRaises(TelegramBadRequest):
            await sm._resolve_pack(bot, "a", USER_ID, 0, "B", 120)


# ═════════════════════════════════════════════════════════════════
# /unkang validation paths
# ═════════════════════════════════════════════════════════════════

class TestUnkangValidation(unittest.IsolatedAsyncioTestCase):
    async def test_no_reply_usage(self):
        msg = _Msg(text="/unkang")
        await call(sm.unkang_command, msg, bot=_FakeBot())
        self.assertIn("Reply to the sticker", msg.last["text"])

    async def test_reply_without_sticker_usage(self):
        msg = _Msg(text="/unkang", reply=_reply())
        await call(sm.unkang_command, msg, bot=_FakeBot())
        self.assertIn("Reply to the sticker", msg.last["text"])

    async def test_foreign_pack_refused(self):
        msg = _Msg(text="/unkang", reply=_reply(
            sticker=_sticker(set_name="a_999_by_OtherBot")
        ))
        bot = _FakeBot()
        await call(sm.unkang_command, msg, bot=bot)
        self.assertIn("isn't in your pack", msg.last["text"])
        # refused BEFORE any pack call — deleteStickerFromSet never runs
        self.assertFalse([c for c in bot.calls
                          if c[0] == "delete_sticker_from_set"],
                         f"pack remove attempted: {bot.calls}")

    async def test_own_pack_removes_over_bot_api(self):
        msg = _Msg(text="/unkang", reply=_reply(sticker=_sticker()))
        bot = _WorkingBot()
        await call(sm.unkang_command, msg, bot=bot)

        self.assertIn("Removing", msg.replies[0]["text"])
        self.assertEqual(len(msg.replies), 1)
        self.assertEqual(bot.deleted, ["STFILE"])
        self.assertIn("removed successfully", msg.prog.last["text"].lower())
        # no log-channel re-send / message deletion round trip
        self.assertFalse([c for c in bot.calls
                          if c[0] in ("send_sticker", "delete_message")])


# ═════════════════════════════════════════════════════════════════
# Download commands — validation only
# ═════════════════════════════════════════════════════════════════

class TestGetSticker(unittest.IsolatedAsyncioTestCase):
    async def test_no_reply_usage(self):
        msg = _Msg(text="/getsticker")
        await call(sm.getsticker_command, msg, bot=_FakeBot())
        self.assertIn("Reply to a sticker", msg.last["text"])

    async def test_animated_refused(self):
        msg = _Msg(text="/getsticker", reply=_reply(sticker=_sticker(animated=True)))
        await call(sm.getsticker_command, msg, bot=_FakeBot())
        self.assertIn("Animated", msg.last["text"])

    async def test_video_hint(self):
        msg = _Msg(text="/getsticker", reply=_reply(sticker=_sticker(video=True)))
        await call(sm.getsticker_command, msg, bot=_FakeBot())
        self.assertIn("/getvidsticker", msg.last["text"])


class TestGetVidSticker(unittest.IsolatedAsyncioTestCase):
    async def test_no_reply_usage(self):
        msg = _Msg(text="/getvidsticker")
        await call(sm.getvidsticker_command, msg, bot=_FakeBot())
        self.assertIn("Reply to a video sticker", msg.last["text"])

    async def test_static_hint(self):
        msg = _Msg(text="/getvidsticker", reply=_reply(sticker=_sticker()))
        await call(sm.getvidsticker_command, msg, bot=_FakeBot())
        self.assertIn("/getsticker", msg.last["text"])


class TestGetVideo(unittest.IsolatedAsyncioTestCase):
    async def test_no_reply_usage(self):
        msg = _Msg(text="/getvideo")
        await call(sm.getvideo_command, msg, bot=_FakeBot())
        self.assertIn("Reply to a GIF", msg.last["text"])

    async def test_sticker_reply_refused(self):
        msg = _Msg(text="/getvideo", reply=_reply(sticker=_sticker()))
        await call(sm.getvideo_command, msg, bot=_FakeBot())
        self.assertIn("Reply to a GIF", msg.last["text"])


# ═════════════════════════════════════════════════════════════════
# /stickerid and /stickerinfo
# ═════════════════════════════════════════════════════════════════

class TestStickerId(unittest.IsolatedAsyncioTestCase):
    async def test_no_reply_usage(self):
        msg = _Msg(text="/stickerid")
        await call(sm.stickerid_command, msg)
        self.assertIn("Reply to a sticker", msg.last["text"])

    async def test_shows_file_id_card(self):
        msg = _Msg(text="/stickerid", reply=_reply(sticker=_sticker(file_id="AAA BBB")))
        await call(sm.stickerid_command, msg)
        text = msg.last["text"]
        self.assertIn("Sticker ID", text)
        self.assertIn("<code>AAA BBB</code>", text)  # escaped & wrapped


class TestStickerInfo(unittest.IsolatedAsyncioTestCase):
    async def test_no_reply_usage(self):
        msg = _Msg(text="/stickerinfo")
        await call(sm.stickerinfo_command, msg)
        self.assertIn("Reply to a sticker", msg.last["text"])

    async def test_full_card_with_pack_button(self):
        msg = _Msg(text="/stinfo", reply=_reply(sticker=_sticker()))
        await call(sm.stickerinfo_command, msg)
        text = msg.last["text"]
        self.assertIn("Sticker information", text)
        self.assertIn("Static", text)
        self.assertIn("a_42_by_PiModulerBot", text)
        self.assertIn("UNIQUE1", text)
        markup = msg.last.get("reply_markup")
        self.assertIsNotNone(markup)
        urls = [b.url for row in markup.inline_keyboard for b in row]
        self.assertIn("https://t.me/addstickers/a_42_by_PiModulerBot", urls)
        # colored pack button (primary/blue on info cards)
        self.assertEqual(_style(markup.inline_keyboard[0][0]), "primary")

    async def test_animated_type_shown(self):
        msg = _Msg(text="/stickerinfo", reply=_reply(sticker=_sticker(animated=True)))
        await call(sm.stickerinfo_command, msg)
        self.assertIn("Animated", msg.last["text"])


# ═════════════════════════════════════════════════════════════════
# /mmf validation
# ═════════════════════════════════════════════════════════════════

class TestMmfValidation(unittest.IsolatedAsyncioTestCase):
    async def test_no_reply_usage(self):
        msg = _Msg(text="/mmf hello")
        await call(sm.mmf_command, msg, bot=_FakeBot())
        self.assertIn("Reply to an image", msg.last["text"])

    async def test_reply_without_media_usage(self):
        msg = _Msg(text="/mmf hello", reply=_reply())
        await call(sm.mmf_command, msg, bot=_FakeBot())
        self.assertIn("memify", msg.last["text"])

    async def test_animated_sticker_refused(self):
        msg = _Msg(text="/mmf hello", reply=_reply(sticker=_sticker(animated=True)))
        await call(sm.mmf_command, msg, bot=_FakeBot())
        self.assertIn("Animated", msg.last["text"])

    async def test_missing_text_usage(self):
        reply = _reply(photo=[SimpleNamespace(file_id="P")])
        msg = _Msg(text="/mmf", reply=reply)
        await call(sm.mmf_command, msg, bot=_FakeBot())
        self.assertIn("Provide some text", msg.last["text"])

    async def test_back_only_usage(self):
        reply = _reply(photo=[SimpleNamespace(file_id="P")])
        msg = _Msg(text="/mmf -back red", reply=reply)
        await call(sm.mmf_command, msg, bot=_FakeBot())
        self.assertIn("Provide some text", msg.last["text"])


# ═════════════════════════════════════════════════════════════════
# Registration + help menu
# ═════════════════════════════════════════════════════════════════

class TestSetup(unittest.TestCase):
    def test_registers_all_commands(self):
        pipeline.clear()
        routes = sm.setup()
        entries = pipeline.snapshot()
        commands = set()
        for entry in entries:
            for cf in command_filters(entry.flt):
                commands.update(cf.commands)
        for expected in (
            "kang", "unkang", "getsticker", "getvidsticker",
            "getvideo", "stickerid", "stickerinfo", "stinfo", "mmf", "memify",
        ):
            self.assertIn(expected, commands, expected)
        self.assertEqual(len(entries), 8)
        self.assertEqual(len(routes), 8)
        self.assertTrue(all(r.startswith("/") for r in routes))

    def test_callbacks_are_async_handlers(self):
        pipeline.clear()
        sm.setup()
        for entry in pipeline.snapshot():
            self.assertTrue(callable(entry.fn))
            self.assertTrue(inspect.iscoroutinefunction(entry.fn))


class TestHelpEntry(unittest.TestCase):
    def setUp(self):
        self.entry = next((m for m in HELP_MENU if m["key"] == "stickers"), None)

    def test_entry_exists(self):
        self.assertIsNotNone(self.entry)
        self.assertEqual(self.entry["title"], "Stickers")
        self.assertTrue(self.entry["icon"])
        self.assertIsInstance(self.entry["notes"], list)

    def test_lines_start_with_slash(self):
        for _, cmds in self.entry["sections"]:
            for line in cmds:
                self.assertTrue(line.startswith("/"), line)

    def test_core_commands_documented(self):
        joined = " ".join(
            line for _, cmds in self.entry["sections"] for line in cmds
        )
        for cmd in ("/kang", "/unkang", "/stickerinfo", "/stickerid",
                    "/getsticker", "/getvidsticker", "/getvideo", "/mmf"):
            self.assertIn(cmd, joined)

    def test_em_dash_separators(self):
        for _, cmds in self.entry["sections"]:
            for line in cmds:
                self.assertIn("\u2014", line, line)


class TestFfmpegBin(unittest.TestCase):
    """_ffmpeg_bin resolution: PATH → FFMPEG_PATH → imageio-ffmpeg → WinGet.

    The imageio-ffmpeg leg is what makes video kangs work on Railway,
    where there is no system ffmpeg, no WinGet and no FFMPEG_PATH.
    """

    def test_imageio_fallback_when_no_system_ffmpeg(self):
        fake = SimpleNamespace(get_ffmpeg_exe=lambda: __file__)
        with mock.patch.dict(sys.modules, {"imageio_ffmpeg": fake}), \
                mock.patch.object(sm.shutil, "which", return_value=None), \
                mock.patch.dict(os.environ,
                                {"FFMPEG_PATH": "", "LOCALAPPDATA": ""}):
            self.assertEqual(sm._ffmpeg_bin(), __file__)

    def test_none_when_no_source_available(self):
        # sys.modules[name] = None makes the import raise ImportError,
        # simulating a box without imageio-ffmpeg installed.
        with mock.patch.dict(sys.modules, {"imageio_ffmpeg": None}), \
                mock.patch.object(sm.shutil, "which", return_value=None), \
                mock.patch.object(sm.os.path, "isdir", return_value=False), \
                mock.patch.dict(os.environ,
                                {"FFMPEG_PATH": "", "LOCALAPPDATA": ""}):
            self.assertIsNone(sm._ffmpeg_bin())

    def test_env_path_wins_over_imageio(self):
        with mock.patch.object(sm.shutil, "which", return_value=None), \
                mock.patch.dict(os.environ, {"FFMPEG_PATH": __file__}):
            self.assertEqual(sm._ffmpeg_bin(), __file__)


if __name__ == "__main__":
    unittest.main(verbosity=2)
