"""Tests for the sticker suite (bot/modules/sticker.py).

Run from the Pi/Pi root:

    python tests/test_sticker.py
    python -m unittest discover -s tests

Environment isolation (BEFORE any bot import):
    * BOT_TOKEN is forced — importing `bot` pulls bot.config, which
      exits without one.
    * LOCALAPPDATA points at a temp dir so any DB touch stays isolated.

No network: pure helpers are called directly; handlers run against
fakes and the MTProto client seam (``_mtproto_client``) is patched, so
no Telegram or ffmpeg call ever happens. The one real dependency is
PIL, used by the resize round-trip test.
"""

from __future__ import annotations

import atexit
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
    """Command message — records every reply_* call."""

    def __init__(self, text: str = "/kang", reply=None, entities=None):
        self.text = text
        self.reply_to_message = reply
        self.entities = entities or []
        self.chat = SimpleNamespace(id=1, type="private")
        self.replies: list = []          # reply_text kwargs dicts
        self.prog = None               # last progress message (edits live here)
        self.documents: list = []        # reply_document kwargs
        self.animations: list = []
        self.videos: list = []
        self.stickers_sent: list = []

    async def reply_text(self, text, **kw):
        self.replies.append({"text": text, **kw})
        self.prog = _Prog()
        return self.prog

    async def reply_document(self, document=None, **kw):
        self.documents.append({"document": document, **kw})
        return SimpleNamespace(message_id=2)

    async def reply_animation(self, animation=None, **kw):
        self.animations.append({"animation": animation, **kw})
        return SimpleNamespace(message_id=2)

    async def reply_video(self, video=None, **kw):
        self.videos.append({"video": video, **kw})
        return SimpleNamespace(message_id=2)

    async def reply_sticker(self, sticker=None, **kw):
        self.stickers_sent.append({"sticker": sticker, **kw})
        return SimpleNamespace(message_id=2)

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


def _update(msg: _Msg, user_id=USER_ID, username=None):
    user = None if user_id is None else SimpleNamespace(
        id=user_id, username=username, first_name="Lewin"
    )
    return SimpleNamespace(effective_message=msg, effective_user=user, message=msg)


def _ctx():
    return SimpleNamespace(bot=_FakeBot(), args=[])


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


def _no_mtproto():
    """Patch the client seam to 'MTProto off' for handler tests."""
    async def _none():
        return None
    return mock.patch.object(sm, "_mtproto_client", _none)


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
        msg = _Msg(text="/kang")
        await sm.kang_command(_update(msg, user_id=None), _ctx())
        self.assertIn("anonymously", msg.last["text"])

    async def test_no_reply_no_url_shows_usage(self):
        msg = _Msg(text="/kang")
        await sm.kang_command(_update(msg), _ctx())
        self.assertIn("/kang", msg.last["text"])
        self.assertIn("reply", msg.last["text"].lower())

    async def test_non_media_reply_refused(self):
        msg = _Msg(text="/kang", reply=_reply())
        await sm.kang_command(_update(msg), _ctx())
        self.assertIn("Unable to kang", msg.last["text"])

    async def test_photo_reply_mtproto_off_error(self):
        msg = _Msg(text="/kang", reply=_reply(photo=[SimpleNamespace(file_id="P")]))
        with _no_mtproto():
            await sm.kang_command(_update(msg), _ctx())
        self.assertIn("Processing", msg.replies[0]["text"])
        # the failure was edited onto the progress message, not re-sent
        self.assertEqual(len(msg.replies), 1)
        self.assertTrue(msg.prog.edits)
        self.assertIn("MTProto", msg.prog.last["text"])

    async def test_url_kang_reaches_mtproto_check(self):
        msg = _Msg(text="/kang https://example.com/a.png")
        seen = {}

        async def _rec():
            seen["hit"] = True
            return None

        with mock.patch.object(sm, "_mtproto_client", _rec):
            await sm.kang_command(_update(msg), _ctx())
        self.assertTrue(seen.get("hit"))
        self.assertIn("Processing", msg.replies[0]["text"])
        self.assertIn("MTProto", msg.prog.last["text"])

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

from telethon.errors import StickersetInvalidError  # noqa: E402
from telethon.tl.types import InputPeerUser, InputUser  # noqa: E402

from PIL import Image  # noqa: E402 — module-level: _WorkingBot writes real PNGs


