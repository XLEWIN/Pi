"""Tests for /tiktok /x /yt /ig profile search (bot/modules/profiles.py).

Run from the repo root:

    python tests/test_profiles.py
    python -m unittest discover -s tests

Environment isolation (BEFORE any bot import):
    * BOT_TOKEN is forced — importing `bot` pulls bot.config, which
      exits without one.
    * LOCALAPPDATA points at a temp dir so runtime files are isolated.

No network: parsers/rendering are pure; command tests patch
``profiles._lookup`` / ``profiles._fetch_avatar``.
"""

from __future__ import annotations

import atexit
import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# ── Environment isolation — must precede bot imports ──────────────
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["BOT_TOKEN"] = "1:TEST-TOKEN-FOR-UNIT-TESTS"
_TEST_DIR = tempfile.mkdtemp(prefix="pi_profiles_test_")
os.environ["LOCALAPPDATA"] = _TEST_DIR
atexit.register(shutil.rmtree, _TEST_DIR, ignore_errors=True)

# ── Imports (after env) ───────────────────────────────────────────
from aiofakes import FakeMessage, call, command_filters  # noqa: E402
from bot import pipeline  # noqa: E402
from bot.constants import HELP_MENU  # noqa: E402
from bot.modules import profiles as pm  # noqa: E402


# ═════════════════════════════════════════════════════════════════
# Sample payloads (shapes captured from live endpoint probes)
# ═════════════════════════════════════════════════════════════════

_TIKWM = {
    "code": 0,
    "data": {
        "user": {
            "uniqueId": "messi",
            "nickname": "Messi",
            "signature": "football life",
            "avatarThumb": "https://p16-sign.tiktokcdn.com/x.jpeg",
        },
        "stats": {"followerCount": 10924, "heartCount": 18234, "videoCount": 0},
    },
}

_TIKTOK_DIRECT = {
    "userInfo": {
        "user": {
            "uniqueId": "messi",
            "nickname": "Messi",
            "signature": "football life",
            "avatarLarger": "https://p16-sign.tiktokcdn.com/large.jpeg",
        },
        "stats": {"followerCount": 10924, "heartCount": 18234, "videoCount": 0},
    },
}

_X_HTML = (
    '<script>{"screen_name":"ElonMusk","name":"Elon Musk",'
    '"description":"Making Mars happen \\u2014 Mars &amp; more",'
    '"profile_image_url_https":"https://pbs.twimg.com/profile_images/'
    '123/photo_normal.jpg","followers_count":241727911,'
    '"friends_count":1414,"statuses_count":100217}</script>'
)

_YT_MAIN = (
    '<meta property="og:title" content="TriggeredInsaan">'
    '<meta property="og:image" content="https://yt3.ggpht.com/avatar.jpg">'
    '"canonicalBaseUrl":"/@TriggeredInsaan"'
    '"channelMetadataRenderer":{"title":"TriggeredInsaan",'
    '"description":"L\\u00f3l, rofl, memes \\u2014 new videos every week"}'
    '"content":"26.9M subscribers"'
    '"content":"373 videos"'
)
_YT_ABOUT = "<div>5,334,053,111 views</div>"

_IG_PAYLOAD = {
    "data": {
        "user": {
            "username": "choud4ary",
            "full_name": "Choud4ary",
            "biography": "jb Tk account na ude chalate rho",
            "profile_pic_url_hd": "https://scontent.cdninstagram.com/hd.jpg",
            "edge_followed_by": {"count": 22},
            "edge_follow": {"count": 24},
            "edge_owner_to_timeline_media": {"count": 0},
        }
    },
}


def _profile(name="TriggeredInsaan", handle="@TriggeredInsaan",
             bio="Lol rofl memes", stats=None):
    return {
        "name": name,
        "handle": handle,
        "bio": bio,
        "avatar": "",
        "stats": stats or ("26.9M Subscribers", "373 Videos", "5.3B Views"),
    }


# ═════════════════════════════════════════════════════════════════
# Number formatting
# ═════════════════════════════════════════════════════════════════

