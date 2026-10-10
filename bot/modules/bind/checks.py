"""Bind module checks — admin, membership cache, grace, gate matching."""

import logging
import time
from typing import Any, Dict, Optional, Set, Tuple

from aiogram.enums import ChatMemberStatus
from aiogram.types import Message, User

from bot.pipeline import is_service_update

from .config import FAIL_STATUSES, MEMBERSHIP_CACHE_TTL, PASS_STATUSES

logger = logging.getLogger(__name__)

# (channel_id, user_id) -> (is_member: bool, expires_at: float)
_member_cache: Dict[Tuple[int, int], Tuple[bool, float]] = {}

# Membership answers, so a gate decision and a button press cannot drift
# apart or be spelled two different ways.
MEMBERSHIP_MEMBER = "member"
MEMBERSHIP_NOT_MEMBER = "not_member"
#: Telegram could not be asked at all (no rights / chat not visible).
#: Never treat this as "not a member" — that is how every message in a
#: group ends up deleted because of a mis-configured channel.
MEMBERSHIP_UNKNOWN = "unknown"

# channel_id -> (bot may call getChatMember there, expires_at)
_bot_verify_cache: Dict[int, Tuple[bool, float]] = {}
# A working probe rarely changes; a failing one should recover quickly so
# "I added the bot as channel admin" takes effect within a minute.
BOT_VERIFY_OK_TTL = 300.0
BOT_VERIFY_FAIL_TTL = 60.0

# Leave detection.  Telegram sends no update when someone leaves a
# channel, so the only place a leave can be noticed is the next
# membership check the gate performs.  We therefore remember the last
# non-unknown answer we gave about each (channel, user) and raise a
# one-shot flag when it flips member -> not_member.
_last_state: Dict[Tuple[int, int], str] = {}
#: (channel_id, user_id) with a leave not yet consumed by the gate.
_leave_pending: Set[Tuple[int, int]] = set()
#: Match the size guard on _member_cache so neither grows without bound.
_MAX_TRACKED = 5000


def invalidate_cache(channel_id: int, user_id: int) -> None:
    _member_cache.pop((channel_id, user_id), None)


def clear_cache() -> None:
    _member_cache.clear()
    _bot_verify_cache.clear()
    _last_state.clear()
    _leave_pending.clear()


def pop_leave(channel_id: int, user_id: int) -> bool:
    """True exactly once after membership flipped member → not_member.

    The gate calls this on every deletion decision: a freshly noticed
    leave has to drop the stored join stamp (otherwise the grace window
    computed from an old join keeps letting the leaver through) and is
    worth a log line.  Consumed on read, so someone who sends ten
    messages after leaving only trips it once.
    """
    key = (channel_id, user_id)
    if key not in _leave_pending:
        return False
    _leave_pending.discard(key)
    logger.info(
        "bind: user %s left channel %s — gating their messages in the "
        "group until they rejoin",
        user_id, channel_id,
    )
    return True


def _remember(channel_id: int, user_id: int, state: str) -> str:
    """Record ``state`` and raise the leave flag on member → not_member."""
    key = (channel_id, user_id)
    if len(_last_state) > _MAX_TRACKED:
        # A leave flag that can't be remembered is simply not noticed;
        # losing history beats unbounded growth in the hottest handler.
        _last_state.clear()
        _leave_pending.clear()
    previous = _last_state.get(key)
    _last_state[key] = state
    if state == MEMBERSHIP_NOT_MEMBER:
        if previous == MEMBERSHIP_MEMBER:
            _leave_pending.add(key)
    else:
        # Rejoined before the gate got around to noticing the leave.
        _leave_pending.discard(key)
    return state


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
        return member.status in (
            "administrator",
            "creator",
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR,
        )
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


async def bot_can_verify(bot, channel_id: int) -> bool:
    """True when Telegram will let *this* bot ask about ``channel_id``.

    Asking about the bot's own membership is the cheapest probe with a
    real answer: if it is refused, every per-user check would fail too,
    and those failures must not be read as "the user isn't a member".
    Probing once per channel (with a TTL) keeps this off the hot path.
    """
    now = time.monotonic()
    hit = _bot_verify_cache.get(channel_id)
    if hit is not None and now < hit[1]:
        return hit[0]

    try:
        await bot.get_chat_member(channel_id, bot.id)
        ok = True
    except Exception as e:
        ok = False
        logger.warning(
            f"bind: cannot verify membership in {channel_id} — "
            f"add me to that chat with rights to see members ({e})"
        )
    ttl = BOT_VERIFY_OK_TTL if ok else BOT_VERIFY_FAIL_TTL
    _bot_verify_cache[channel_id] = (ok, now + ttl)
    return ok


