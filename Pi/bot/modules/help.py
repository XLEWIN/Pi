"""Help module — paginated inline menu of every module's commands.

Private chats get a THREE-part stack, in this order:

    [text]  menu header / module page text   (edited by id)
    [image] bot/assets/help_logo.png         (the Pi 3.14 logo)
    [buttons] colored grid + CLOSE/BACK      (attached to the image msg)

The header text is sent first so its message id can be embedded in every
callback as a trailing ``t<message_id>`` (e.g. ``help:open:fun:0:t123``).
Button labels are plain titles — the custom emoji renders once via
``icon_custom_emoji_id`` (no literal emoji in the label text).

In groups /help replies with a single DM-redirect button instead.

Callback data:
    help:main:<page>[:t<id>]       main menu page
    help:open:<key>:<page>[:t<id>] module page (page = caller's menu page)
    help:close[:t<id>]             delete the menu (both messages)
    help:start[:t<id>]             text → /start screen, image removed
    start:help                     handled in start.py → opens this menu

Callbacks WITHOUT the ``t`` suffix arrived on the text message itself
(photo send failed / legacy) — edited in place like before.
"""

from __future__ import annotations

import re
from html import escape
from pathlib import Path

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

from bot.command_handler import CommandHandler
from bot.constants import BOT_DESCRIPTION, HELP_MENU, START_TEXT
from bot.emojis import E, EID
from bot.keyboards.colored import (
    btn_danger,
    btn_default,
    btn_primary,
    btn_url,
    build_keyboard,
)
from bot.logger import logger

_TG_EMOJI_RE = re.compile(r'<tg-emoji emoji-id="\d+">.*?</tg-emoji>')
_EMOJI_ID_RE = re.compile(r'emoji-id="(\d+)"')
#: trailing ``t1234`` message-id segment in callback data
_TAIL_TID_RE = re.compile(r"t\d+")

#: 3 rows × 3 module buttons per page — matches the menu design.
PAGE_SIZE = 9
COLS = 3

# Fallback deep link; at runtime bot_data["username"] (post_init) wins.
_FALLBACK_USERNAME = "PiModulerBot"

# Pi 3.14 logo sitting between the menu text and its inline buttons.
_LOGO_PATH = Path(__file__).resolve().parent.parent / "assets" / "help_logo.png"


def _logo_path() -> Path | None:
    return _LOGO_PATH if _LOGO_PATH.is_file() else None


def _strip_custom_emoji(text: str) -> str:
    """Replace <tg-emoji> tags with their inner fallback emoji."""
    return _TG_EMOJI_RE.sub(
        lambda m: m.group(0).split(">", 1)[1].rsplit("</", 1)[0], text
    )


def _icon_id(icon_html: str) -> str | None:
    """Custom-emoji id from an E.* tag — for icon_custom_emoji_id buttons."""
    m = _EMOJI_ID_RE.search(icon_html)
    return m.group(1) if m else None


def _cb(data: str, tmid: int | None) -> str:
    """Embed the header-text message id so callbacks can edit both."""
    return f"{data}:t{tmid}" if tmid is not None else data


def _page_count() -> int:
    return (len(HELP_MENU) + PAGE_SIZE - 1) // PAGE_SIZE


def _clamp_page(page: int) -> int:
    return max(0, min(page, _page_count() - 1))


def _module(key: str) -> dict | None:
    for mod in HELP_MENU:
        if mod["key"] == key:
            return mod
    return None


def _bot_username(context: ContextTypes.DEFAULT_TYPE) -> str:
    name = None
    try:
        name = (context.bot_data or {}).get("username")
    except (AttributeError, TypeError):
        pass
    name = name or getattr(context.bot, "username", None)
    return name or _FALLBACK_USERNAME


# ── Page texts ───────────────────────────────────────────────────

def _page_text(page: int) -> str:
    lines = [
        f"{E.INFO} Help Menu",
        f"├ {E.FOLDER} Modules: {len(HELP_MENU)} · Page {page + 1}/{_page_count()}",
        f"├ {E.SPARKLE} Prefixes: / ! . # $ % &amp; ? — e.g. !help",
        "└ Pick a module to browse its commands",
    ]
    return "\n".join(lines)


