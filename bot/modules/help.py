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

Renderers
---------
There are TWO renderers for the same content:

* **Rich** (default, ``HELP_RICH``) — Bot API 10.1+ Rich Messages via
  ``bot/rich.py``: H1/H2/H3 headings, a real ``RichBlockTable`` for the
  command grid (no NBSP column padding), dividers, footers and the
  module grid as in-message ``RichBlockButtons``.
* **HTML** — the original renderer, kept as the automatic fallback and
  reachable by setting ``HELP_RICH = False``.

Both share ONE paginator (``_module_chunks`` yields
``(header, rendered, raw, kind)``), so ``sub`` in the callback data
indexes the same page on either path — a rich→HTML fallback can never
show a different page than it started on, and the HTML fallback is
handed the FULL module keyboard (on the rich path that grid lives in
the body instead).

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

from aiogram import Bot, F
from aiogram.enums import ParseMode
from aiogram.types import CallbackQuery, Message

from bot.constants import BOT_DESCRIPTION, HELP_MENU, START_TEXT
from bot.pipeline import cmd, on
from bot.reply import reply_text
from bot.emojis import E, EID
from bot import rich as R
from bot.keyboards.colored import (
    btn_danger,
    btn_default,
    btn_primary,
    btn_url,
    build_keyboard,
)
from bot.logger import logger

#: Render the menu with Bot API 10.1+ Rich Messages (structured blocks,
#: real headings, a real table grid, in-message buttons).  Set to False
#: to force the legacy HTML renderer — every page still works either way
#: because a rich failure falls back to HTML automatically.
HELP_RICH = True

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

#: Grid layout (owner's table screenshot): every command line renders as
#: ``<code>command padded</code>Description`` so descriptions start at one
#: x position. Padding uses non-breaking spaces inside the code cell.
_NBSP = "\u00A0"
#: Command column cap — commands longer than this keep a single gap
#: instead of being padded (they'd blow the row past the screen edge).
GRID_COL_CAP = 30

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


# ── Grid rows (command | description columns) ────────────────────

def _split_row(line: str) -> tuple[str, str]:
    """``/cmd args — Description`` → ``("/cmd args", "Description")``."""
    left, sep, right = line.partition(" — ")
    if not sep:
        return line, ""
    return left, right


def _cell(text: str, width: int) -> str:
    """Pad *text* with NBSPs so every cell ends at *width* + 1.

    The extra column is the gap between the code cell and the
    description — without it, exact-width commands would run into their
    descriptions while shorter ones keep a gap (one-column skew).
    """
    return text + _NBSP * max(width - _visible_len(text) + 1, 1)


def _grid_row(left: str, right: str, width: int) -> str:
    """One table row: padded command cell + description."""
    row = f"<code>{_cell(left, width)}</code>"
    return row + right if right else row


def _module_width(mod: dict) -> int:
    """Column width for a module — widest command cell, capped."""
    lefts = [
        _split_row(line)[0]
        for _, cmds in mod["sections"]
        for line in cmds
    ]
    return min(max((_visible_len(left) for left in lefts), default=0), GRID_COL_CAP)


def _col_header(width: int) -> str:
    """Bold column header row — the table's "Metric | Value" line."""
    return f"<code>{_cell('Command', width)}</code><b>Description</b>"


def _bot_username(bot: Bot, bot_data: dict) -> str:
    name = None
    try:
        name = (bot_data or {}).get("username")
    except (AttributeError, TypeError):
        pass
    name = name or getattr(bot, "username", None)
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