class TestCompact(unittest.TestCase):
    def test_owner_screenshot_values(self):
        self.assertEqual(pm.compact(241727911), "241.7M")
        self.assertEqual(pm.compact(1414), "1.4K")
        self.assertEqual(pm.compact(109243), "109.2K")
        self.assertEqual(pm.compact(373), "373")
        self.assertEqual(pm.compact(0), "0")

    def test_billions_and_trailing_zero(self):
        self.assertEqual(pm.compact(5334053111), "5.3B")
        self.assertEqual(pm.compact(1000), "1K")        # ".0" stripped
        self.assertEqual(pm.compact(2000000), "2M")

    def test_forgiving_inputs(self):
        self.assertEqual(pm.compact(None), "0")
        self.assertEqual(pm.compact(-5), "0")
        self.assertEqual(pm.compact("26.9M"), "26.9M")  # passthrough
        self.assertEqual(pm.compact("bogus"), "bogus")

    def test_num_tokens(self):
        self.assertEqual(pm._num("26.9M"), 26900000)
        self.assertEqual(pm._num("1,024"), 1024)
        self.assertEqual(pm._num("373"), 373)
        self.assertEqual(pm._num("1.5K"), 1500)
        self.assertEqual(pm._num(""), 0)
        self.assertEqual(pm._num("n/a"), 0)


# ═════════════════════════════════════════════════════════════════
# Parsers (pure — no network)
# ═════════════════════════════════════════════════════════════════

class TestParsers(unittest.TestCase):
    def test_tikwm(self):
        p = pm._parse_tikwm(_TIKWM)
        self.assertEqual(p["name"], "Messi")
        self.assertEqual(p["handle"], "@messi")
        self.assertEqual(p["bio"], "football life")
        self.assertEqual(p["stats"],
                         ("10.9K Followers", "18.2K Likes", "0 Videos"))
        self.assertTrue(p["avatar"].startswith("https://"))

    def test_tikwm_missing_user_raises(self):
        with self.assertRaises(pm.ProfileError):
            pm._parse_tikwm({"code": 0, "data": {}})

    def test_tiktok_direct(self):
        p = pm._parse_tiktok_direct(_TIKTOK_DIRECT)
        self.assertEqual(p["handle"], "@messi")
        self.assertEqual(p["stats"],
                         ("10.9K Followers", "18.2K Likes", "0 Videos"))
        self.assertIn("large.jpeg", p["avatar"])

    def test_x(self):
        p = pm._parse_x(_X_HTML)
        self.assertEqual(p["name"], "Elon Musk")
        self.assertEqual(p["handle"], "@ElonMusk")
        self.assertEqual(p["stats"],
                         ("241.7M Followers", "1.4K Following", "100.2K Tweets"))
        self.assertIn("_400x400.", p["avatar"])       # upgraded avatar size
        self.assertNotIn("_normal.", p["avatar"])
        self.assertIn("Mars", p["bio"])

    def test_x_missing_user_raises(self):
        with self.assertRaises(pm.ProfileError):
            pm._parse_x("<html>no profile here</html>")

    def test_youtube(self):
        p = pm._parse_youtube(_YT_MAIN, _YT_ABOUT)
        self.assertEqual(p["name"], "TriggeredInsaan")
        self.assertEqual(p["handle"], "@TriggeredInsaan")
        self.assertEqual(p["stats"],
                         ("26.9M Subscribers", "373 Videos", "5.3B Views"))
        self.assertIn("avatar.jpg", p["avatar"])
        self.assertIn("memes", p["bio"])

    def test_youtube_without_about_keeps_zero_views(self):
        p = pm._parse_youtube(_YT_MAIN, "")
        self.assertTrue(p["stats"][2].endswith(" Views"))
        self.assertTrue(p["stats"][2].startswith("0"))

    def test_youtube_missing_channel_raises(self):
        with self.assertRaises(pm.ProfileError):
            pm._parse_youtube("<html>gone</html>", "")

    def test_instagram(self):
        p = pm._parse_instagram(_IG_PAYLOAD)
        self.assertEqual(p["name"], "Choud4ary")
        self.assertEqual(p["handle"], "@choud4ary")
        self.assertEqual(p["bio"], "jb Tk account na ude chalate rho")
        self.assertEqual(p["stats"], ("22 Followers", "24 Following", "0 Posts"))

    def test_instagram_missing_user_raises(self):
        with self.assertRaises(pm.ProfileError):
            pm._parse_instagram({"data": {}})


