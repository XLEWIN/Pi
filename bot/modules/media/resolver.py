"""Media resolution — yt-dlp Python API (no subprocess) + optional fallback.

Shared helpers (``base_ydl_opts`` / ``extract_entry`` /
``classify_ytdlp_error``) power the platform resolvers in
``youtube.py`` / ``tiktok.py`` as well as the Instagram chain below.
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from .exceptions import (
    IGError,
    IGInvalidUrl,
    IGMediaGone,
    IGPlaylist,
    IGPrivateMedia,
    IGRateLimited,
    IGResolveFailed,
)
from .metrics import ig_log, metrics
from .models import MediaAsset, MediaKind, PostType, ResolvedPost
from .url_utils import (
    assert_media_host,
    classify_post_type,
    extract_media_id,
    host_allowed,
    normalize_url,
)


def base_ydl_opts() -> Dict[str, Any]:
    """Shared yt-dlp options for a single-entry, no-download extraction."""
    opts: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "extract_flat": False,
        "socket_timeout": 12,
        "retries": 1,
        "fragment_retries": 1,
        "skip_download": True,
        "ignoreerrors": False,
        "nocheckcertificate": False,
    }
    # Optional egress proxy — TikTok blocks many datacenter IPs outright.
    from .config import ig_config as _cfg

    if _cfg.proxy_url:
        opts["proxy"] = _cfg.proxy_url
    # Cookies ride every strategy: a signed-in session passes YouTube's
    # bot check on the first attempt instead of the last-resort step.
    # yt-dlp only sends cookies matching each request's domain, so this
    # is inert for TikTok/Instagram calls.
    if _cfg.youtube_cookies_file:
        opts["cookiefile"] = _cfg.youtube_cookies_file
    return opts


def classify_ytdlp_error(e: Exception) -> IGError:
    """Map a yt-dlp DownloadError (or IGError passthrough) to a card error."""
    if isinstance(e, IGError):
        return e
    msg = str(e).lower()
    # Precise phrases only — a bare "age" also matches "p-a-g-e".
    if (
        "sign in to confirm" in msg
        or "confirm your age" in msg
        or "age-restricted" in msg
        or "age restricted" in msg
        or "age verification" in msg
    ):
        return IGResolveFailed(
            "This media demands sign in (bot check) — cookies required "
            "to bypass it."
        )
    if "private" in msg or "login" in msg or "signin" in msg:
        return IGPrivateMedia()
    if "404" in msg or "not found" in msg or "gone" in msg or "removed" in msg:
        return IGMediaGone()
    if "429" in msg or "rate" in msg:
        return IGRateLimited()
    if "unsupported url" in msg or "no video" in msg or "not a valid url" in msg:
        return IGInvalidUrl()
    if "playlist" in msg:
        return IGPlaylist()
    ig_log(f"resolve fail: {e}")
    return IGResolveFailed()


def extract_entry(url: str, opts: Dict[str, Any]) -> Dict[str, Any]:
    """One yt-dlp extraction pass (sync — call inside asyncio.to_thread).

    Raises a classified IGError on failure; never returns None.
    """
    import yt_dlp
    from yt_dlp.utils import DownloadError

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except DownloadError as e:
        metrics.bump("resolved_fail", error=str(e)[:200])
        raise classify_ytdlp_error(e) from e
    except Exception as e:  # non-DownloadError (import problems etc.)
        metrics.bump("resolved_fail", error=str(e)[:200])
        raise classify_ytdlp_error(e) from e
    if not info:
        metrics.bump("resolved_fail", error="empty info")
        raise IGMediaGone()
    return info


def _classify_vcodec(vcodec: str | None, acodec: str | None, ext: str) -> MediaKind:
    vc = (vcodec or "").lower()
    ac = (acodec or "").lower()
    if vc in {"none", ""} and ac not in {"none", ""}:
        return MediaKind.AUDIO
    if ext in {"gif"} or vc == "gif":
        return MediaKind.ANIMATION
    if vc not in {"none", ""}:
        return MediaKind.VIDEO
    if ac not in {"none", ""}:
        return MediaKind.AUDIO
    if ext in {"jpg", "jpeg", "png", "webp"}:
        return MediaKind.PHOTO
    if "image" in vc or "mjpeg" in vc:
        return MediaKind.PHOTO
    return MediaKind.DOCUMENT


def _best_format(fmts: List[Dict[str, Any]], prefer_video: bool) -> Optional[Dict[str, Any]]:
    """Pick a single progressive-ish format (video+audio when possible)."""
    if not fmts:
        return None
    scored: List[tuple] = []
    for f in fmts:
        if not f.get("url"):
            continue
        if f.get("protocol") == "mhtml" or f.get("ext") in {"mhtml", "jpg", "png"}:
            if f.get("vcodec") in (None, "none") and not f.get("acodec"):
                continue
        height = f.get("height") or 0
        tbr = f.get("tbr") or 0
        has_v = f.get("vcodec") not in (None, "none", "")
        has_a = f.get("acodec") not in (None, "none", "")
        if prefer_video and has_v and has_a:
            score = 1_000_000 + height * 1000 + tbr
        elif prefer_video and has_v:
            score = 500_000 + height * 1000 + tbr
        elif not prefer_video and has_a and not has_v:
            score = 1_000_000 + tbr
        elif has_v:
            score = 100_000 + height * 1000 + tbr
        else:
            score = tbr
        if (f.get("protocol") or "").startswith("https"):
            score += 50_000
        scored.append((score, f))
    if not scored:
        for f in fmts:
            if f.get("url"):
                return f
        return None
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1]


def _fmt_has_video(f: Dict[str, Any]) -> bool:
    return f.get("vcodec") not in (None, "none", "")


def _fmt_has_audio(f: Dict[str, Any]) -> bool:
    return f.get("acodec") not in (None, "none", "")


def _best_audio_only(fmts: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Best audio-only stream in *fmts* (DASH audio for merge pairing)."""
    cands = [
        f for f in fmts
        if f.get("url") and _fmt_has_audio(f) and not _fmt_has_video(f)
    ]
    if not cands:
        return None
    cands.sort(
        key=lambda f: (
            1 if (f.get("ext") or "") in {"m4a", "mp4", "mp3"} else 0,
            float(f.get("tbr") or 0),
            1 if str(f.get("protocol") or "").startswith("https") else 0,
        ),
        reverse=True,
    )
    return cands[0]


