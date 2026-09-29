"""Format selection for YouTube/TikTok — pure, side-effect free.

Strategy (per spec):
  1. Progressive MP4 (video+audio in one file) at or under the quality cap.
  2. If no reasonable progressive file exists — pick a video-only stream plus
     the best compatible audio stream; the caller merges them with ffmpeg.
  3. Otherwise the best progressive fallback.

Size guard: a merge pair whose estimate exceeds the cap is rejected so the
downloader never pulls 400 MB just to throw it away at Telegram's 50 MB gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

# Quality setting → maximum video height. "auto" = 1080p, "best" = no cap.
_QUALITY_CAPS = {
    "auto": 1080,
    "720": 720,
    "720p": 720,
    "1080": 1080,
    "1080p": 1080,
    "1440": 1440,
    "1440p": 1440,
    "2160": 2160,
    "2160p": 2160,
    "4320": 4320,
    "best": 100_000,
}


def quality_cap(quality: str) -> int:
    """Map a quality setting ("auto"/"720"/"1080"/…/"best") to max height."""
    return _QUALITY_CAPS.get((quality or "auto").lower(), 1080)


def _has_video(f: Dict[str, Any]) -> bool:
    return f.get("vcodec") not in (None, "", "none") and bool(f.get("height"))


def _has_audio(f: Dict[str, Any]) -> bool:
    return f.get("acodec") not in (None, "", "none")


def _usable(f: Dict[str, Any]) -> bool:
    if not f.get("url"):
        return False
    # Storyboards / mhtml poster frames carry no real media.
    if f.get("protocol") == "mhtml" or f.get("ext") in {"mhtml"}:
        return False
    return _has_video(f) or _has_audio(f)


def estimate_bytes(f: Dict[str, Any], duration: Optional[float] = None) -> int:
    """Estimated download size: filesize → filesize_approx → tbr × duration."""
    for key in ("filesize", "filesize_approx"):
        v = f.get(key)
        if v:
            try:
                return int(v)
            except (TypeError, ValueError):
                pass
    tbr = f.get("tbr") or 0
    dur = duration or f.get("duration") or 0
    if tbr and dur:
        return int(float(tbr) * float(dur) / 8.0 * 1024)  # kbit/s → bytes
    return 0


def _prog_score(f: Dict[str, Any]) -> tuple:
    """Higher is better among progressive candidates."""
    height = int(f.get("height") or 0)
    tbr = float(f.get("tbr") or 0)
    mp4 = 1 if f.get("ext") == "mp4" else 0
    https = 1 if str(f.get("protocol") or "").startswith("https") else 0
    return (mp4, https, height, tbr)


@dataclass
class FormatChoice:
    """Result of select_format — tells the caller what to download."""

    kind: str                    # "progressive" | "merge" | "none"
    video: Optional[Dict[str, Any]] = None
    audio: Optional[Dict[str, Any]] = None
    est_bytes: int = 0

    @property
    def needs_merge(self) -> bool:
        return self.kind == "merge"


def select_format(
    formats: List[Dict[str, Any]],
    *,
    quality: str = "auto",
    max_bytes: int = 50 * 1024 * 1024,
    duration: Optional[float] = None,
    allow_merge: bool = True,
) -> FormatChoice:
    """Pick the best download plan for *formats* under *quality*/*max_bytes*."""
    cap = quality_cap(quality)
    usable = [f for f in formats if _usable(f)]

    progressive = [
        f for f in usable
        if _has_video(f) and _has_audio(f) and int(f.get("height") or 0) <= cap
    ]
    progressive.sort(key=_prog_score, reverse=True)

    # 1) Progressive at a "reasonable" height (≥ 720p or exactly the cap).
    for f in progressive:
        h = int(f.get("height") or 0)
        est = estimate_bytes(f, duration)
        if est and max_bytes and est > max_bytes:
            continue
        if h >= min(720, cap) or h == cap:
            return FormatChoice(kind="progressive", video=f, est_bytes=est)

    # 2) Merge pair: best video-only ≤ cap + best audio stream.
    if allow_merge:
        video_only = [
            f for f in usable
            if _has_video(f) and not _has_audio(f) and int(f.get("height") or 0) <= cap
        ]
        audio_only = [
            f for f in usable
            if _has_audio(f) and not _has_video(f)
        ]

        def _audio_score(f: Dict[str, Any]) -> tuple:
            ext = f.get("ext") or ""
            mp4ish = 1 if ext in {"m4a", "mp4"} else 0
            return (mp4ish, float(f.get("tbr") or 0))

        if video_only and audio_only:
            video_only.sort(key=_prog_score, reverse=True)
            audio_only.sort(key=_audio_score, reverse=True)
            v, a = video_only[0], audio_only[0]
            est = estimate_bytes(v, duration) + estimate_bytes(a, duration)
            if not max_bytes or not est or est <= max_bytes:
                return FormatChoice(kind="merge", video=v, audio=a, est_bytes=est)

    # 3) Any progressive under the cap (size-guarded, smallest pass first).
    if progressive:
        best = progressive[0]
        est = estimate_bytes(best, duration)
        if not max_bytes or not est or est <= max_bytes:
            return FormatChoice(kind="progressive", video=best, est_bytes=est)
        # Everything too big — fall back to the lowest-height progressive.
        progressive.sort(key=lambda f: int(f.get("height") or 0))
        small = progressive[0]
        return FormatChoice(
            kind="progressive", video=small, est_bytes=estimate_bytes(small, duration)
        )

    # 4) Whatever the site gave us (downloader enforces the hard cap).
    for f in sorted(usable, key=_prog_score, reverse=True):
        if _has_video(f):
            return FormatChoice(
                kind="progressive", video=f, est_bytes=estimate_bytes(f, duration)
            )
    if usable:
        return FormatChoice(
            kind="progressive", video=usable[0], est_bytes=estimate_bytes(usable[0], duration)
        )
    return FormatChoice(kind="none")
