"""Download resolved assets — HTTP streaming first, yt-dlp only as fallback.

Also owns the split-stream merge step (YouTube video-only + audio-only
pairs are merged with ffmpeg stream-copy before upload).
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlsplit

from .config import TEMP_DIR, ig_config
from .exceptions import IGDownloadFailed, IGTooLarge
from .metrics import ig_log, metrics
from .models import MediaAsset, MediaKind, ResolvedPost
from .progress import JobProgress
from .url_utils import host_allowed

# Only these directory-name patterns are ever eligible for sweep cleanup.
_JOB_DIR_RE = re.compile(r"^[A-Za-z0-9_\-]{1,80}_\d{10,16}$")

# Shared httpx client (one TLS handshake per worker, reused across jobs).
_HTTP_CLIENT = None
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

# Parallel ranged downloads — several connections pull different byte
# ranges of one file at once. Only worth it for larger files whose size
# is known up front; small/unknown-size assets keep the simple stream.
_PARALLEL_MIN = 4 * 1024 * 1024
# 1 MiB stream chunks — fewer syscall round-trips than 256 KiB on long
# CDN streams (and the per-range unit for parallel downloads).
_CHUNK = 1024 * 1024


class _RangeUnsupported(Exception):
    """Server refused our Range request — caller falls back to one stream."""

# Per-platform Referer/Origin — some CDNs reject requests carrying the
# wrong site's referrer (Instagram's default would break YouTube CDNs).
_PLATFORM_HEADERS = {
    "youtube": {
        "Referer": "https://www.youtube.com/",
        "Origin": "https://www.youtube.com",
    },
    "tiktok": {"Referer": "https://www.tiktok.com/"},
    "instagram": {"Referer": "https://www.instagram.com/"},
}

_CT_EXT = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
    "video/mp4": "mp4",
    "video/webm": "webm",
    "video/quicktime": "mov",
    "audio/mpeg": "mp3",
    "audio/mp4": "m4a",
}


@dataclass
class DownloadedFile:
    path: Path
    kind: MediaKind
    asset: MediaAsset
    size: int
    cache_key: str  # media_id:index


def ensure_temp_root() -> Path:
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    return TEMP_DIR


def _is_safe_job_dir(path: Path) -> bool:
    """Guard against wiping anything that is not one of our job directories."""
    try:
        root = TEMP_DIR.resolve()
        resolved = path.resolve()
    except OSError:
        return False
    # Must be strictly inside TEMP_DIR (not TEMP_DIR itself, not CWD, not home).
    if resolved == root or root not in resolved.parents:
        return False
    if resolved in {Path.cwd().resolve(), Path.home().resolve()}:
        return False
    return bool(_JOB_DIR_RE.match(resolved.name))


def cleanup_dir(path: Path) -> None:
    if not _is_safe_job_dir(path):
        ig_log(f"cleanup refused for unsafe path: {path}")
        return
    shutil.rmtree(path, ignore_errors=True)


def sweep_stale(max_age_sec: int = 3600) -> int:
    """Remove leftover job dirs older than *max_age_sec* (startup only).

    Hard guards: never runs unless TEMP_DIR is an absolute path distinct from
    CWD/home, and only deletes directories matching our job-name pattern.
    """
    try:
        root = TEMP_DIR.resolve()
    except OSError:
        return 0
    if not root.is_absolute():
        return 0
    if root in {Path.cwd().resolve(), Path.home().resolve(), Path("/").resolve()}:
        ig_log(f"sweep skipped — TEMP_DIR unsafe: {root}")
        return 0
    if not root.is_dir():
        return 0
    # Only sweep the dedicated ig_temp folder name.
    if root.name not in {"ig_temp", "PiBot"} and "ig_temp" not in root.parts:
        ig_log(f"sweep skipped — unexpected TEMP_DIR name: {root}")
        return 0

    removed = 0
    now = time.time()
    try:
        for child in root.iterdir():
            if not child.is_dir():
                continue
            if not _is_safe_job_dir(child):
                continue
            try:
                if now - child.stat().st_mtime > max_age_sec:
                    shutil.rmtree(child, ignore_errors=True)
                    removed += 1
            except OSError:
                continue
    except OSError:
        return removed
    if removed:
        ig_log(f"swept {removed} stale temp dir(s)")
    return removed


def _get_http_client():
    """Lazily create a shared AsyncClient so CDN connections stay warm."""
    global _HTTP_CLIENT
    if _HTTP_CLIENT is None or getattr(_HTTP_CLIENT, "is_closed", False):
        import httpx

        _HTTP_CLIENT = httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(
                ig_config.read_timeout, connect=ig_config.connect_timeout
            ),
            # Room for parallel ranged workers + concurrent jobs/uploads.
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
            proxy=ig_config.proxy_url or None,
            headers={
                "User-Agent": _UA,
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://www.instagram.com/",
            },
        )
    return _HTTP_CLIENT


def _ext_from(url: str, content_type: str) -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in _CT_EXT:
        return _CT_EXT[ct]
    path = urlsplit(url).path
    suffix = path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else ""
    if suffix in _CT_EXT.values() or suffix in {"jpeg", "jpg", "png", "webp", "gif", "mp4", "m4a", "mp3"}:
        return "jpg" if suffix == "jpeg" else suffix
    return "mp4"


async def _http_download(
    url: str,
    dest: Path,
    asset: MediaAsset,
    *,
    platform: str = "instagram",
    max_bytes: Optional[int] = None,
    progress: Optional[JobProgress] = None,
) -> List[Path]:
    """Stream a direct CDN URL to disk — no yt-dlp extraction pass.

    Large files with a known size are fetched with several parallel
    Range connections first (single stream as fallback).  Retries
    transient failures (connect errors, 5xx, 403/429) with short
    backoff; writes to a ``.part`` file and renames on success so a
    crash never leaves a half file that looks complete.
    """
    if not host_allowed(url):
        raise IGDownloadFailed()

    client = _get_http_client()
    max_b = max_bytes or ig_config.max_file_bytes
    hdrs = dict(_PLATFORM_HEADERS.get(platform) or _PLATFORM_HEADERS["instagram"])
    hdrs.update(asset.headers or {})

    size = asset.filesize or 0
    use_parallel = (
        ig_config.parallel_download
        and size >= _PARALLEL_MIN
        and size <= max_b
        and ".m3u8" not in url
    )

    attempts = 1 + max(0, ig_config.max_network_retries)
    last: Exception = IGDownloadFailed()
    for attempt in range(attempts):
        if attempt:
            await asyncio.sleep(min(0.5 * (2 ** (attempt - 1)), 5.0))
        part: Optional[Path] = None
        final: Optional[Path] = None
        try:
            if use_parallel:
                try:
                    return await _parallel_download(
                        client, url, dest, hdrs, size, max_b, progress
                    )
                except _RangeUnsupported:
                    # Server ignored Range — same attempt falls through
                    # to the plain single-stream GET below.
                    ig_log("range download unsupported — single stream")
            async with client.stream("GET", url, headers=hdrs) as resp:
                if resp.status_code in (403, 410):
                    # Expired CDN signature — caller re-resolves once.
                    raise IGDownloadFailed()
                if resp.status_code == 429 or resp.status_code >= 500:
                    last = IGDownloadFailed()
                    continue
                if resp.status_code >= 400:
                    raise IGDownloadFailed()
                ext = _ext_from(url, resp.headers.get("content-type", ""))
                final = dest / f"media.{ext}"
                part = dest / f"media.{ext}.part"
                total = 0
                with part.open("wb") as fh:
                    # 1 MiB chunks — fewer syscall round-trips on long
                    # CDN streams; progress bytes land in the status msg.
                    async for chunk in resp.aiter_bytes(_CHUNK):
                        total += len(chunk)
                        if total > max_b:
                            fh.close()
                            part.unlink(missing_ok=True)
                            raise IGTooLarge(total)
                        fh.write(chunk)
                        if progress is not None:
                            progress.add(len(chunk))
                if total == 0:
                    part.unlink(missing_ok=True)
                    raise IGDownloadFailed()
                os.replace(part, final)
                return [final]
        except (IGTooLarge,):
            raise
        except IGDownloadFailed as e:
            if part:
                part.unlink(missing_ok=True)
            # 403/410/4xx are definitive for this URL — don't retry blindly.
            last = e
            if attempt + 1 >= attempts:
                raise
            continue
        except Exception as e:
            if part:
                part.unlink(missing_ok=True)
            last = e
            ig_log(f"http download fail (attempt {attempt + 1}): {e}")
            continue
    ig_log(f"http download exhausted retries: {last}")
    if isinstance(last, IGDownloadFailed):
        raise last
    raise IGDownloadFailed() from last


async def _parallel_download(
    client,
    url: str,
    dest: Path,
    hdrs: dict,
    size: int,
    max_b: int,
    progress: Optional[JobProgress] = None,
) -> List[Path]:
    """Fetch *url* with several concurrent Range connections.

    The ``.part`` file is pre-allocated to *size* and each worker writes
    its own byte range (seek + write happen with no await in between, so
    one shared handle is safe on the event loop).  Raises
    ``_RangeUnsupported`` when the server ignores Range requests (the
    caller retries with a plain GET) and ``IGDownloadFailed`` otherwise.
    """
    if size > max_b:
        raise IGTooLarge(size)
    ext = _ext_from(url, "")
    final = dest / f"media.{ext}"
    part = dest / f"media.{ext}.part"

    n = max(2, min(ig_config.parallel_connections, 8, max(2, size // _CHUNK)))
    span = -(-size // n)  # ceil — equal-or-smaller ranges
    ranges = []
    start = 0
    while start < size:
        ranges.append((start, min(start + span - 1, size - 1)))
        start += span

    try:
        with part.open("wb") as fh:
            fh.truncate(size)

            async def _one(start: int, end: int) -> None:
                h = dict(hdrs)
                h["Range"] = f"bytes={start}-{end}"
                need = end - start + 1
                got = 0
                async with client.stream("GET", url, headers=h) as resp:
                    if resp.status_code in (200, 416):
                        # 200 = Range ignored; 416 = server refuses our
                        # exact range — both mean "do the simple thing".
                        raise _RangeUnsupported()
                    if resp.status_code in (403, 410):
                        raise IGDownloadFailed()
                    if resp.status_code == 429 or resp.status_code >= 400:
                        raise IGDownloadFailed()
                    cr = (resp.headers.get("content-range") or "").strip()
                    if cr and "/" in cr:
                        try:
                            total = int(cr.rsplit("/", 1)[1])
                        except ValueError:
                            total = 0
                        if total and total != size:
                            raise _RangeUnsupported()
                    async for chunk in resp.aiter_bytes(_CHUNK):
                        if not chunk:
                            continue
                        if got + len(chunk) > need:
                            raise IGDownloadFailed()
                        fh.seek(start + got)  # sync seek+write, no await
                        fh.write(chunk)
                        got += len(chunk)
                        if progress is not None:
                            progress.add(len(chunk))
                if got != need:
                    raise IGDownloadFailed()

            results = await asyncio.gather(
                *(_one(s, e) for s, e in ranges), return_exceptions=True
            )

        # Priority: Range problems first (sequential GET is the fix),
        # then any real worker failure, then assemble.
        for r in results:
            if isinstance(r, _RangeUnsupported):
                raise r
        for r in results:
            if isinstance(r, BaseException):
                if isinstance(r, (IGTooLarge, IGDownloadFailed)):
                    raise r
                raise IGDownloadFailed() from r
        os.replace(part, final)
        return [final]
    finally:
        # No-op after a successful replace; cleans partial/aborted files.
        part.unlink(missing_ok=True)


def _ydl_download_opts(dest: Path) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        # Fixed name per item dir — %(id)s from CDN formats can include
        # '?' and query junk, which Windows rejects (Errno 22).
        "outtmpl": str(dest / "media.%(ext)s"),
        # Sanitize any remaining illegal characters for Windows.
        "windowsfilenames": True,
        "format": "bestvideo*+bestaudio/best/best",
        "merge_output_format": "mp4",
        "socket_timeout": 20,
        "retries": 1,
        "fragment_retries": 2,
        # Pull DASH/HLS fragments from several connections at once —
        # the same speed lever as the parallel ranged HTTP path above.
        "concurrent_fragment_downloads": 4,
        "max_filesize": ig_config.max_file_bytes,
        "noprogress": True,
        "ignoreerrors": False,
    }
    if ig_config.proxy_url:
        opts["proxy"] = ig_config.proxy_url
    return opts


def _sanitize_filename(name: str) -> str:
    """Strip Windows-illegal path characters (belt & suspenders)."""
    illegal = '<>:"/\\|?*'
    out = "".join("_" if c in illegal or ord(c) < 32 else c for c in name)
    out = out.rstrip(". ")
    return out or "media"


def _safe_file(path: Path) -> Path:
    """Rename *path* if its name is not a legal Windows filename."""
    fixed = _sanitize_filename(path.name)
    if fixed == path.name:
        return path
    target = path.with_name(fixed)
    try:
        if target.exists():
            stem, dot, ext = fixed.partition(".")
            target = path.with_name(f"{stem}_{int(time.time())}{dot}{ext}")
        path.rename(target)
        return target
    except OSError:
        return path


def _download_sync(url: str, dest: Path) -> List[Path]:
    import yt_dlp
    from yt_dlp.utils import DownloadError

    opts = _ydl_download_opts(dest)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info(url, download=True)
    except DownloadError as e:
        msg = str(e).lower()
        if "filesize" in msg or "too large" in msg or "max_filesize" in msg:
            raise IGTooLarge() from e
        ig_log(f"ydl download fail: {e}")
        raise IGDownloadFailed() from e
    except Exception as e:
        ig_log(f"download error: {e}")
        raise IGDownloadFailed() from e

    files = []
    for p in sorted(dest.iterdir()):
        if not p.is_file():
            continue
        if p.suffix.lower() in {".part", ".ytdl", ".json"}:
            continue
        # Drop failed .part leftovers (illegal-name downloads).
        if ".part" in p.name.lower():
            try:
                p.unlink()
            except OSError:
                pass
            continue
        files.append(_safe_file(p))
    if not files:
        raise IGDownloadFailed()
    return files


def _kind_from_path(path: Path, fallback: MediaKind) -> MediaKind:
    ext = path.suffix.lower().lstrip(".")
    if ext in {"jpg", "jpeg", "png", "webp", "heic"}:
        return MediaKind.PHOTO
    if ext in {"mp4", "webm", "mov", "mkv"}:
        return MediaKind.VIDEO
    if ext in {"mp3", "m4a", "ogg", "opus", "wav"}:
        return MediaKind.AUDIO
    if ext == "gif":
        return MediaKind.ANIMATION
    return fallback


async def _fetch_one(
    dest: Path,
    idx: int,
    asset: MediaAsset,
    post: ResolvedPost,
    max_bytes: Optional[int] = None,
    progress: Optional[JobProgress] = None,
) -> List[DownloadedFile]:
    """Fetch one asset: direct HTTP first, yt-dlp only if that fails."""
    cap = max_bytes or ig_config.max_file_bytes
    size = asset.filesize
    if size and size > cap:
        ig_log(f"oversized asset[{idx}] size={size} > cap={cap}")
        raise IGTooLarge(size)

    sub = dest / f"item_{idx}"
    sub.mkdir(parents=True, exist_ok=True)

    paths: Optional[List[Path]] = None
    try:
        paths = await _http_download(
            asset.url, sub, asset,
            platform=post.platform, max_bytes=cap, progress=progress,
        )
    except IGTooLarge:
        raise
    except IGDownloadFailed:
        paths = None  # fall through to yt-dlp

    if paths is None:
        try:
            paths = await asyncio.to_thread(_download_sync, asset.url, sub)
        except IGTooLarge:
            raise
        except IGDownloadFailed:
            return []

    out: List[DownloadedFile] = []
    for p in paths:
        try:
            sz = p.stat().st_size
        except OSError:
            continue
        if sz > cap:
            p.unlink(missing_ok=True)
            continue
        kind = _kind_from_path(p, asset.kind)
        out.append(
            DownloadedFile(
                path=p,
                kind=kind,
                asset=asset,
                size=sz,
                cache_key=f"{post.platform}:{post.media_id}:{idx}",
            )
        )
        metrics.bump("downloads")
        metrics.bump("bytes_sent", n=sz)
    return out


async def download_post(
    post: ResolvedPost,
    job_dir: Optional[Path] = None,
    *,
    max_bytes: Optional[int] = None,
    progress: Optional[JobProgress] = None,
) -> List[DownloadedFile]:
    """Download all assets for *post* in parallel (HTTP-first per asset).

    Split YouTube streams (post.needs_merge) are merged with ffmpeg here;
    on merge failure both parts are returned so the upload still happens.
    *max_bytes* overrides the global size cap (per-chat File Size setting).
    *progress* receives byte/stage updates for the live status message.
    """
    ensure_temp_root()
    dest = job_dir or job_workdir(post.media_id)
    dest.mkdir(parents=True, exist_ok=True)

    if not post.assets:
        raise IGDownloadFailed()

    if progress is not None:
        progress.stage("download")
        progress.expect(
            sum(a.filesize or 0 for a in post.assets) or None
        )

    sem = asyncio.Semaphore(max(1, ig_config.max_concurrent))

    async def _guarded(idx: int, asset: MediaAsset) -> List[DownloadedFile]:
        async with sem:
            return await _fetch_one(dest, idx, asset, post, max_bytes, progress)

    t0 = time.perf_counter()
    batches = await asyncio.gather(
        *(_guarded(i, a) for i, a in enumerate(post.assets)),
        return_exceptions=True,
    )

    results: List[DownloadedFile] = []
    errors = 0
    too_large = 0
    biggest = 0
    for batch in batches:
        if isinstance(batch, BaseException):
            ig_log(f"asset fetch error: {batch}")
            errors += 1
            if isinstance(batch, IGTooLarge):
                too_large += 1
                biggest = max(biggest, getattr(batch, "size", 0) or 0)
            continue
        if not batch:
            errors += 1
            continue
        results.extend(batch)

    if not results:
        # Every asset blew the cap → say so precisely, not generically.
        if too_large:
            raise IGTooLarge(biggest or None)
        raise IGDownloadFailed()

    # A half-finished merge pair would upload a lone audio track — fail
    # with the real reason instead of sending something nonsensical.
    if post.needs_merge and len(post.assets) == 2 and len(results) < 2:
        if too_large:
            raise IGTooLarge(biggest or None)
        raise IGDownloadFailed()

    # Merge pass for split streams (video part + audio part → one MP4).
    if post.needs_merge and len(post.assets) == 2 and len(results) >= 2:
        if progress is not None:
            progress.stage("merge")
        merged = await _try_merge(post, results, dest, max_bytes)
        if merged:
            results = merged

    metrics.bump("dl_ms", dl_ms=int((time.perf_counter() - t0) * 1000))
    ig_log(f"downloaded {len(results)} file(s) for {post.media_id} ({errors} skipped)")
    return results


async def _try_merge(
    post: ResolvedPost,
    files: List[DownloadedFile],
    dest: Path,
    max_bytes: Optional[int] = None,
) -> Optional[List[DownloadedFile]]:
    """ffmpeg-merge v/a parts; None → caller keeps the separate parts."""
    from .merge import merge_streams

    cap = max_bytes or ig_config.max_file_bytes
    video = next(
        (f for f in files if f.asset.merge_role == "v"), None
    )
    audio = next(
        (f for f in files if f.asset.merge_role == "a"), None
    )
    if not video or not audio:
        return None
    out = await merge_streams(video.path, audio.path, dest)
    if not out:
        return None
    try:
        size = out.stat().st_size
    except OSError:
        return None
    if size > cap:
        out.unlink(missing_ok=True)
        return None  # fall back to parts (downloader cap rejects later)
    # Remove the source parts so only the merged file uploads.
    for f in (video, audio):
        try:
            f.path.unlink(missing_ok=True)
        except OSError:
            pass
    merged_file = DownloadedFile(
        path=out,
        kind=MediaKind.VIDEO,
        asset=video.asset,
        size=size,
        cache_key=f"{post.platform}:{post.media_id}:merged",
    )
    metrics.bump("downloads")
    metrics.bump("bytes_sent", n=size)
    ig_log(f"merged streams → {out.name} ({size} bytes)")
    return [merged_file]


def job_workdir(media_id: str) -> Path:
    safe_id = re.sub(r"[^A-Za-z0-9_\-]", "_", media_id)[:64] or "job"
    d = ensure_temp_root() / f"{safe_id}_{int(time.time() * 1000)}"
    d.mkdir(parents=True, exist_ok=True)
    return d