def _is_merge_pair(assets: List[MediaAsset]) -> bool:
    """True when *assets* is exactly one video + one audio split stream."""
    return len(assets) == 2 and {a.merge_role for a in assets} == {"v", "a"}


def _assets_from_entry(
    entry: Dict[str, Any], prefer_video: bool, pair_audio: bool = True
) -> List[MediaAsset]:
    assets: List[MediaAsset] = []

    entries = entry.get("entries")
    if entries:
        for sub in entries:
            if not sub:
                continue
            # Sidecar items are delivered item-by-item; only a standalone
            # post's [video, audio] pair triggers the ffmpeg merge pass.
            assets.extend(_assets_from_entry(sub, prefer_video, pair_audio=False))
        return assets

    url = entry.get("url")
    vcodec = entry.get("vcodec")
    acodec = entry.get("acodec")
    ext = entry.get("ext") or "mp4"
    fmt = None
    audio_fmt = None
    if entry.get("formats"):
        fmt = _best_format(entry["formats"], prefer_video)
        if fmt:
            url = fmt.get("url") or url
            vcodec = fmt.get("vcodec") or vcodec
            acodec = fmt.get("acodec") or acodec
            ext = fmt.get("ext") or ext
            # Modern yt-dlp serves Instagram as DASH: video-only + audio-only
            # formats with no progressive stream. Remember the audio pair so
            # the downloader can merge them — otherwise the sent video is
            # silent (the old IG bug).
            if pair_audio and _fmt_has_video(fmt) and not _fmt_has_audio(fmt):
                audio_fmt = _best_audio_only(entry["formats"])

    if not url:
        req = entry.get("requested_formats") or []
        if req:
            best_v = max(
                (r for r in req if r.get("url") and r.get("vcodec") not in (None, "none")),
                key=lambda r: (r.get("height") or 0),
                default=None,
            )
            if best_v:
                url = best_v.get("url")
                vcodec = best_v.get("vcodec")
                ext = best_v.get("ext") or ext
                if pair_audio:
                    audio_fmt = _best_audio_only(req)

    if not url:
        return assets

    kind = _classify_vcodec(vcodec, acodec, ext)
    filesize = entry.get("filesize") or entry.get("filesize_approx")
    if fmt:
        filesize = fmt.get("filesize") or filesize or fmt.get("filesize_approx")

    if not host_allowed(url):
        try:
            assert_media_host(url)
        except IGInvalidUrl:
            return assets

    duration = entry.get("duration") or (fmt or {}).get("duration")
    assets.append(
        MediaAsset(
            url=url,
            kind=kind,
            width=entry.get("width") or (fmt or {}).get("width"),
            height=entry.get("height") or (fmt or {}).get("height"),
            duration=duration,
            filesize=int(filesize) if filesize else None,
            ext=ext or "mp4",
            codec=vcodec,
            merge_role="v" if audio_fmt else None,
        )
    )

    if audio_fmt:
        a_url = audio_fmt.get("url")
        a_size = audio_fmt.get("filesize") or audio_fmt.get("filesize_approx")
        if a_url and host_allowed(a_url):
            assets.append(
                MediaAsset(
                    url=a_url,
                    kind=MediaKind.AUDIO,
                    duration=audio_fmt.get("duration") or duration,
                    filesize=int(a_size) if a_size else None,
                    ext=audio_fmt.get("ext") or "m4a",
                    codec=audio_fmt.get("acodec"),
                    merge_role="a",
                )
            )
    return assets


