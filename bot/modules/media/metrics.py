"""In-process metrics for /mediastats and structured [MEDIA] logs."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from threading import Lock
from typing import Deque, Dict


@dataclass
class IGMetrics:
    resolved_ok: int = 0
    resolved_fail: int = 0
    downloads: int = 0
    download_fail: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    uploads: int = 0
    upload_fail: int = 0
    auto_triggers: int = 0
    busy_rejects: int = 0
    rate_rejects: int = 0
    direct_ok: int = 0
    direct_fallback: int = 0
    merges: int = 0
    youtube_n: int = 0
    tiktok_n: int = 0
    instagram_n: int = 0
    bytes_sent: int = 0
    last_error: str = ""
    last_error_at: float = 0.0
    recent_ms: Deque[int] = field(default_factory=lambda: deque(maxlen=50))
    recent_download_ms: Deque[int] = field(default_factory=lambda: deque(maxlen=50))
    started_at: float = field(default_factory=time.time)
    _lock: Lock = field(default_factory=Lock, repr=False)

    def bump(self, name: str, *, n: int = 1, error: str | None = None, ms: int | None = None,
             dl_ms: int | None = None) -> None:
        with self._lock:
            if hasattr(self, name):
                cur = getattr(self, name)
                if isinstance(cur, int):
                    setattr(self, name, cur + n)
            if error:
                self.last_error = error[:200]
                self.last_error_at = time.time()
            if ms is not None:
                self.recent_ms.append(int(ms))
            if dl_ms is not None:
                self.recent_download_ms.append(int(dl_ms))

    def bump_platform(self, platform: str) -> None:
        attr = {"youtube": "youtube_n", "tiktok": "tiktok_n"}.get(platform, "instagram_n")
        self.bump(attr)

    def snapshot(self) -> Dict[str, object]:
        with self._lock:
            ms = list(self.recent_ms)
            avg = sum(ms) // len(ms) if ms else 0
            dl = list(self.recent_download_ms)
            dl_avg = sum(dl) // len(dl) if dl else 0
            total_requests = self.resolved_ok + self.resolved_fail
            return {
                "resolved_ok": self.resolved_ok,
                "resolved_fail": self.resolved_fail,
                "downloads": self.downloads,
                "download_fail": self.download_fail,
                "cache_hits": self.cache_hits,
                "cache_misses": self.cache_misses,
                "uploads": self.uploads,
                "upload_fail": self.upload_fail,
                "auto_triggers": self.auto_triggers,
                "busy_rejects": self.busy_rejects,
                "rate_rejects": self.rate_rejects,
                "direct_ok": self.direct_ok,
                "direct_fallback": self.direct_fallback,
                "merges": self.merges,
                "youtube_n": self.youtube_n,
                "tiktok_n": self.tiktok_n,
                "instagram_n": self.instagram_n,
                "requests": total_requests,
                "bytes_sent": self.bytes_sent,
                "avg_resolve_ms": avg,
                "avg_download_ms": dl_avg,
                "last_error": self.last_error,
                "uptime_sec": int(time.time() - self.started_at),
            }


metrics = IGMetrics()


def ig_log(msg: str) -> None:
    """Structured single-line log with [MEDIA] prefix."""
    from bot.logger import logger

    logger.info(f"[MEDIA] {msg}")
