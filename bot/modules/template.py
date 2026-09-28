"""Template module — /template command with inline selection.

Lists the rank card templates in the house text style with numbered
colored buttons; tapping one (or sending /template <number>) applies it.
"""

import re

from aiogram import F
from aiogram.enums import ParseMode
from aiogram.types import CallbackQuery, Message

from bot.database import db
from bot.emojis import E, EID
from bot.keyboards.colored import btn_danger, btn_primary, btn_success, build_keyboard
from bot.pipeline import cmd, on
from bot.profile_templates import THEMES
from bot.reply import reply_text
from bot.async_bridge import adb


def _build_template_buttons():
    """Colored keyboard — one icon-tagged button per template.

    Colors keep the original mapping: #1 primary, evens success,
    remaining odds danger.
    """
    rows = []
    row = []
    for tid, theme in THEMES.items():
        label = f"{tid}. {theme['name']}"
        data = f"template:{tid}"
        if tid == 1:
            btn = btn_primary(label, data, icon_emoji_id=EID.SPARKLE)
        elif tid % 2 == 0:
            btn = btn_success(label, data, icon_emoji_id=EID.SPARKLE)
        else:
            btn = btn_danger(label, data, icon_emoji_id=EID.SPARKLE)
        row.append(btn)
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return build_keyboard(rows)


def _header(active_name: str, active_id: int) -> str:
    """Shared branded header — current selection always visible."""
    return (
        f"{E.SPARKLE} <b>Rank Templates</b>\n"
        f"├ {E.CHECK} Active: <b>{active_name}</b> (#{active_id})"
    )


async def template_command(message: Message):
    """Handle /template — list templates + inline selection (text only)."""
    if message.chat.type != "private":
        await reply_text(
            message,
            f"{E.INFO} Use this command in my DM for privacy.",
            parse_mode=ParseMode.HTML,
        )
        return

    # Current selection — same source as /rank (user_level.template).
    info = await adb(db.get_user_rank_info(message.from_user.id
    ))
    active_id = info["template"]
    active_name = THEMES.get(active_id, THEMES[1])["name"]

    theme_lines = "\n".join(
        f"│  {line.strip()}" for line in get_theme_list().splitlines()
    )
    await reply_text(
        message,
        f"{_header(active_name, active_id)}\n"
        f"├ {E.INFO} Styles: tap a button below\n"
        f"{theme_lines}\n"
        f"└ {E.SETTINGS} Usage: <code>/template &lt;number&gt;</code>"
        f" · Example: <code>/template 3</code>",
        reply_markup=_build_template_buttons(),
        parse_mode=ParseMode.HTML,
    )


async def template_callback(callback_query: CallbackQuery):
    """Handle template selection callback."""
    query = callback_query
    data = query.data

    if not data.startswith("template:"):
        return

    try:
        template_id = int(data.split(":")[1])
    except (ValueError, IndexError):
        return

    if template_id not in THEMES:
        await query.answer("Invalid template.", show_alert=True)
        return

    # Save template choice
    await adb(db.set_template(query.from_user.id, template_id))
    theme_name = THEMES[template_id]["name"]

    await query.answer(f"Template set to {theme_name}!", show_alert=False)

    # Update the message with confirmation (it's a text message now —
    # edit_message_text, not edit_message_caption; stale taps stay silent).
    try:
        await query.message.edit_text(
            f"{E.CHECK} <b>Template Selected</b>\n"
            f"├ {E.SPARKLE} Style: <b>{theme_name}</b> (#{template_id})\n"
            f"├ {E.INFO} Applied to: your /rank card\n"
            f"└ {E.SETTINGS} Change anytime with /template",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass


def get_theme_list():
    """Get formatted theme list."""
    lines = []
    for tid, t in THEMES.items():
        lines.append(f"  {tid}. {t['name']}")
    return "\n".join(lines)


def setup() -> list[str]:
    """Register template commands."""
    on("message", template_command, flt=cmd("template"))
    on("callback_query", template_callback,
       flt=F.data.regexp(re.compile(r"^template:")))
    return ["/template", "template:* callbacks"]