class _FakeMtClient:
    """Telethon stand-in for the kang create path.

    Deliberately exposes ONLY ``get_input_entity`` — if production ever
    regresses to boabot's pyrogram-only ``resolve_peer``, these tests
    fail instead of silently returning None (that AttributeError was
    swallowed by bare excepts and pushed every kang onto the bot-self
    fallback → USER_IS_BOT).
    """

    def __init__(self, owner_peer=None):
        self.owner_peer = owner_peer          # None → resolver keeps missing
        self.resolve_calls: list = []
        self.requests: list = []
        self.created = None

    async def get_input_entity(self, ref):
        self.resolve_calls.append(ref)
        if self.owner_peer is None:
            raise ValueError("Could not find the entity")
        return self.owner_peer

    async def __call__(self, request):
        self.requests.append(request)
        name = type(request).__name__
        if name == "GetStickerSetRequest":
            raise StickersetInvalidError(request=None)  # pack doesn't exist yet
        if name == "CreateStickerSetRequest":
            self.created = request
        return None

    async def send_file(self, chat_id, path, force_document=False):
        doc = SimpleNamespace(id=111, access_hash=222, file_reference=b"ref")
        return SimpleNamespace(id=7, media=SimpleNamespace(document=doc))

    async def delete_messages(self, chat_id, ids):
        self.requests.append(("delete_messages", chat_id, tuple(ids)))


class _WorkingBot(_FakeBot):
    """get_file that writes a real image instead of raising."""

    async def get_file(self, file_id):
        self.calls.append(("get_file", file_id))

        class _F:
            async def download_to_drive(self, dest):
                Image.new("RGB", (64, 64), (10, 20, 30)).save(dest, "PNG")

        return _F()


class TestKangOwnerResolution(unittest.IsolatedAsyncioTestCase):
    async def test_resolve_owner_prefers_username_via_telethon_api(self):
        calls = []

        class _C:
            async def get_input_entity(self, ref):
                calls.append(ref)
                return InputPeerUser(USER_ID, 77)

        got = await sm._resolve_owner(
            _C(), SimpleNamespace(id=USER_ID, username="lewin"))
        self.assertEqual(calls, ["lewin"])
        self.assertIsInstance(got, InputUser)
        self.assertEqual((got.user_id, got.access_hash), (USER_ID, 77))

    async def test_resolve_owner_numeric_cache_fallback(self):
        calls = []

        class _C:
            async def get_input_entity(self, ref):
                calls.append(ref)
                if isinstance(ref, str):
                    raise ValueError("no such username")
                return InputPeerUser(ref, 99)

        got = await sm._resolve_owner(
            _C(), SimpleNamespace(id=USER_ID, username=None))
        self.assertEqual(calls, [USER_ID])
        self.assertEqual(got.access_hash, 99)

    async def test_resolve_owner_not_cached_is_none(self):
        class _C:
            async def get_input_entity(self, ref):
                raise ValueError("Could not find the entity")

        self.assertIsNone(await sm._resolve_owner(
            _C(), SimpleNamespace(id=USER_ID, username=None)))

    async def test_create_uses_requester_owner_not_self(self):
        msg = _Msg(text="/kang", reply=_reply(
            photo=[SimpleNamespace(file_id="P")]))
        client = _FakeMtClient(owner_peer=InputPeerUser(USER_ID, 1234))

        async def _give():
            return client

        with mock.patch.object(sm, "_mtproto_client", _give):
            await sm.kang_command(_update(msg), _ctx_working())
        self.assertIsNotNone(client.created)
        owner = client.created.user_id
        self.assertIsInstance(owner, InputUser)
        self.assertNotIsInstance(owner, sm.InputUserSelf)
        self.assertEqual((owner.user_id, owner.access_hash), (USER_ID, 1234))
        self.assertTrue(client.created.short_name.startswith("a_42_by_"))
        # success card was edited onto the single progress message
        self.assertEqual(len(msg.replies), 1)
        self.assertIn("successfully", msg.prog.last["text"].lower())
        self.assertEqual(msg.prog.last.get("parse_mode"), "HTML")
        # colored "View Sticker Pack" button (success/green)
        markup = msg.prog.last.get("reply_markup")
        self.assertIsNotNone(markup)
        view_btn = markup.inline_keyboard[0][0]
        self.assertEqual(view_btn.api_kwargs.get("style"), "success")
        # log-channel source upload cleaned up
        self.assertIn(("delete_messages", sm.LOG_CHANNEL_ID, (7,)),
                      client.requests)

    async def test_unresolvable_owner_clear_error_no_create(self):
        msg = _Msg(text="/kang", reply=_reply(
            photo=[SimpleNamespace(file_id="P")]))
        client = _FakeMtClient(owner_peer=None)

        async def _give():
            return client

        with mock.patch.object(sm, "_mtproto_client", _give), \
                mock.patch.object(sm, "_OWNER_RETRY_WAIT", 0):
            await sm.kang_command(_update(msg), _ctx_working())
        self.assertIsNone(client.created)            # create never attempted
        self.assertEqual(len(client.resolve_calls), 2)  # one wait-retry
        last = msg.prog.last["text"]
        self.assertIn("resolve your account", last)
        self.assertIn("@username", last)


