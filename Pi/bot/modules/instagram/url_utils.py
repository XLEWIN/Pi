"""URL detection, normalization, and SSRF allowlist checks."""

from __future__ import annotations

import re
from typing import Iterable, List, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .config import ALLOWED_HOSTS, ALLOWED_MEDIA_HOST_SUFFIXES
from .exceptions import IGInvalidUrl
from .models import PostType

# Matches Instagram / Instagr.am links inside free text (with or without scheme).
_URL_RE = re.compile(
    r"""(?ix)
    \b(?:
        https?://
      | www\.
    )?
    (?:
        instagram\.com
      | instagr\.am
    )
    /[^\s<>"']*
    """
)

# Path prefixes that identify downloadable content vs profiles.
_POST_PATHS = ("/p/", "/reel/", "/reels/", "/tv/")
_STORY_PATH = "/stories/"
_HIGHLIGHT_PATH = "/highlights/"
_PROFILE_SEGMENTS = frozenset(
    {
        "p", "reel", "reels", "tv", "stories", "explore", "about",
        "legal", "developer", "directory", "web", "accounts",
    }
)


def find_instagram_urls(text: str) -> List[str]:
    """Return every Instagram-looking URL token in *text* (raw, pre-normalize)."""
    if not text:
        return []
    return _URL_RE.findall(text)


def _clean_query(query: str) -> str:
    """Strip tracking params; keep meaningful ones (e.g. img_index for carousels)."""
    keep_keys = {"img_index"}
    pairs = [
        (k, v)
        for k, v in parse_qsl(query, keep_blank_values=True)
        if k.lower() in keep_keys
    ]
    return urlencode(pairs)


def normalize_url(raw: str) -> str:
    """
    Canonicalize an Instagram URL:
    - force https, lowercase host, strip default ports
    - instagr.am → instagram.com
    - drop tracking query params (keep img_index)
    - drop trailing punctuation from chat pastes
    """
    url = raw.strip().rstrip(").,]>\"'")
    if not re.match(r"(?i)^https?://", url):
        url = "https://" + url
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if not host:
        raise IGInvalidUrl()
    if host.endswith("instagr.am"):
        host = host[: -len("instagr.am")] + "instagram.com"
    if host.startswith("www.") or host.startswith("m."):
        bare = host.split(".", 1)[1]
    else:
        bare = host
    # Exact allowlist match only — no subdomain tricks.
    if bare not in ALLOWED_HOSTS and host not in ALLOWED_HOSTS:
        raise IGInvalidUrl()
    # Canonical form: apex instagram.com
    canon_host = "instagram.com"
    path = parts.path or "/"
    query = _clean_query(parts.query)
    return urlunsplit(("https", canon_host, path, query, ""))


def host_allowed(url: str) -> bool:
    """True if *url*'s host is on the allowlist (for any outbound fetch)."""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    if not host:
        return False
    if host in ALLOWED_HOSTS:
        return True
    return any(host.endswith(suf) for suf in ALLOWED_MEDIA_HOST_SUFFIXES)


def assert_media_host(url: str) -> None:
    if not host_allowed(url):
        raise IGInvalidUrl()


def classify_post_type(url: str) -> PostType:
    path = urlsplit(url).path or "/"
    low = path.lower()
    if any(p in low for p in _POST_PATHS):
        if "/reel" in low or "/tv/" in low:
            return PostType.REEL
        return PostType.POST
    if low.startswith(_STORY_PATH) or _STORY_PATH in low:
        return PostType.STORY
    if low.startswith(_HIGHLIGHT_PATH) or _HIGHLIGHT_PATH in low:
        return PostType.HIGHLIGHT
    seg = [s for s in low.split("/") if s]
    if len(seg) == 1 and seg[0] not in _PROFILE_SEGMENTS:
        return PostType.PROFILE
    return PostType.UNKNOWN


def extract_media_id(url: str) -> str:
    """Stable id from shortcode path when possible."""
    path = urlsplit(url).path or "/"
    low = path.lower()
    for prefix in ("/p/", "/reel/", "/reels/", "/tv/", "/stories/highlights/", "/stories/"):
        if low.startswith(prefix):
            rest = low[len(prefix):]
            code = rest.split("/")[0].split("?")[0]
            if code:
                return code
    return path.strip("/").replace("/", "_") or "unknown"


def first_post_url(urls: Iterable[str]) -> Optional[str]:
    """Pick the first URL that classifies as downloadable media."""
    best: Optional[str] = None
    for raw in urls:
        try:
            norm = normalize_url(raw)
        except IGInvalidUrl:
            continue
        kind = classify_post_type(norm)
        if kind in (PostType.POST, PostType.REEL, PostType.STORY, PostType.HIGHLIGHT):
            return norm
        if best is None and kind == PostType.UNKNOWN:
            best = norm
    return best
