"""Platform detection + canonicalization (YouTube / TikTok / Instagram).

Pure functions — no I/O, no yt-dlp — so they are cheap to test and safe
to run inside message filters on every incoming update.
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple
from urllib.parse import urlsplit

from .exceptions import IGInvalidUrl
from .url_utils import find_instagram_urls, normalize_url, _URL_RE

PLATFORM_YOUTUBE = "youtube"
PLATFORM_TIKTOK = "tiktok"
PLATFORM_INSTAGRAM = "instagram"

# YouTube: watch?v= / shorts / embed / live / v/ / youtu.be — 11-char id.
_YT_RE = re.compile(
    r"(?i)(?:https?://)?(?:www\.|m\.|music\.)?"
    r"(?:"
    r"youtube\.com/watch\?(?:[^#\s\"']*&)?v=([A-Za-z0-9_-]{11})"
    r"|youtube\.com/(?:shorts|embed|live|v)/([A-Za-z0-9_-]{11})"
    r"|youtu\.be/([A-Za-z0-9_-]{11})"
    r")"
)

# TikTok: /@user/video/<id> or /@user/photo/<id>
_TT_RE = re.compile(
    r"(?i)(?:https?://)?(?:www\.|m\.)?"
    r"tiktok\.com(/@[\w.\-]+/(?:video|photo)/\d{6,25})/?"
)

# TikTok short links (vm / vt) — real id resolves after redirect.
_TT_SHORT_RE = re.compile(
    r"(?i)(?:https?://)?(?:vm|vt)\.tiktok\.com/([A-Za-z0-9]{5,20})/?"
)


def _clean_token(raw: str) -> str:
    """Strip trailing punctuation chat clients glue onto pasted links."""
    return raw.strip().rstrip(").,]>\"'")


def detect_platform(url: str) -> Optional[str]:
    """Return "youtube"|"tiktok"|"instagram" for *url*, else None."""
    u = _clean_token(url)
    if not u:
        return None
    if _YT_RE.search(u):
        return PLATFORM_YOUTUBE
    if _TT_RE.search(u) or _TT_SHORT_RE.search(u):
        return PLATFORM_TIKTOK
    # Instagram — reuse the module's own detector (host + path required).
    if find_instagram_urls(u):
        return PLATFORM_INSTAGRAM
    return None


def youtube_video_id(url: str) -> Optional[str]:
    m = _YT_RE.search(_clean_token(url))
    if not m:
        return None
    return m.group(1) or m.group(2) or m.group(3)


def is_youtube_short(url: str) -> bool:
    """True for /shorts/ links (used by the per-chat Shorts toggle)."""
    return bool(re.search(r"(?i)youtube\.com/shorts/[A-Za-z0-9_-]{11}", _clean_token(url)))


def canonicalize(platform: str, url: str) -> str:
    """Canonical HTTPS URL for *url* (platform must already match).

    YouTube → https://www.youtube.com/watch?v=<id>
    TikTok  → https://www.tiktok.com/@user/video/<id> (short links kept as-is)
    Instagram → url_utils.normalize_url (existing behaviour + allowlist check)
    """
    u = _clean_token(url)
    if platform == PLATFORM_YOUTUBE:
        vid = youtube_video_id(u)
        if not vid:
            raise IGInvalidUrl()
        return f"https://www.youtube.com/watch?v={vid}"
    if platform == PLATFORM_TIKTOK:
        m = _TT_RE.match(u) or _TT_RE.search(u)
        if m:
            path = m.group(1)
            host = (urlsplit("https://" + u.lstrip("hHtTpPsS:/")).hostname or "www.tiktok.com")
            return f"https://{host}{path}"
        s = _TT_SHORT_RE.search(u)
        if s:
            return f"https://vm.tiktok.com/{s.group(1)}/"
        raise IGInvalidUrl()
    # Instagram (also validates against the SSRF allowlist).
    return normalize_url(u)


def media_key(platform: str, canonical_url: str) -> str:
    """Stable single-flight / resolve-cache key: ``platform:id``.

    YouTube keys on the 11-char video id; TikTok on the numeric item id
    (or the short code before redirect); Instagram on its URL path.
    """
    if platform == PLATFORM_YOUTUBE:
        vid = youtube_video_id(canonical_url)
        if vid:
            return f"{platform}:{vid}"
    if platform == PLATFORM_TIKTOK:
        path = (urlsplit(canonical_url).path or "").strip("/")
        m = re.search(r"(?:video|photo)/(\d+)", path)
        if m:
            return f"{platform}:{m.group(1)}"
        code = path.rsplit("/", 1)[-1]
        return f"{platform}:code:{code}"
    # Instagram: path-based (matches the historic cache key format).
    return f"{platform}:{canonical_url.split('?')[0]}"


def find_media_urls(text: str) -> List[Tuple[str, str]]:
    """Every downloadable-looking media URL in *text*, in text order.

    Returns [(platform, raw_url), …]. Instagram bare profiles are kept
    here (the caller filters) so /dl can still explain what went wrong.
    """
    if not text:
        return []
    found: List[Tuple[int, int, str, str]] = []
    order = 0
    for m in _YT_RE.finditer(text):
        found.append((m.start(), order, PLATFORM_YOUTUBE, m.group(0)))
        order += 1
    for m in _TT_RE.finditer(text):
        found.append((m.start(), order, PLATFORM_TIKTOK, m.group(0)))
        order += 1
    for m in _TT_SHORT_RE.finditer(text):
        found.append((m.start(), order, PLATFORM_TIKTOK, m.group(0)))
        order += 1
    for m in _URL_RE.finditer(text):
        found.append((m.start(), order, PLATFORM_INSTAGRAM, m.group(0)))
        order += 1
    found.sort(key=lambda t: (t[0], t[1]))
    return [(plat, url) for _, _, plat, url in found]


def pick_media_url(text: str) -> Optional[Tuple[str, str]]:
    """First URL worth processing (YouTube/TikTok first, else first IG post).

    Instagram bare profiles are skipped — same rule the auto-detect
    handler has always applied.
    """
    from .url_utils import first_post_url

    items = find_media_urls(text)
    for plat, url in items:
        if plat in (PLATFORM_YOUTUBE, PLATFORM_TIKTOK):
            return plat, url
    ig_only = [u for p, u in items if p == PLATFORM_INSTAGRAM]
    if ig_only:
        got = first_post_url(ig_only)
        if got:
            return PLATFORM_INSTAGRAM, got
    return None


def resolve_target(url: str) -> Tuple[str, str]:
    """Detect platform + canonical URL, raising IGInvalidUrl when unsupported."""
    platform = detect_platform(url)
    if not platform:
        raise IGInvalidUrl()
    return platform, canonicalize(platform, url)
