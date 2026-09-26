"""Bind module checks — admin, membership cache, grace, gate matching."""

import logging
import time
from typing import Any, Dict, Optional, Tuple

from telegram import ChatMember, Message, User

from .config import FAIL_STATUSES, MEMBERSHIP_CACHE_TTL, PASS_STATUSES

logger = logging.getLogger(__name__)

# (channel_id, user_id) -> (is_member: bool, expires_at: float)
_member_cache: Dict[Tuple[int, int], Tuple[bool, float]] = {}


def invalidate_cache(channel_id: int, user_id: int) -> None:
    _member_cache.pop((channel_id, user_id), None)


def clear_cache() -> None:
    _member_cache.clear()


def _cache_put(channel_id: int, user_id: int, is_member: bool) -> None:
    # Bound cache size so long-running bots don't grow unbounded.
    if len(_member_cache) > 5000:
        _member_cache.clear()
    _member_cache[(channel_id, user_id)] = (is_member, time.monotonic() + MEMBERSHIP_CACHE_TTL)


def _cache_get(channel_id: int, user_id: int) -> Optional[bool]:
    hit = _member_cache.get((channel_id, user_id))
    if not hit:
        return None
    is_member, expires = hit
    if time.monotonic() > expires:
        _member_cache.pop((channel_id, user_id), None)
        return None
    return is_member


async def is_group_admin(bot, chat_id: int, user_id: int) -> bool:
    """True if user is admin/creator in the group."""
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator", ChatMember.ADMINISTRATOR, ChatMember.OWNER)
    except Exception:
        return False


async def is_bot_admin(bot, group_id: int) -> bool:
    """True if the bot itself can delete messages in the group."""
    try:
        member = await bot.get_chat_member(group_id, bot.id)
        if member.status not in ("administrator", "creator"):
            return False
        return bool(getattr(member, "can_delete_messages", True))
    except Exception:
        return False


async def fetch_channel_member(bot, channel_id: int, user_id: int) -> Tuple[str, bool]:
    """Fetch raw status + pass/fail for a user in the bound channel."""
    try:
        member = await bot.get_chat_member(channel_id, user_id)
        status = member.status
        if status in PASS_STATUSES:
            return status, True
        if status in FAIL_STATUSES:
            return status, False
        # Unknown/new status — treat non-left/kicked as member only if listed.
        return status, status in PASS_STATUSES
    except Exception as e:
        # User not found / not a member / channel inaccessible.
        logger.debug(f"channel member check failed ({channel_id}/{user_id}): {e}")
        return "left", False


async def is_channel_member(bot, channel_id: int, user_id: int, *, fresh: bool = False) -> bool:
    """Membership check with 30–60s cache. fresh=True bypasses cache."""
    if not fresh:
        cached = _cache_get(channel_id, user_id)
        if cached is not None:
            return cached
    _, ok = await fetch_channel_member(bot, channel_id, user_id)
    _cache_put(channel_id, user_id, ok)
    return ok


def in_grace(join_ts: Optional[float], grace_minutes: int) -> bool:
    """True if the user is still inside the configured grace window.

    Unknown join timestamp (never seen joining) → no grace (strict).
    """
    if grace_minutes <= 0:
        return False
    if join_ts is None:
        return False
    age = time.time() - join_ts
    return 0 <= age < (grace_minutes * 60)


def _has_link(message: Message) -> bool:
    text = message.text or message.caption or ""
    if re_search_link(text):
        return True
    for entities in (message.entities, message.caption_entities):
        if not entities:
            continue
        for ent in entities:
            if ent.type in ("url", "text_link"):
                return True
    return False


def re_search_link(text: str) -> bool:
    """Lightweight URL detect without importing re at module load twice."""
    lowered = text.lower()
    return (
        "http://" in lowered
        or "https://" in lowered
        or "t.me/" in lowered
        or "telegram.me/" in lowered
        or "www." in lowered
    )


def message_gates(message: Message) -> set:
    """Which gate keys this message matches (text/media/link/document/gif/audio/sticker)."""
    gates = set()
    m = message

    if m.photo or m.video or m.video_note:
        gates.add("media")
    if m.document:
        gates.add("document")
    if m.animation:
        gates.add("gif")
        gates.add("media")
    if m.audio or m.voice:
        gates.add("audio")
    if m.sticker:
        gates.add("sticker")
    if m.text or m.caption:
        # Pure text message (no media) counts as text gate.
        if not (m.photo or m.video or m.video_note or m.document or m.animation
                or m.audio or m.voice or m.sticker):
            gates.add("text")
        if _has_link(m):
            gates.add("link")
    elif _has_link(m):
        gates.add("link")

    return gates


def gate_enabled(settings: Dict[str, Any], gate_key: str) -> bool:
    col = {
        "text": "gate_text",
        "media": "gate_media",
        "link": "gate_link",
        "document": "gate_document",
        "gif": "gate_gif",
        "audio": "gate_audio",
        "sticker": "gate_sticker",
    }.get(gate_key)
    if not col:
        return False
    return bool(int(settings.get(col) or 0))


def should_enforce(
    settings: Dict[str, Any],
    message: Message,
    user: Optional[User],
    *,
    is_admin: bool,
    in_grace_window: bool,
) -> bool:
    """Decide whether this message should be gated (True → block/delete).

    Rules:
      • No binding / force_join off and no matching gate → False
      • Bots always ignored → False
      • Admin bypass ON + admin → False
      • Grace period → False
      • force_join master OR any matching enabled gate → True
    """
    if not settings or not settings.get("channel_id"):
        return False

    # Bots (including this bot) are never gated.
    if user is None:
        return False
    if getattr(user, "is_bot", False):
        return False

    if bool(int(settings.get("admin_bypass") or 1)) and is_admin:
        return False

    if in_grace_window:
        return False

    force = bool(int(settings.get("force_join") or 0))
    matched = message_gates(message)
    any_gate_on = any(gate_enabled(settings, g) for g in matched)

    # force_join is the master switch: when ON, every non-exempt message is gated.
    if force:
        return True

    # force_join OFF → only type-specific gates apply.
    return any_gate_on