class BaseResolver(ABC):
    name = "base"

    @abstractmethod
    async def resolve(self, url: str) -> ResolvedPost:
        ...


class YtDlpResolver(BaseResolver):
    """Primary resolver using yt-dlp's Python API."""

    name = "ytdlp"

    def _ydl_opts(self) -> Dict[str, Any]:
        return {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "extract_flat": False,
            "socket_timeout": 12,
            "retries": 1,
            "fragment_retries": 1,
            "skip_download": True,
            "ignoreerrors": False,
            "nocheckcertificate": False,
        }

    async def resolve(self, url: str) -> ResolvedPost:

        return await asyncio.to_thread(self._resolve_sync, url)

    def _resolve_sync(self, url: str) -> ResolvedPost:
        import yt_dlp
        from yt_dlp.utils import DownloadError

        start = time.perf_counter()
        norm = normalize_url(url)
        post_type = classify_post_type(norm)
        media_id = extract_media_id(norm)

        try:
            with yt_dlp.YoutubeDL(self._ydl_opts()) as ydl:
                info = ydl.extract_info(norm, download=False)
        except DownloadError as e:
            metrics.bump("resolved_fail", error=str(e)[:200])
            raise classify_ytdlp_error(e) from e
        except Exception as e:
            metrics.bump("resolved_fail", error=str(e)[:200])
            ig_log(f"resolve error: {e}")
            raise IGResolveFailed() from e

        if not info:
            raise IGMediaGone()

        from .config import ig_config

        assets = _assets_from_entry(info, ig_config.prefer_video)
        seen = set()
        uniq: List[MediaAsset] = []
        for a in assets:
            if a.url in seen:
                continue
            seen.add(a.url)
            uniq.append(a)
        assets = uniq[: ig_config.max_items]

        if not assets:
            raise IGResolveFailed("No downloadable media in this post.")

        elapsed = int((time.perf_counter() - start) * 1000)
        metrics.bump("resolved_ok", ms=elapsed)
        metrics.bump_platform("instagram")
        ig_log(f"resolved {media_id} type={post_type.value} assets={len(assets)} in {elapsed}ms")

        title = info.get("title") or info.get("description") or ""
        caption = info.get("description") or title
        uploader = info.get("uploader") or info.get("channel") or info.get("creator") or ""
        webpage = info.get("webpage_url") or norm

        return ResolvedPost(
            canonical_url=norm,
            post_type=post_type if post_type != PostType.UNKNOWN else PostType.POST,
            media_id=info.get("id") or media_id,
            title=str(title or "")[:200],
            uploader=str(uploader or "")[:80],
            caption=str(caption or "")[:800],
            webpage_url=str(webpage),
            assets=assets,
            is_sidecar=bool(info.get("entries")),
            thumbnail=info.get("thumbnail"),
            resolve_ms=elapsed,
            resolver=self.name,
            platform="instagram",
            needs_merge=_is_merge_pair(assets),
        )