#: Error text that really does mean "this person is not in that chat".
#: Everything else that makes the call fail means *we could not ask* —
#: rate limits, network drops, lost rights — and must not be read as
#: absence, or one throttled lookup deletes a whole group's messages.
_ABSENT_MARKERS = (
    "user not found",
    "user_not_found",
    "not a participant",
    "user_not_participant",
    "participant_id_invalid",
)


async def fetch_channel_member(bot, channel_id: int, user_id: int) -> Tuple[str, bool]:
    """Fetch raw status + pass/fail for a user in the bound channel.

    Returns ``(status, is_member)``.  ``status`` is Telegram's own value
    for a real answer, or ``MEMBERSHIP_UNKNOWN`` when the question could
    not be answered at all — see :data:`_ABSENT_MARKERS` for why the
    distinction matters.
    """
    try:
        member = await bot.get_chat_member(channel_id, user_id)
    except Exception as e:
        text = str(e).lower()
        if any(marker in text for marker in _ABSENT_MARKERS):
            logger.debug(f"channel member check ({channel_id}/{user_id}): {e}")
            return "left", False
        logger.warning(
            f"bind: could not ask Telegram about {user_id} in {channel_id} — "
            f"treating as unknown rather than absent ({e})"
        )
        return MEMBERSHIP_UNKNOWN, False
    status = member.status
    if status in PASS_STATUSES:
        return status, True
    if status in FAIL_STATUSES:
        return status, False
    # Unknown/new status — only a listed status counts as membership.
    return status, False


async def membership_state(bot, channel_id: int, user_id: int, *, fresh: bool = False) -> str:
    """``member`` / ``not_member`` / ``unknown`` with a short cache.

    ``fresh=True`` bypasses the per-user cache (the "I've Joined" button
    must never trust it).  ``unknown`` short-circuits before any caching:
    a failed probe says nothing about the user, so it must never poison
    the cache either.

    Every resolved answer is run through :func:`_remember`, which is what
    turns "the next check says not a member" into a *noticed leave* —
    see :func:`pop_leave`.
    """
    if not await bot_can_verify(bot, channel_id):
        return MEMBERSHIP_UNKNOWN
    if not fresh:
        cached = _cache_get(channel_id, user_id)
        if cached is not None:
            return _remember(
                channel_id, user_id,
                MEMBERSHIP_MEMBER if cached else MEMBERSHIP_NOT_MEMBER,
            )
    status, ok = await fetch_channel_member(bot, channel_id, user_id)
    if status == MEMBERSHIP_UNKNOWN:
        # Could not ask.  Not cached (it says nothing about the user) and
        # not remembered as a leave — the gate fails open for this message.
        return MEMBERSHIP_UNKNOWN
    _cache_put(channel_id, user_id, ok)
    return _remember(
        channel_id, user_id,
        MEMBERSHIP_MEMBER if ok else MEMBERSHIP_NOT_MEMBER,
    )


async def is_channel_member(bot, channel_id: int, user_id: int, *, fresh: bool = False) -> bool:
    """Membership check with the short cache. fresh=True bypasses cache."""
    state = await membership_state(bot, channel_id, user_id, fresh=fresh)
    return state == MEMBERSHIP_MEMBER


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
    """Which gate keys this message matches.

    Covers every type Telegram can send: the specific gates
    (text/media/link/document/gif/audio/sticker) plus ``other``, the
    catch-all for polls, contacts, venues, locations, dice, games,
    invoices, giveaways and stories.  ``other`` only fires when nothing
    else matched, so a photo-with-caption never flips it too.
    """
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

    # Catch-all: nothing above matched, so this is a type with no gate of
    # its own.  Without it a gate-only chat gated text and let a poll
    # through.
    if not gates:
        gates.add("other")

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
        "other": "gate_other",
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
      • Channel / anonymous senders (sender_chat set) → False
      • Bots always ignored → False
      • Admin bypass ON + admin → False
      • Grace period → False
      • force_join master OR any matching enabled gate → True
    """
    if not settings or not settings.get("channel_id"):
        return False

    # A sender_chat means the message came from a channel or from an
    # admin posting anonymously — there is no human member behind it to
    # prompt, and deleting it would just strip a linked-channel post out
    # of the discussion group.  from_user for those is either the
    # channel itself or GroupAnonymousBot, both of which would otherwise
    # fall through to the bot check below and be "gated" every time.
    if getattr(message, "sender_chat", None) is not None:
        return False

    # Bots (including this bot) are never gated.
    if user is None:
        return False
    if getattr(user, "is_bot", False):
        return False

    # Status updates are not activity: force-join asks people to talk to
    # us, so it must wait until they actually say or send something.
    # Gating a join/leave/pin notice would delete the notice, and since
    # the gate raises StopChain it would also starve welcome/goodbye,
    # which read that very same update in group 10.
    if is_service_update(message):
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
