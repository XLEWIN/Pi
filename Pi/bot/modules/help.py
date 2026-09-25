"""Help module — single-message inline menu of every module's commands.

Structure follows Forge's /data command pattern:

* ONE message at a time: a plain **text** message carrying the menu and
  the inline buttons (no photo/media attached).
* Every callback edits that same message in place — ``edit_message_text``
  for the normal case, ``edit_message_caption`` kept as a compatibility
  path for menus sent by older versions as photo captions — never
  sending follow-ups.
* ``_build_*`` message/keyboard builders + a single ``help_callback``
  router that splits ``help:<action>:<args>`` callback data.
* ``_safe_edit`` mirrors Forge's ``_safe_edit_message``: brand first,
  plain (emoji-stripped) fallback, "message is not modified" ignored.

Page note: module sections are greedily packed into sub-pages
(``_module_chunks``) so every page stays comfortably short to read
(1024 visible characters), and the module keyboard shows < / > when a
module spans more than one page.

In groups /help replies with the same single text message (carrying a
DM-redirect button).

Callback data:
    help:main:<page>                  main menu page
    help:open:<key>:<menu_page>:<sub> module page (menu_page = caller's grid
                                      page for BACK, sub = content sub-page)
    help:close                        delete the menu message
    help:start                        edit the menu message → /start screen
    start:help                        handled in start.py → opens this menu
"""

from __future__ import annotations

import re
from html import escape, unescape

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
_TAG_RE = re.compile(r"<[^>]+>")

#: Page-packing cap — visible characters per page (Telegram text messages
#: allow 4096; pages are kept short so each fits one comfortable screen).
CAPTION_LIMIT = 1024
#: Slack kept below the cap to absorb footer/page-indicator variance.
_CAPTION_SLACK = 16

#: 3 rows × 3 module buttons per page — matches the menu design.
PAGE_SIZE = 9
COLS = 3

#: Callback data prefix (Forge uses ``_CB = "d"``).
_CB = "help"

# Fallback deep link; at runtime bot_data["username"] (post_init) wins.
_FALLBACK_USERNAME = "PiModulerBot"


# ── Small helpers ────────────────────────────────────────────────

def _strip_custom_emoji(text: str) -> str:
    """Replace <tg-emoji> tags with their inner fallback emoji."""
    return _TG_EMOJI_RE.sub(
        lambda m: m.group(0).split(">", 1)[1].rsplit("</", 1)[0], text
    )


def _visible_len(text: str) -> int:
    """Length Telegram counts against the caption limit (tags/entities resolved)."""
    return len(unescape(_TAG_RE.sub("", text)))


def _icon_id(icon_html: str) -> str | None:
    """Custom-emoji id from an E.* tag — for icon_custom_emoji_id buttons."""
    m = _EMOJI_ID_RE.search(icon_html)
    return m.group(1) if m else None


def _int_at(parts: list[str], index: int) -> int:
    return int(parts[index]) if len(parts) > index and parts[index].isdigit() else 0


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


# ── Message builders ─────────────────────────────────────────────

def _build_main_message(page: int) -> str:
    """Main menu header text (caption)."""
    return "\n".join(
        [
            f"{E.INFO} Help Menu",
            f"├ {E.FOLDER} Modules: {len(HELP_MENU)} · Page {page + 1}/{_page_count()}",
            f"├ {E.SPARKLE} Prefixes: / ! . # $ % &amp; ? — e.g. !help",
            "└ Pick a module to browse its commands",
        ]
    )


def _module_header(mod: dict, sub: int, total: int) -> list[str]:
    """Module page header lines (title, count, prefixes, footer)."""
    first_cmd = (mod["sections"][0][1][0] or "/help").split()[0].lstrip("/")
    count = sum(len(cmds) for _, cmds in mod["sections"])
    footer = (
        f"└ {E.ARROW} Page {sub}/{total} · tap BACK for the menu"
        if total > 1
        else f"└ {E.ARROW} Tap BACK to return to the menu"
    )
    return [
        f"{mod['icon']} {escape(mod['title'])}",
        f"├ {E.FOLDER} Commands: {count}",
        f"├ {E.SPARKLE} Prefixes: / ! . # $ % &amp; ? — e.g. !{first_cmd}",
        footer,
    ]