def _module_text(mod: dict, page: int) -> str:
    first_cmd = (mod["sections"][0][1][0] or "/help").split()[0].lstrip("/")
    count = sum(len(cmds) for _, cmds in mod["sections"])
    lines = [
        f"{mod['icon']} {escape(mod['title'])}",
        f"├ {E.FOLDER} Commands: {count}",
        f"├ {E.SPARKLE} Prefixes: / ! . # $ % &amp; ? — e.g. !{first_cmd}",
        "└ Tap BACK to return to the menu",
    ]
    for header, cmds in mod["sections"]:
        lines.append("")
        if header:
            lines.append(f"<b>{escape(header)}</b>")
        lines.extend(cmds)
    if mod["notes"]:
        lines.append("")
        lines.extend(mod["notes"])
    return "\n".join(lines)


# ── Keyboards (colored, Bot API 9.4+ styling) ────────────────────

def main_menu_keyboard(page: int, *, tmid: int | None = None, icons: bool = True):
    """Module grid (3 per row) + nav row + CLOSE/BACK rows.

    Labels are plain titles — the custom emoji icon already shows, a
    literal emoji in the text would duplicate it.
    """
    page = _clamp_page(page)
    chunk = HELP_MENU[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
    rows = []
    for i in range(0, len(chunk), COLS):
        row = []
        for mod in chunk[i : i + COLS]:
            row.append(
                btn_primary(
                    mod["title"],
                    _cb(f"help:open:{mod['key']}:{page}", tmid),
                    icon_emoji_id=_icon_id(mod["icon"]) if icons else None,
                )
            )
        rows.append(row)

    nav = []
    if page > 0:
        nav.append(btn_default("<", _cb(f"help:main:{page - 1}", tmid)))
    if page < _page_count() - 1:
        nav.append(btn_default(">", _cb(f"help:main:{page + 1}", tmid)))
    if nav:
        rows.append(nav)

    rows.append(
        [
            btn_danger(
                "CLOSE", _cb("help:close", tmid),
                icon_emoji_id=EID.CROSS if icons else None,
            )
        ]
    )
    rows.append([btn_primary("BACK", _cb("help:start", tmid))])
    return build_keyboard(rows)


def module_keyboard(page: int, *, tmid: int | None = None, icons: bool = True):
    """CLOSE + BACK rows for a module page (BACK → caller's menu page)."""
    return build_keyboard(
        [
            [
                btn_danger(
                    "CLOSE", _cb("help:close", tmid),
                    icon_emoji_id=EID.CROSS if icons else None,
                )
            ],
            [btn_primary("BACK", _cb(f"help:main:{page}", tmid))],
        ]
    )


def _dm_keyboard(username: str, *, icons: bool = True):
    """Single deep-link row for the group redirect."""
    return build_keyboard(
        [
            [
                btn_url(
                    "Open in DM",
                    f"https://t.me/{username}",
                    icon_emoji_id=EID.USER if icons else None,
                )
            ]
        ]
    )


# ── Send / edit helpers (brand first, plain fallback) ────────────

async def _reply_menu(message, text: str, markup, plain_markup):
    """Reply with the branded payload; plain (emoji-stripped) on rejection.

    Returns the sent message (for its id) or None.
    """
    try:
        return await message.reply_text(
            text, parse_mode=ParseMode.HTML, reply_markup=markup
        )
    except Exception as e:
        # Never retry timeouts — that doubles the user-visible wait.
        if type(e).__name__ in {"TimedOut", "NetworkError"}:
            logger.warning(f"Help reply network issue: {e}")
            return None
        try:
            return await message.reply_text(
                _strip_custom_emoji(text),
                parse_mode=ParseMode.HTML,
                reply_markup=plain_markup,
            )
        except Exception as e2:
            logger.warning(f"Failed to send help: {e2}")
            return None


async def _send_logo(target, markup, plain_markup) -> bool:
    """Reply with the Pi logo photo carrying the menu buttons."""
    logo = _logo_path()
    if logo is None:
        logger.warning(f"Help logo missing at {_LOGO_PATH}")
        return False
    try:
        await target.reply_photo(str(logo), reply_markup=markup)
        return True
    except Exception as e:
        if type(e).__name__ in {"TimedOut", "NetworkError"}:
            logger.warning(f"Help logo network issue: {e}")
            return False
        try:
            await target.reply_photo(str(logo), reply_markup=plain_markup)
            return True
        except Exception as e2:
            logger.warning(f"Help logo send failed: {e2}")
            return False


async def _open_menu_pair(message, context) -> bool:
    """Send [header text][logo + buttons] as one visual stack."""
    text_msg = await _reply_menu(message, _page_text(0), None, None)
    if text_msg is None:
        return False
    tmid = getattr(text_msg, "message_id", None)
    markup = main_menu_keyboard(0, tmid=tmid)
    plain_markup = main_menu_keyboard(0, tmid=tmid, icons=False)
    if await _send_logo(message, markup, plain_markup):
        return True
    # No image (missing file / rejected): keep the menu usable by
    # putting its buttons on the header text instead.
    try:
        await text_msg.edit_message_reply_markup(reply_markup=markup)
    except Exception as e:
        logger.warning(f"Help menu buttons failed: {e}")
    return True


async def _edit_text_at(
    context, chat_id, message_id: int, text: str, markup, plain_markup
) -> None:
    """Edit the header-text message (the one above the logo)."""
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
        )
    except Exception as e:
        if type(e).__name__ in {"TimedOut", "NetworkError"}:
            logger.warning(f"Help text edit network issue: {e}")
            return
        if "message is not modified" in str(e):
            return
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=_strip_custom_emoji(text),
                parse_mode=ParseMode.HTML,
                reply_markup=plain_markup,
            )
        except Exception as e2:
            if "message is not modified" not in str(e2):
                logger.warning(f"Help text edit failed: {e2}")