def _module_chunks(
    mod: dict,
) -> list[list[tuple[str | None, list[str], list[str], str]]]:
    """Greedily pack sections (+ notes) so each page fits one caption.

    Budget uses the widest possible header (worst-case page indicator +
    the grid column-header row), and command bodies are measured in
    their RENDERED grid form, so no rendered page can exceed
    CAPTION_LIMIT visible characters.

    Each entry is ``(header, rendered_rows, raw_lines, kind)`` where
    ``kind`` is ``"section"`` or ``"notes"``.  The rendered rows drive
    the HTML message, the raw lines drive the Rich Message table — both
    come out of ONE packer so ``sub`` in the callback data means the
    same page on either path, and a rich→HTML fallback can never show a
    different page than it started on.
    """
    width = _module_width(mod)
    worst_header = "\n".join(_module_header(mod, 99, 99))
    if any(cmds for _, cmds in mod["sections"]):
        worst_header += "\n" + _col_header(width)
    budget = CAPTION_LIMIT - _visible_len(worst_header) - _CAPTION_SLACK

    pages: list[list[tuple[str | None, list[str], list[str], str]]] = [[]]
    used = 0

    def push(header: str | None, raw: list[str], kind: str) -> None:
        nonlocal used
        # Notes are prose and go in as-is; only command rows get the
        # <code>|description grid treatment (matches pre-rich output).
        body = (
            list(raw)
            if kind == "notes"
            else [_grid_row(*_split_row(line), width) for line in raw]
        )
        size = _visible_len("\n".join(([header] if header else []) + body)) + 1
        if pages[-1] and used + size > budget:
            pages.append([])
            used = 0
        pages[-1].append((header, body, raw, kind))
        used += size

    for header, cmds in mod["sections"]:
        push(header, list(cmds), "section")
    if mod["notes"]:
        push(None, list(mod["notes"]), "notes")
    return pages


def _module_page_count(mod: dict) -> int:
    return len(_module_chunks(mod))


def _build_module_message(mod: dict, sub: int) -> str:
    """Module page caption — grid rows, one content sub-page at a time."""
    pages = _module_chunks(mod)
    total = len(pages)
    sub = max(0, min(sub, total - 1))
    width = _module_width(mod)
    lines = _module_header(mod, sub + 1, total)
    wrote_col_header = False
    for header, body, _raw, _kind in pages[sub]:
        lines.append("")
        if header:
            lines.append(f"<b>{escape(header)}</b>")
        if body and body[0].startswith("<code>") and not wrote_col_header:
            lines.append(_col_header(width))
            wrote_col_header = True
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


# ── Rich Message builders (Bot API 10.1+) ───────────────────────
#
# Rich Messages are NOT HTML: no parse_mode, no <b>/<code>.  Every line
# from HELP_MENU (authored as HTML) goes through R.html_to_rich, which
# unwraps tags, unescapes entities and converts <tg-emoji> into a real
# custom-emoji node.  The command grid becomes a RichBlockTable with a
# real header row — no NBSP padding, the table aligns the columns.

def _rich_title(icon_html: str, title: str, size: int) -> dict:
    """Heading block carrying the module's custom-emoji icon + title."""
    return R.heading(R.html_to_rich(f"{icon_html} {escape(title)}"), size)


def _rich_grid(raw: list[str]) -> dict:
    """``/cmd args — Description`` lines → one RichBlockTable."""
    rows: list[list[dict]] = [
        [R.cell("Command", header=True), R.cell("Description", header=True)]
    ]
    for line in raw:
        left, right = _split_row(line)
        rows.append([
            R.cell(R.code(R.strip_html(left))),
            R.cell(R.html_to_rich(right) if right else " "),
        ])
    return R.table(rows)