def _ctx_working():
    """Context whose bot can actually 'download' (writes a real image)."""
    return SimpleNamespace(bot=_WorkingBot(), args=[])


# ═════════════════════════════════════════════════════════════════
# /unkang validation paths
# ═════════════════════════════════════════════════════════════════

class TestUnkangValidation(unittest.IsolatedAsyncioTestCase):
    async def test_no_reply_usage(self):
        msg = _Msg(text="/unkang")
        await sm.unkang_command(_update(msg), _ctx())
        self.assertIn("Reply to the sticker", msg.last["text"])

    async def test_reply_without_sticker_usage(self):
        msg = _Msg(text="/unkang", reply=_reply())
        await sm.unkang_command(_update(msg), _ctx())
        self.assertIn("Reply to the sticker", msg.last["text"])

    async def test_foreign_pack_refused(self):
        msg = _Msg(text="/unkang", reply=_reply(
            sticker=_sticker(set_name="a_999_by_OtherBot")
        ))
        await sm.unkang_command(_update(msg), _ctx())
        self.assertIn("isn't in your pack", msg.last["text"])
        # refused BEFORE any MTProto call — the seam must not be touched
        with mock.patch.object(
            sm, "_mtproto_client",
            mock.AsyncMock(side_effect=AssertionError("should not be called")),
        ):
            await sm.unkang_command(_update(msg), _ctx())

    async def test_own_pack_mtproto_off(self):
        msg = _Msg(text="/unkang", reply=_reply(sticker=_sticker()))
        with _no_mtproto():
            await sm.unkang_command(_update(msg), _ctx())
        self.assertIn("Removing", msg.replies[0]["text"])
        self.assertEqual(len(msg.replies), 1)
        self.assertIn("MTProto", msg.prog.last["text"])


# ═════════════════════════════════════════════════════════════════
# Download commands — validation only
# ═════════════════════════════════════════════════════════════════

class TestGetSticker(unittest.IsolatedAsyncioTestCase):
    async def test_no_reply_usage(self):
        msg = _Msg(text="/getsticker")
        await sm.getsticker_command(_update(msg), _ctx())
        self.assertIn("Reply to a sticker", msg.last["text"])

    async def test_animated_refused(self):
        msg = _Msg(text="/getsticker", reply=_reply(sticker=_sticker(animated=True)))
        await sm.getsticker_command(_update(msg), _ctx())
        self.assertIn("Animated", msg.last["text"])

    async def test_video_hint(self):
        msg = _Msg(text="/getsticker", reply=_reply(sticker=_sticker(video=True)))
        await sm.getsticker_command(_update(msg), _ctx())
        self.assertIn("/getvidsticker", msg.last["text"])


class TestGetVidSticker(unittest.IsolatedAsyncioTestCase):
    async def test_no_reply_usage(self):
        msg = _Msg(text="/getvidsticker")
        await sm.getvidsticker_command(_update(msg), _ctx())
        self.assertIn("Reply to a video sticker", msg.last["text"])

    async def test_static_hint(self):
        msg = _Msg(text="/getvidsticker", reply=_reply(sticker=_sticker()))
        await sm.getvidsticker_command(_update(msg), _ctx())
        self.assertIn("/getsticker", msg.last["text"])


class TestGetVideo(unittest.IsolatedAsyncioTestCase):
    async def test_no_reply_usage(self):
        msg = _Msg(text="/getvideo")
        await sm.getvideo_command(_update(msg), _ctx())
        self.assertIn("Reply to a GIF", msg.last["text"])

    async def test_sticker_reply_refused(self):
        msg = _Msg(text="/getvideo", reply=_reply(sticker=_sticker()))
        await sm.getvideo_command(_update(msg), _ctx())
        self.assertIn("Reply to a GIF", msg.last["text"])


