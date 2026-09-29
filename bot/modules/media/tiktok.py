"""TikTok resolver — direct MP4 via yt-dlp, same format plan as YouTube."""

from __future__ import annotations

import asyncio
import time
from typing import Dict, Optional

from .config import ig_config
from .exceptions import IGMediaGone, IGPlaylist, IGPrivateMedia, IGResolveFailed
from .formats import select_format
from .metrics import ig_log, metrics
from .models import PostType, ResolvedPost
from .platforms import canonicalize, media_key
from .resolver import base_ydl_opts, classify_ytdlp_error, extract_entry
from .url_utils import host_allowed
from .youtube import _assets_from_choice


class TikTokResolver:
    """Resolve a TikTok video/photo URL into a ResolvedPost plan."""

    name = "tiktok"

    async def resolve(self, url: str) -> ResolvedPost:
        return await asyncio.to_thread(self._resolve_sync, url)

    def _resolve_sync(self, url: str) -> ResolvedPost:
        start = time.perf_counter()
        norm = canonicalize("tiktok", url)
        key = media_key("tiktok", norm)

        opts = base_ydl_opts()
        try:
            info: Optional[Dict] = extract_entry(norm, opts)
        except (IGPrivateMedia, IGMediaGone, IGPlaylist):
            raise
        except Exception as e:
            raise classify_ytdlp_error(e) from e
        if not info:
            raise IGMediaGone()

        if info.get("_type") in {"playlist", "multi_video"} or info.get("entries"):
            raise IGPlaylist()

        formats = info.get("formats") or []
        if not formats and info.get("url"):
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
        metrics.bump_platform("tiktok")
        ig_log(f"resolved {key} merge={needs_merge} in {elapsed}ms")

        title = str(info.get("title") or info.get("description") or "")[:200]
        uploader = str(
            info.get("uploader") or info.get("creator") or info.get("channel") or ""
        )[:80]
        return ResolvedPost(
            canonical_url=norm,
            post_type=PostType.POST,
            media_id=str(info.get("id") or key.split(":", 1)[-1]),
            title=title,
            uploader=uploader,
            caption=str(info.get("description") or title)[:800],
            webpage_url=str(info.get("webpage_url") or norm),
            assets=assets,
            thumbnail=info.get("thumbnail"),
            resolve_ms=elapsed,
            resolver=self.name,
            platform="tiktok",
            needs_merge=needs_merge,
        )