# ═════════════════════════════════════════════════════════════════
# Rendering — values must never overflow their slots
# ═════════════════════════════════════════════════════════════════

_SIZES = {"tiktok": (2172, 724), "youtube": (2172, 724),
          "instagram": (2172, 724), "x": (2048, 683)}

# Allowed text regions: bars (bio gets +80px for line 2) + pills.
_BARS = {
    "std": [(589, 158, 1270, 223), (589, 253, 925, 299),
            (589, 329, 1520, 462)],
    "x": [(557, 149, 1175, 211), (557, 240, 870, 283),
          (557, 312, 1430, 442)],
}
_PILLS = {
    "std": [(593, 494, 891, 572), (909, 494, 1206, 572),
            (1223, 494, 1516, 572)],
    "x": [(562, 463, 839, 546), (861, 463, 1137, 546),
          (1156, 463, 1430, 546)],
}
_AVATAR = {
    "tiktok": (82, 137, 520, 577),
    "youtube": (80, 136, 515, 580),
    "instagram": (80, 137, 518, 579),
    "x": (77, 130, 495, 551),
}


class TestRender(unittest.TestCase):
    """render_card output: valid PNG, template size, zero slot overflow."""

    def _render(self, platform, prof, avatar=None):
        return pm.render_card(platform, prof, avatar)

    def _assert_within_slots(self, platform, png):
        from PIL import Image, ImageChops, ImageDraw

        tpl = Image.open(
            pm._TEMPLATES / pm._PLATFORMS[platform]["template"]
        ).convert("RGB")
        card = Image.open(io.BytesIO(png)).convert("RGB")
        self.assertEqual(card.size, tpl.size)
        diff = ImageChops.difference(card, tpl).convert("L")
        changed = diff.point(lambda p: 255 if p >= 8 else 0)
        layout = "x" if platform == "x" else "std"
        mask = Image.new("L", card.size, 0)
        dr = ImageDraw.Draw(mask)
        for box in [_AVATAR[platform]] + _BARS[layout] + _PILLS[layout]:
            dr.rectangle(box, fill=255)
        outside = ImageChops.multiply(changed, ImageChops.invert(mask))
        n = outside.histogram()[255]
        self.assertEqual(n, 0, f"{n} px rendered outside the slots")

    def test_every_platform_renders_template_size(self):
        from PIL import Image

        for platform in ("tiktok", "x", "youtube", "instagram"):
            with self.subTest(platform=platform):
                png = self._render(platform, _profile())
                self.assertTrue(png.startswith(b"\x89PNG"))
                self.assertEqual(Image.open(io.BytesIO(png)).size,
                                 _SIZES[platform])
                self._assert_within_slots(platform, png)

    def test_stress_values_stay_inside_slots(self):
        """Longest realistic values (owner's hard requirement)."""
        stress = {
            "tiktok": _profile(
                name="Supercalifragilisticexpialidocious " * 3,
                handle="@" + "verylonghandlename" * 3,
                bio="A really long bio line that keeps going and going "
                    "past the bar with more words to force wrapping "
                    "into the second line as well, and then some extra",
                stats=("1,024,567,890 Followers", "987,654,321 Likes",
                       "55,555 Videos"),
            ),
            "x": _profile(
                name="X Æ A-12 Musk — underscore & ampersand name",
                handle="@handle_with.dots_and_underscores_42",
                bio="https://t.co/shortlinkonly " * 12,
                stats=("241,727,911 Followers", "9,999 Following",
                       "4,444,444 Tweets"),
            ),
            "youtube": _profile(
                name="A YouTube channel name that is absurdly long " * 3,
                handle="@" + "channelhandle" * 4,
                bio="Channel description that spans comfortably across "
                    "the full bio bar and needs a second wrapped line "
                    "to fit all of the words in here",
                stats=("26.9M Subscribers", "3,333 Videos",
                       "1,024,567,890 Views"),
            ),
            "instagram": _profile(
                name="Instagram Display Name With Emoji-Free Length",
                handle="@" + "username" * 5,
                bio="jb Tk account na ude chalate rho and more text to "
                    "push the wrap into line two of the bio bar here",
                stats=("22 Followers", "24 Following", "1,234 Posts"),
            ),
        }
        for platform, prof in stress.items():
            with self.subTest(platform=platform):
                png = self._render(platform, prof)
                self.assertTrue(png.startswith(b"\x89PNG"))
                self._assert_within_slots(platform, png)

    def test_missing_fields_still_render(self):
        prof = {"name": "", "handle": "", "bio": "", "avatar": "",
                "stats": ("0 Followers", "", None)}
        for platform in ("tiktok", "x", "youtube", "instagram"):
            with self.subTest(platform=platform):
                png = self._render(platform, prof)
                self.assertTrue(png.startswith(b"\x89PNG"))
                self._assert_within_slots(platform, png)

    def test_corrupt_avatar_does_not_break_render(self):
        png = self._render("youtube", _profile(), avatar=b"not-an-image")
        self.assertTrue(png.startswith(b"\x89PNG"))


