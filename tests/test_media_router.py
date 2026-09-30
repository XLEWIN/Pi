"""Tests for the media module router, format plan, limits and captions.

Run from the repo root:

    python tests/test_media_router.py
    python -m unittest discover -s tests

Environment isolation (BEFORE any bot import):
    * BOT_TOKEN is forced — importing `bot` pulls bot.config, which
      exits without one.
    * LOCALAPPDATA points at a temp dir so runtime files are isolated.

No network: only pure functions and in-memory logic run — yt-dlp,
httpx and Telegram are never touched.
"""

from __future__ import annotations

import asyncio
import atexit
import dataclasses
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
_TEST_DIR = tempfile.mkdtemp(prefix="pi_media_test_")
os.environ["LOCALAPPDATA"] = _TEST_DIR
atexit.register(shutil.rmtree, _TEST_DIR, ignore_errors=True)

# ── Imports (after env) ───────────────────────────────────────────
from types import SimpleNamespace  # noqa: E402

from aiofakes import FakeBot, call, make_callback  # noqa: E402
from bot.modules.media.exceptions import IGInvalidUrl, IGPlaylist, IGResolveFailed  # noqa: E402
from bot.modules.media.exceptions import IGPrivateMedia, IGTooLarge  # noqa: E402
from bot.modules.media.formats import (  # noqa: E402
    estimate_bytes,
    quality_cap,
    select_format,
)
from bot.modules.media.handlers import (  # noqa: E402
    _build_caption,
    _gate_allows,
    _media_settings,
    _settings_card,
    ig_callback,
)
from bot.modules.media.downloader import download_post  # noqa: E402
from bot.modules.media.progress import JobProgress  # noqa: E402
from bot.modules.media.resolver import classify_ytdlp_error  # noqa: E402
from bot.modules.media.keyboards import settings_keyboard  # noqa: E402
from bot.modules.media.merge import merge_command  # noqa: E402
from bot.modules.media.models import MediaAsset, MediaKind, PostType, ResolvedPost  # noqa: E402
from bot.modules.media.platforms import (  # noqa: E402
    canonicalize,
    detect_platform,
    find_media_urls,
    is_youtube_short,
    media_key,
    pick_media_url,
    resolve_target,
)
from bot.modules.media.ratelimit import RateLimiter  # noqa: E402
from bot.modules.media.singleflight import run_exclusive  # noqa: E402
from bot.modules.media.exceptions import IGBusy  # noqa: E402
from bot.modules.media import streams as streams_mod  # noqa: E402
from bot.modules.media import youtube as yt_mod  # noqa: E402
from bot.modules.media.url_utils import host_allowed  # noqa: E402


# ═════════════════════════════════════════════════════════════════
# Platform detection + canonicalization
# ═════════════════════════════════════════════════════════════════

