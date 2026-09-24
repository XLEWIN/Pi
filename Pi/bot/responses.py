"""Pi Bot action reply cards — branded success/error layouts.

Style (structure inspired by community promote cards, emojis = Pi set only):

    ✅ PROMOTION SUCCESSFUL~

    👤 USER: Name (123456789)
    👑 PROMOTED BY: Actor
    💼 TITLE: ADMIN

All HTML uses bot.emojis.E / custom <tg-emoji> — never stock foreign emoji.
"""

from __future__ import annotations

from html import escape
from typing import Any, Iterable, Optional, Sequence, Tuple, Union

from bot.emojis import E

# (emoji_html, LABEL, value_html) — value already HTML-safe
Field = Tuple[str, str, str]

UserLike = Any  # telegram.User


def mention(user: Optional[UserLike]) -> str:
    """HTML <a> mention for a User (or plain placeholder)."""
    if user is None:
        return "Unknown"
    name = user.username or user.first_name or str(getattr(user, "id", "?"))
    name = escape(str(name))
    uid = getattr(user, "id", None)
    if uid is None:
        return name
    return f'<a href="tg://user?id={uid}">{name}</a>'


def user_label(user: Optional[UserLike]) -> str:
    """Display name + (id) — e.g. Robin Assistant (7796251789)."""
    if user is None:
        return "Unknown"
    first = getattr(user, "first_name", None) or ""
    last = getattr(user, "last_name", None) or ""
    full = getattr(user, "full_name", None) or ""
    display = " ".join(p for p in (first, last) if p) or full or getattr(user, "username", None)
    if not display:
        display = str(getattr(user, "id", "?"))
    display = escape(str(display))
    uid = getattr(user, "id", None)
    if uid is None:
        return display
    return f"{display} ({uid})"


def actor_label(user: Optional[UserLike]) -> str:
    """Actor for BY lines: prefer @username, else display name."""
    if user is None:
        return "System"
    if getattr(user, "username", None):
        return f"@{escape(user.username)}"
    first = getattr(user, "first_name", None) or ""
    last = getattr(user, "last_name", None) or ""
    name = " ".join(p for p in (first, last) if p) or getattr(user, "full_name", None)
    if name:
        return escape(str(name))
    return escape(str(getattr(user, "id", "System")))


def _header_line(icon: str, title: str) -> str:
    # Trailing ~ matches the reference card style; title always UPPER + bold.
    clean = escape(title.strip().upper())
    return f"{icon} <b>{clean}~</b>"


def action_card(
    title: str,
    fields: Sequence[Field],
    *,
    icon: Optional[str] = None,
) -> str:
    """Build a branded action card.

    title: e.g. "PROMOTION SUCCESSFUL" (forced UPPER)
    fields: list of (emoji_html, "LABEL", "value_html") — LABEL forced UPPER
    icon: header icon; defaults to first field icon or E.CHECK
    """
    head_icon = icon or (fields[0][0] if fields else E.CHECK)
    lines = [_header_line(head_icon, title), ""]
    for em, label, value in fields:
        lab = escape(label.strip().rstrip(":").upper())
        lines.append(f"{em} {lab}: {value}")
    return "\n".join(lines)


def success_card(
    title: str,
    fields: Sequence[Field],
    *,
    icon: Optional[str] = None,
) -> str:
    return action_card(title, fields, icon=icon or E.CHECK)


def error_card(title: str, detail: str, *, icon: Optional[str] = None) -> str:
    return action_card(
        title,
        [(E.INFO, "DETAIL", detail)],
        icon=icon or E.ERROR,
    )


# ── Field builders ─────────────────────────────────────────────

def field_user(user: Optional[UserLike]) -> Field:
    return (E.USER, "USER", user_label(user))


def field_by(user: Optional[UserLike], label: str = "BY") -> Field:
    """e.g. PROMOTED BY / BANNED BY / MUTED BY / WARNED BY"""
    return (E.CROWN, label, actor_label(user))


def field_reason(reason: Optional[str]) -> Field:
    text = escape(reason or "No reason provided")
    return (E.INFO, "REASON", text)


def field_duration(duration: Optional[str]) -> Field:
    text = escape(str(duration)) if duration else "Permanent"
    return (E.TIME, "DURATION", text)


def field_extra(emoji: str, label: str, value: str) -> Field:
    return (emoji, label, value)


def field_count(current: int, limit: int) -> Field:
    return (E.WARN, "WARNINGS", f"{current}/{limit}")


def field_title(role: str) -> Field:
    return (E.SETTINGS, "TITLE", escape(str(role).upper()))


def field_status(value: str) -> Field:
    return (E.INFO, "STATUS", escape(str(value).upper()))


# ── Common one-liners (still branded) ──────────────────────────

def plain_error(msg: str) -> str:
    return f"{E.ERROR} {msg}"


def plain_ok(msg: str) -> str:
    return f"{E.CHECK} {msg}"


# ── Inline buttons under action cards ───────────────────────────

def card_keyboard(user: Optional[UserLike] = None):
    """Colored keyboard under an action card: View Profile (if user) + Close."""
    from bot.emojis import EID
    from bot.keyboards.colored import btn_danger, btn_url, build_keyboard

    rows = []
    uid = getattr(user, "id", None) if user is not None else None
    if uid:
        rows.append(
            [btn_url("View Profile", f"tg://user?id={uid}", icon_emoji_id=EID.USER)]
        )
    rows.append(
        [btn_danger("Close", "card:close", icon_emoji_id=EID.CROSS)]
    )
    return build_keyboard(rows)


async def reply_card(message, text: str, *, user: Optional[UserLike] = None):
    """Reply with a card body + its standard inline buttons."""
    return await message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=card_keyboard(user),
    )