def _module_chunks(mod: dict) -> list[list[tuple[str | None, list[str]]]]:
    """Greedily pack sections (+ notes) so each page fits one caption.

    Budget uses the widest possible header (worst-case page indicator),
    so no rendered page can exceed CAPTION_LIMIT visible characters.
    """
    worst_header = "\n".join(_module_header(mod, 99, 99))
    budget = CAPTION_LIMIT - _visible_len(worst_header) - _CAPTION_SLACK

    pages: list[list[tuple[str | None, list[str]]]] = [[]]
    used = 0

    def push(header: str | None, body: list[str]) -> None:
        nonlocal used
        size = _visible_len("\n".join(([header] if header else []) + body)) + 1
        if pages[-1] and used + size > budget:
            pages.append([])
            used = 0
        pages[-1].append((header, body))
        used += size

    for header, cmds in mod["sections"]:
        push(header, list(cmds))
    if mod["notes"]:
        push(None, list(mod["notes"]))
    return pages


def _module_page_count(mod: dict) -> int:
    return len(_module_chunks(mod))


def _build_module_message(mod: dict, sub: int) -> str:
    """Module page caption — one content sub-page at a time."""
    pages = _module_chunks(mod)
    total = len(pages)
    sub = max(0, min(sub, total - 1))
    lines = _module_header(mod, sub + 1, total)
    for header, body in pages[sub]:
        lines.append("")
        if header:
            lines.append(f"<b>{escape(header)}</b>")
        lines.extend(body)
    return "\n".join(lines)


def _build_start_message(username: str) -> str:
    """The /start screen, rendered as this message's new caption."""
    return START_TEXT.format(
        fire=E.FIRE,
        username=f"@{username}",
        description=BOT_DESCRIPTION,
        arrow=E.ARROW,
    )


# ── Keyboards (colored, Bot API 9.4+ styling) ────────────────────

def main_menu_keyboard(page: int, *, icons: bool = True):
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
                    f"{_CB}:open:{mod['key']}:{page}:0",
                    icon_emoji_id=_icon_id(mod["icon"]) if icons else None,
                )
            )
        rows.append(row)

    nav = []
    if page > 0:
        nav.append(btn_default("<", f"{_CB}:main:{page - 1}"))
    if page < _page_count() - 1:
        nav.append(btn_default(">", f"{_CB}:main:{page + 1}"))
    if nav:
        rows.append(nav)

    rows.append(
        [
            btn_danger(
                "CLOSE", f"{_CB}:close",
                icon_emoji_id=EID.CROSS if icons else None,
            )
        ]
    )
    rows.append([btn_primary("BACK", f"{_CB}:start")])
    return build_keyboard(rows)


def module_keyboard(key: str, menu_page: int, sub: int, *, icons: bool = True):
    """Content nav (< / >, when the module spans pages) + CLOSE + BACK."""
    mod = _module(key)
    total = _module_page_count(mod) if mod else 1

    rows = []
    if total > 1:
        nav = []
        if sub > 0:
            nav.append(btn_default("<", f"{_CB}:open:{key}:{menu_page}:{sub - 1}"))
        if sub < total - 1:
            nav.append(btn_default(">", f"{_CB}:open:{key}:{menu_page}:{sub + 1}"))
        if nav:
            rows.append(nav)

    rows.append(
        [
            btn_danger(
                "CLOSE", f"{_CB}:close",
                icon_emoji_id=EID.CROSS if icons else None,
            )
        ]
    )
    rows.append([btn_primary("BACK", f"{_CB}:main:{_clamp_page(menu_page)}")])
    return build_keyboard(rows)


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


# ── Send / edit helpers (Forge-style, brand first) ───────────────

async def _send_text(target, text: str, markup, plain_markup) -> bool:
    """Send ONE plain text message with the menu buttons attached.

    Brand first, plain (emoji-stripped) fallback; network issues are
    never retried (a retry would double the user-visible wait).
    """
    last: Exception | None = None
    for body, kb in ((text, markup), (_strip_custom_emoji(text), plain_markup)):
        try:
            await target.reply_text(
                body, parse_mode=ParseMode.HTML, reply_markup=kb
            )
            return True
        except Exception as e:
            if type(e).__name__ in {"TimedOut", "NetworkError"}:
                logger.warning(f"Help reply network issue: {e}")
                return False
            logger.debug(f"Help text attempt failed: {e}")
            last = e
    logger.warning(f"Failed to send help: {last}")
    return False


