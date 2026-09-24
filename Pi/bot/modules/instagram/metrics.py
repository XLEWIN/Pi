"""In-process metrics for /igstats and structured [IG] logs."""

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
    bytes_sent: int = 0
    last_error: str = ""
    last_error_at: float = 0.0
    recent_ms: Deque[int] = field(default_factory=lambda: deque(maxlen=50))
    started_at: float = field(default_factory=time.time)
    _lock: Lock = field(default_factory=Lock, repr=False)

    def bump(self, name: str, *, n: int = 1, error: str | None = None, ms: int | None = None) -> None:
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

    def snapshot(self) -> Dict[str, object]:
        with self._lock:
            ms = list(self.recent_ms)
            avg = sum(ms) // len(ms) if ms else 0
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
                "bytes_sent": self.bytes_sent,
                "avg_resolve_ms": avg,
                "last_error": self.last_error,
                "uptime_sec": int(time.time() - self.started_at),
            }


metrics = IGMetrics()


def ig_log(msg: str) -> None:
    """Structured single-line log with [IG] prefix."""
    from bot.logger import logger

    logger.info(f"[IG] {msg}")
