"""Candidate ordering — mode-specific sort keys.

Sort keys (lower sorts first; `key` returns values meant for min-order):

    online_first / all : presence rank → last activity → user_id
    recent             : last activity → user_id
    random             : shuffled (same set, arbitrary order)

Filtering (window, admins, bots, left users, max cap) lives in
member_registry; this module only decides ORDER.
"""

from __future__ import annotations

import random
from typing import List, Sequence

from .models import Candidate


def sort_key(candidate: Candidate, mode: str):
    """Tuple key — presence rank ascending (0 first), activity descending."""
    if mode == "recent":
        return (-candidate.activity_ts, candidate.user_id)
    # online_first + all: presence first, then activity, then id.
    return (
        candidate.presence_rank,      # 0 online sorts first (ascending)
        -candidate.activity_ts,
        candidate.user_id,
    )


def order(candidates: Sequence[Candidate], mode: str, *, seed=None) -> List[Candidate]:
    """Return candidates ordered per mode (stable for ties via user_id)."""
    items = list(candidates)
    if mode == "random":
        rng = random.Random(seed) if seed is not None else random
        rng.shuffle(items)
        return items
    items.sort(key=lambda c: sort_key(c, mode))
    return items


def apply_limit(candidates: Sequence[Candidate], max_mentions: int) -> List[Candidate]:
    """0 = unlimited; otherwise keep the top slice after ordering."""
    items = list(candidates)
    if max_mentions and max_mentions > 0:
        items = items[:max_mentions]
    return items
