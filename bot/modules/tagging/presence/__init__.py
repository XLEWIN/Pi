"""Presence subpackage — activity + optional MTProto providers."""

from .base import PresenceProvider, RANK_ACTIVE, RANK_ONLINE, RANK_RECENT, RANK_STALE
from .manager import PresenceManager, get_manager, manager

# NOTE: the attribute `manager` above is the singleton INSTANCE and
# shadows the `bot.modules.tagging.presence.manager` submodule for
# `from . import manager` lookups. To reach the module (or its
# `known_chat_ids` helper), use `from .manager import ...` instead.

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
