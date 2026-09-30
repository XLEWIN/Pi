"""Live job progress for the "Fetching media…" status message.

Pure bookkeeping — no Telegram/network calls.  The downloader reports
bytes/stages here; handlers.py edits the status message at most once
per ``edit_interval`` and only when the text actually changed.
"""

from __future__ import annotations

import time
from typing import Callable, List, Optional, Tuple

# Quarter-second speed buckets — bounded so the deque can't grow forever.
_BUCKET_SEC = 0.25
_MAX_BUCKETS = 32
_SPEED_WINDOW = 2.5  # seconds considered when estimating download speed


def human_bytes(n: float) -> str:
    """1536 → '1.5 KB' style display (always at most one decimal)."""
    if n < 1024:
        return f"{max(0, int(n))} B"
    for unit in ("KB", "MB", "GB"):
        n /= 1024.0
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}"
    return f"{n:.1f} GB"


def human_rate(bps: float) -> str:
    return f"{human_bytes(bps)}/s"


class JobProgress:
    """Byte/stage accumulator for one media job (single event loop)."""

    def __init__(
        self,
        *,
        edit_interval: float = 0.9,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.stage_name = "resolve"
        self.expected = 0
        self.raw = 0  # unclamped — retries may recount bytes; speed uses this
        self.edit_interval = max(0.0, float(edit_interval))
        self._clock = clock
        self._t0 = clock()
        # (timestamp, raw-so-far) buckets, updated in place within a bucket.
        self._buckets: List[Tuple[float, int]] = [(self._t0, 0)]
        self._last_text = ""
        self._last_edit = 0.0

    # ── reporting (called from the downloader) ────────────────────

    def expect(self, total: Optional[int]) -> None:
        """Declare the expected total bytes (sum of asset sizes)."""
        if total and total > 0:
            self.expected = int(total)

    def add(self, n: int) -> None:
        if n <= 0:
            return
        self.raw += int(n)
        now = self._clock()
        if now - self._buckets[-1][0] >= _BUCKET_SEC:
            self._buckets.append((now, self.raw))
            while len(self._buckets) > _MAX_BUCKETS:
                self._buckets.pop(0)
        else:
            self._buckets[-1] = (now, self.raw)

    def stage(self, name: str) -> None:
        self.stage_name = name

    # ── derived values ────────────────────────────────────────────

    @property
    def done(self) -> int:
        """Display bytes: clamped to the expected total for a sane %."""
        if self.expected and self.raw > self.expected:
            return self.expected
        return self.raw

    def percent(self) -> Optional[float]:
        if not self.expected:
            return None
        return min(100.0, 100.0 * self.raw / self.expected)

    def speed(self) -> float:
        """Bytes/sec over the trailing ~2.5 s window (0 until warm)."""
        if len(self._buckets) < 2:
            return 0.0
        now = self._clock()
        cutoff = now - _SPEED_WINDOW
        base_t, base_b = self._buckets[0]
        for t, b in self._buckets:
            if t <= cutoff:
                base_t, base_b = t, b
            else:
                break
        dt = now - base_t
        if dt < 0.2:
            return 0.0
        return max(0.0, (self.raw - base_b) / dt)

    def text(self) -> str:
        """Current status line (plain text — safe for HTML parse mode)."""
        if self.stage_name == "merge":
            return "Merging video + audio…"
        if self.stage_name == "upload":
            return "Uploading to Telegram…"
        if self.stage_name == "resolve":
            return "Resolving…"
        # download
        pct = self.percent()
        body = human_bytes(self.done)
        if self.expected:
            body = f"{body}/{human_bytes(self.expected)}"
        speed = self.speed()
        if pct is not None:
            head = f"Downloading… {pct:.0f}%"
        else:
            head = "Downloading…"
        if speed > 0:
            return f"{head} ({body}, {human_rate(speed)})"
        return f"{head} ({body})"

    def text_for_edit(self) -> Optional[str]:
        """Next status text, or None when throttled/unchanged."""
        now = self._clock()
        if now - self._last_edit < self.edit_interval:
            return None
        text = self.text()
        if text == self._last_text:
            return None
        self._last_edit = now
        self._last_text = text
        return text
