"""Media resolution — yt-dlp Python API (no subprocess) + optional fallback."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from .exceptions import (
    IGInvalidUrl,
    IGMediaGone,
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


def _assets_from_entry(entry: Dict[str, Any], prefer_video: bool) -> List[MediaAsset]:
    assets: List[MediaAsset] = []

    entries = entry.get("entries")
    if entries:
        for sub in entries:
            if not sub:
                continue
            assets.extend(_assets_from_entry(sub, prefer_video))
        return assets

    url = entry.get("url")
    vcodec = entry.get("vcodec")
    acodec = entry.get("acodec")
    ext = entry.get("ext") or "mp4"
    fmt = None
    if entry.get("formats"):
        fmt = _best_format(entry["formats"], prefer_video)
        if fmt:
            url = fmt.get("url") or url
            vcodec = fmt.get("vcodec") or vcodec
            acodec = fmt.get("acodec") or acodec
            ext = fmt.get("ext") or ext

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

    assets.append(
        MediaAsset(
            url=url,
            kind=kind,
            width=entry.get("width") or (fmt or {}).get("width"),
            height=entry.get("height") or (fmt or {}).get("height"),
            duration=entry.get("duration") or (fmt or {}).get("duration"),
            filesize=int(filesize) if filesize else None,
            ext=ext or "mp4",
            codec=vcodec,
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
        import asyncio

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
            msg = str(e).lower()
            metrics.bump("resolved_fail", error=str(e)[:200])
            if "private" in msg or "login" in msg or "signin" in msg:
                raise IGPrivateMedia() from e
            if "404" in msg or "not found" in msg or "gone" in msg:
                raise IGMediaGone() from e
            if "429" in msg or "rate" in msg:
                raise IGRateLimited() from e
            if "unsupported url" in msg or "no video" in msg:
                raise IGInvalidUrl() from e
            ig_log(f"resolve fail: {e}")
            raise IGResolveFailed() from e
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
        )


class FallbackResolver(BaseResolver):
    """Secondary resolver — re-runs with noplaylist=False for multi-entry posts."""

    name = "fallback"

    def __init__(self, primary: BaseResolver) -> None:
        self._primary = primary

    async def resolve(self, url: str) -> ResolvedPost:
        import asyncio

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
            except (IGPrivateMedia, IGMediaGone, IGInvalidUrl, IGRateLimited):
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


def ensure_yt_dlp() -> None:
    """Raise a clear error if yt-dlp is not installed."""
    try:
        import yt_dlp  # noqa: F401
    except ImportError as e:
        raise IGResolveFailed(
            "yt-dlp is not installed. Run: pip install yt-dlp"
        ) from e