async def _edit_markup(query, markup, plain_markup) -> None:
    """Replace the buttons on the message that carries them."""
    try:
        await query.edit_message_reply_markup(reply_markup=markup)
    except Exception as e:
        if type(e).__name__ in {"TimedOut", "NetworkError"}:
            logger.warning(f"Help markup edit network issue: {e}")
            return
        if "message is not modified" in str(e):
            return
        try:
            await query.edit_message_reply_markup(reply_markup=plain_markup)
        except Exception as e2:
            if "message is not modified" not in str(e2):
                logger.warning(f"Help markup edit failed: {e2}")


async def _edit_menu(query, text: str, markup, plain_markup) -> None:
    """In-place edit for callbacks that arrived on the text message itself."""
    try:
        await query.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=markup
        )
    except Exception as e:
        if type(e).__name__ in {"TimedOut", "NetworkError"}:
            logger.warning(f"Help edit network issue: {e}")
            return
        if "message is not modified" in str(e):
            return
        try:
            await query.edit_message_text(
                _strip_custom_emoji(text),
                parse_mode=ParseMode.HTML,
                reply_markup=plain_markup,
            )
        except Exception as e2:
            if "message is not modified" not in str(e2):
                logger.warning(f"Help edit failed: {e2}")


async def _answer(query, text: str | None = None, *, alert: bool = False) -> None:
    try:
        if text:
            await query.answer(text, show_alert=alert)
        else:
            await query.answer()
    except Exception:
        pass


async def _close(query) -> None:
    """Delete the button message; fall back to stripping its buttons."""
    try:
        await query.message.delete()
    except Exception:
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception as e:
            logger.debug(f"help close failed: {e}")


