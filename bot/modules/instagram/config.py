"""Instagram downloader configuration — env + runtime limits."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _int(key: str, default: int) -> int:
    raw = os.getenv(key, "")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _bool(key: str, default: bool) -> bool:
    raw = (os.getenv(key) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


# Handler groups — leave 0–13 free for existing modules; IG auto-detect uses 14.
HANDLER_GROUP = 14
COMMAND_GROUP = 0


def _resolve_temp_dir() -> Path:
    """
    Temp workspace for media downloads.

    NEVER default to Path("") / Path(".") — that resolves to the process CWD
    and has previously caused cleanup code to wipe the project tree.
    """
    raw = (os.getenv("IG_TEMP_DIR") or "").strip()
    if raw:
        p = Path(raw).expanduser()
        # Refuse obviously dangerous roots.
        if p.resolve() in {Path.cwd().resolve(), Path.home().resolve(), Path("/").resolve()}:
            pass  # fall through to safe default
        else:
            return p
    base = os.getenv("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "PiBot" / "ig_temp"


TEMP_DIR = _resolve_temp_dir()

# URL allowlist (SSRF: only these hosts are ever requested).
ALLOWED_HOSTS = frozenset(
    {
        "instagram.com",
        "www.instagram.com",
        "m.instagram.com",
        "instagr.am",
        "www.instagr.am",
    }
)

# CDN hosts yt-dlp / our fetcher may touch after resolve (belt & suspenders).
ALLOWED_MEDIA_HOST_SUFFIXES = (
    ".cdninstagram.com",
    ".fbcdn.net",
    ".facebook.com",
    ".akamaihd.net",
    ".ttwicdn.net",
)


@dataclass(frozen=True)
class IGConfig:
    """Typed env-backed settings for the Instagram module."""

    enabled: bool = _bool("IG_ENABLED", True)
    auto_download: bool = _bool("IG_AUTO_DOWNLOAD", True)
    # Max carousel / album items to forward per post.
    max_items: int = max(1, min(_int("IG_MAX_ITEMS", 10), 50))
    # Concurrency: resolve + download jobs.
    max_concurrent: int = max(1, min(_int("IG_MAX_CONCURRENT", 3), 10))
    # Per-job wall-clock timeout (seconds).
    job_timeout: int = max(10, min(_int("IG_JOB_TIMEOUT", 90), 300))
    # Prefer video over photo when both exist (reels/posts).
    prefer_video: bool = _bool("IG_PREFER_VIDEO", True)
    # Optional local Bot API server for files > 50 MiB (or always, if set).
    local_bot_api_url: str = (os.getenv("LOCAL_BOT_API_URL") or "").rstrip("/")
    use_local_bot_api: bool = _bool("USE_LOCAL_BOT_API", True)
    # Cache TTL for file_id rows (seconds); 0 = keep forever until clear.
    cache_ttl_hours: int = max(0, _int("IG_CACHE_TTL_HOURS", 168))
    # Telegram Bot API hard caps (standard server).
    max_file_bytes: int = _int("IG_MAX_FILE_BYTES", 50 * 1024 * 1024)


ig_config = IGConfig()

# Ensure temp dir exists early so handlers never race on mkdir.
try:
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    pass
