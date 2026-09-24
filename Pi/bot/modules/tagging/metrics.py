"""Per-session counters for progress reporting and completion cards."""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class SessionMetrics:
    """Mutable counters owned by one tagging session."""

    started_at: float = field(default_factory=time.monotonic)
    messages_sent: int = 0
    mentions_sent: int = 0
    floodwaits: int = 0
    retries: int = 0
    edits: int = 0

    @property
    def elapsed(self) -> float:
        """Seconds since the session started."""
        return time.monotonic() - self.started_at

    @property
    def rate_per_min(self) -> float:
        """Batch messages per minute (0 until something is sent)."""
        mins = self.elapsed / 60.0
        if mins <= 0 or self.messages_sent == 0:
            return 0.0
        return self.messages_sent / mins

    def record_send(self, mentions: int) -> None:
        self.messages_sent += 1
        self.mentions_sent += mentions
