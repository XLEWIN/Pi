"""YouTube resolver — yt-dlp Python API, strategy fallbacks, format plan.

Strategies tried in order (each only when it can help):
  1. default clients
  2. android_vr client   — bot-check-free, no sign-in / PO token needed
  3. tv client           — second sign-in-free client
  4. alternate clients   — web/android fixes many signature issues
  5. stream mirrors      — Piped/Invidious direct CDN stream URLs; bypasses
                           YouTube's player API entirely (MEDIA_STREAM_FALLBACK)
  6. PO token            — only when YOUTUBE_PO_TOKEN is set
  7. cookies file        — only when YOUTUBE_COOKIES_FILE is set

No credentials are hard-coded — operators opt in via env.  Playlists are
rejected with a friendly "direct video link" message.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional, Tuple

from .config import ig_config
from .exceptions import (
    IGError,
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
    if name == "android_vr":
        opts["extractor_args"] = {"youtube": {"player_client": ["android_vr"]}}
    elif name == "tv":
        opts["extractor_args"] = {"youtube": {"player_client": ["tv"]}}
    elif name == "alt_clients":
        opts["extractor_args"] = {"youtube": {"player_client": ["web", "android"]}}
    elif name == "po_token" and ig_config.youtube_po_token:
        opts["extractor_args"] = {"youtube": {"po_token": [ig_config.youtube_po_token]}}
    elif name == "cookies" and ig_config.youtube_cookies_file:
        opts["cookiefile"] = ig_config.youtube_cookies_file
    return opts


def client_strategies() -> List[Tuple[str, Dict[str, Any]]]:
    """yt-dlp player-client attempts — no operator setup required."""
    return [
        ("default", _strategy_opts("default")),
        ("android_vr", _strategy_opts("android_vr")),
        ("tv", _strategy_opts("tv")),
        ("alt_clients", _strategy_opts("alt_clients")),
    ]


def env_strategies() -> List[Tuple[str, Dict[str, Any]]]:
    """Operator opt-ins — appear only when the matching env is set."""
    out: List[Tuple[str, Dict[str, Any]]] = []
    if ig_config.youtube_po_token:
        out.append(("po_token", _strategy_opts("po_token")))
    if ig_config.youtube_cookies_file:
        out.append(("cookies", _strategy_opts("cookies")))
    return out


def resolve_steps() -> List[Tuple[str, str]]:
    """Ordered (kind, name) attempts: kind 'yt' runs yt-dlp, 'stream' hits mirrors."""
    steps: List[Tuple[str, str]] = [("yt", name) for name, _ in client_strategies()]
    if ig_config.stream_fallback:
        steps.append(("stream", "stream_fallback"))
    steps.extend(("yt", name) for name, _ in env_strategies())
    return steps


def strategies() -> List[Tuple[str, Dict[str, Any]]]:
    """All yt-dlp (name, opts) pairs — kept for callers that only extract."""
    return client_strategies() + env_strategies()


# Errors that no other strategy can fix — fail fast instead of retrying.
_FATAL_CODES = {"invalid_url", "playlist", "gone", "private"}


def _exhausted_error(sign_in_seen: bool, last_err: Optional[IGError]) -> IGError:
    """Final error after every strategy failed.

    The sign-in case gets an honest, complete message: mirrors were tried
    too, so only cookies/PO token remain as operator levers.
    """
    if sign_in_seen:
        return IGResolveFailed(
            "YouTube is demanding a sign-in from this server (bot check). "
            "Alternate players and public stream mirrors were tried as well. "
            "Set YOUTUBE_COOKIES_B64 (base64 of an exported browser cookie "
            "file) or YOUTUBE_PO_TOKEN to bypass it."
        )
    return last_err or IGResolveFailed()


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

        yt_opts: Dict[str, Dict[str, Any]] = dict(client_strategies())
        yt_opts.update(env_strategies())

        info: Optional[Dict[str, Any]] = None
        used = "default"
        last_err: Optional[IGError] = None
        sign_in_seen = False

        for kind, name in resolve_steps():
            try:
                if kind == "stream":
                    # Direct CDN stream URLs — bypasses YouTube's player
                    # API (and its sign-in wall) completely.
                    from .streams import fetch_stream

                    fmts, meta, _mirror = fetch_stream(vid)
                    if (
                        select_format(
                            fmts,
                            quality=ig_config.quality,
                            max_bytes=ig_config.max_video_mb * 1024 * 1024,
                            duration=meta.get("duration"),
                            allow_merge=True,
                        ).kind
                        == "none"
                    ):
                        raise IGResolveFailed("Stream mirror formats unusable.")
                    info = {
                        "id": vid,
                        "title": meta.get("title"),
                        "uploader": meta.get("uploader"),
                        "description": meta.get("description"),
                        "thumbnail": meta.get("thumbnail"),
                        "duration": meta.get("duration"),
                        "webpage_url": norm,
                        "formats": fmts,
                    }
                else:
                    info = extract_entry(norm, yt_opts[name])
                used = name
                break
            except (IGInvalidUrl, IGPlaylist, IGPrivateMedia, IGMediaGone):
                raise
            except Exception as e:  # classified DownloadError or plain failure
                err = classify_ytdlp_error(e)
                if getattr(err, "code", "") in _FATAL_CODES:
                    raise err from e
                if "sign in" in str(err).lower():
                    sign_in_seen = True
                last_err = err
                ig_log(f"yt strategy '{name}' failed: {e}")
                continue
        if info is None:
            raise _exhausted_error(sign_in_seen, last_err)

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