# ── Handlers ─────────────────────────────────────────────────────

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/help — DM: text+logo+menu stack; groups: DM-redirect button."""
    message = update.effective_message or update.message
    chat = update.effective_chat
    if message is None:
        return

    if chat is not None and chat.type != "private":
        username = _bot_username(context)
        text = (
            f"{E.INFO} Help Menu\n"
            f"├ {E.USER} Detail: Browse every command in the bot's DM\n"
            f"└ {E.ARROW} Tap the button below to continue"
        )
        await _reply_menu(
            message,
            text,
            _dm_keyboard(username),
            _dm_keyboard(username, icons=False),
        )
        return

    await _open_menu_pair(message, context)


async def show_main_menu(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Open the menu stack (start:help) — swaps out the clicked message."""
    query = update.callback_query
    await _answer(query)
    target = query.message
    if target is None:
        return
    sent = await _open_menu_pair(target, context)
    if sent:
        try:
            await target.delete()
        except Exception as e:
            logger.debug(f"help start-swap delete failed: {e}")


async def _handle_close(query, context, tmid: int | None) -> None:
    """Delete the button message and its header text (when separate)."""
    await _close(query)
    if tmid is None:
        return
    msg = query.message
    if msg is None or tmid == getattr(msg, "message_id", None):
        return
    try:
        await context.bot.delete_message(msg.chat.id, tmid)
    except Exception as e:
        logger.debug(f"help close header delete failed: {e}")


async def help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route help:* callbacks — pagination, drill-down, close, back."""
    query = update.callback_query
    if query is None:
        return
    data = query.data or ""
    if not data.startswith("help:"):
        return
    parts = data.split(":")

    # Trailing t<id> = header-text message id (absent on legacy/text-origin).
    tmid = None
    if len(parts) > 2 and _TAIL_TID_RE.fullmatch(parts[-1]):
        tmid = int(parts[-1][1:])
        parts = parts[:-1]

    route = parts[1] if len(parts) > 1 else ""

    if route == "close":
        await _answer(query)
        await _handle_close(query, context, tmid)
        return

    if route not in {"main", "open", "start"}:
        await _answer(query, "Unknown option", alert=True)
        return

    await _answer(query)

    if route == "main":
        raw = parts[2] if len(parts) > 2 and parts[2].isdigit() else "0"
        page = _clamp_page(int(raw))
        text = _page_text(page)
        markup = main_menu_keyboard(page, tmid=tmid)
        plain_markup = main_menu_keyboard(page, tmid=tmid, icons=False)
    elif route == "open":
        key = parts[2] if len(parts) > 2 else ""
        raw = parts[3] if len(parts) > 3 and parts[3].isdigit() else "0"
        page = _clamp_page(int(raw))
        mod = _module(key)
        if mod is None:
            logger.debug(f"help menu: unknown module {key!r}")
            return
        text = _module_text(mod, page)
        markup = module_keyboard(page, tmid=tmid)
        plain_markup = module_keyboard(page, tmid=tmid, icons=False)
    else:  # start — back to the /start screen
        # Local import — start.py delegates back to us inside its callback.
        from bot.modules.start import build_start_keyboard

        text = START_TEXT.format(
            fire=E.FIRE,
            username=f"@{_bot_username(context)}",
            description=BOT_DESCRIPTION,
            arrow=E.ARROW,
        )
        markup = build_start_keyboard()
        plain_markup = build_start_keyboard(icons=False)

    msg = query.message
    if route == "start" and tmid is not None and msg is not None:
        # Header text becomes the start screen; the logo message goes away.
        await _edit_text_at(
            context, msg.chat.id, tmid, text, markup, plain_markup
        )
        try:
            await msg.delete()
        except Exception:
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception as e:
                logger.debug(f"help start photo delete failed: {e}")
        return

    if tmid is not None and msg is not None:
        # Stack layout: edit header text by id, buttons on the clicked msg.
        await _edit_text_at(
            context, msg.chat.id, tmid, text, markup, plain_markup
        )
        await _edit_markup(query, markup, plain_markup)
        return

    # Text-origin callback (photo missing or legacy): edit in place.
    await _edit_menu(query, text, markup, plain_markup)


def setup(app: Application) -> list[str]:
    """Register this module's handlers. Returns route descriptions for the log."""
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(help_callback, pattern=r"^help:"))
    return ["/help", "help:* callbacks"]
