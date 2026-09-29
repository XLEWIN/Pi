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
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

# ── Environment isolation — must precede bot imports ──────────────
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["BOT_TOKEN"] = "1:TEST-TOKEN-FOR-UNIT-TESTS"
_TEST_DIR = tempfile.mkdtemp(prefix="pi_media_test_")
os.environ["LOCALAPPDATA"] = _TEST_DIR
atexit.register(shutil.rmtree, _TEST_DIR, ignore_errors=True)

# ── Imports (after env) ───────────────────────────────────────────
from bot.modules.media.exceptions import IGInvalidUrl, IGPlaylist  # noqa: E402
from bot.modules.media.formats import (  # noqa: E402
    estimate_bytes,
    quality_cap,
    select_format,
)
from bot.modules.media.handlers import (  # noqa: E402
    _build_caption,
    _gate_allows,
    _settings_card,
)
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
