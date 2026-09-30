"""Direct stream-URL mirrors — Piped / Invidious (no YouTube player API).

When YouTube's bot check demands a sign-in, these public mirrors hand
back the *same* googlevideo CDN stream URLs they extracted server-side:
no API key, no cookies, no PO token, no player-client tricks — pure
HTTPS GETs. Candidates come from the official Invidious instance list
(``api.invidious.io``, refreshed every 30 min) plus our own config
(``MEDIA_STREAM_INSTANCES``); SSRF stays bounded because only those two
sources are ever contacted.

Payloads are normalized into yt-dlp-style format dicts so the existing
``select_format`` strategy (progressive → size-guarded merge pair)
applies unchanged; the downloader then streams them over plain HTTP.
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlsplit

from .config import ig_config
from .exceptions import IGResolveFailed
from .metrics import ig_log, metrics
from .url_utils import host_allowed, register_stream_host

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Probe order per instance — Invidious path first (the official list
# only publishes Invidious instances), Piped second for static hosts.
_ENDPOINTS = (
    ("invidious", "/api/v1/videos/{vid}"),
    ("piped", "/streams/{vid}"),
)

# Official Invidious instance list — the ONLY discovery source (SSRF:
# hosts are contacted only when published there or configured below).
# Static config alone rots: mirrors die and are replaced by new ones
# without a redeploy, so the list is refreshed every _DYN_TTL seconds.
_DISCOVERY_URL = "https://api.invidious.io/instances.json?sort_by=health"
_DYN_TTL = 1800.0
_dyn_hosts: Tuple[str, ...] = ()
_dyn_ts = 0.0

_AUDIO_CODECS = ("mp4a", "aac", "opus", "vorbis", "ac-3", "ec-3", "flac")
_VIDEO_CODECS = ("avc", "av01", "vp0", "vp9", "hvc", "hev", "theora")


def _height(label: Any) -> Optional[int]:
    """'1080p' / '720' / 'hd' → 1080 / 720 / None."""
    m = re.search(r"(\d{3,4})", str(label or ""))
    return int(m.group(1)) if m else None


def _tbr_kbit(bits: Any) -> Optional[float]:
    """Mirrors report bit/s — select_format's tbr is kbit/s."""
    try:
        b = float(bits or 0)
    except (TypeError, ValueError):
        return None
    if b <= 0:
        return None
    return round(b / 1000.0, 1)


def _size(raw: Any) -> Optional[int]:
    try:
        v = int(str(raw).strip())
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def _media_url(raw: Any) -> str:
    """Mirror payloads sometimes HTML-escape query separators (&amp;)."""
    return str(raw or "").replace("&amp;", "&")


def _codec_split(typ: str) -> Tuple[List[str], List[str]]:
    """'video/mp4; codecs="avc1..., mp4a..."' → ([video tokens], [audio tokens])."""
    m = re.search(r'codecs="?([^"]+)"?', typ or "")
    tokens = [t.strip().lower() for t in (m.group(1) if m else "").split(",") if t.strip()]
    video = [t for t in tokens if t.startswith(_VIDEO_CODECS)]
    audio = [t for t in tokens if t.startswith(_AUDIO_CODECS)]
    return video, audio


