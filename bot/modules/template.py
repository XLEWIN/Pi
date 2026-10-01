"""Template module — /template and /wear with sectioned inline selection.

/templates are grouped into two sections shown as two-column buttons:

* **Free** — milestone unlocks (global messages), listed with lock state
* **Fictional** — requirements announced later (stay locked)

Locked templates wear the premium lock emoji on their button and in the
list, and tapping one answers with the exact reason it is locked.
``/wear`` equips the owner-exclusive template (#19).
"""

import re

from aiogram import F
from aiogram.enums import ParseMode
from aiogram.types import CallbackQuery, Message

from bot.config import settings
from bot.database import db
from bot.emojis import E, EID
from bot.keyboards.colored import btn_danger, btn_default, btn_primary, btn_success, build_keyboard
from bot.pipeline import cmd, on
from bot.profile_templates import (
    SECTIONS,
    SECTION_TITLES,
    THEMES,
    check_unlock,
    templates_in_section,
)
from bot.reply import reply_text
from bot.async_bridge import adb

OWNER_TEMPLATE = 19


def _is_owner(user) -> bool:
    """Bot owner check (matches the pattern used by broadcast/bstats)."""
    return bool(user is not None and settings.owner_id
                and user.id == settings.owner_id)


# ============================================================
# KEYBOARDS
# ============================================================

def _main_buttons():
    """Two-column section buttons — [Free (n)] [Fictional (n)]."""
    row = []
    for i, section in enumerate(SECTIONS):
        title = SECTION_TITLES[section]
        n = len(templates_in_section(section))
        label = f"{title} ({n})"
        icon = EID.SPARKLE if i == 0 else EID.STAR
        maker = btn_primary if i == 0 else btn_success
        row.append(maker(label, f"template:sec:{section}", icon_emoji_id=icon))
    return build_keyboard([row])


def _section_buttons(section: str, global_messages: int, is_owner: bool):
    """Colored keyboard — one icon-tagged button per template in section.

    Colors keep the original mapping: #1 primary, evens success,
    remaining odds danger. Locked templates get the lock emoji icon.
    """
    rows = []
    row = []
    for tid, theme in templates_in_section(section).items():
        label = f"{tid}. {theme['name']}"
        data = f"template:{tid}"
        ok, _ = check_unlock(tid, global_messages, is_owner)
        icon = EID.SPARKLE if ok else EID.LOCK
        if tid == 1:
            btn = btn_primary(label, data, icon_emoji_id=icon)
        elif tid % 2 == 0:
            btn = btn_success(label, data, icon_emoji_id=icon)
        else:
            btn = btn_danger(label, data, icon_emoji_id=icon)
        row.append(btn)
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([btn_default("← Back", "template:sec:main")])
    return build_keyboard(rows)


# ============================================================
# TEXT BODIES
# ============================================================

def _active_line(active_name: str, active_id: int) -> str:
    return f"├ {E.CHECK} Active: <b>{active_name}</b> (#{active_id})"


def _main_body(active_name: str, active_id: int) -> str:
    return (
        f"{E.SPARKLE} <b>Rank Templates</b>\n"
        f"{_active_line(active_name, active_id)}\n"
        f"├ {E.INFO} Pick a category below\n"
        f"└ {E.SETTINGS} Usage: <code>/template &lt;number&gt;</code>"
        f" · Example: <code>/template 3</code>"
    )


def _section_line(tid: int, theme: dict, active_id: int,
                  global_messages: int, is_owner: bool) -> str:
    ok, reason = check_unlock(tid, global_messages, is_owner)
    if tid == active_id:
        mark = f" {E.CHECK} <b>(active)</b>"
    elif ok:
        mark = ""
    elif reason.endswith("coming soon."):
        mark = f" {E.LOCK} soon"
    elif reason == "Owner exclusive.":
        mark = f" {E.LOCK} owner"
    else:
        # "Needs 1,000 global messages." -> compact "1,000 GM"
        need = reason.split("Needs ", 1)[1].split(" ", 1)[0]
        mark = f" {E.LOCK} {need} GM"
    return f"├ {tid}. {theme['name']}{mark}"


def _section_body(section: str, active_name: str, active_id: int,
                  global_messages: int, is_owner: bool) -> str:
    title = SECTION_TITLES[section]
    lines = [
        f"{E.SPARKLE} <b>Rank Templates — {title}</b>",
        _active_line(active_name, active_id),
        f"├ {E.INFO} Styles: tap a button below",
    ]
    for tid, theme in templates_in_section(section).items():
        lines.append(_section_line(tid, theme, active_id,
                                   global_messages, is_owner))
    lines.append(
        f"└ {E.SETTINGS} Usage: <code>/template &lt;number&gt;</code>"
        f" · Example: <code>/template 3</code>"
    )
    return "\n".join(lines)


