"""YouTube resolver — yt-dlp Python API, strategy fallbacks, format plan.

Strategies tried in order (each only when it can help):
  1. default clients
  2. alternate player clients (web/android fixes many signature issues)
  3. PO token        — only when YOUTUBE_PO_TOKEN is set
  4. cookies file    — only when YOUTUBE_COOKIES_FILE is set

No credentials are hard-coded — operators opt in via env. Playlists are
rejected with a friendly "direct video link" message.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional, Tuple

from .config import ig_config
from .exceptions import (
    IGInvalidUrl,
    IGMediaGone,
    IGPlaylist,
    IGPrivateMedia,
    IGResolveFailed,
)
from .formats import select_format
from .metrics import ig_log, metrics
from .models import MediaAsset, MediaKind, PostType, ResolvedPost
from .platforms import canonicalize, is_youtube_short, youtube_video_id
from .resolver import base_ydl_opts, classify_ytdlp_error, extract_entry
from .url_utils import host_allowed


def _strategy_opts(name: str) -> Dict[str, Any]:
    """Build yt-dlp opts for one named strategy."""
    opts = base_ydl_opts()
    if name == "alt_clients":
        opts["extractor_args"] = {"youtube": {"player_client": ["web", "android"]}}
    elif name == "po_token" and ig_config.youtube_po_token:
        opts["extractor_args"] = {"youtube": {"po_token": [ig_config.youtube_po_token]}}
    elif name == "cookies" and ig_config.youtube_cookies_file:
        opts["cookiefile"] = ig_config.youtube_cookies_file
    return opts


def strategies() -> List[Tuple[str, Dict[str, Any]]]:
    """Ordered (name, opts) pairs — env-gated entries appear only when set."""
    out: List[Tuple[str, Dict[str, Any]]] = [
        ("default", _strategy_opts("default")),
        ("alt_clients", _strategy_opts("alt_clients")),
    ]
    if ig_config.youtube_po_token:
        out.append(("po_token", _strategy_opts("po_token")))
    if ig_config.youtube_cookies_file:
        out.append(("cookies", _strategy_opts("cookies")))
    return out


# Errors that no other strategy can fix — fail fast instead of retrying.
_FATAL_CODES = {"invalid_url", "playlist", "gone", "private"}


def _assets_from_choice(choice, info: Dict[str, Any]) -> Tuple[List[MediaAsset], bool]:
    """Turn a FormatChoice into MediaAsset list (+ merge flag)."""
    duration = info.get("duration")
    assets: List[MediaAsset] = []
    if choice.kind == "progressive" and choice.video:
        f = choice.video
        assets.append(
            MediaAsset(
                url=str(f.get("url")),
                kind=MediaKind.VIDEO,
                width=f.get("width"),
                height=f.get("height"),
                duration=duration,
                filesize=f.get("filesize") or f.get("filesize_approx"),
                ext=str(f.get("ext") or "mp4"),
                codec=f.get("vcodec"),
            )
        )
        return assets, False
    if choice.kind == "merge" and choice.video and choice.audio:
        v, a = choice.video, choice.audio
        assets.append(
            MediaAsset(
                url=str(v.get("url")),
                kind=MediaKind.VIDEO,
                width=v.get("width"),
                height=v.get("height"),
                duration=duration,
                filesize=v.get("filesize") or v.get("filesize_approx"),
                ext=str(v.get("ext") or "mp4"),
                codec=v.get("vcodec"),
                merge_role="v",
            )
        )
        assets.append(
            MediaAsset(
                url=str(a.get("url")),
                kind=MediaKind.AUDIO,
                duration=duration,
                filesize=a.get("filesize") or a.get("filesize_approx"),
                ext=str(a.get("ext") or "m4a"),
                codec=a.get("acodec"),
                merge_role="a",
            )
        )
        return assets, True
    return assets, False


class YouTubeResolver:
    """Resolve a single YouTube video URL into a ResolvedPost plan."""

    name = "youtube"

    async def resolve(self, url: str) -> ResolvedPost:
        return await asyncio.to_thread(self._resolve_sync, url)

    def _resolve_sync(self, url: str) -> ResolvedPost:
        start = time.perf_counter()
        norm = canonicalize("youtube", url)
        vid = youtube_video_id(norm) or ""
        post_type = PostType.REEL if is_youtube_short(url) else PostType.POST

        info: Optional[Dict[str, Any]] = None
        used = "default"
        last_err: Optional[Exception] = None
        for name, opts in strategies():
            try:
                info = extract_entry(norm, opts)
                used = name
                break
            except IGInvalidUrl:
                raise
            except IGPlaylist:
                raise
            except IGPrivateMedia:
                raise
            except IGMediaGone:
                raise
            except Exception as e:  # classified DownloadError or plain failure
                err = classify_ytdlp_error(e)
                if getattr(err, "code", "") in _FATAL_CODES:
                    raise err from e
                last_err = err
                ig_log(f"yt strategy '{name}' failed: {e}")
                continue
        if info is None:
            raise last_err or IGResolveFailed()

        # Playlists / multi-entry pages are rejected with guidance.
        if info.get("_type") in {"playlist", "multi_video"} or info.get("entries"):
            raise IGPlaylist()

        formats = info.get("formats") or []
        if not formats and info.get("url"):
            # Flat entries already carry a direct URL.
            formats = [info]

        choice = select_format(
            formats,
            quality=ig_config.quality,
            max_bytes=ig_config.max_video_mb * 1024 * 1024,
            duration=info.get("duration"),
            allow_merge=True,
        )
        if choice.kind == "none":
            raise IGResolveFailed("No downloadable format for this video.")

        assets, needs_merge = _assets_from_choice(choice, info)
        for a in assets:
            if not host_allowed(a.url):
                raise IGResolveFailed("Blocked media host.")

        elapsed = int((time.perf_counter() - start) * 1000)
        metrics.bump("resolved_ok", ms=elapsed)
        metrics.bump_platform("youtube")
        if needs_merge:
            metrics.bump("merges")
        height = (assets and assets[0].height) or 0
        ig_log(
            f"resolved yt:{vid} strategy={used} merge={needs_merge} "
            f"h={height} in {elapsed}ms"
        )

        title = str(info.get("title") or "")[:200]
        uploader = str(
            info.get("uploader") or info.get("channel") or info.get("creator") or ""
        )[:80]
        return ResolvedPost(
            canonical_url=norm,
            post_type=post_type,
            media_id=vid or str(info.get("id") or "youtube"),
            title=title,
            uploader=uploader,
            caption=str(info.get("description") or title)[:800],
            webpage_url=str(info.get("webpage_url") or norm),
            assets=assets,
            thumbnail=info.get("thumbnail"),
            resolve_ms=elapsed,
            resolver=f"{self.name}:{used}",
            platform="youtube",
            needs_merge=needs_merge,
        )
