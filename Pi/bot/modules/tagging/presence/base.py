"""Presence providers — who is online/active right now.

Ranks (lower = earlier in the tag order):
    0 ONLINE  — MTProto-confirmed online (never inferred from activity)
    1 ACTIVE  — messaged within PRESENCE_TTL
    2 RECENT  — active within a day
    3 STALE   — older or unknown
"""

from __future__ import annotations

from typing import Sequence

from ..models import RANK_ACTIVE, RANK_ONLINE, RANK_RECENT, RANK_STALE, Candidate

__all__ = [
    "PresenceProvider",
    "RANK_ONLINE",
    "RANK_ACTIVE",
    "RANK_RECENT",
    "RANK_STALE",
]


class PresenceProvider:
    """Base interface: enrich candidates' presence_rank/presence_at."""

    name = "none"
    available = False

    async def enrich(
        self,
        chat_id: int,
        candidates: Sequence[Candidate],
        now: float,
    ) -> None:
        """Set presence fields on candidates (best-effort, never raises)."""
        raise NotImplementedError