# ============================================================
# /template
# ============================================================

async def _rank_info(user_id: int) -> dict:
    return await adb(db.get_user_rank_info(user_id))


async def template_command(message: Message, args: list):
    """Handle /template — sections, list + inline selection, or direct set."""
    if message.chat.type != "private":
        await reply_text(
            message,
            f"{E.INFO} Use this command in my DM for privacy.",
            parse_mode=ParseMode.HTML,
        )
        return

    info = await _rank_info(message.from_user.id)
    active_id = info["template"]
    active_name = THEMES.get(active_id, THEMES[1])["name"]

    # /template <number> — direct selection with unlock enforcement
    if args:
        try:
            tid = int(args[0])
        except ValueError:
            tid = -1
        if tid not in THEMES:
            await reply_text(
                message,
                f"{E.ERROR} Unknown template — pick 1-{len(THEMES)}.",
                parse_mode=ParseMode.HTML,
            )
            return
        ok, reason = check_unlock(
            tid, info["global_messages"], _is_owner(message.from_user)
        )
        if not ok:
            await reply_text(
                message,
                f"{E.LOCK} <b>{THEMES[tid]['name']}</b> is locked — {reason}",
                parse_mode=ParseMode.HTML,
            )
            return
        await adb(db.set_template(message.from_user.id, tid))
        await reply_text(
            message,
            f"{E.CHECK} <b>Template Selected</b>\n"
            f"├ {E.SPARKLE} Style: <b>{THEMES[tid]['name']}</b> (#{tid})\n"
            f"├ {E.INFO} Applied to: your /rank card\n"
            f"└ {E.SETTINGS} Change anytime with /template",
            parse_mode=ParseMode.HTML,
        )
        return

    # default: main screen with section buttons
    await reply_text(
        message,
        _main_body(active_name, active_id),
        reply_markup=_main_buttons(),
        parse_mode=ParseMode.HTML,
    )


async def template_callback(query: CallbackQuery):
    """Handle template callbacks: section navigation + selection."""
    data = query.data
    if not data.startswith("template:"):
        return

    rest = data[len("template:"):]

    # ---- section navigation ----
    if rest.startswith("sec:"):
        section = rest[len("sec:"):]
        if section != "main" and section not in SECTIONS:
            await query.answer("Unknown section.", show_alert=True)
            return
        info = await _rank_info(query.from_user.id)
        active_id = info["template"]
        active_name = THEMES.get(active_id, THEMES[1])["name"]
        gm, owner = info["global_messages"], _is_owner(query.from_user)
        if section == "main":
            text, markup = _main_body(active_name, active_id), _main_buttons()
        else:
            text = _section_body(section, active_name, active_id, gm, owner)
            markup = _section_buttons(section, gm, owner)
        await query.answer()
        try:
            await query.message.edit_text(
                text, reply_markup=markup, parse_mode=ParseMode.HTML
            )
        except Exception:
            pass
        return

    # ---- template selection ----
    try:
        template_id = int(rest.split(":")[0])
    except (ValueError, IndexError):
        return

    if template_id not in THEMES:
        await query.answer("Invalid template.", show_alert=True)
        return

    info = await _rank_info(query.from_user.id)
    ok, reason = check_unlock(
        template_id, info["global_messages"], _is_owner(query.from_user)
    )
    if not ok:
        await query.answer(reason, show_alert=True)
        return

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


# ============================================================
# /wear — owner-exclusive template
# ============================================================

async def wear_command(message: Message):
    """Equip the owner-exclusive template (#19)."""
    if message.chat.type != "private":
        await reply_text(
            message,
            f"{E.INFO} Use this command in my DM for privacy.",
            parse_mode=ParseMode.HTML,
        )
        return

    if not _is_owner(message.from_user):
        await reply_text(
            message,
            f"{E.LOCK} That style is owner exclusive.",
            parse_mode=ParseMode.HTML,
        )
        return

    theme = THEMES[OWNER_TEMPLATE]
    await adb(db.set_template(message.from_user.id, OWNER_TEMPLATE))
    await reply_text(
        message,
        f"{E.CHECK} <b>Template Worn</b>\n"
        f"├ {E.SPARKLE} Style: <b>{theme['name']}</b> (#{OWNER_TEMPLATE})\n"
        f"├ {E.CROWN} Owner exclusive — others cannot equip it\n"
        f"└ {E.SETTINGS} Change anytime with /template",
        parse_mode=ParseMode.HTML,
    )


def setup() -> list[str]:
    """Register template commands."""
    on("message", template_command, flt=cmd("template"))
    on("message", wear_command, flt=cmd("wear"))
    on("callback_query", template_callback,
       flt=F.data.regexp(re.compile(r"^template:")))
    return ["/template", "/wear", "template:* callbacks"]
