"""Presence subpackage — activity + optional MTProto providers."""

from .base import PresenceProvider, RANK_ACTIVE, RANK_ONLINE, RANK_RECENT, RANK_STALE
from .manager import PresenceManager, get_manager, manager

__all__ = [
    "PresenceProvider",
    "PresenceManager",
    "get_manager",
    "manager",
    "RANK_ONLINE",
    "RANK_ACTIVE",
    "RANK_RECENT",
    "RANK_STALE",
]