class TestDetectPlatform(unittest.TestCase):
    def test_youtube_watch_url(self):
        self.assertEqual(
            detect_platform("https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
            "youtube",
        )

    def test_youtube_short_link(self):
        self.assertEqual(detect_platform("https://youtu.be/dQw4w9WgXcQ"), "youtube")

    def test_youtube_shorts_url(self):
        self.assertEqual(
            detect_platform("https://www.youtube.com/shorts/dQw4w9WgXcQ"),
            "youtube",
        )

    def test_youtube_embed_url(self):
        self.assertEqual(
            detect_platform("https://www.youtube.com/embed/dQw4w9WgXcQ"),
            "youtube",
        )

    def test_tiktok_video_url(self):
        self.assertEqual(
            detect_platform("https://www.tiktok.com/@user/video/7123456789012345678"),
            "tiktok",
        )

    def test_tiktok_short_link(self):
        self.assertEqual(detect_platform("https://vm.tiktok.com/ZMabcdefg/"), "tiktok")

    def test_instagram_url(self):
        self.assertEqual(
            detect_platform("https://www.instagram.com/reel/ABC123/"),
            "instagram",
        )

    def test_garbage_is_none(self):
        self.assertIsNone(detect_platform("hello world"))
        self.assertIsNone(detect_platform("https://example.com/video/1"))
        self.assertIsNone(detect_platform(""))


class TestCanonicalize(unittest.TestCase):
    def test_youtube_id_only(self):
        self.assertEqual(
            canonicalize("youtube", "https://youtu.be/dQw4w9WgXcQ?si=XYZ"),
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        )

    def test_youtube_strips_tracking(self):
        self.assertEqual(
            canonicalize(
                "youtube",
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL123&index=5",
            ),
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        )

    def test_tiktok_full_path(self):
        self.assertEqual(
            canonicalize(
                "tiktok",
                "https://www.tiktok.com/@someone/video/7123456789012345678?is_from_web=1",
            ),
            "https://www.tiktok.com/@someone/video/7123456789012345678",
        )

    def test_instagram_normalized(self):
        self.assertEqual(
            canonicalize("instagram", "https://instagr.am/p/XYZ/?igsh=1"),
            "https://instagram.com/p/XYZ/",
        )

    def test_garbage_raises(self):
        with self.assertRaises(IGInvalidUrl):
            canonicalize("youtube", "https://youtube.com/watch?list=PL1")


class TestMediaKey(unittest.TestCase):
    def test_youtube_key(self):
        self.assertEqual(
            media_key("youtube", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
            "youtube:dQw4w9WgXcQ",
        )

    def test_tiktok_key(self):
        self.assertEqual(
            media_key("tiktok", "https://www.tiktok.com/@u/video/7123456789012345678"),
            "tiktok:7123456789012345678",
        )

    def test_tiktok_short_code_key(self):
        self.assertEqual(
            media_key("tiktok", "https://vm.tiktok.com/ZMabcdefg/"),
            "tiktok:code:ZMabcdefg",
        )

    def test_instagram_key_is_path_based(self):
        key = media_key("instagram", "https://instagram.com/reel/ABC/")
        self.assertTrue(key.startswith("instagram:"))
        self.assertIn("/reel/ABC/", key)

    def test_keys_are_platform_scoped(self):
        """Same id on two platforms must never collide."""
        yt = media_key("youtube", "https://youtu.be/dQw4w9WgXcQ")
        ig = media_key("instagram", "https://instagram.com/p/dQw4w9WgXcQ/")
        self.assertNotEqual(yt, ig)


class TestFindMediaUrls(unittest.TestCase):
    def test_mixed_links_in_text_order(self):
        text = (
            "first https://youtu.be/dQw4w9WgXcQ then "
            "https://www.tiktok.com/@u/video/7123456789012345678 "
            "and https://instagram.com/reel/XYZ/"
        )
        got = find_media_urls(text)
        self.assertEqual([p for p, _ in got], ["youtube", "tiktok", "instagram"])

    def test_no_links(self):
        self.assertEqual(find_media_urls("just chatting"), [])
        self.assertEqual(find_media_urls(""), [])

    def test_pick_prefers_youtube(self):
        text = "https://instagram.com/p/AAA/ https://youtu.be/dQw4w9WgXcQ"
        plat, url = pick_media_url(text)
        self.assertEqual(plat, "youtube")

    def test_pick_skips_bare_instagram_profile(self):
        self.assertIsNone(pick_media_url("follow me on https://www.instagram.com/someone/"))

    def test_pick_takes_first_instagram_post(self):
        plat, url = pick_media_url("https://www.instagram.com/reel/ABC/")
        self.assertEqual(plat, "instagram")
        self.assertIn("/reel/ABC/", url)

    def test_youtube_short_flag(self):
        self.assertTrue(
            is_youtube_short("https://www.youtube.com/shorts/dQw4w9WgXcQ")
        )
        self.assertFalse(
            is_youtube_short("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        )

    def test_resolve_target_returns_pair(self):
        plat, norm = resolve_target("https://youtu.be/dQw4w9WgXcQ")
        self.assertEqual(plat, "youtube")
        self.assertEqual(norm, "https://www.youtube.com/watch?v=dQw4w9WgXcQ")

    def test_resolve_target_rejects_unknown(self):
        with self.assertRaises(IGInvalidUrl):
            resolve_target("https://example.com/not-media")


# ═════════════════════════════════════════════════════════════════
# Format selection (pure)
# ═════════════════════════════════════════════════════════════════

def _fmt(**kw) -> dict:
    base = {
        "url": "https://cdn.example/x",
        "vcodec": "avc1",
        "acodec": "mp4a",
        "height": 1080,
        "width": 1920,
        "tbr": 2500,
        "ext": "mp4",
        "protocol": "https",
        "filesize": 5_000_000,
    }
    base.update(kw)
    return base


class TestSelectFormat(unittest.TestCase):
    def test_quality_cap_mapping(self):
        self.assertEqual(quality_cap("auto"), 1080)
        self.assertEqual(quality_cap("720"), 720)
        self.assertEqual(quality_cap("1080p"), 1080)
        self.assertEqual(quality_cap("best"), 100_000)
        self.assertEqual(quality_cap("nonsense"), 1080)

    def test_prefers_progressive_under_cap(self):
        fmts = [
            _fmt(height=720, filesize=3_000_000),
            _fmt(height=1080, filesize=8_000_000),
        ]
        choice = select_format(fmts, quality="1080", max_bytes=50 * 1024 * 1024)
        self.assertEqual(choice.kind, "progressive")
        self.assertEqual(choice.video["height"], 1080)
        self.assertFalse(choice.needs_merge)

    def test_quality_cap_excludes_taller_progressive(self):
        fmts = [
            _fmt(height=720, filesize=3_000_000),
            _fmt(height=2160, filesize=30_000_000),
        ]
        choice = select_format(fmts, quality="720", max_bytes=50 * 1024 * 1024)
        self.assertEqual(choice.kind, "progressive")
        self.assertEqual(choice.video["height"], 720)

    def test_merge_pair_when_no_progressive(self):
        fmts = [
            _fmt(height=1440, acodec="none", filesize=40_000_000),
            _fmt(height=720, acodec="none", filesize=15_000_000),
            _fmt(height=None, vcodec="none", acodec="mp4a", ext="m4a",
                 filesize=3_000_000),
        ]
        choice = select_format(fmts, quality="auto", max_bytes=60 * 1024 * 1024)
        self.assertEqual(choice.kind, "merge")
        self.assertTrue(choice.needs_merge)
        self.assertEqual(choice.video["height"], 720)
        self.assertEqual(choice.audio["ext"], "m4a")

    def test_merge_pair_rejected_when_too_big(self):
        fmts = [
            _fmt(height=2160, acodec="none", filesize=400_000_000),
            _fmt(height=None, vcodec="none", acodec="opus", ext="webm",
                 filesize=50_000_000),
            _fmt(height=360, filesize=2_000_000),  # small progressive fallback
        ]
        choice = select_format(fmts, quality="best", max_bytes=50 * 1024 * 1024)
        self.assertNotEqual(choice.kind, "none")
        self.assertFalse(choice.needs_merge)

    def test_empty_formats(self):
        choice = select_format([], quality="auto")
        self.assertEqual(choice.kind, "none")

    def test_storyboard_urls_ignored(self):
        fmts = [
            {"url": "https://cdn/storyboard", "vcodec": "none", "acodec": "none",
             "ext": "mhtml", "protocol": "mhtml"},
            _fmt(height=480, filesize=2_000_000),
        ]
        choice = select_format(fmts, quality="auto", max_bytes=50 * 1024 * 1024)
        self.assertEqual(choice.kind, "progressive")
        self.assertEqual(choice.video["height"], 480)

    def test_estimate_bytes_from_tbr(self):
        f = {"tbr": 2000, "duration": 8}  # 2000 kbit/s × 8s / 8 = 2000 KB-ish
        est = estimate_bytes(f)
        self.assertGreater(est, 1_500_000)
        self.assertLess(est, 2_500_000)

    def test_estimate_prefers_filesize(self):
        self.assertEqual(estimate_bytes({"filesize": 12345}), 12345)


# ═════════════════════════════════════════════════════════════════
# ffmpeg merge command (pure argv)
# ═════════════════════════════════════════════════════════════════

class TestMergeCommand(unittest.TestCase):
    def test_stream_copy_argv(self):
        cmd = merge_command(
            Path("v.mp4"), Path("a.m4a"), Path("out/media.mp4"), reencode=False
        )
        self.assertIn("-c:v", cmd)
        self.assertIn("copy", cmd)
        self.assertIn("+faststart", cmd)
        self.assertEqual(cmd[-1], str(Path("out/media.mp4")))

    def test_reencode_argv(self):
        cmd = merge_command(
            Path("v.webm"), Path("a.opus"), Path("out/media.mp4"), reencode=True
        )
        self.assertIn("libx264", cmd)
        self.assertIn("aac", cmd)


# ═════════════════════════════════════════════════════════════════
# Rate limiting
# ═════════════════════════════════════════════════════════════════

class TestRateLimiter(unittest.TestCase):
    def test_requests_per_minute_window(self):
        rl = RateLimiter(per_minute=3, jobs_per_user=1)
        self.assertTrue(rl.check_request(1))
        self.assertTrue(rl.check_request(1))
        self.assertTrue(rl.check_request(1))
        self.assertFalse(rl.check_request(1))  # 4th in same window rejected

    def test_users_are_independent(self):
        rl = RateLimiter(per_minute=1, jobs_per_user=1)
        self.assertTrue(rl.check_request(1))
        self.assertFalse(rl.check_request(1))
        self.assertTrue(rl.check_request(2))

    def test_active_job_cap(self):
        rl = RateLimiter(per_minute=10, jobs_per_user=2)
        self.assertTrue(rl.try_acquire_job(7))
        self.assertTrue(rl.try_acquire_job(7))
        self.assertFalse(rl.try_acquire_job(7))
        rl.release_job(7)
        self.assertTrue(rl.try_acquire_job(7))

    def test_release_never_goes_negative(self):
        rl = RateLimiter(per_minute=10, jobs_per_user=1)
        rl.release_job(9)  # no matching acquire
        self.assertTrue(rl.try_acquire_job(9))


# ═════════════════════════════════════════════════════════════════
# Single-flight wait semantics
# ═════════════════════════════════════════════════════════════════

class TestSingleFlight(unittest.IsolatedAsyncioTestCase):
    async def test_waiter_attaches_after_leader(self):
        order = []

        async def leader():
            order.append("leader-start")
            await asyncio.sleep(0.05)
            order.append("leader-end")
            return "done"

        async def waiter():
            await asyncio.sleep(0.01)  # ensure leader grabs the lock first
            return await run_exclusive("sf:test:attach", leader, timeout=5)

        a, b = await asyncio.gather(
            run_exclusive("sf:test:attach", leader, timeout=5),
            waiter(),
        )
        self.assertEqual(a, "done")
        self.assertEqual(b, "done")
        # leader ran first, then the waiter's own job (cache-hit path).
        self.assertEqual(order[:2], ["leader-start", "leader-end"])

    async def test_timeout_raises_busy(self):
        async def slow():
            await asyncio.sleep(1.0)
            return "x"

        async def waiter():
            return await run_exclusive("sf:test:timeout", slow, timeout=0.05)

        # Start a job holding the lock, then a second that must time out.
        holder = asyncio.ensure_future(run_exclusive("sf:test:timeout", slow, timeout=5))
        await asyncio.sleep(0.01)
        with self.assertRaises(IGBusy):
            await waiter()
        holder.cancel()
        try:
            await holder
        except (asyncio.CancelledError, Exception):
            pass

    async def test_no_wait_raises_busy_immediately(self):
        async def slow():
            await asyncio.sleep(0.5)
            return "x"

        holder = asyncio.ensure_future(run_exclusive("sf:test:nowrap", slow, timeout=5))
        await asyncio.sleep(0.01)
        with self.assertRaises(IGBusy):
            await run_exclusive("sf:test:nowrap", slow, wait=False)
        holder.cancel()
        try:
            await holder
        except (asyncio.CancelledError, Exception):
            pass


# ═════════════════════════════════════════════════════════════════
# Captions / gates / settings UI
# ═════════════════════════════════════════════════════════════════

def _post() -> ResolvedPost:
    return ResolvedPost(
        canonical_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        post_type=PostType.POST,
        media_id="dQw4w9WgXcQ",
        title="Never Gonna Give You Up",
        uploader="RickAstley",
        platform="youtube",
        assets=[
            MediaAsset(url="https://cdn/x.mp4", kind=MediaKind.VIDEO, height=720)
        ],
    )


class TestCaptions(unittest.TestCase):
    def test_off_returns_none(self):
        self.assertIsNone(_build_caption(_post(), "off", "https://x"))

    def test_short_is_credit_only(self):
        cap = _build_caption(_post(), "short", "https://x")
        self.assertIsNotNone(cap)
        self.assertIn("t.me/", cap)
        self.assertNotIn("<b>Never", cap)

    def test_full_has_title_and_credit(self):
        cap = _build_caption(_post(), "full", "https://x")
        self.assertIn("<b>Never Gonna Give You Up</b>", cap)
        self.assertIn("t.me/", cap)

    def test_unknown_mode_falls_back_to_short(self):
        cap = _build_caption(_post(), "weird", "https://x")
        self.assertIn("t.me/", cap)
        self.assertNotIn("<b>Never", cap)


class TestGates(unittest.TestCase):
    def test_youtube_watch_gated_by_videos(self):
        st = {"videos": 0, "shorts": 1, "yt_enabled": 1}
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        self.assertFalse(_gate_allows(st, "youtube", url))
        st["videos"] = 1
        self.assertTrue(_gate_allows(st, "youtube", url))

    def test_youtube_shorts_gated_by_shorts(self):
        st = {"videos": 1, "shorts": 0, "yt_enabled": 1}
        url = "https://www.youtube.com/shorts/dQw4w9WgXcQ"
        self.assertFalse(_gate_allows(st, "youtube", url))
        st["shorts"] = 1
        self.assertTrue(_gate_allows(st, "youtube", url))

    def test_disabled_platform_blocked(self):
        self.assertFalse(
            _gate_allows({"yt_enabled": 0, "videos": 1}, "youtube",
                         "https://youtu.be/dQw4w9WgXcQ")
        )
        self.assertFalse(_gate_allows({"tt_enabled": 0}, "tiktok", "https://vm.tiktok.com/x/"))

    def test_tiktok_enabled(self):
        self.assertTrue(_gate_allows({"tt_enabled": 1}, "tiktok", "https://vm.tiktok.com/x/"))


class TestSettingsUi(unittest.TestCase):
    def test_card_has_every_row(self):
        st = {
            "auto_download": 1, "yt_enabled": 1, "tt_enabled": 1,
            "videos": 1, "shorts": 1, "quality": "1080",
            "captions": "short", "max_mb": 50, "delete_source": 0,
            "progress": 1,
        }
        card = _settings_card(st)
        for needle in (
            "Media Download Settings", "Auto download", "YouTube", "TikTok",
            "Videos", "Shorts", "Max quality", "Captions", "Max file size",
            "Delete source link", "Progress",
        ):
            self.assertIn(needle, card, f"missing row: {needle}")

    def test_keyboard_layout(self):
        st = {
            "auto_download": 1, "yt_enabled": 1, "tt_enabled": 1,
            "videos": 1, "shorts": 1, "quality": "auto",
            "captions": "short", "max_mb": 50, "delete_source": 0,
            "progress": 1,
        }
        kb = settings_keyboard(st)
        datas = [
            b.callback_data
            for row in kb.inline_keyboard
            for b in row
            if getattr(b, "callback_data", None)
        ]
        for cb in (
            "ig:set:auto", "ig:set:yt", "ig:set:tt", "ig:set:videos",
            "ig:set:shorts", "ig:set:quality", "ig:set:maxmb",
            "ig:set:captions", "ig:set:delete", "ig:set:progress", "ig:close",
        ):
            self.assertIn(cb, datas, f"missing button: {cb}")

    def test_keyboard_shows_states(self):
        st = {"yt_enabled": 0, "tt_enabled": 1}
        kb = settings_keyboard(st)
        texts = [
            b.text for row in kb.inline_keyboard for b in row
            if getattr(b, "callback_data", None) == "ig:set:yt"
        ]
        self.assertEqual(texts, ["YouTube: Off"])


class TestPlaylistReject(unittest.IsolatedAsyncioTestCase):
    def test_playlist_message(self):
        err = IGPlaylist()
        self.assertIn("direct video link", err.user)
        self.assertEqual(err.code, "playlist")


# ═════════════════════════════════════════════════════════════════
# Direct stream-URL fallback (YouTube sign-in bypass)
# ═════════════════════════════════════════════════════════════════

class TestResolveSteps(unittest.TestCase):
    def test_default_order(self):
        steps = yt_mod.resolve_steps()
        self.assertEqual(
            steps[:4],
            [
                ("yt", "default"),
                ("yt", "android_vr"),
                ("yt", "tv"),
                ("yt", "alt_clients"),
            ],
        )
        self.assertIn(("stream", "stream_fallback"), steps)

    def test_stream_before_env_optins(self):
        orig = yt_mod.ig_config
        yt_mod.ig_config = dataclasses.replace(
            orig, youtube_po_token="tok", youtube_cookies_file="ck.txt"
        )
        try:
            names = [n for _, n in yt_mod.resolve_steps()]
            self.assertLess(names.index("stream_fallback"), names.index("po_token"))
            self.assertLess(names.index("po_token"), names.index("cookies"))
        finally:
            yt_mod.ig_config = orig

    def test_stream_fallback_can_disable(self):
        orig = yt_mod.ig_config
        yt_mod.ig_config = dataclasses.replace(orig, stream_fallback=False)
        try:
            names = [n for _, n in yt_mod.resolve_steps()]
            self.assertNotIn("stream_fallback", names)
        finally:
            yt_mod.ig_config = orig

    def test_strategy_opts_clients(self):
        self.assertEqual(
            yt_mod._strategy_opts("android_vr")["extractor_args"],
            {"youtube": {"player_client": ["android_vr"]}},
        )
        self.assertEqual(
            yt_mod._strategy_opts("tv")["extractor_args"],
            {"youtube": {"player_client": ["tv"]}},
        )


class TestStreamNormalize(unittest.TestCase):
    def _piped(self):
        return {
            "title": "Demo",
            "uploader": "Chan",
            "duration": 60,
            "thumbnail": "https://i.ytimg.com/x.jpg",
            "videoStreams": [
                {
                    "url": "https://rr1---sn-x.googlevideo.com/videoplayback?p=720",
                    "format": "mp4",
                    "quality": "720p",
                    "height": 720,
                    "width": 1280,
                    "videoOnly": False,
                    "bitrate": 1_200_000,
                    "contentLength": "8000000",
                    "itag": "18",
                    "codec": "avc1",
                },
                {
                    "url": "https://rr1---sn-x.googlevideo.com/videoplayback?v=1080",
                    "format": "mp4",
                    "quality": "1080p",
                    "height": 1080,
                    "width": 1920,
                    "videoOnly": True,
                    "bitrate": 4_500_000,
                    "contentLength": "30000000",
                    "itag": "137",
                    "codec": "avc1",
                },
            ],
            "audioStreams": [
                {
                    "url": "https://rr1---sn-x.googlevideo.com/videoplayback?a=1",
                    "mimeType": "audio/mp4; codecs=\"mp4a.40.2\"",
                    "codec": "mp4a.40.2",
                    "bitrate": 128_000,
                    "contentLength": "1000000",
                    "itag": "140",
                },
            ],
        }

    def test_piped_shapes(self):
        fmts = streams_mod.piped_formats(self._piped())
        self.assertEqual(len(fmts), 3)
        by_id = {f["format_id"]: f for f in fmts}
        # Progressive carries audio; video-only does not; audio-only is audio.
        self.assertEqual(by_id["18"]["acodec"], "aac")
        self.assertIsNone(by_id["137"]["acodec"])
        self.assertEqual(by_id["140"]["vcodec"], "none")
        # bit/s normalized to kbit/s for select_format's tbr.
        self.assertAlmostEqual(by_id["18"]["tbr"], 1200.0)

    def test_piped_progressive_wins(self):
        fmts = streams_mod.piped_formats(self._piped())
        choice = select_format(
            fmts, quality="auto", max_bytes=50 * 1024 * 1024, duration=60
        )
        self.assertEqual(choice.kind, "progressive")
        self.assertEqual(choice.video["height"], 720)

    def test_piped_merge_when_no_progressive(self):
        payload = self._piped()
        payload["videoStreams"] = [payload["videoStreams"][1]]  # drop 720p muxed
        fmts = streams_mod.piped_formats(payload)
        choice = select_format(
            fmts, quality="auto", max_bytes=50 * 1024 * 1024, duration=60
        )
        self.assertEqual(choice.kind, "merge")
        self.assertEqual(choice.video["height"], 1080)
        self.assertEqual(choice.audio["ext"], "m4a")

    def test_invidious_adaptive_split(self):
        payload = {
            "title": "T",
            "author": "A",
            "lengthSeconds": 30,
            "videoThumbnails": [
                {"url": "https://i.example/lo.jpg", "quality": "low"},
                {"url": "https://i.example/max.jpg", "quality": "maxres"},
            ],
            "formatStreams": [
                {
                    "url": "https://rr1---sn-x.googlevideo.com/videoplayback?f=360",
                    "itag": "18",
                    "container": "mp4",
                    "bitrate": 500000,
                    "width": 640,
                    "height": 360,
                    "qualityLabel": "360p",
                    "contentLength": "4000000",
                },
            ],
            "adaptiveFormats": [
                {
                    "url": "https://rr1---sn-x.googlevideo.com/videoplayback?v=1080",
                    "itag": "137",
                    "type": "video/mp4; codecs=\"avc1.640028\"",
                    "bitrate": 4_000_000,
                    "clen": "25000000",
                    "width": 1920,
                    "height": 1080,
                    "qualityLabel": "1080p",
                    "container": "mp4",
                },
                {
                    "url": "https://rr1---sn-x.googlevideo.com/videoplayback?a=140",
                    "itag": "140",
                    "type": "audio/mp4; codecs=\"mp4a.40.2\"",
                    "bitrate": 128000,
                    "clen": "900000",
                    "container": "mp4",
                },
            ],
        }
        fmts = streams_mod.invidious_formats(payload)
        self.assertEqual(len(fmts), 3)
        by = {f["format_id"]: f for f in fmts}
        self.assertEqual(by["137"]["acodec"], "none")  # video-only
        self.assertEqual(by["140"]["vcodec"], "none")  # audio-only
        self.assertEqual(by["18"]["acodec"], "aac")    # muxed progressive

        fmts2, meta = streams_mod.parse_payload("invidious", payload)
        self.assertEqual(meta["uploader"], "A")
        self.assertEqual(meta["thumbnail"], "https://i.example/max.jpg")
        choice = select_format(
            fmts2, quality="auto", max_bytes=50 * 1024 * 1024, duration=30
        )
        self.assertEqual(choice.kind, "merge")

    def test_stream_hosts_allowed(self):
        # Direct CDN stream URLs.
        self.assertTrue(
            host_allowed("https://rr1---sn-x.googlevideo.com/videoplayback?id=1")
        )
        # Configured mirror host itself + proxied sibling subdomain.
        self.assertTrue(host_allowed("https://invidious.f5.si/api/v1/videos/x"))
        self.assertTrue(host_allowed("https://proxy.f5.si/watch/x"))
        # Everything else stays blocked (SSRF).
        self.assertFalse(host_allowed("https://evil.example.com/x"))
        self.assertFalse(host_allowed("https://googlevideo.com.evil.com/x"))

    def test_register_stream_host_allows_mirror_media(self):
        # A mirror discovered at runtime may proxy media itself; fetching
        # registers it, and only then does its media pass the allowlist.
        self.assertFalse(host_allowed("https://mirror.newly-found.example/v.mp4"))
        self.assertFalse(host_allowed("https://cdn.mirror.newly-found.example/v.mp4"))
        streams_mod.register_stream_host("mirror.newly-found.example")
        self.assertTrue(host_allowed("https://mirror.newly-found.example/v.mp4"))
        self.assertTrue(host_allowed("https://cdn.mirror.newly-found.example/v.mp4"))
        # Unrelated hosts stay blocked (SSRF).
        self.assertFalse(host_allowed("https://other.evil.example/x"))

    def test_parse_instance_list_api_flag_and_https(self):
        payload = [
            ["good.example", {"api": True, "uri": "https://good.example"}],
            ["noapi.example", {"api": False, "uri": "https://noapi.example"}],
            ["insecure.example", {"api": True, "uri": "http://insecure.example"}],
            ["nouri.example", {"api": True}],  # falls back to https://name
            "garbage-entry",
        ]
        self.assertEqual(
            streams_mod.parse_instance_list(payload),
            ("good.example", "nouri.example"),
        )

    def test_parse_instance_list_shapes(self):
        wrapped = {
            "instances": [["a.example", {"api": True, "uri": "https://a.example"}]]
        }
        self.assertEqual(streams_mod.parse_instance_list(wrapped), ("a.example",))
        mapped = {"b.example": {"api": True, "uri": "https://b.example"}}
        self.assertEqual(streams_mod.parse_instance_list(mapped), ("b.example",))
        self.assertEqual(streams_mod.parse_instance_list(None), ())
        self.assertEqual(streams_mod.parse_instance_list([["broken"]]), ())
        self.assertEqual(
            streams_mod.parse_instance_list({"c.example": {"api": False}}), ()
        )

    def test_candidate_hosts_dynamic_first_then_static(self):
        orig_dyn = (streams_mod._dyn_hosts, streams_mod._dyn_ts)
        orig_cfg = streams_mod.ig_config
        try:
            streams_mod._dyn_hosts = ("fresh.example", "invidious.f5.si")
            streams_mod._dyn_ts = streams_mod.time.monotonic()
            streams_mod.ig_config = dataclasses.replace(
                orig_cfg, stream_instances=("invidious.f5.si", "static.example.com")
            )
            cands = streams_mod._candidate_hosts(5.0)
            self.assertEqual(
                cands, ("fresh.example", "invidious.f5.si", "static.example.com")
            )
        finally:
            streams_mod._dyn_hosts, streams_mod._dyn_ts = orig_dyn
            streams_mod.ig_config = orig_cfg

    def test_invidious_amp_unescape(self):
        payload = {
            "title": "T",
            "author": "A",
            "lengthSeconds": 5,
            "formatStreams": [
                {
                    "url": "https://rr1---sn-x.googlevideo.com/videoplayback"
                    "?a=1&amp;b=2",
                    "itag": "18",
                    "container": "mp4",
                    "height": 360,
                    "qualityLabel": "360p",
                    "contentLength": "1000",
                },
            ],
        }
        fmts = streams_mod.invidious_formats(payload)
        self.assertIn("b=2", fmts[0]["url"])
        self.assertNotIn("&amp;", fmts[0]["url"])

    @staticmethod
    def _fake_httpx():
        """(Response class, hits dict, Client class) for fetch_stream tests."""
        hits = {"invidious": 0, "piped": 0, "other": 0}

        class _Resp:
            def __init__(self, status=200, data=None):
                self.status_code = status
                self._data = data

            def json(self):
                if self._data is None:
                    raise ValueError("not json")
                return self._data

        return _Resp, hits

    def test_fetch_stream_transient_retry_and_registration(self):
        _Resp, hits = self._fake_httpx()
        payload = {
            "title": "Demo",
            "author": "Chan",
            "lengthSeconds": 42,
            "videoThumbnails": [],
            "formatStreams": [],
            "adaptiveFormats": [
                {
                    "url": "https://host-a.example/videoplayback?v=1",
                    "itag": "137",
                    "type": "video/mp4; codecs=\"avc1.640028\"",
                    "bitrate": 4_000_000,
                    "clen": "25000000",
                    "height": 1080,
                    "qualityLabel": "1080p",
                    "container": "mp4",
                },
                {
                    "url": "https://host-a.example/videoplayback?a=1",
                    "itag": "140",
                    "type": "audio/mp4; codecs=\"mp4a.40.2\"",
                    "bitrate": 128000,
                    "clen": "900000",
                    "container": "mp4",
                },
            ],
        }

        class _Client:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, url):
                if "host-a.example" in url:
                    if "/api/v1/videos/" in url:
                        hits["invidious"] += 1
                        if hits["invidious"] == 1:
                            return _Resp(500)  # transient → retry pass
                        return _Resp(200, payload)
                    hits["piped"] += 1
                    return _Resp(404)
                hits["other"] += 1
                return _Resp(404)

        cfg = dataclasses.replace(
            streams_mod.ig_config,
            stream_instances=("host-a.example", "host-b.example"),
        )
        with mock.patch.object(streams_mod, "ig_config", cfg), \
                mock.patch.object(
                    streams_mod, "_dynamic_instances", return_value=()
                ), \
                mock.patch("httpx.Client", _Client):
            fmts, meta, host = streams_mod.fetch_stream("abc123")

        self.assertEqual(host, "host-a.example")
        self.assertEqual(len(fmts), 2)
        self.assertEqual(meta.get("duration"), 42)
        # Pass 1: 500 + piped 404 + host-b misses; retry: healthy 200.
        self.assertEqual(hits["invidious"], 2)
        self.assertEqual(hits["other"], 2)  # host-b probed in both passes
        # Mirror's own host now passes every downstream allowlist check.
        self.assertTrue(host_allowed("https://host-a.example/v.mp4"))
        self.assertTrue(host_allowed("https://cdn.host-a.example/v.mp4"))

    def test_fetch_stream_no_retry_on_definitive_404(self):
        _Resp, hits = self._fake_httpx()

        class _Client:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, url):
                if "/api/v1/videos/" in url:
                    hits["invidious"] += 1
                else:
                    hits["piped"] += 1
                return _Resp(404)

        cfg = dataclasses.replace(
            streams_mod.ig_config,
            stream_instances=("only.example",),
        )
        with mock.patch.object(streams_mod, "ig_config", cfg), \
                mock.patch.object(
                    streams_mod, "_dynamic_instances", return_value=()
                ), \
                mock.patch("httpx.Client", _Client):
            with self.assertRaises(IGResolveFailed):
                streams_mod.fetch_stream("zz")

        # Definitive misses don't retry: exactly one pass over the list.
        self.assertEqual(hits["invidious"], 1)
        self.assertEqual(hits["piped"], 1)


class TestExhaustedError(unittest.TestCase):
    def test_signin_message_mentions_mirrors_and_levers(self):
        err = yt_mod._exhausted_error(True, None)
        self.assertIn("sign-in", err.user)
        self.assertIn("stream mirrors", err.user.lower())
        self.assertIn("YOUTUBE_COOKIES_B64", err.user)

    def test_generic_falls_back_to_last_error(self):
        last = IGResolveFailed("plain failure")
        self.assertIs(yt_mod._exhausted_error(False, last), last)
        generic = yt_mod._exhausted_error(False, None)
        self.assertEqual(generic.code, "resolve")


# ═════════════════════════════════════════════════════════════════
# Regressions: oversize selection / chat_id crash / classify / proxy
# ═════════════════════════════════════════════════════════════════

_MB = 1024 * 1024


class TestOversizeSelection(unittest.TestCase):
    """The 225 MB/4K bug — every selection step must honour max_bytes."""

    def test_merge_walks_down_when_tallest_pair_too_big(self):
        fmts = [
            {"format_id": "v4k", "url": "https://cdn/4k",
             "vcodec": "av01", "acodec": "none", "height": 2160,
             "width": 3840, "ext": "mp4", "protocol": "https",
             "filesize": 600_000_000},
            {"format_id": "v1080", "url": "https://cdn/1080",
             "vcodec": "vp09", "acodec": "none", "height": 1080,
             "width": 1920, "ext": "mp4", "protocol": "https",
             "filesize": 40_000_000},
            {"format_id": "a", "url": "https://cdn/a",
             "vcodec": "none", "acodec": "mp4a", "ext": "m4a",
             "protocol": "https", "filesize": 5_000_000},
        ]
        choice = select_format(
            fmts, quality="best", max_bytes=50 * _MB, duration=600
        )
        self.assertEqual(choice.kind, "merge")
        self.assertEqual(choice.video["height"], 1080)  # walked down from 4K
        self.assertLessEqual(choice.est_bytes, 50 * _MB)

    def test_step4_prefers_fitting_stream(self):
        # DASH-only list, no audio stream → step 4 used to hand back the
        # 4K/225 MB file no matter the cap.
        fmts = [
            {"format_id": "4k", "url": "https://cdn/4k",
             "vcodec": "avc1", "acodec": "none", "height": 2160,
             "width": 3840, "ext": "mp4", "protocol": "https",
             "filesize": 225_000_000},
            {"format_id": "1080", "url": "https://cdn/1080",
             "vcodec": "avc1", "acodec": "none", "height": 1080,
             "width": 1920, "ext": "mp4", "protocol": "https",
             "filesize": 20_000_000},
        ]
        choice = select_format(
            fmts, quality="auto", max_bytes=50 * _MB
        )
        self.assertEqual(choice.kind, "progressive")
        self.assertEqual(choice.video["height"], 1080)

    def test_step4_last_resort_when_nothing_fits(self):
        # Unchanged old behaviour as a final fallback — the downloader's
        # hard cap still rejects it with a clear IGTooLarge message.
        fmts = [
            {"format_id": "4k", "url": "https://cdn/4k",
             "vcodec": "avc1", "acodec": "none", "height": 2160,
             "width": 3840, "ext": "mp4", "protocol": "https",
             "filesize": 600_000_000},
        ]
        choice = select_format(
            fmts, quality="auto", max_bytes=50 * _MB
        )
        self.assertEqual(choice.video["height"], 2160)


class TestClassifyErrors(unittest.TestCase):
    def test_webpage_error_is_not_a_signin_error(self):
        # Regression: "age" matched inside "p-age" → bogus cookies advice.
        e = Exception(
            "ERROR: [TikTok] 6718: Unexpected response from webpage "
            "request; please report this issue on GitHub"
        )
        err = classify_ytdlp_error(e)
        self.assertEqual(err.code, "resolve")
        self.assertNotIn("sign in", err.user.lower())

    def test_signin_detected_with_neutral_message(self):
        e = Exception("ERROR: Sign in to confirm your age with this video")
        err = classify_ytdlp_error(e)
        self.assertIn("sign in", err.user.lower())
        self.assertNotIn("YOUTUBE_COOKIES_B64", err.user)

    def test_age_restricted_detected(self):
        e = Exception("ERROR: [youtube] x: age-restricted — confirmation required")
        err = classify_ytdlp_error(e)
        self.assertIn("sign in", err.user.lower())


class TestTooLargeMessage(unittest.TestCase):
    def test_stores_size_and_shows_mb(self):
        e = IGTooLarge(225_562_129)
        self.assertEqual(e.size, 225_562_129)
        self.assertEqual(e.code, "too_large")
        self.assertIn("215 MB", e.user)  # 225562129 B ≈ 215.1 MiB

    def test_unknown_size_still_constructs(self):
        e = IGTooLarge()
        self.assertIsNone(e.size)
        self.assertIn("too large", e.user.lower())


class TestDownloadOversize(unittest.IsolatedAsyncioTestCase):
    async def test_download_post_raises_too_large(self):
        post = ResolvedPost(
            canonical_url="https://youtu.be/oversize",
            post_type=PostType.POST,
            media_id="yt:oversize_regression",
            platform="youtube",
            assets=[
                MediaAsset(
                    url="https://rr1---sn-x.googlevideo.com/videoplayback?id=1",
                    kind=MediaKind.VIDEO,
                    height=2160,
                    filesize=225_000_000,
                )
            ],
        )
        with self.assertRaises(IGTooLarge) as ctx:
            await download_post(post, max_bytes=50 * _MB)
        self.assertEqual(ctx.exception.size, 225_000_000)
        # …and the card message names the size in MB, not raw bytes.
        self.assertIn("215 MB", str(ctx.exception))


class TestSettingsCallback(unittest.IsolatedAsyncioTestCase):
    async def test_toggle_uses_chat_dot_id_not_chat_id(self):
        # Regression: `query.message.chat_id` → AttributeError crash on
        # every mediasettings button press.
        bot = FakeBot()
        bot.chat_members[(-100777001, 42)] = SimpleNamespace(
            user=SimpleNamespace(id=42, is_bot=False, first_name="T"),
            status="administrator",
        )
        cb = make_callback("ig:set:yt", chat_id=-100777001, user_id=42)
        before = _media_settings(-100777001).get("yt_enabled")
        await call(ig_callback, cb, bot=bot)
        after = _media_settings(-100777001).get("yt_enabled")
        self.assertNotEqual(before, after, "yt_enabled did not toggle")
        # The settings card was re-rendered on the same message.
        self.assertTrue(
            any(c[0] == "edit_text" for c in cb.message.calls),
            "settings card not edited after toggle",
        )

    async def test_inaccessible_message_returns_quietly(self):
        # InaccessibleMessage has no .chat — must not raise.
        cb = make_callback(
            "ig:set:yt", message=SimpleNamespace(data="ig:set:yt")
        )
        await call(ig_callback, cb, bot=FakeBot())
        # Only the initial bare ack — no "Admins only" alert, no toggle.
        self.assertEqual(len(cb.answers), 1)
        self.assertIsNone(cb.answers[0]["text"])


class TestProxyWiring(unittest.TestCase):
    def test_config_exposes_proxy_url(self):
        from bot.modules.media.config import ig_config
        self.assertTrue(hasattr(ig_config, "proxy_url"))
        self.assertIsInstance(ig_config.proxy_url, str)

    def test_resolver_opts_pick_up_proxy(self):
        from bot.modules.media import config as cfg_mod
        from bot.modules.media import resolver as res_mod
        original = cfg_mod.ig_config
        try:
            cfg_mod.ig_config = dataclasses.replace(
                original, proxy_url="http://127.0.0.1:9999"
            )
            self.assertEqual(
                res_mod.base_ydl_opts().get("proxy"), "http://127.0.0.1:9999"
            )
            cfg_mod.ig_config = dataclasses.replace(original, proxy_url="")
            self.assertNotIn("proxy", res_mod.base_ydl_opts())
        finally:
            cfg_mod.ig_config = original


# ═════════════════════════════════════════════════════════════════
# TikTok: yt-dlp → tikwm mirror fallback
# ═════════════════════════════════════════════════════════════════

_TIKWM_DATA = {
    "id": "7312345678901234567",
    "title": "Scramble up ur name",
    "cover": "https://p16-tiktokcdn.com/cover.jpg",
    "duration": 10,
    "play": "https://v16m.tiktokcdn-us.com/abc/video.mp4",
    "hdplay": None,
    "size": 2953029,
    "hd_size": None,
    "author": {"unique_id": "scout2015"},
}

_TIKWM_URL = "https://www.tiktok.com/@scout2015/video/6718335390845095173"


class TestTikwmBuilder(unittest.TestCase):
    def test_video_post_plan(self):
        from bot.modules.media.tiktok import _post_from_tikwm
        post = _post_from_tikwm(dict(_TIKWM_DATA), _TIKWM_URL, "tiktok:x", 12)
        self.assertEqual(post.platform, "tiktok")
        self.assertEqual(post.resolver, "tikwm")
        self.assertFalse(post.needs_merge)
        self.assertEqual(post.uploader, "scout2015")
        self.assertEqual(len(post.assets), 1)
        a = post.assets[0]
        self.assertEqual(a.kind, MediaKind.VIDEO)
        self.assertEqual(a.filesize, 2953029)
        self.assertTrue(host_allowed(a.url))  # tiktokcdn-us is allowlisted

    def test_relative_play_url_prefixed(self):
        from bot.modules.media.tiktok import _post_from_tikwm
        data = dict(_TIKWM_DATA, play="/3797/vid.mp4", hdplay=None)
        post = _post_from_tikwm(data, _TIKWM_URL, "tiktok:x", 5)
        self.assertTrue(post.assets[0].url.startswith("https://www.tikwm.com/"))
        self.assertTrue(host_allowed(post.assets[0].url))  # .tikwm.com suffix

    def test_photo_carousel(self):
        from bot.modules.media.tiktok import _post_from_tikwm
        data = dict(_TIKWM_DATA, images=[
            "https://p16-tiktokcdn.com/1.jpg",
            "https://p16-tiktokcdn.com/2.jpg",
            "https://p16-tiktokcdn.com/3.jpg",
        ])
        post = _post_from_tikwm(data, _TIKWM_URL, "tiktok:x", 5)
        self.assertEqual(len(post.assets), 3)
        self.assertTrue(all(a.kind == MediaKind.PHOTO for a in post.assets))

    def test_blocked_host_rejected(self):
        from bot.modules.media.tiktok import _post_from_tikwm
        data = dict(_TIKWM_DATA, play="https://evil.example.com/x.mp4")
        with self.assertRaises(IGResolveFailed):
            _post_from_tikwm(data, _TIKWM_URL, "tiktok:x", 5)


class TestTikTokFallback(unittest.IsolatedAsyncioTestCase):
    async def test_mirror_rescues_blocked_ytdlp(self):
        from bot.modules.media import tiktok as tk
        from bot.modules.media.exceptions import IGResolveFailed as RF

        orig_entry, orig_api = tk.extract_entry, tk._tikwm_api
        tk.extract_entry = lambda *a, **k: (_ for _ in ()).throw(
            RF("webpage request blocked")
        )
        tk._tikwm_api = lambda url: dict(_TIKWM_DATA)
        try:
            post = await tk.TikTokResolver().resolve(_TIKWM_URL)
            self.assertEqual(post.resolver, "tikwm")
            self.assertEqual(post.assets[0].kind, MediaKind.VIDEO)
        finally:
            tk.extract_entry, tk._tikwm_api = orig_entry, orig_api

    async def test_original_error_when_both_fail(self):
        from bot.modules.media import tiktok as tk
        from bot.modules.media.exceptions import IGResolveFailed as RF

        orig_entry, orig_api = tk.extract_entry, tk._tikwm_api
        tk.extract_entry = lambda *a, **k: (_ for _ in ()).throw(
            RF("yt boom")
        )
        tk._tikwm_api = lambda url: (_ for _ in ()).throw(
            RuntimeError("api down")
        )
        try:
            with self.assertRaises(RF) as ctx:
                await tk.TikTokResolver().resolve(_TIKWM_URL)
            self.assertIn("yt boom", str(ctx.exception))
        finally:
            tk.extract_entry, tk._tikwm_api = orig_entry, orig_api

    async def test_private_media_not_sent_to_mirror(self):
        from bot.modules.media import tiktok as tk

        calls = []
        orig_entry, orig_api = tk.extract_entry, tk._tikwm_api
        tk.extract_entry = lambda *a, **k: (_ for _ in ()).throw(
            IGPrivateMedia()
        )
        tk._tikwm_api = lambda url: calls.append(url)
        try:
            with self.assertRaises(IGPrivateMedia):
                await tk.TikTokResolver().resolve(_TIKWM_URL)
            self.assertEqual(calls, [], "mirror must not be called for private")
        finally:
            tk.extract_entry, tk._tikwm_api = orig_entry, orig_api

    async def test_mirror_payload_used_end_to_end(self):
        # Full happy path with only the network call stubbed.
        from bot.modules.media import tiktok as tk

        orig_entry, orig_api = tk.extract_entry, tk._tikwm_api

        def _blocked(*a, **k):
            raise IGResolveFailed("bot check")

        tk.extract_entry = _blocked
        tk._tikwm_api = lambda url: dict(
            _TIKWM_DATA,
            images=["https://p16-tiktokcdn.com/1.jpg",
                    "https://p16-tiktokcdn.com/2.jpg"],
        )
        try:
            post = await tk.TikTokResolver().resolve(_TIKWM_URL)
            self.assertEqual(len(post.assets), 2)
            self.assertEqual(post.media_id, "7312345678901234567")
        finally:
            tk.extract_entry, tk._tikwm_api = orig_entry, orig_api


# ═════════════════════════════════════════════════════════════════
# Cookies env wiring + live progress + parallel ranged download
# ═════════════════════════════════════════════════════════════════

class TestYoutubeCookiesEnv(unittest.TestCase):
    def _helper(self):
        from bot.modules.media.config import _youtube_cookies_file
        return _youtube_cookies_file

    def test_explicit_path_wins(self):
        with mock.patch.dict(os.environ, {"YOUTUBE_COOKIES_FILE": "/x/ck.txt"}):
            self.assertEqual(self._helper()(), "/x/ck.txt")

    def test_b64_decodes_to_runtime_file_outside_repo(self):
        import base64

        raw = (
            "# Netscape HTTP Cookie File\n"
            ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tabc123\n"
        )
        env = {
            "YOUTUBE_COOKIES_B64": base64.b64encode(raw.encode()).decode(),
        }
        with mock.patch.dict(os.environ, env):
            os.environ.pop("YOUTUBE_COOKIES_FILE", None)
            path = self._helper()()
        self.assertTrue(path, "b64 cookies not accepted")
        try:
            self.assertEqual(Path(path).read_text(encoding="utf-8"), raw)
            # Credentials must never be written into the repo tree.
            self.assertFalse(Path(path).is_relative_to(ROOT), path)
        finally:
            Path(path).unlink(missing_ok=True)

    def test_junk_content_and_bad_b64_are_refused(self):
        env = {"YOUTUBE_COOKIES_B64": "aGVsbG8gd29ybGQ="}  # "hello world"
        with mock.patch.dict(os.environ, env):
            os.environ.pop("YOUTUBE_COOKIES_FILE", None)
            self.assertEqual(self._helper()(), "", "non-cookie content accepted")
        env = {"YOUTUBE_COOKIES_B64": "!!!not base64!!!"}
        with mock.patch.dict(os.environ, env):
            os.environ.pop("YOUTUBE_COOKIES_FILE", None)
            self.assertEqual(self._helper()(), "", "garbage accepted")

    def test_raw_text_env_supported(self):
        env = {"YOUTUBE_COOKIES_TEXT": ".youtube.com\tTRUE\t/\t0\tSID\tx"}
        with mock.patch.dict(os.environ, env):
            os.environ.pop("YOUTUBE_COOKIES_FILE", None)
            os.environ.pop("YOUTUBE_COOKIES_B64", None)
            path = self._helper()()
        self.assertTrue(path)
        try:
            self.assertIn("youtube.com", Path(path).read_text(encoding="utf-8"))
        finally:
            Path(path).unlink(missing_ok=True)


class TestCookiesInBaseOpts(unittest.TestCase):
    def test_base_opts_carry_cookiefile_for_every_strategy(self):
        from bot.modules.media import config as cfg_mod
        from bot.modules.media import resolver as res_mod

        original = cfg_mod.ig_config
        try:
            cfg_mod.ig_config = dataclasses.replace(
                original, youtube_cookies_file="ck.txt"
            )
            self.assertEqual(
                res_mod.base_ydl_opts().get("cookiefile"), "ck.txt"
            )
            cfg_mod.ig_config = dataclasses.replace(
                original, youtube_cookies_file=""
            )
            self.assertNotIn("cookiefile", res_mod.base_ydl_opts())
        finally:
            cfg_mod.ig_config = original


class TestJobProgress(unittest.TestCase):
    def test_percent_and_display_clamp(self):
        p = JobProgress()
        p.expect(100)
        p.add(50)
        self.assertEqual(p.percent(), 50.0)
        self.assertEqual(p.done, 50)
        p.add(200)  # retries may recount — display never exceeds 100%
        self.assertEqual(p.percent(), 100.0)
        self.assertEqual(p.done, 100)
        self.assertEqual(p.raw, 250)

    def test_speed_over_time_window(self):
        t = [1000.0]
        p = JobProgress(clock=lambda: t[0])
        p.expect(1000)
        p.add(100)
        t[0] = 1002.0
        p.add(500)
        # (600 raw - 100 first bucket) / 2 s = 250 B/s
        self.assertAlmostEqual(p.speed(), 250.0)

    def test_text_throttle_and_stage_change(self):
        t = [5.0]
        p = JobProgress(edit_interval=0.9, clock=lambda: t[0])
        self.assertIn("Resolving", p.text_for_edit())
        t[0] = 5.5
        self.assertIsNone(p.text_for_edit(), "interval not enforced")
        t[0] = 6.0
        self.assertIsNone(p.text_for_edit(), "unchanged text resent")
        p.stage("download")
        p.expect(1000)
        p.add(500)
        text = p.text_for_edit()
        self.assertIsNotNone(text, "stage change not surfaced")
        self.assertIn("Downloading", text)
        self.assertIn("50%", text)
        self.assertIn("500 B/1000 B", text)

    def test_download_text_without_total(self):
        p = JobProgress(edit_interval=0.0)
        p.stage("download")
        p.add(4096)
        self.assertIn("Downloading… (4.0 KB)", p.text())


class TestParallelDownload(unittest.IsolatedAsyncioTestCase):
    URL = "https://redirector.googlevideo.com/videoplayback?id=x"
    SIZE = 8192

    class _Resp:
        def __init__(self, status, headers, body):
            self.status_code = status
            self.headers = headers
            self._body = body

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def aiter_bytes(self, n):
            for i in range(0, len(self._body), n):
                yield self._body[i : i + n]

    class _Client:
        def __init__(self, handler):
            self._handler = handler
            self.calls = []

        def stream(self, method, url, headers=None):
            h = dict(headers or {})
            self.calls.append(h)
            return self._handler(h)

    def _payload(self):
        return (b"0123456789abcdef" * (self.SIZE // 16))[: self.SIZE]

    def _range_handler(self, payload, honor_range=True):
        def handler(h):
            if "Range" in h and honor_range:
                a, b = h["Range"].split("=")[1].split("-")
                a, b = int(a), int(b)
                return self._Resp(
                    206,
                    {
                        "content-range": f"bytes {a}-{b}/{len(payload)}",
                        "content-type": "video/mp4",
                    },
                    payload[a : b + 1],
                )
            return self._Resp(200, {"content-type": "video/mp4"}, payload)
        return handler

    async def _download(self, payload, handler, *, filesize=None, progress=None):
        from bot.modules.media import downloader as dl_mod

        dest = Path(tempfile.mkdtemp(prefix="pi_par_dl_"))
        self.addCleanup(shutil.rmtree, dest, ignore_errors=True)
        asset = MediaAsset(
            url=self.URL,
            kind=MediaKind.VIDEO,
            filesize=len(payload) if filesize is None else filesize,
        )
        client = self._Client(handler)
        with mock.patch.object(dl_mod, "_get_http_client", return_value=client), \
                mock.patch.object(dl_mod, "_PARALLEL_MIN", 1024):
            paths = await dl_mod._http_download(
                self.URL, dest, asset, platform="youtube", progress=progress
            )
        return paths, client, dest

    async def test_honored_ranges_assemble_exact_bytes(self):
        payload = self._payload()
        prog = JobProgress()
        paths, client, dest = await self._download(
            payload, self._range_handler(payload), progress=prog
        )
        self.assertEqual(paths[0].read_bytes(), payload)
        self.assertEqual(len(client.calls), 2, "expected two range workers")
        self.assertTrue(all("Range" in c for c in client.calls))
        self.assertEqual(prog.raw, self.SIZE, "progress missed ranged bytes")
        leftovers = [p.name for p in dest.iterdir()]
        self.assertEqual(leftovers, ["media.mp4"], leftovers)

    async def test_range_ignored_falls_back_to_single_stream(self):
        payload = self._payload()
        paths, client, dest = await self._download(
            payload, self._range_handler(payload, honor_range=False)
        )
        self.assertEqual(paths[0].read_bytes(), payload)
        # Two range workers (both refused) + one plain GET.
        self.assertEqual(len(client.calls), 3, "range workers + plain GET")
        self.assertIn("Range", client.calls[0])
        self.assertIn("Range", client.calls[1])
        self.assertNotIn("Range", client.calls[2])
        self.assertEqual([p.name for p in dest.iterdir()], ["media.mp4"])

    async def test_small_file_stays_single_stream(self):
        payload = self._payload()[:512]
        paths, client, dest = await self._download(
            payload,
            self._range_handler(payload),
            filesize=512,
        )
        self.assertEqual(paths[0].read_bytes(), payload)
        self.assertEqual(len(client.calls), 1, "small file must not fan out")
        self.assertNotIn("Range", client.calls[0])


class TestStatusProgress(unittest.IsolatedAsyncioTestCase):
    async def test_progress_edits_then_finish_deletes(self):
        from bot.modules.media import handlers as h

        sent = SimpleNamespace(edit_calls=[], deleted=False)

        async def _edit(text=None, **kw):
            sent.edit_calls.append(text)

        async def _delete():
            sent.deleted = True

        async def _reply(*a, **kw):
            return sent

        sent.edit_text = _edit
        sent.delete = _delete

        prog = JobProgress(edit_interval=0.0)
        holder = {}
        orig_reply = h.reply_text
        h.reply_text = _reply
        try:
            task = asyncio.create_task(
                h._delayed_status(
                    object(), 0.01, True, quote=True,
                    progress=prog, holder=holder,
                )
            )
            await asyncio.sleep(0.35)
            self.assertIs(holder.get("msg"), sent, "holder not populated")
            prog.stage("download")
            prog.expect(100)
            prog.add(42)
            await asyncio.sleep(0.35)
            self.assertTrue(sent.edit_calls, "no live progress edit")
            self.assertIn("Downloading", sent.edit_calls[-1])
            await h._finish_status(task, success=True, holder=holder)
            self.assertTrue(sent.deleted, "status not deleted on success")
        finally:
            h.reply_text = orig_reply


# ═════════════════════════════════════════════════════════════════
# Instagram DASH audio pairing (silent-video fix)
# ═════════════════════════════════════════════════════════════════

_DASH_VIDEO = {
    "url": "https://scontent.cdninstagram.com/v/videoonly.mp4",
    "vcodec": "h264",
    "acodec": "none",
    "height": 1080,
    "tbr": 2500,
    "ext": "mp4",
}
_DASH_AUDIO = {
    "url": "https://scontent.cdninstagram.com/v/audio.m4a",
    "vcodec": "none",
    "acodec": "mp4a.40.2",
    "tbr": 128,
    "ext": "m4a",
}


class TestIgDashAudioPair(unittest.TestCase):
    def test_video_only_gets_audio_pair(self):
        from bot.modules.media.resolver import _assets_from_entry, _is_merge_pair

        entry = {"formats": [dict(_DASH_VIDEO), dict(_DASH_AUDIO)]}
        assets = _assets_from_entry(entry, True)
        self.assertEqual(len(assets), 2, "video-only pick must fetch its audio pair")
        video, audio = assets
        self.assertEqual(video.kind, MediaKind.VIDEO)
        self.assertEqual(video.merge_role, "v")
        self.assertEqual(audio.kind, MediaKind.AUDIO)
        self.assertEqual(audio.merge_role, "a")
        self.assertEqual(audio.ext, "m4a")
        self.assertTrue(_is_merge_pair(assets))

    def test_progressive_stays_single_asset(self):
        from bot.modules.media.resolver import _assets_from_entry, _is_merge_pair

        progressive = dict(_DASH_VIDEO, acodec="mp4a.40.2", url=_DASH_VIDEO["url"])
        entry = {"formats": [progressive]}
        assets = _assets_from_entry(entry, True)
        self.assertEqual(len(assets), 1, "progressive stream already has audio")
        self.assertIsNone(assets[0].merge_role)
        self.assertFalse(_is_merge_pair(assets))

    def test_video_only_without_audio_stream(self):
        from bot.modules.media.resolver import _assets_from_entry, _is_merge_pair

        assets = _assets_from_entry({"formats": [dict(_DASH_VIDEO)]}, True)
        self.assertEqual(len(assets), 1)
        self.assertFalse(_is_merge_pair(assets))

    def test_sidecar_recursion_does_not_pair(self):
        from bot.modules.media.resolver import _assets_from_entry, _is_merge_pair

        entry = {
            "entries": [
                {"formats": [dict(_DASH_VIDEO), dict(_DASH_AUDIO)]},
                {"formats": [dict(_DASH_VIDEO)]},
            ],
        }
        assets = _assets_from_entry(entry, True)
        self.assertEqual(len(assets), 2, "sidecar items are delivered separately")
        self.assertFalse(_is_merge_pair(assets))

    def test_merge_pair_helper_exact_roles(self):
        from bot.modules.media.models import MediaAsset
        from bot.modules.media.resolver import _is_merge_pair

        def a(role):
            return MediaAsset(
                url="https://scontent.cdninstagram.com/x", kind=MediaKind.VIDEO,
                merge_role=role,
            )

        self.assertTrue(_is_merge_pair([a("v"), a("a")]))
        self.assertFalse(_is_merge_pair([a("v")]))
        self.assertFalse(_is_merge_pair([a("a")]))
        self.assertFalse(_is_merge_pair([a("v"), a("v")]))
        self.assertFalse(_is_merge_pair([a("v"), a("a"), a(None)]))

    def test_audio_pair_survives_dedup_and_flags_post(self):
        """YtDlp-style pass: pair + uniq must still yield needs_merge=True."""
        from bot.modules.media.resolver import _assets_from_entry, _is_merge_pair

        entry = {"formats": [dict(_DASH_VIDEO), dict(_DASH_AUDIO), dict(_DASH_VIDEO)]}
        assets = _assets_from_entry(entry, True)
        seen, uniq = set(), []
        for x in assets:
            if x.url in seen:
                continue
            seen.add(x.url)
            uniq.append(x)
        self.assertTrue(_is_merge_pair(uniq))


if __name__ == "__main__":
    unittest.main(verbosity=2)