def _rich_main_blocks(page: int) -> list[dict]:
    """Main menu: H1 title, metadata paragraphs, 3-wide module grid, footer."""
    page = _clamp_page(page)
    mods = HELP_MENU[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
    footer_text = (
        f"Page {page + 1}/{_page_count()} · tap CLOSE to dismiss"
        if _page_count() > 1
        else "tap CLOSE to dismiss"
    )
    blocks: list[dict] = [
        _rich_title(E.INFO, "Help Menu", 1),
        R.paragraph(R.html_to_rich(
            f"{E.FOLDER} Modules: {len(HELP_MENU)} · "
            f"Page {page + 1}/{_page_count()}"
        )),
        R.paragraph(R.html_to_rich(
            f"{E.SPARKLE} Prefixes: / ! . # $ % &amp; ? — e.g. !help"
        )),
        R.divider(),
        R.paragraph(R.html_to_rich(
            f"{E.ARROW} Pick a module to browse its commands"
        )),
        R.heading(R.html_to_rich(f"{E.FOLDER} Modules"), 2),
    ]
    for i in range(0, len(mods), COLS):
        blocks.append(
            R.buttons_block(
                [
                    R.button(
                        mod["title"],
                        callback_data=f"{_CB}:open:{mod['key']}:{page}:0",
                        style="primary",
                    )
                    for mod in mods[i : i + COLS]
                ],
                align="left",
            )
        )
    blocks.append(R.divider())
    blocks.append(R.footer(R.html_to_rich(footer_text)))
    return blocks


def _rich_module_blocks(mod: dict, sub: int) -> list[dict]:
    """Module page: H1 title, metadata, H3 section headings + table grid."""
    pages = _module_chunks(mod)
    total = len(pages)
    sub = max(0, min(sub, total - 1))
    count = sum(len(cmds) for _, cmds in mod["sections"])
    first_cmd = (mod["sections"][0][1][0] or "/help").split()[0].lstrip("/")

    blocks: list[dict] = [
        _rich_title(mod["icon"], mod["title"], 1),
        R.paragraph(R.html_to_rich(f"{E.FOLDER} Commands: {count}")),
        R.paragraph(R.html_to_rich(
            f"{E.SPARKLE} Prefixes: / ! . # $ % &amp; ? — e.g. !{first_cmd}"
        )),
        R.divider(),
    ]
    for header, _body, raw, kind in pages[sub]:
        if kind == "notes":
            for note in raw:
                blocks.append(R.paragraph(R.html_to_rich(note)))
            continue
        if header:
            blocks.append(R.heading(R.html_to_rich(escape(header)), 3))
        if raw:
            blocks.append(_rich_grid(raw))

    footer_text = (
        f"Page {sub + 1}/{total} · tap BACK for the menu"
        if total > 1
        else "tap BACK to return to the menu"
    )
    blocks.append(R.divider())
    blocks.append(R.footer(R.html_to_rich(footer_text)))
    return blocks


def _rich_group_blocks(username: str) -> list[dict]:
    """The group stub: one heading, one line, one deep-link button."""
    return [
        _rich_title(E.INFO, "Help Menu", 1),
        R.paragraph(R.html_to_rich(
            f"{E.USER} Detail: Browse every command in the bot's DM"
        )),
        R.paragraph(R.html_to_rich(f"{E.ARROW} Tap the button below to continue")),
        R.divider(),
        R.buttons_block(
            [R.button("Open in DM", url=f"https://t.me/{username}?start=help")],
            align="center",
        ),
    ]


# ── Keyboards (colored, Bot API 9.4+ styling) ────────────────────

def main_menu_keyboard(page: int, *, icons: bool = True):
    """Module grid (3 per row) + nav row + CLOSE row.

    Labels are plain titles — the custom emoji icon already shows, a
    literal emoji in the text would duplicate it.  No BACK row: the
    owner asked for it to be removed (CLOSE dismisses the menu).
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
    return build_keyboard(rows)


def main_menu_nav_keyboard(page: int, *, icons: bool = True):
    """Nav + CLOSE only — used when the module grid lives in the rich body.

    On the rich path the 3-wide module grid is rendered as in-message
    ``RichBlockButtons`` (Bot API 10.3), so the attached keyboard keeps
    just paging and dismissal.  Those two survive on every client, so a
    client that cannot render rich buttons can still page and close.
    """
    page = _clamp_page(page)
    rows = []
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
    """Single deep-link row for the group redirect — opens /help in the DM."""
    return build_keyboard(
        [
            [
                btn_url(
                    "Open in DM",
                    f"https://t.me/{username}?start=help",
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
            await reply_text(target, 
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


async def _send_menu(target, bot=None) -> bool:
    """Open the main menu as the single menu message."""
    return await _send_rich(
        bot,
        target,
        _blocks(_rich_main_blocks, 0),
        main_menu_nav_keyboard(0),
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
                await query.message.edit_caption(
                    caption=body,
                    parse_mode=ParseMode.HTML,
                    reply_markup=kb,
                )
            else:
                await query.message.edit_text(
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


def _network_error(e: Exception) -> bool:
    """A transport failure — retrying inside the same handler only doubles
    the wait, so callers bail instead of falling back."""
    return type(e).__name__ in {"TimedOut", "NetworkError"}


def _blocks(fn, *args):
    """Build + validate rich blocks.  ``None`` means "skip the rich attempt".

    Both block builders are pure, but they walk HELP_MENU data — so a
    malformed entry must degrade to the HTML renderer rather than abort
    the command.
    """
    if not HELP_RICH:
        return None
    try:
        built = fn(*args)
        R.validate(built)
        return built
    except Exception as e:
        logger.debug(f"help: rich blocks unavailable ({fn.__name__}): {e}")
        return None


async def _send_rich(bot, target, blocks, rich_markup,
                     html_text, html_markup, html_plain_markup) -> bool:
    """Open the menu as a Rich Message, falling back to the HTML one.

    The two paths carry DIFFERENT keyboards on purpose: on the rich path
    the module grid lives in the message body as ``RichBlockButtons``,
    so the attached keyboard only pages and closes.  Handing the HTML
    fallback that same keyboard would leave it with no way to reach a
    module, hence the separate markup arguments.
    """
    if blocks is not None and bot is not None:
        attempts = [blocks]
        if R.has_styled_buttons(blocks):
            attempts.append(R.unstyle(blocks))
        for i, blk in enumerate(attempts):
            try:
                await R.send_rich(bot, target.chat.id, blk, reply_markup=rich_markup)
                return True
            except Exception as e:
                if _network_error(e):
                    logger.warning(f"Help rich send network issue: {e}")
                    return False
                logger.debug(
                    f"Help rich send attempt {i + 1}/{len(attempts)} failed, "
                    f"{'using HTML' if i == len(attempts) - 1 else 'retrying'}: {e}"
                )
    return await _send_text(target, html_text, html_markup, html_plain_markup)


async def _safe_edit_rich(bot, query, blocks, rich_markup,
                          html_text, html_markup, html_plain_markup) -> None:
    """Rewrite the menu message in place — rich first, HTML fallback."""
    msg = query.message
    if blocks is not None and bot is not None and msg is not None and not bool(
        getattr(msg, "photo", None)
    ):
        attempts = [blocks]
        if R.has_styled_buttons(blocks):
            attempts.append(R.unstyle(blocks))
        for i, blk in enumerate(attempts):
            try:
                await R.edit_rich(
                    bot, msg.chat.id, msg.message_id, blk, reply_markup=rich_markup
                )
                return
            except Exception as e:
                if "message is not modified" in str(e):
                    return
                if _network_error(e):
                    logger.warning(f"Help rich edit network issue: {e}")
                    return
                logger.debug(
                    f"Help rich edit attempt {i + 1}/{len(attempts)} failed, "
                    f"{'using HTML' if i == len(attempts) - 1 else 'retrying'}: {e}"
                )
    await _safe_edit(query, html_text, html_markup, html_plain_markup)


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
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception as e:
            logger.debug(f"help close failed: {e}")


# ── Handlers ─────────────────────────────────────────────────────

async def help_command(message: Message, bot: Bot, bot_data: dict) -> None:
    """/help — DM: menu text message; groups: DM-redirect text message."""
    chat = message.chat
    if message is None:
        return

    if chat is not None and chat.type != "private":
        username = _bot_username(bot, bot_data)
        text = (
            f"{E.INFO} Help Menu\n"
            f"├ {E.USER} Detail: Browse every command in the bot's DM\n"
            f"└ {E.ARROW} Tap the button below to continue"
        )
        await _send_rich(
            bot,
            message,
            _blocks(_rich_group_blocks, username),
            None,  # rich body carries the deep-link button itself
            text,
            _dm_keyboard(username),
            _dm_keyboard(username, icons=False),
        )
        return

    await _send_menu(message, bot)


async def show_main_menu(callback_query: CallbackQuery) -> None:
    """Open the menu (start:help) — swaps out the clicked message."""
    query = callback_query
    await _answer(query)
    target = query.message
    if target is None:
        return
    if await _send_menu(target, getattr(query, "bot", None)):
        try:
            await target.delete()
        except Exception as e:
            logger.debug(f"help start-swap delete failed: {e}")


async def help_callback(callback_query: CallbackQuery, bot: Bot, bot_data: dict) -> None:
    """Route help:* callbacks — pagination, drill-down, close, back."""
    query = callback_query
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
        await _safe_edit_rich(
            bot,
            query,
            _blocks(_rich_main_blocks, page),
            main_menu_nav_keyboard(page),
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
        # Same keyboard on both paths: the module grid lives in the rich
        # body, so the attached keyboard is identical to the HTML one.
        await _safe_edit_rich(
            bot,
            query,
            _blocks(_rich_module_blocks, mod, sub),
            module_keyboard(key, menu_page, sub),
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
            _build_start_message(_bot_username(bot, bot_data)),
            build_start_keyboard(),
            build_start_keyboard(icons=False),
        )
        return

    await _answer(query, "Unknown option", alert=True)


def setup() -> list[str]:
    """Register this module's handlers. Returns route descriptions for the log."""
    on("message", help_command, flt=cmd("help"))
    on("callback_query", help_callback, flt=F.data.regexp(re.compile(rf"^{_CB}:")))
    return ["/help", "help:* callbacks"]