# ═════════════════════════════════════════════════════════════════
# /stickerid and /stickerinfo
# ═════════════════════════════════════════════════════════════════

class TestStickerId(unittest.IsolatedAsyncioTestCase):
    async def test_no_reply_usage(self):
        msg = _Msg(text="/stickerid")
        await sm.stickerid_command(_update(msg), _ctx())
        self.assertIn("Reply to a sticker", msg.last["text"])

    async def test_shows_file_id_card(self):
        msg = _Msg(text="/stickerid", reply=_reply(sticker=_sticker(file_id="AAA BBB")))
        await sm.stickerid_command(_update(msg), _ctx())
        text = msg.last["text"]
        self.assertIn("Sticker ID", text)
        self.assertIn("<code>AAA BBB</code>", text)  # escaped & wrapped


class TestStickerInfo(unittest.IsolatedAsyncioTestCase):
    async def test_no_reply_usage(self):
        msg = _Msg(text="/stickerinfo")
        await sm.stickerinfo_command(_update(msg), _ctx())
        self.assertIn("Reply to a sticker", msg.last["text"])

    async def test_full_card_with_pack_button(self):
        msg = _Msg(text="/stinfo", reply=_reply(sticker=_sticker()))
        await sm.stickerinfo_command(_update(msg), _ctx())
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
        self.assertEqual(
            markup.inline_keyboard[0][0].api_kwargs.get("style"), "primary")

    async def test_animated_type_shown(self):
        msg = _Msg(text="/stickerinfo", reply=_reply(sticker=_sticker(animated=True)))
        await sm.stickerinfo_command(_update(msg), _ctx())
        self.assertIn("Animated", msg.last["text"])


# ═════════════════════════════════════════════════════════════════
# /mmf validation
# ═════════════════════════════════════════════════════════════════

class TestMmfValidation(unittest.IsolatedAsyncioTestCase):
    async def test_no_reply_usage(self):
        msg = _Msg(text="/mmf hello")
        await sm.mmf_command(_update(msg), _ctx())
        self.assertIn("Reply to an image", msg.last["text"])

    async def test_reply_without_media_usage(self):
        msg = _Msg(text="/mmf hello", reply=_reply())
        await sm.mmf_command(_update(msg), _ctx())
        self.assertIn("memify", msg.last["text"])

    async def test_animated_sticker_refused(self):
        msg = _Msg(text="/mmf hello", reply=_reply(sticker=_sticker(animated=True)))
        await sm.mmf_command(_update(msg), _ctx())
        self.assertIn("Animated", msg.last["text"])

    async def test_missing_text_usage(self):
        reply = _reply(photo=[SimpleNamespace(file_id="P")])
        msg = _Msg(text="/mmf", reply=reply)
        await sm.mmf_command(_update(msg), _ctx())
        self.assertIn("Provide some text", msg.last["text"])

    async def test_back_only_usage(self):
        reply = _reply(photo=[SimpleNamespace(file_id="P")])
        msg = _Msg(text="/mmf -back red", reply=reply)
        await sm.mmf_command(_update(msg), _ctx())
        self.assertIn("Provide some text", msg.last["text"])


# ═════════════════════════════════════════════════════════════════
# Registration + help menu
# ═════════════════════════════════════════════════════════════════

class _FakeApp:
    def __init__(self) -> None:
        self.handlers: list = []

    def add_handler(self, handler, group=0):
        self.handlers.append((group, handler))


class TestSetup(unittest.TestCase):
    def test_registers_all_commands(self):
        app = _FakeApp()
        routes = sm.setup(app)
        commands = set()
        for _, handler in app.handlers:
            cmds = getattr(handler, "commands", None) or getattr(handler, "command", None)
            if cmds:
                commands.update(cmds)
        for expected in (
            "kang", "unkang", "getsticker", "getvidsticker",
            "getvideo", "stickerid", "stickerinfo", "stinfo", "mmf", "memify",
        ):
            self.assertIn(expected, commands, expected)
        self.assertEqual(len(app.handlers), 8)
        self.assertEqual(len(routes), 8)
        self.assertTrue(all(r.startswith("/") for r in routes))

    def test_callbacks_are_async_handlers(self):
        app = _FakeApp()
        sm.setup(app)
        for _, handler in app.handlers:
            self.assertTrue(callable(handler.callback))


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
