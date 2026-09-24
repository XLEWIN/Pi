"""Member registry — join/leave tracking + candidate assembly.

Bot API cannot enumerate group members; the registry is built from what
the bot actually observes (messages, joins, optional MTProto sync).
Candidate pipeline for /all:

    fetch (non-left) → exclude bots/admins → window filter (mode-based)
    → presence enrich → order (mode) → max cap
"""

from __future__ import annotations

import time
from typing import List, Optional, Sequence, Set

from bot.logger import logger

from . import database as tdb, sorter
from .models import Candidate, TagSettings
from .presence import get_manager


def on_join(chat_id: int, user) -> None:
    """Immediate write — a join must be visible to the next /all."""
    name = " ".join(
        p for p in (
            getattr(user, "first_name", None) or "",
            getattr(user, "last_name", None) or "",
        ) if p
    ) or str(getattr(user, "id", "?"))
    try:
        tdb.mark_join(
            chat_id,
            user.id,
            username=getattr(user, "username", None),
            display_name=name,
            is_bot=bool(getattr(user, "bot", False)),
        )
    except Exception as e:
        logger.warning(f"Tagging join record failed: {e}")


def on_leave(chat_id: int, user) -> None:
    try:
        tdb.mark_leave(chat_id, user.id)
    except Exception as e:
        logger.warning(f"Tagging leave record failed: {e}")


def _row_to_candidate(row: dict) -> Candidate:
    return Candidate(
        user_id=int(row["user_id"]),
        display_name=row["display_name"] or str(row["user_id"]),
        username=row["username"],
        last_active_at=float(row["last_active_at"] or 0.0),
        first_seen=float(row["first_seen"] or 0.0),
        presence_at=float(row["presence_at"] or 0.0),
        is_bot=bool(row["is_bot"]),
    )


def window_allows(candidate: Candidate, settings: TagSettings, now: float) -> bool:
    """Window applies only to modes that filter by activity/recency."""
    if settings.mode not in ("online_first", "recent"):
        return True
    if settings.window_hours <= 0:
        return True
    cutoff = now - settings.window_hours * 3600
    return candidate.activity_ts >= cutoff


async def assemble_candidates(
    chat_id: int,
    settings: TagSettings,
    admin_ids: Set[int],
    *,
    now: Optional[float] = None,
    presence=None,
) -> List[Candidate]:
    """Full /all pipeline. Never raises on presence failures."""
    now = now if now is not None else time.time()
    presence = presence or get_manager()

    # Optional MTProto full sync in 'sync' registry mode.
    try:
        await presence.sync_members(chat_id, settings.registry_mode)
    except Exception as e:
        logger.warning(f"Tagging member sync skipped: {e}")

    rows = tdb.fetch_members(chat_id)
    candidates: List[Candidate] = []
    for row in rows:
        c = _row_to_candidate(row)
        if c.is_bot:
            continue
        if c.user_id in admin_ids:
            continue  # admins are never tagged
        if not window_allows(c, settings, now):
            continue
        candidates.append(c)

    await presence.enrich(chat_id, candidates, now, registry_mode=settings.registry_mode)
    ordered = sorter.order(candidates, settings.mode)
    return sorter.apply_limit(ordered, settings.max_mentions)


def mention_list(candidates: Sequence[Candidate]) -> List[str]:
    """Render ordered candidates as mention HTML strings."""
    from .mention_builder import build_mention

    return [build_mention(c.user_id, c.display_name) for c in candidates]
