"""Download resolved assets — HTTP streaming first, yt-dlp only as fallback."""

from __future__ import annotations

import asyncio
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
            timeout=httpx.Timeout(30.0, connect=8.0),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=10),
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


async def _http_download(url: str, dest: Path, asset: MediaAsset) -> List[Path]:
    """Stream a direct CDN URL to disk — no yt-dlp extraction pass."""
    if not host_allowed(url):
        raise IGDownloadFailed()

    client = _get_http_client()
    max_b = ig_config.max_file_bytes

    try:
        async with client.stream("GET", url) as resp:
            if resp.status_code >= 400:
                raise IGDownloadFailed()
            ext = _ext_from(url, resp.headers.get("content-type", ""))
            path = dest / f"media.{ext}"
            total = 0
            with path.open("wb") as fh:
                async for chunk in resp.aiter_bytes(64 * 1024):
                    total += len(chunk)
                    if total > max_b:
                        fh.close()
                        path.unlink(missing_ok=True)
                        raise IGTooLarge(total)
                    fh.write(chunk)
            if total == 0:
                path.unlink(missing_ok=True)
                raise IGDownloadFailed()
            return [path]
    except (IGTooLarge, IGDownloadFailed):
        raise
    except Exception as e:
        ig_log(f"http download fail: {e}")
        raise IGDownloadFailed() from e


def _ydl_download_opts(dest: Path) -> dict:
    return {
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
        "max_filesize": ig_config.max_file_bytes,
        "noprogress": True,
        "ignoreerrors": False,
    }


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


async def _fetch_one(dest: Path, idx: int, asset: MediaAsset, post: ResolvedPost) -> List[DownloadedFile]:
    """Fetch one asset: direct HTTP first, yt-dlp only if that fails."""
    size = asset.filesize
    if size and size > ig_config.max_file_bytes:
        ig_log(f"skip oversized asset[{idx}] size={size}")
        return []

    sub = dest / f"item_{idx}"
    sub.mkdir(parents=True, exist_ok=True)

    paths: Optional[List[Path]] = None
    try:
        paths = await _http_download(asset.url, sub, asset)
    except IGTooLarge:
        return []
    except IGDownloadFailed:
        paths = None  # fall through to yt-dlp

    if paths is None:
        try:
            paths = await asyncio.to_thread(_download_sync, asset.url, sub)
        except IGTooLarge:
            return []
        except IGDownloadFailed:
            return []

    out: List[DownloadedFile] = []
    for p in paths:
        try:
            sz = p.stat().st_size
        except OSError:
            continue
        if sz > ig_config.max_file_bytes:
            p.unlink(missing_ok=True)
            continue
        kind = _kind_from_path(p, asset.kind)
        out.append(
            DownloadedFile(
                path=p,
                kind=kind,
                asset=asset,
                size=sz,
                cache_key=f"{post.media_id}:{idx}",
            )
        )
        metrics.bump("downloads")
        metrics.bump("bytes_sent", n=sz)
    return out


async def download_post(post: ResolvedPost, job_dir: Optional[Path] = None) -> List[DownloadedFile]:
    """Download all assets for *post* in parallel (HTTP-first per asset)."""
    ensure_temp_root()
    dest = job_dir or job_workdir(post.media_id)
    dest.mkdir(parents=True, exist_ok=True)

    if not post.assets:
        raise IGDownloadFailed()

    sem = asyncio.Semaphore(max(1, ig_config.max_concurrent))

    async def _guarded(idx: int, asset: MediaAsset) -> List[DownloadedFile]:
        async with sem:
            return await _fetch_one(dest, idx, asset, post)

    batches = await asyncio.gather(
        *(_guarded(i, a) for i, a in enumerate(post.assets)),
        return_exceptions=True,
    )

    results: List[DownloadedFile] = []
    errors = 0
    for batch in batches:
        if isinstance(batch, BaseException):
            ig_log(f"asset fetch error: {batch}")
            errors += 1
            continue
        if not batch:
            errors += 1
            continue
        results.extend(batch)

    if not results:
        raise IGDownloadFailed()

    ig_log(f"downloaded {len(results)} file(s) for {post.media_id} ({errors} skipped)")
    return results


def job_workdir(media_id: str) -> Path:
    safe_id = re.sub(r"[^A-Za-z0-9_\-]", "_", media_id)[:64] or "job"
    d = ensure_temp_root() / f"{safe_id}_{int(time.time() * 1000)}"
    d.mkdir(parents=True, exist_ok=True)
    return d
