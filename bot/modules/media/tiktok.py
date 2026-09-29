"""TikTok resolver — yt-dlp first, tikwm mirror fallback.

TikTok blocks many datacenter IPs at the webpage layer ("Unexpected
response from webpage request"). When yt-dlp hits that wall (or any
other resolve error short of private/gone), we retry through the public
tikwm.com API, which answers with direct CDN MP4/photo URLs. The
operator's MEDIA_PROXY_URL proxy (if set) is used for both paths.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Optional

from .config import ig_config
from .exceptions import IGMediaGone, IGPlaylist, IGPrivateMedia, IGResolveFailed
from .formats import select_format
from .metrics import ig_log, metrics
from .models import MediaAsset, MediaKind, PostType, ResolvedPost
from .platforms import canonicalize, media_key
from .resolver import base_ydl_opts, classify_ytdlp_error, extract_entry
from .url_utils import host_allowed
from .youtube import _assets_from_choice

# Public TikTok mirror — hardcoded (not operator-configurable), so the
# only host we ever contact for this path is this constant.
TIKWM_API = "https://www.tikwm.com/api/"
TIKWM_TIMEOUT = 10.0
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def _tikwm_api(url: str) -> Dict[str, Any]:
    """One sync GET against the tikwm API (call inside asyncio.to_thread)."""
    import httpx

    resp = httpx.get(
        TIKWM_API,
        params={"url": url},
        headers={"User-Agent": _UA, "Accept": "application/json",
                 "Referer": "https://www.tikwm.com/"},
        timeout=TIKWM_TIMEOUT,
        follow_redirects=True,
        proxy=ig_config.proxy_url or None,
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("code") != 0 or not payload.get("data"):
        raise IGResolveFailed(
            f"tikwm mirror: {payload.get('msg') or 'no data'}"
        )
    return payload["data"]


def _abs_tikwm(u: Optional[str]) -> str:
    """tikwm usually returns absolute CDN URLs; handle leading '/' too."""
    u = (u or "").strip()
    if u.startswith("/"):
        return "https://www.tikwm.com" + u
    return u


def _post_from_tikwm(
    data: Dict[str, Any], norm: str, key: str, elapsed: int
) -> ResolvedPost:
    """Pure builder: tikwm ``data`` payload → ResolvedPost plan."""
    images = [u for u in (data.get("images") or []) if u]
    if images:
        assets = [
            MediaAsset(url=_abs_tikwm(u), kind=MediaKind.PHOTO)
            for u in images[: ig_config.max_items]
        ]
    else:
        # hdplay only exists when the source has an HD rendition.
        url = _abs_tikwm(data.get("hdplay") or data.get("play")
                         or data.get("wmplay"))
        if not url:
            raise IGResolveFailed("tikwm mirror returned no media URL.")
        size = data.get("hd_size") or data.get("size")
        assets = [
            MediaAsset(
                url=url,
                kind=MediaKind.VIDEO,
                duration=data.get("duration"),
                filesize=size,
                ext="mp4",
            )
        ]

    for a in assets:
        if not host_allowed(a.url):
            raise IGResolveFailed("Blocked media host.")

    title = str(data.get("title") or "")[:200]
    uploader = str(((data.get("author") or {}).get("unique_id")) or "")[:80]
    return ResolvedPost(
        canonical_url=norm,
        post_type=PostType.POST,
        media_id=str(data.get("id") or key.split(":", 1)[-1]),
        title=title,
        uploader=uploader,
        caption=title[:800],
        webpage_url=norm,
        assets=assets,
        thumbnail=data.get("cover"),
        resolve_ms=elapsed,
        resolver="tikwm",
        platform="tiktok",
        needs_merge=False,
    )


class TikTokResolver:
    """Resolve a TikTok video/photo URL into a ResolvedPost plan."""

    name = "tiktok"

    async def resolve(self, url: str) -> ResolvedPost:
        return await asyncio.to_thread(self._resolve_sync, url)

    def _resolve_sync(self, url: str) -> ResolvedPost:
        start = time.perf_counter()
        norm = canonicalize("tiktok", url)
        key = media_key("tiktok", norm)

        try:
            return self._resolve_ytdlp(norm, key, start)
        except (IGPrivateMedia, IGMediaGone, IGPlaylist):
            # Definitive answers — the mirror won't resurrect them.
            raise
        except Exception as e:
            yt_err = e if isinstance(e, IGResolveFailed) \
                else classify_ytdlp_error(e)
        # yt-dlp is IP-blocked or otherwise stuck → public mirror API.
        try:
            post = self._resolve_tikwm(norm, key, start)
            ig_log(f"tikwm mirror rescued {key}")
            return post
        except Exception as mirror_err:
            ig_log(f"tikwm mirror also failed: {mirror_err}")
            raise yt_err from None

    def _resolve_ytdlp(
        self, norm: str, key: str, start: float
    ) -> ResolvedPost:
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

    def _resolve_tikwm(
        self, norm: str, key: str, start: float
    ) -> ResolvedPost:
        data = _tikwm_api(norm)
        elapsed = int((time.perf_counter() - start) * 1000)
        post = _post_from_tikwm(data, norm, key, elapsed)
        metrics.bump("resolved_ok", ms=elapsed)
        metrics.bump_platform("tiktok")
        ig_log(
            f"resolved {key} via tikwm in {elapsed}ms "
            f"assets={len(post.assets)}"
        )
        return post