def piped_formats(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Piped ``/streams/{id}`` payload → yt-dlp-style format dicts."""
    dur = data.get("duration")
    out: List[Dict[str, Any]] = []
    for i, s in enumerate(data.get("videoStreams") or []):
        url = _media_url(s.get("url"))
        if not url:
            continue
        h = s.get("height") or _height(s.get("quality"))
        video_only = bool(s.get("videoOnly"))
        ext = str(s.get("format") or "mp4").lower()
        if ext == "3gpp":
            ext = "3gp"
        out.append(
            {
                "format_id": str(s.get("itag") or f"v{i}"),
                "url": url,
                "ext": ext,
                "width": s.get("width"),
                "height": h,
                "vcodec": str(s.get("codec") or "avc1") if h else None,
                "acodec": None if video_only else "aac",
                "tbr": _tbr_kbit(s.get("bitrate")),
                "filesize": _size(s.get("contentLength")),
                "duration": dur,
                "protocol": "https",
            }
        )
    for i, s in enumerate(data.get("audioStreams") or []):
        url = _media_url(s.get("url"))
        if not url:
            continue
        mime = str(s.get("mimeType") or "")
        codec = str(s.get("codec") or "")
        ext = "m4a" if ("mp4" in mime or "mp4a" in codec) else "webm"
        out.append(
            {
                "format_id": str(s.get("itag") or f"a{i}"),
                "url": url,
                "ext": ext,
                "vcodec": "none",
                "acodec": codec or "opus",
                "tbr": _tbr_kbit(s.get("bitrate")),
                "filesize": _size(s.get("contentLength")),
                "duration": dur,
                "protocol": "https",
            }
        )
    return out


def invidious_formats(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Invidious ``/api/v1/videos/{id}`` payload → yt-dlp-style format dicts."""
    dur = data.get("lengthSeconds")
    out: List[Dict[str, Any]] = []
    # formatStreams: muxed progressive (low quality, video+audio).
    for i, s in enumerate(data.get("formatStreams") or []):
        url = _media_url(s.get("url"))
        if not url:
            continue
        out.append(
            {
                "format_id": str(s.get("itag") or f"f{i}"),
                "url": url,
                "ext": str(s.get("container") or "mp4"),
                "width": s.get("width"),
                "height": s.get("height") or _height(s.get("qualityLabel")),
                "vcodec": "avc1",
                "acodec": "aac",
                "tbr": _tbr_kbit(s.get("bitrate")),
                "filesize": _size(s.get("contentLength")) or _size(s.get("size")),
                "duration": dur,
                "protocol": "https",
            }
        )
    # adaptiveFormats: single-track — video-only or audio-only (or muxed).
    for i, s in enumerate(data.get("adaptiveFormats") or []):
        url = _media_url(s.get("url"))
        if not url:
            continue
        typ = str(s.get("type") or "")
        video_c, audio_c = _codec_split(typ)
        h = s.get("height") or _height(s.get("qualityLabel"))
        container = str(s.get("container") or "").lower()
        if container not in {"mp4", "webm", "m4a", "3gp"}:
            container = "mp4" if "mp4" in typ else ("webm" if "webm" in typ else "mp4")
        if not video_c and not audio_c:
            # No codec header — decide by presence of a resolution.
            if h:
                video_c = ["avc1"]
            else:
                audio_c = ["aac"]
        if video_c and audio_c:
            vcodec, acodec = video_c[0], audio_c[0]  # muxed entry
        elif video_c:
            vcodec, acodec = video_c[0], "none"  # video-only
        else:
            vcodec, acodec = "none", audio_c[0]  # audio-only
        out.append(
            {
                "format_id": str(s.get("itag") or f"af{i}"),
                "url": url,
                "ext": container,
                "width": s.get("width"),
                "height": h,
                "vcodec": vcodec,
                "acodec": acodec,
                "tbr": _tbr_kbit(s.get("bitrate")),
                "filesize": _size(s.get("clen"))
                or _size(s.get("contentLength"))
                or _size(s.get("size")),
                "duration": dur,
                "protocol": "https",
            }
        )
    return out


def _pick_thumbnail(data: Dict[str, Any], kind: str) -> Optional[str]:
    if kind == "piped":
        return data.get("thumbnail")
    thumbs = data.get("videoThumbnails") or []
    for quality in ("maxres", "standard", "high", "medium"):
        for t in thumbs:
            if str(t.get("quality") or "").lower() == quality and t.get("url"):
                return t.get("url")
    if thumbs and thumbs[0].get("url"):
        return thumbs[0].get("url")
    return data.get("thumbnail")


def parse_payload(
    kind: str, data: Dict[str, Any]
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """(formats, meta) from one mirror payload — meta feeds the result card."""
    if kind == "piped":
        fmts = piped_formats(data)
        meta = {
            "title": data.get("title"),
            "uploader": data.get("uploader"),
            "duration": data.get("duration"),
            "thumbnail": data.get("thumbnail"),
            "description": data.get("description"),
        }
    else:
        fmts = invidious_formats(data)
        meta = {
            "title": data.get("title"),
            "uploader": data.get("author"),
            "duration": data.get("lengthSeconds"),
            "thumbnail": _pick_thumbnail(data, kind),
            "description": data.get("description"),
        }
    return fmts, {k: v for k, v in meta.items() if v}


def parse_instance_list(payload: Any) -> Tuple[str, ...]:
    """Official ``instances.json`` → https hosts whose JSON API is enabled.

    Handles the documented ``[[name, meta], ...]`` shape, a plain
    ``{name: meta}`` map, and a ``{"instances": [...]}`` wrapper.
    Anything unexpected yields an empty tuple — static config remains
    the safety net.
    """
    pairs: List[Tuple[str, Dict[str, Any]]] = []
    if isinstance(payload, list):
        for item in payload:
            if (
                isinstance(item, (list, tuple))
                and len(item) == 2
                and isinstance(item[1], dict)
            ):
                pairs.append((str(item[0]), item[1]))
            elif isinstance(item, dict):
                pairs.append((str(item.get("name") or ""), item))
    elif isinstance(payload, dict):
        inner = payload.get("instances")
        if isinstance(inner, list):
            return parse_instance_list(inner)
        for name, meta in payload.items():
            if isinstance(meta, dict):
                pairs.append((str(name), meta))
    out: List[str] = []
    for name, meta in pairs:
        if not meta.get("api"):
            continue
        uri = str(meta.get("uri") or f"https://{name}")
        if not uri.lower().startswith("https://"):
            continue
        try:
            host = (urlsplit(uri).hostname or "").lower()
        except ValueError:
            continue
        if host and "." in host and host not in out:
            out.append(host)
    return tuple(out)


def _dynamic_instances(timeout: float) -> Tuple[str, ...]:
    """Freshly health-sorted official-list instances (cached 30 min).

    Failures return the last known set (possibly empty) — the static
    config list still gets its turn either way.
    """
    global _dyn_hosts, _dyn_ts
    now = time.monotonic()
    if now - _dyn_ts < _DYN_TTL:
        return _dyn_hosts
    import httpx

    try:
        resp = httpx.get(
            _DISCOVERY_URL,
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": _UA, "Accept": "application/json"},
        )
        if resp.status_code != 200:
            raise ValueError(f"HTTP {resp.status_code}")
        hosts = parse_instance_list(resp.json())
    except Exception as e:
        ig_log(f"stream instance list unavailable: {type(e).__name__}")
        _dyn_ts = now  # don't hammer the list endpoint on outage
        return _dyn_hosts
    if hosts:
        _dyn_hosts, _dyn_ts = hosts, now
    else:
        # Empty answer is suspicious (list format drift) — keep the
        # previous working set and retry after the TTL.
        _dyn_ts = now
    return _dyn_hosts


def _candidate_hosts(timeout: float) -> Tuple[str, ...]:
    """Discovered instances first (fresh health data), then static config."""
    out: List[str] = []
    for h in (*_dynamic_instances(timeout), *ig_config.stream_instances):
        h = str(h).lower().strip().strip(".")
        if h and "." in h and " " not in h and h not in out:
            out.append(h)
    return tuple(out)


def _same_host(url: str, host: str) -> bool:
    try:
        h = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return h == host


def fetch_stream(video_id: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any], str]:
    """First mirror (discovered or configured) with usable stream URLs.

    Returns ``(formats, meta, mirror_host)``.  Raises ``IGResolveFailed``
    when every mirror fails — a *non-fatal* outcome so the caller can
    continue with remaining yt-dlp strategies (PO token / cookies).

    Candidates come from the official Invidious instance list (refreshed
    every 30 min) plus the configured ``MEDIA_STREAM_INSTANCES`` list, so
    dead static entries cost at most one quick probe and replacement
    mirrors appear without a redeploy.

    Timeouts are hard-bounded: 2× the per-request timeout for up to two
    candidates, growing with the list but never past 20 s — a wall of
    dead mirrors can't eat the job's time budget.  A pass that ends on
    transient signals (5xx / 429 / transport errors) gets ONE retry pass
    inside the same deadline; definitive 404/403 answers don't retry.
    """
    import httpx

    vid = quote(str(video_id or "").strip(), safe="")
    if not vid:
        raise IGResolveFailed("Stream mirrors: missing video id.")

    per_request = ig_config.stream_timeout
    candidates = _candidate_hosts(per_request)
    deadline = time.monotonic() + min(20.0, per_request * max(2, len(candidates)))
    last = "no mirror reachable"
    transient = False

    def _pass() -> Optional[Tuple[List[Dict[str, Any]], Dict[str, Any], str]]:
        nonlocal last, transient
        for host in candidates:
            if time.monotonic() > deadline:
                break
            base = f"https://{host}"
            try:
                client = httpx.Client(
                    timeout=per_request,
                    follow_redirects=True,
                    proxy=ig_config.proxy_url or None,
                    headers={"User-Agent": _UA, "Accept": "application/json"},
                )
            except Exception as e:  # pragma: no cover — client construction
                last = f"{host}: {type(e).__name__}"
                continue
            with client:
                for kind, tpl in _ENDPOINTS:
                    if time.monotonic() > deadline:
                        break
                    url = base + tpl.format(vid=vid)
                    try:
                        resp = client.get(url)
                    except Exception as e:
                        # Dead host — don't wait again on it, move to next.
                        last = f"{host}: {type(e).__name__}"
                        transient = True
                        break
                    if resp.status_code != 200:
                        last = f"{host}: HTTP {resp.status_code}"
                        if resp.status_code == 429 or resp.status_code >= 500:
                            transient = True
                        continue
                    try:
                        data = resp.json()
                    except ValueError:
                        last = f"{host}: bad json"
                        continue
                    if not isinstance(data, dict):
                        continue
                    if kind == "invidious" and not (
                        data.get("adaptiveFormats") or data.get("formatStreams")
                    ):
                        continue
                    if kind == "piped" and not (
                        data.get("videoStreams") or data.get("audioStreams")
                    ):
                        continue
                    fmts, meta = parse_payload(kind, data)
                    usable = [
                        f
                        for f in fmts
                        if f.get("url")
                        and (host_allowed(f["url"]) or _same_host(f["url"], host))
                    ]
                    if not usable:
                        last = f"{host}: no usable streams"
                        continue
                    # Media may be served by the mirror itself — let the
                    # resolver/downloader allowlist checks pass for it.
                    register_stream_host(host)
                    metrics.bump("stream_hits")
                    ig_log(
                        f"stream mirror {host} ({kind}) → {len(usable)} direct URL(s)"
                    )
                    return usable, meta, host
        return None

    result = _pass()
    if result is None and transient and time.monotonic() < deadline:
        ig_log("stream mirrors: transient failure — one retry pass")
        result = _pass()
    if result is None:
        raise IGResolveFailed(f"Stream mirrors unreachable ({last}).")
    return result
