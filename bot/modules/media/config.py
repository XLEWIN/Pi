"""Media downloader configuration — env + runtime limits.

Originally the Instagram-only module; now serves YouTube, TikTok and
Instagram.  Legacy ``IG_*`` env names keep working; new knobs use
``MEDIA_*`` (with ``IG_*`` fallbacks where a legacy name exists).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple


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


def _str(key: str, default: str = "") -> str:
    return (os.getenv(key) or "").strip() or default


# Handler groups — leave 0–13 free for existing modules; media auto-detect uses 14.
HANDLER_GROUP = 14
COMMAND_GROUP = 0


def _resolve_temp_dir() -> Path:
    """
    Temp workspace for media downloads.

    NEVER default to Path("") / Path(".") — that resolves to the process CWD
    and has previously caused cleanup code to wipe the project tree.
    """
    raw = (os.getenv("IG_TEMP_DIR") or os.getenv("MEDIA_TEMP_DIR") or "").strip()
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
        # Instagram
        "instagram.com",
        "www.instagram.com",
        "m.instagram.com",
        "instagr.am",
        "www.instagr.am",
        # YouTube
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
        # TikTok
        "tiktok.com",
        "www.tiktok.com",
        "m.tiktok.com",
        "vm.tiktok.com",
        "vt.tiktok.com",
    }
)

# CDN hosts yt-dlp / our fetcher may touch after resolve (belt & suspenders).
ALLOWED_MEDIA_HOST_SUFFIXES = (
    ".cdninstagram.com",
    ".fbcdn.net",
    ".facebook.com",
    ".akamaihd.net",
    ".ttwicdn.com",
    ".tiktokcdn.com",
    ".tiktokv.com",
    ".tiktokcdn-us.com",
    ".byteoversea.com",
    ".ibytedtos.com",
    ".googlevideo.com",
    ".gvt1.com",
    ".ytimg.com",
    ".youtube.com",
)

# ── Direct stream mirrors (Piped / Invidious) ──────────────────────
# Public mirrors that hand back the same googlevideo CDN stream URLs —
# used when YouTube's player API demands a sign-in (bot check).  No API
# key, cookies or PO token required: pure HTTPS GETs against hosts we
# configure ourselves (SSRF-safe — only this list is ever contacted).
# Override with MEDIA_STREAM_INSTANCES (comma-separated hosts);
# MEDIA_STREAM_FALLBACK=0 disables the whole path.
_STREAM_DEFAULT = (
    "pipedapi.adminforge.de",
    "pipedapi.ducks.party",
    "api.piped.private.coffee",
    "inv.nadeko.net",
    "yewtu.be",
    "invidious.nerdvpn.de",
)


def _stream_instances() -> Tuple[str, ...]:
    raw = _str("MEDIA_STREAM_INSTANCES")
    if not raw:
        return _STREAM_DEFAULT
    out = []
    for h in raw.split(","):
        h = h.strip().lower()
        h = h.removeprefix("https://").removeprefix("http://")
        h = h.split("/")[0].strip(".")
        if h and "." in h and " " not in h:
            out.append(h)
    return tuple(out) or _STREAM_DEFAULT


STREAM_INSTANCES = _stream_instances()
# Exact hosts + registrable domains (last two labels): proxied media
# often lives on a sibling subdomain such as pipedproxy.<domain>.
STREAM_MEDIA_HOSTS = frozenset(STREAM_INSTANCES)
STREAM_MEDIA_DOMAINS = frozenset(
    ".".join(h.split(".")[-2:]) for h in STREAM_INSTANCES if h.count(".") >= 1
)


@dataclass(frozen=True)
class IGConfig:
    """Typed env-backed settings for the media downloader module."""

    enabled: bool = _bool("MEDIA_ENABLED", _bool("IG_ENABLED", True))
    auto_download: bool = _bool("MEDIA_AUTO_DOWNLOAD", _bool("IG_AUTO_DOWNLOAD", True))
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

    # ── Platform toggles (global; per-chat via /mediasettings) ──
    youtube_enabled: bool = _bool("MEDIA_YOUTUBE", True)
    tiktok_enabled: bool = _bool("MEDIA_TIKTOK", True)

    # ── Quality ────────────────────────────────────────────────
    # auto / 720 / 1080 / 1440 / 2160 / best — auto = cap at 1080.
    quality: str = _str("MEDIA_QUALITY", "auto").lower()
    # Hard size cap for merge pairs (video+audio estimated bytes).
    max_video_mb: int = max(1, _int("MEDIA_MAX_VIDEO_MB", 50))

    # ── YouTube strategy knobs (optional — no defaults are secrets) ──
    youtube_po_token: str = _str("YOUTUBE_PO_TOKEN")
    youtube_cookies_file: str = _str("YOUTUBE_COOKIES_FILE")

    # ── Direct stream mirrors (bypass YouTube's player API) ──────
    # Tried after the sign-in-free player clients and before the
    # operator's PO token / cookies opt-ins.
    stream_fallback: bool = _bool("MEDIA_STREAM_FALLBACK", True)
    stream_instances: Tuple[str, ...] = STREAM_INSTANCES
    # Per-request timeout; the whole mirror phase is capped at 2× this.
    stream_timeout: float = max(1.0, float(os.getenv("MEDIA_STREAM_TIMEOUT", "5") or 5))

    # ── Delivery ───────────────────────────────────────────────
    # Try Telegram-side HTTP fetch (sendVideo with URL) before downloading.
    direct_url: bool = _bool("MEDIA_DIRECT_URL", True)
    # Send a "Fetching media…" status only if the job exceeds this (seconds).
    status_delay: float = max(0.0, float(os.getenv("MEDIA_STATUS_DELAY", "1.8") or 1.8))
    # Captions default: off / short / full.
    captions: str = _str("MEDIA_CAPTIONS", "short").lower()

    # ── Rate limits / concurrency (per spec) ───────────────────
    max_requests_per_minute: int = max(1, _int("MEDIA_REQUESTS_PER_MINUTE", 10))
    max_active_jobs_per_user: int = max(1, _int("MEDIA_JOBS_PER_USER", 2))
    max_active_jobs: int = max(1, _int("MEDIA_MAX_ACTIVE_JOBS", 8))
    max_active_downloads: int = max(1, _int("MEDIA_MAX_ACTIVE_DOWNLOADS", 8))
    max_active_uploads: int = max(1, _int("MEDIA_MAX_ACTIVE_UPLOADS", 4))

    # ── Network timeouts / retries ─────────────────────────────
    connect_timeout: float = max(1.0, float(os.getenv("MEDIA_CONNECT_TIMEOUT", "10") or 10))
    read_timeout: float = max(1.0, float(os.getenv("MEDIA_READ_TIMEOUT", "30") or 30))
    download_timeout: float = max(10.0, float(os.getenv("MEDIA_DOWNLOAD_TIMEOUT", "300") or 300))
    max_network_retries: int = max(0, min(_int("MEDIA_NETWORK_RETRIES", 3), 5))
    # Startup sweep age for leftover temp job dirs (seconds).
    tmp_cleanup_age: int = max(60, _int("MEDIA_TMP_CLEANUP_AGE", 3600))


ig_config = IGConfig()

# Ensure temp dir exists early so handlers never race on mkdir.
try:
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    pass