# ═════════════════════════════════════════════════════════════════
# Lookup cache
# ═════════════════════════════════════════════════════════════════

class TestCache(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        pm._CACHE.clear()

    def tearDown(self):
        pm._CACHE.clear()

    async def test_second_lookup_served_from_cache(self):
        calls = []

        async def _fake_fetch(client, user):
            calls.append(user)
            return _profile()

        with mock.patch.object(pm, "_fetch_x", _fake_fetch):
            await pm._lookup("x", "ElonMusk")
            await pm._lookup("x", "elonmusk")   # case-insensitive key
        self.assertEqual(calls, ["ElonMusk"])

    async def test_expired_entry_refetches(self):
        calls = []

        async def _fake_fetch(client, user):
            calls.append(user)
            return _profile()

        with mock.patch.object(pm, "_fetch_x", _fake_fetch):
            await pm._lookup("x", "a")
            key = ("x", "a")
            ts, prof = pm._CACHE[key]
            pm._CACHE[key] = (ts - pm._CACHE_TTL - 1, prof)
            await pm._lookup("x", "a")
        self.assertEqual(calls, ["a", "a"])


# ═════════════════════════════════════════════════════════════════
# Commands (network patched out)
# ═════════════════════════════════════════════════════════════════

class TestCommands(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        pm._CACHE.clear()

    def tearDown(self):
        pm._CACHE.clear()

    @staticmethod
    def _no_avatar():
        async def _fake(client, url):
            return None
        return mock.patch.object(pm, "_fetch_avatar", _fake)

    async def test_missing_arg_shows_usage(self):
        msg = FakeMessage("/tiktok")
        await call(pm.tiktok_command, msg, args=[])
        self.assertEqual(msg.last[0], "reply")
        self.assertEqual(msg.last[2].get("parse_mode"), "HTML")
        self.assertIn("Usage: /tiktok", msg.last[1])
        self.assertIn("&lt;username&gt;", msg.last[1])
        self.assertNotIn("&amp;lt;", msg.last[1])   # no double-escape

    async def test_invalid_handle_shows_usage(self):
        msg = FakeMessage("/x")
        await call(pm.x_command, msg, args=["bad handle!"])
        self.assertEqual(msg.last[0], "reply")
        self.assertIn("Usage: /x", msg.last[1])

    async def test_lookup_error_becomes_card(self):
        async def _boom(client, user):
            raise pm.ProfileError("user not found")

        msg = FakeMessage("/yt Nobody")
        with mock.patch.object(pm, "_fetch_youtube", _boom):
            await call(pm.yt_command, msg, args=["Nobody"])
        self.assertEqual(msg.last[0], "reply")
        self.assertIn("YouTube Profile", msg.last[1])
        self.assertIn("user not found", msg.last[1])
        self.assertEqual(msg.last[2].get("parse_mode"), "HTML")

    async def test_unexpected_error_becomes_friendly_card(self):
        async def _boom(client, user):
            raise ValueError("socket exploded")

        msg = FakeMessage("/ig someone")
        with mock.patch.object(pm, "_fetch_instagram", _boom):
            await call(pm.ig_command, msg, args=["someone"])
        self.assertEqual(msg.last[0], "reply")
        self.assertIn("Instagram Profile", msg.last[1])
        self.assertIn("Lookup failed", msg.last[1])

    async def test_success_sends_card_with_blue_button(self):
        async def _fake_lookup(platform, user):
            return _profile()

        msg = FakeMessage("/yt TriggeredInsaan")
        with mock.patch.object(pm, "_lookup", _fake_lookup), self._no_avatar():
            await call(pm.yt_command, msg, args=["TriggeredInsaan"])
        photos = [c for c in msg.calls if c[0] == "answer_photo"]
        self.assertEqual(len(photos), 1)
        _, media, kw = photos[0]
        self.assertTrue(media.data.startswith(b"\x89PNG"))
        self.assertEqual(media.filename, "TriggeredInsaan.png")
        # photo goes out with an empty caption (matches the screenshots)
        self.assertNotIn("caption", kw)
        rows = kw["reply_markup"].inline_keyboard
        self.assertEqual(len(rows), 1)
        btn = rows[0][0]
        self.assertEqual(btn.text, "View on YouTube")
        self.assertEqual(btn.url, "https://www.youtube.com/@TriggeredInsaan")
        self.assertEqual(btn.style, "primary")     # all buttons blue

    async def test_at_prefix_and_trailing_slash_cleaned(self):
        async def _fake_lookup(platform, user):
            self.assertEqual(user, "ElonMusk")
            return _profile()

        msg = FakeMessage("/x @ElonMusk/")
        with mock.patch.object(pm, "_lookup", _fake_lookup), self._no_avatar():
            await call(pm.x_command, msg, args=["@ElonMusk/"])
        self.assertEqual(len([c for c in msg.calls if c[0] == "answer_photo"]), 1)

    async def test_render_failure_becomes_card(self):
        async def _fake_lookup(platform, user):
            return _profile()

        def _boom(*a, **kw):
            raise RuntimeError("font missing")

        msg = FakeMessage("/tiktok messi")
        with mock.patch.object(pm, "_lookup", _fake_lookup), \
                mock.patch.object(pm, "render_card", _boom):
            await call(pm.tiktok_command, msg, args=["messi"])
        self.assertEqual(msg.last[0], "reply")
        self.assertIn("TikTok Profile", msg.last[1])
        self.assertIn("Card rendering failed", msg.last[1])


# ═════════════════════════════════════════════════════════════════
# Wiring + help
# ═════════════════════════════════════════════════════════════════

class TestWiring(unittest.TestCase):
    def test_setup_registers_four_commands(self):
        pipeline.clear()
        routes = pm.setup()
        self.assertEqual(routes, ["/tiktok", "/x", "/yt", "/ig"])
        entries = pipeline.snapshot()
        msgs = [e for e in entries if e.event == "message"]
        self.assertEqual(len(msgs), 4)
        cmds = sorted(set().union(*(
            set(cf.commands) for e in msgs for cf in command_filters(e.flt)
        )))
        self.assertEqual(cmds, ["ig", "tiktok", "x", "yt"])

    def test_help_documents_profile_search(self):
        general = next(m for m in HELP_MENU if m["key"] == "general")
        lines = [line for _, cmds in general["sections"] for line in cmds]
        self.assertTrue(
            any(line.startswith("/tiktok") for line in lines),
            "General help is missing the /tiktok profile line",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
