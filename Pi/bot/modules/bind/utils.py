"""Bind module helpers — channel parsing, formatting, background spawn."""

import asyncio
import re
from html import escape
from typing import Any, Dict, Optional, Tuple

from bot.emojis import E
from bot.logger import logger

from .config import DEFAULT_CUSTOM_MESSAGE, ON, OFF

# t.me links, @username, bare username, numeric chat id (-100… or positive).
_TME_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/(?:c/(\d+)|joinchat/[\w-]+)?/?(?:\+[\w-]+)?/?([\w]+)?",
    re.IGNORECASE,
)
_USERNAME_RE = re.compile(r"^@?([A-Za-z][A-Za-z0-9_]{4,31})$")
_CHAT_ID_RE = re.compile(r"^(-?\d{5,})$")


def parse_channel_ref(raw: str) -> Optional[Tuple[Optional[int], Optional[str], Optional[str]]]:
    """Parse a channel reference into (channel_id|None, username|None, link|None).

    Accepts:
      @username | username | https://t.me/username | t.me/username
      | -100xxxxxxxxxx | numeric id
    Returns (id, username, public_link) or None if unparseable.
    """
    if not raw:
        return None
    text = raw.strip().rstrip("/")

    # Numeric chat id
    m = _CHAT_ID_RE.match(text)
    if m:
        cid = int(m.group(1))
        return (cid, None, None)

    # t.me link
    m = _TME_RE.search(text)
    if m and m.group(2):
        username = m.group(2)
        if username.lower() in {"joinchat", "share", "addstickers", "addemoji"}:
            return None
        link = f"https://t.me/{username}"
        return (None, username, link)

    # @username or bare username
    m = _USERNAME_RE.match(text)
    if m:
        username = m.group(1)
        return (None, username, f"https://t.me/{username}")

    return None


def channel_display(settings: Dict[str, Any]) -> str:
    """Human-readable bound channel name for menus."""
    title = settings.get("channel_title")
    username = settings.get("channel_username")
    cid = settings.get("channel_id")
    if username:
        return f"@{username}" + (f" ({title})" if title and title != username else "")
    if title:
        return escape(str(title))
    if cid:
        return f"<code>{cid}</code>"
    return "Unknown channel"


def status_icon(on: bool) -> str:
    return ON if on else OFF


def format_grace(minutes: int) -> str:
    if minutes <= 0:
        return "OFF"
    if minutes == 1:
        return "1 minute"
    return f"{minutes} minutes"


def format_autodel(seconds: int) -> str:
    if seconds <= 0:
        return "OFF (keep)"
    if seconds == 1:
        return "1 second"
    return f"{seconds} seconds"


def render_custom_message(
    template: Optional[str],
    *,
    user_mention: str,
    user_id: int,
    group_title: str,
    channel_title: str,
    channel_link: str,
) -> str:
    """Fill placeholders in the force-join message. Falls back to default."""
    tpl = template or DEFAULT_CUSTOM_MESSAGE
    try:
        return tpl.format(
            user=user_mention,
            user_id=user_id,
            group=group_title,
            channel=channel_title,
            channel_link=channel_link,
        )
    except (KeyError, IndexError, ValueError):
        # Malformed template — degrade to default rather than crash.
        try:
            return DEFAULT_CUSTOM_MESSAGE.format(
                user=user_mention,
                user_id=user_id,
                group=group_title,
                channel=channel_title,
                channel_link=channel_link,
            )
        except Exception:
            return f"{user_mention}, join {channel_title} first."


def user_mention_html(user) -> str:
    """HTML mention that survives escaping."""
    if user is None:
        return "Someone"
    name = escape(user.full_name or user.first_name or "User")
    return f'<a href="tg://user?id={user.id}">{name}</a>'


def spawn(coro) -> None:
    """Fire-and-forget background task (same pattern as start.py)."""
    task = asyncio.create_task(coro)

    def _done(t: asyncio.Task) -> None:
        if t.cancelled():
            return
        exc = t.exception()
        if exc:
            logger.warning(f"bind background task failed: {exc}")

    task.add_done_callback(_done)


def flag(v: Any) -> bool:
    return bool(int(v or 0))


def indicator(v: Any) -> str:
    return status_icon(flag(v))
