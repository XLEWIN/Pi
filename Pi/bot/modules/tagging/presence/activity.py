"""Activity-based presence — honest tiers, never claims 'online'.

A recent message proves the user is *active*, not that they are online
right now; rank 0 stays reserved for MTProto confirmation (spec rule).
"""

from __future__ import annotations

from typing import Sequence

from .. import config
from ..models import RANK_ACTIVE, RANK_ONLINE, RANK_RECENT, RANK_STALE, Candidate
from .base import PresenceProvider


class ActivityPresence(PresenceProvider):
    name = "activity"
    available = True

    async def enrich(
        self,
        chat_id: int,
        candidates: Sequence[Candidate],
        now: float,
    ) -> None:
        for c in candidates:
            # Existing MTProto presence beats activity-derived ranks.
            if c.presence_at and (now - c.presence_at) <= config.PRESENCE_TTL:
                c.presence_rank = min(c.presence_rank, RANK_ONLINE)
                continue
            last = c.activity_ts
            if last <= 0:
                c.presence_rank = min(c.presence_rank, RANK_STALE)
                continue
            age = now - last
            if age <= config.PRESENCE_TTL:
                c.presence_rank = min(c.presence_rank, RANK_ACTIVE)
            elif age <= config.ACTIVITY_RANK_DAY:
                c.presence_rank = min(c.presence_rank, RANK_RECENT)
            else:
                c.presence_rank = min(c.presence_rank, RANK_STALE)
