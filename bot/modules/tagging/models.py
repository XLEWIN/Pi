"""Tagging module data models — settings + candidate rows."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Optional


@dataclass(frozen=True)
class TagSettings:
    """Per-chat tagging settings (one row in tag_settings)."""

    chat_id: int
    mode: str = "online_first"
    window_hours: int = 24
    max_mentions: int = 0       # 0 = unlimited
    batch_size: int = 3600
    send_mode: str = "normal"
    registry_mode: str = "hybrid"

    def replace_(self, **fields) -> "TagSettings":
        return replace(self, **fields)


# Presence ranks (lower = sorted first).
RANK_ONLINE = 0     # MTProto-confirmed online only
RANK_ACTIVE = 1     # messaged within PRESENCE_TTL
RANK_RECENT = 2     # active within a day
RANK_STALE = 3      # older / unknown


@dataclass
class Candidate:
    """A user eligible for tagging in one chat."""

    user_id: int
    display_name: str
    username: Optional[str] = None
    last_active_at: float = 0.0
    first_seen: float = 0.0
    presence_at: float = 0.0     # best known presence timestamp
    presence_rank: int = RANK_STALE
    is_bot: bool = False

    @property
    def activity_ts(self) -> float:
        """Newest signal we have for this member (message or join)."""
        return max(self.last_active_at, self.first_seen)


@dataclass
class SessionTotals:
    """Aggregates for /tagstats."""

    sessions: int = 0
    completed: int = 0
    stopped: int = 0
    failed: int = 0
    tagged: int = 0
    messages: int = 0
    members: int = 0
    fields: dict = field(default_factory=dict)