class FallbackResolver(BaseResolver):
    """Secondary resolver — re-runs with noplaylist=False for multi-entry posts."""

    name = "fallback"

    def __init__(self, primary: BaseResolver) -> None:
        self._primary = primary

    async def resolve(self, url: str) -> ResolvedPost:

        def _alt() -> ResolvedPost:
            import yt_dlp
            from yt_dlp.utils import DownloadError

            from .config import ig_config
            from .url_utils import classify_post_type, extract_media_id, normalize_url

            start = time.perf_counter()
            norm = normalize_url(url)
            opts = {
                "quiet": True,
                "no_warnings": True,
                "noplaylist": False,
                "extract_flat": False,
                "socket_timeout": 15,
                "retries": 1,
                "skip_download": True,
            }
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(norm, download=False)
            except DownloadError as e:
                raise IGResolveFailed() from e
            if not info:
                raise IGMediaGone()
            assets = _assets_from_entry(info, ig_config.prefer_video)
            assets = assets[: ig_config.max_items]
            if not assets:
                raise IGResolveFailed()
            elapsed = int((time.perf_counter() - start) * 1000)
            metrics.bump("resolved_ok", ms=elapsed)
            metrics.bump_platform("instagram")
            return ResolvedPost(
                canonical_url=norm,
                post_type=classify_post_type(norm),
                media_id=info.get("id") or extract_media_id(norm),
                title=str(info.get("title") or "")[:200],
                uploader=str(info.get("uploader") or "")[:80],
                caption=str(info.get("description") or "")[:800],
                webpage_url=str(info.get("webpage_url") or norm),
                assets=assets,
                is_sidecar=bool(info.get("entries")),
                thumbnail=info.get("thumbnail"),
                resolve_ms=elapsed,
                resolver=self.name,
                platform="instagram",
                needs_merge=_is_merge_pair(assets),
            )

        return await asyncio.to_thread(_alt)


class ResolverChain:
    def __init__(self, resolvers: Optional[List[BaseResolver]] = None) -> None:
        primary = YtDlpResolver()
        self.resolvers = resolvers or [primary, FallbackResolver(primary)]

    async def resolve(self, url: str) -> ResolvedPost:
        last: Optional[Exception] = None
        for i, r in enumerate(self.resolvers):
            try:
                return await r.resolve(url)
            except (IGPrivateMedia, IGMediaGone, IGInvalidUrl, IGRateLimited, IGPlaylist):
                raise
            except Exception as e:
                last = e
                ig_log(f"resolver[{r.name}] failed ({i}): {e}")
                continue
        if isinstance(last, IGResolveFailed):
            raise last
        if isinstance(last, Exception):
            raise IGResolveFailed(str(last)[:120]) from last
        raise IGResolveFailed()


resolver_chain = ResolverChain()


async def resolve_media(platform: str, url: str) -> ResolvedPost:
    """Route *url* to its platform resolver (called after platforms.resolve_target)."""
    if platform == "youtube":
        from .youtube import YouTubeResolver

        return await YouTubeResolver().resolve(url)
    if platform == "tiktok":
        from .tiktok import TikTokResolver

        return await TikTokResolver().resolve(url)
    return await resolver_chain.resolve(url)


def ensure_yt_dlp() -> None:
    """Raise a clear error if yt-dlp is not installed."""
    try:
        __import__("yt_dlp")
    except ImportError as e:
        raise IGResolveFailed(
            "yt-dlp is not installed. Run: pip install yt-dlp"
        ) from e