async def _send_menu(target) -> bool:
    """Open the main menu as the single menu message."""
    return await _send_text(
        target,
        _build_main_message(0),
        main_menu_keyboard(0),
        main_menu_keyboard(0, icons=False),
    )


async def _safe_edit(query, text: str, markup, plain_markup) -> None:
    """Edit the menu message in place — text, or caption for legacy photos.

    Menus are sent as plain text; older versions sent them as a photo
    caption, so that branch is kept for messages still on screen.

    Brand first, plain (emoji-stripped) fallback; "message is not
    modified" is a no-op (Forge's ``_safe_edit_message``).
    """
    msg = query.message
    is_photo = bool(getattr(msg, "photo", None))
    last: Exception | None = None
    for body, kb in ((text, markup), (_strip_custom_emoji(text), plain_markup)):
        try:
            if is_photo:
                await query.edit_message_caption(
                    caption=body,
                    parse_mode=ParseMode.HTML,
                    reply_markup=kb,
                )
            else:
                await query.edit_message_text(
                    body, parse_mode=ParseMode.HTML, reply_markup=kb
                )
            return
        except Exception as e:
            if type(e).__name__ in {"TimedOut", "NetworkError"}:
                logger.warning(f"Help edit network issue: {e}")
                return
            if "message is not modified" in str(e):
                return
            last = e
    logger.warning(f"Help edit failed: {last}")


async def _answer(query, text: str | None = None, *, alert: bool = False) -> None:
    try:
        if text:
            await query.answer(text, show_alert=alert)
        else:
            await query.answer()
    except Exception:
        pass


async def _close(query) -> None:
    """Delete the menu message; fall back to stripping its buttons."""
    try:
        await query.message.delete()
    except Exception:
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception as e:
            logger.debug(f"help close failed: {e}")


# ── Handlers ─────────────────────────────────────────────────────

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/help — DM: menu text message; groups: DM-redirect text message."""
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
        await _send_text(
            message,
            text,
            _dm_keyboard(username),
            _dm_keyboard(username, icons=False),
        )
        return

    await _send_menu(message)


async def show_main_menu(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Open the menu (start:help) — swaps out the clicked message."""
    query = update.callback_query
    await _answer(query)
    target = query.message
    if target is None:
        return
    if await _send_menu(target):
        try:
            await target.delete()
        except Exception as e:
            logger.debug(f"help start-swap delete failed: {e}")


async def help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route help:* callbacks — pagination, drill-down, close, back."""
    query = update.callback_query
    if query is None:
        return
    data = query.data or ""
    if not data.startswith(f"{_CB}:"):
        return
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "close":
        await _answer(query)
        await _close(query)
        return

    if action == "main":
        await _answer(query)
        page = _clamp_page(_int_at(parts, 2))
        await _safe_edit(
            query,
            _build_main_message(page),
            main_menu_keyboard(page),
            main_menu_keyboard(page, icons=False),
        )
        return

    if action == "open":
        key = parts[2] if len(parts) > 2 else ""
        mod = _module(key)
        if mod is None:
            logger.debug(f"help menu: unknown module {key!r}")
            await _answer(query, "Unknown option", alert=True)
            return
        await _answer(query)
        menu_page = _clamp_page(_int_at(parts, 3))
        total = _module_page_count(mod)
        sub = max(0, min(_int_at(parts, 4), total - 1))
        await _safe_edit(
            query,
            _build_module_message(mod, sub),
            module_keyboard(key, menu_page, sub),
            module_keyboard(key, menu_page, sub, icons=False),
        )
        return

    if action == "start":
        # Local import — start.py delegates back to us inside its callback.
        from bot.modules.start import build_start_keyboard

        await _answer(query)
        await _safe_edit(
            query,
            _build_start_message(_bot_username(context)),
            build_start_keyboard(),
            build_start_keyboard(icons=False),
        )
        return

    await _answer(query, "Unknown option", alert=True)


def setup(app: Application) -> list[str]:
    """Register this module's handlers. Returns route descriptions for the log."""
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(help_callback, pattern=rf"^{_CB}:"))
    return ["/help", "help:* callbacks"]
