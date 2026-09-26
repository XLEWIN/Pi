"""Permission checks for tagging commands and callbacks."""

from __future__ import annotations

from typing import Set

from bot.logger import logger

from .exceptions import AdminFetchError, NotAdminError

ADMIN_STATUSES = frozenset({"creator", "administrator"})


async def is_admin(chat_id: int, user_id: int, bot) -> bool:
    """Fresh get_chat_member check (never cached across invocations)."""
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except Exception as e:
        logger.warning(f"Tagging admin check failed for {user_id} in {chat_id}: {e}")
        return False
    return member.status in ADMIN_STATUSES


async def require_admin(chat_id: int, user_id: int, bot) -> None:
    """Raise NotAdminError unless the user administers the chat."""
    if not await is_admin(chat_id, user_id, bot):
        raise NotAdminError()


async def admin_ids(chat_id: int, bot) -> Set[int]:
    """getChatAdministrators once per /all — the authoritative exclusion set.

    Raises AdminFetchError on failure: without it we could not guarantee
    admins are excluded, and a mass tag must never ping admins by accident.
    """
    try:
        admins = await bot.get_chat_administrators(chat_id)
    except Exception as e:
        logger.warning(f"Tagging admin fetch failed for {chat_id}: {e}")
        raise AdminFetchError() from e
    return {m.user.id for m in admins if m.user is not None}
