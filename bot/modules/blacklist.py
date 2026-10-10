"""Blacklist module — boa's word + sticker blacklist, ported to Pi.

Two lists and one mode, per chat:

* **words** — matched with a whole-word, case-insensitive regex against
  the text *or* caption of every group message;
* **stickers** — matched by ``file_unique_id``, so the same sticker in
  another pack does not trip it;
* **mode** — what happens on a hit: ``off`` (do nothing), ``del``,
  ``warn``, ``mute``, ``kick`` or ``ban``.

``/blacklist`` opens the management menu.  The menu body is a Rich
Message (headings + tables + divider) and the controls ride in
``reply_markup`` with the project's coloured buttons and ``EID`` icons —
the *same* keyboard on both render paths, which is what makes the rich →
HTML fallback invisible: there is never a second keyboard to reconcile.

Differs from the existing ``blocklist`` module on purpose: blocklist is a
substring filter with a per-word action and reason; this is boa's
whole-word + sticker model with a single per-chat mode.

Handlers
--------
Commands run at the default group (0) like every other command.
``blacklist_check`` runs at :data:`BLACKLIST_GROUP`, after the force-join
gate (-1) and alongside the other content filters (2, 3), so a gated
message never reaches it.
"""

from __future__ import annotations

import asyncio
import logging
import re
from html import escape
from typing import Any, Dict, List, Optional, Sequence, Tuple

from aiogram import Bot, F
from aiogram.enums import ParseMode
from aiogram.filters.logic import and_f, or_f
from aiogram.types import CallbackQuery, Message

from bot import rich as R
from bot import richsend as rs
from bot.async_bridge import adb
from bot.command_handler import COMMAND
from bot.database import db
from bot.emojis import E, EID
from bot.keyboards.colored import (
    btn_danger,
    btn_default,
    btn_primary,
    btn_success,
    build_keyboard,
)
from bot.pipeline import GROUPS, SERVICE, cmd, is_service_update, on
from bot.reply import reply_text
from bot.responses import action_card, field_extra, field_user

logger = logging.getLogger(__name__)

#: Auto-detect group.  0 is commands, 1-13 are taken by filters (1),
#: blocklist (2), watchwords (3), locks (4), leveling (5), security (6,
#: 8, 12), analytics (7, 13), antispam (9), welcome (10), join tracker
#: (11); 18-23 are chatstats/users/adminbox/afk.  14-17 are free.
BLACKLIST_GROUP = 14

#: Callback prefix (registered with a ``^bl:`` regexp filter).
_CB = "bl"

#: Enforcement modes, in menu order.
MODES: Tuple[str, ...] = ("off", "del", "warn", "mute", "kick", "ban")

MODE_LABELS: Dict[str, str] = {
    "off": "Off",
    "del": "Delete",
    "warn": "Warn",
    "mute": "Mute",
    "kick": "Kick",
    "ban": "Ban",
}

#: Owner's own custom-emoji ids for the button icons (labels stay plain
#: text — a Unicode emoji in a label is drawn from Telegram's stock set,
#: not from the owner's pack).
MODE_EIDS: Dict[str, str] = {
    "off": EID.DISAPPROVE,
    "del": EID.CROSS,
    "warn": EID.WARN,
    "mute": EID.MUTE,
    "kick": EID.KICK,
    "ban": EID.BAN,
}

#: Icon HTML for the message bodies (the same emoji, as <tg-emoji>).
MODE_ICONS: Dict[str, str] = {
    "off": E.DISAPPROVE,
    "del": E.CROSS,
    "warn": E.WARN,
    "mute": E.MUTE,
    "kick": E.KICK,
    "ban": E.BAN,
}

#: How many rows each list shows in the menu before truncating.
MAX_MENU_ROWS = 50


# ── Permissions ────────────────────────────────────────────────────

async def _admin_in(bot: Bot, chat_id: int, user_id: Optional[int]) -> bool:
    if user_id is None:
        return False
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False


async def _is_admin(message: Message, bot: Bot) -> bool:
    if not message.from_user:
        return False
    return await _admin_in(bot, message.chat.id, message.from_user.id)


async def _require_admin(message: Message, bot: Bot) -> bool:
    """Group-only + admin gate shared by every management command.

    Returns True when the command may proceed; already replied otherwise.
    """
    if not message.chat or message.chat.type == "private":
        await reply_text(
            message, f"{E.ERROR} This command only works in groups.",
            parse_mode=ParseMode.HTML,
        )
        return False
    if not await _is_admin(message, bot):
        return False
    return True


# ── Keyboard (identical on the rich and the HTML path) ─────────────

def build_menu_keyboard(mode: str, *, icons: bool = True):
    """The inline controls for ``/blacklist``.

    Colours carry meaning and never change between renderers: the active
    mode is green (success), every selectable mode is blue (primary),
    destructive is red (danger) and Close is plain white.
    """
    active = mode if mode in MODE_LABELS else "off"

    def _mode(m: str):
        label = MODE_LABELS[m]
        eid = MODE_EIDS[m] if icons else None
        if m == active:
            return btn_success(label, f"{_CB}:mode:{m}", icon_emoji_id=eid)
        return btn_primary(label, f"{_CB}:mode:{m}", icon_emoji_id=eid)

    rows = [
        [_mode("off"), _mode("del"), _mode("warn")],
        [_mode("mute"), _mode("kick"), _mode("ban")],
        [
            btn_primary("Refresh", f"{_CB}:refresh",
                        icon_emoji_id=EID.SETTINGS if icons else None),
            btn_danger("Clear Words", f"{_CB}:clear",
                       icon_emoji_id=EID.CROSS if icons else None),
            btn_default("Close", f"{_CB}:close",
                        icon_emoji_id=EID.CROSS if icons else None),
        ],
    ]
    return build_keyboard(rows)


# ── Menu rendering ─────────────────────────────────────────────────

def _title_block() -> Dict[str, Any]:
    icon = R.icon_rich(E.LOCK)
    parts: List[Any] = [icon] if icon else []
    parts.append(" Blacklist")
    return R.heading(parts if len(parts) > 1 else parts[0], 1)


def _list_blocks(kind: str, values: Sequence[str]) -> List[Dict[str, Any]]:
    """A small table for one list, or a placeholder paragraph."""
    if not values:
        noun = "words" if kind == "word" else "stickers"
        return [R.paragraph(f"No blacklisted {noun} yet.")]
    rows: List[List[Dict[str, Any]]] = [[
        R.cell("#", header=True, align="right"),
        R.cell(kind.capitalize(), header=True),
    ]]
    for i, value in enumerate(values[:MAX_MENU_ROWS], 1):
        rows.append([
            R.cell(str(i), align="right"),
            R.cell(value),
        ])
    node = R.table(rows)
    extra = len(values) - MAX_MENU_ROWS
    if extra > 0:
        node["caption"] = f"showing {MAX_MENU_ROWS} of {len(values)}"
    return [node]


def build_menu_blocks(
    chat_title: str,
    words: Sequence[str],
    stickers: Sequence[str],
    mode: str,
) -> List[Dict[str, Any]]:
    """Rich body of the management menu (buttons live in reply_markup)."""
    icon = MODE_ICONS.get(mode, E.INFO)
    blocks: List[Dict[str, Any]] = [
        _title_block(),
        R.paragraph(
            f"Whole-word and sticker filters for {chat_title}. "
            "Matching messages are removed and the configured action is "
            "applied to the sender."
        ),
        R.divider(),
        R.heading("Mode", 3),
        R.paragraph([icon, f" {MODE_LABELS.get(mode, mode)}"]),
        R.heading("Words", 3),
        *_list_blocks("word", words),
        R.heading("Stickers", 3),
        *_list_blocks("sticker", stickers),
        R.divider(),
        R.footer(
            "Add words: /addblacklist <word...> — "
            "Add a sticker: reply to it with /addblsticker — "
            "Set mode: /blacklistmode <mode>"
        ),
    ]
    R.validate(blocks)
    return blocks


def build_menu_html(
    chat_title: str,
    words: Sequence[str],
    stickers: Sequence[str],
    mode: str,
) -> str:
    """HTML twin of :func:`build_menu_blocks` for the fallback path."""
    def _rows(values: Sequence[str], noun: str, label: str) -> str:
        if not values:
            return f"No blacklisted {noun} yet."
        shown = "\n".join(
            f"{i}. <code>{escape(v)}</code>"
            for i, v in enumerate(values[:MAX_MENU_ROWS], 1)
        )
        more = len(values) - MAX_MENU_ROWS
        tail = f"\n… and {more} more" if more > 0 else ""
        return f"<b>{label} ({len(values)}):</b>\n{shown}{tail}"

    return (
        f"{E.LOCK} <b>Blacklist</b>\n"
        f"Whole-word and sticker filters for {escape(chat_title)}. "
        "Matching messages are removed and the configured action is "
        "applied to the sender.\n\n"
        f"{E.SETTINGS} <b>Mode:</b> {MODE_ICONS.get(mode, E.INFO)} "
        f"<b>{MODE_LABELS.get(mode, escape(str(mode)))}</b>\n\n"
        f"{_rows(words, 'words', 'Words')}\n\n"
        f"{_rows(stickers, 'stickers', 'Stickers')}\n\n"
        f"{E.INFO} <i>Add words: /addblacklist &lt;word...&gt; — "
        "Add a sticker: reply to it with /addblsticker — "
        "Set mode: /blacklistmode &lt;mode&gt;</i>"
    )


async def _open_menu(message: Message, bot: Bot) -> None:
    """Send the menu rich-first, HTML second — same keyboard either way."""
    chat_id = message.chat.id
    chat_title = message.chat.title or "this group"
    words: List[str] = await adb(db.get_blacklist_words(chat_id))
    stickers: List[str] = await adb(db.get_blacklist_stickers(chat_id))
    mode_state: Dict[str, Any] = await adb(db.get_blacklist_mode(chat_id))
    mode = str(mode_state.get("mode") or "off")

    keyboard = build_menu_keyboard(mode)
    blocks = rs.build_blocks(
        build_menu_blocks, chat_title, words, stickers, mode
    )

    async def _html():
        await reply_text(
            message,
            build_menu_html(chat_title, words, stickers, mode),
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )

    await rs.send_with_fallback(
        bot, chat_id, blocks, rich_markup=keyboard, fallback=_html
    )


async def _refresh_menu(query: CallbackQuery, bot: Bot) -> None:
    """Re-render the menu message after a mode change or Clear."""
    msg = query.message
    if msg is None:
        return
    chat_id = msg.chat.id
    chat_title = msg.chat.title or "this group"
    words: List[str] = await adb(db.get_blacklist_words(chat_id))
    stickers: List[str] = await adb(db.get_blacklist_stickers(chat_id))
    mode_state: Dict[str, Any] = await adb(db.get_blacklist_mode(chat_id))
    mode = str(mode_state.get("mode") or "off")

    keyboard = build_menu_keyboard(mode)
    blocks = rs.build_blocks(
        build_menu_blocks, chat_title, words, stickers, mode
    )
    html_text = build_menu_html(chat_title, words, stickers, mode)

    async def _html_edit():
        try:
            await msg.edit_text(
                html_text, parse_mode=ParseMode.HTML, reply_markup=keyboard
            )
            return True
        except Exception as e:
            if "message is not modified" in str(e):
                return True
            logger.debug(f"blacklist HTML menu edit failed: {e}")
            return False

    await rs.edit_with_fallback(
        bot, chat_id, msg.message_id, blocks,
        rich_markup=keyboard, fallback=_html_edit,
    )


# ── Command handlers ───────────────────────────────────────────────

def _command_words(message: Message, args: Sequence[str]) -> List[str]:
    """Words from the command line, or from the replied-to message.

    boa documented "reply to a message or use in command" but only ever
    read the command line; this does both.
    """
    if args:
        source = list(args)
    elif message.reply_to_message:
        source = (message.reply_to_message.text
                  or message.reply_to_message.caption or "").split()
    else:
        source = []
    seen: List[str] = []
    for raw in source:
        word = str(raw).strip().lower()
        if word and word not in seen:
            seen.append(word)
    return seen


async def blacklist_command(message: Message, bot: Bot) -> None:
    """/blacklist — open the management menu."""
    if not await _require_admin(message, bot):
        return
    await _open_menu(message, bot)


async def add_blacklist(message: Message, bot: Bot, args: list) -> None:
    """/addblacklist <word...> — add words (or the replied message's)."""
    if not await _require_admin(message, bot):
        return

    words = _command_words(message, args)
    if not words:
        await reply_text(
            message,
            f"{E.INFO} <b>Usage:</b>\n"
            "• /addblacklist &lt;word1&gt; &lt;word2&gt; ... — add words\n"
            "• Reply to a message with /addblacklist — add its words",
            parse_mode=ParseMode.HTML,
        )
        return

    chat_id = message.chat.id
    existing: List[str] = await adb(db.get_blacklist_words(chat_id))
    added: List[str] = []
    already: List[str] = []
    for word in words:
        if word in existing:
            already.append(word)
            continue
        if await adb(db.add_blacklist_word(chat_id, word)):
            added.append(word)

    lines: List[str] = []
    if added:
        lines.append(
            f"{E.CHECK} Added: "
            + ", ".join(f"<code>{escape(w)}</code>" for w in added)
        )
    if already:
        lines.append(
            f"{E.INFO} Already blacklisted: "
            + ", ".join(f"<code>{escape(w)}</code>" for w in already)
        )
    if not lines:
        lines.append(f"{E.ERROR} Nothing was added.")
    await reply_text(message, "\n".join(lines), parse_mode=ParseMode.HTML)


async def remove_blacklist(message: Message, bot: Bot, args: list) -> None:
    """/unblacklist <word...> — remove words."""
    if not await _require_admin(message, bot):
        return

    words = _command_words(message, args)
    if not words:
        await reply_text(
            message,
            f"{E.INFO} Usage: /unblacklist &lt;word1&gt; &lt;word2&gt; ...",
            parse_mode=ParseMode.HTML,
        )
        return

    chat_id = message.chat.id
    existing: List[str] = await adb(db.get_blacklist_words(chat_id))
    removed: List[str] = []
    missing: List[str] = []
    for word in words:
        if word not in existing:
            missing.append(word)
            continue
        if await adb(db.remove_blacklist_word(chat_id, word)):
            removed.append(word)

    lines: List[str] = []
    if removed:
        lines.append(
            f"{E.CHECK} Removed: "
            + ", ".join(f"<code>{escape(w)}</code>" for w in removed)
        )
    if missing:
        lines.append(
            f"{E.INFO} Not blacklisted: "
            + ", ".join(f"<code>{escape(w)}</code>" for w in missing)
        )
    if not lines:
        lines.append(f"{E.ERROR} Nothing was removed.")
    await reply_text(message, "\n".join(lines), parse_mode=ParseMode.HTML)


async def blacklist_mode(message: Message, bot: Bot, args: list) -> None:
    """/blacklistmode <off|del|warn|mute|kick|ban>."""
    if not await _require_admin(message, bot):
        return

    if not args or str(args[0]).lower() not in MODES:
        await reply_text(
            message,
            f"{E.ERROR} <b>Usage:</b> /blacklistmode "
            f"&lt;{'|'.join(MODES)}&gt;\n"
            f"{E.INFO} <b>off</b> — stop enforcing · "
            f"<b>del</b> — only delete · "
            f"<b>warn</b> — delete and warn · "
            f"<b>mute</b> / <b>kick</b> / <b>ban</b> — delete and act",
            parse_mode=ParseMode.HTML,
        )
        return

    mode = str(args[0]).lower()
    await adb(db.set_blacklist_mode(message.chat.id, mode, 0))
    await reply_text(
        message,
        f"{E.CHECK} Blacklist mode set to "
        f"{MODE_ICONS[mode]} <b>{MODE_LABELS[mode]}</b>.",
        parse_mode=ParseMode.HTML,
    )


async def list_blacklist_stickers(message: Message, bot: Bot) -> None:
    """/blsticker — list blacklisted stickers."""
    if not await _require_admin(message, bot):
        return

    stickers: List[str] = await adb(db.get_blacklist_stickers(message.chat.id))
    if not stickers:
        await reply_text(
            message,
            f"{E.INFO} No blacklisted stickers in this chat.",
            parse_mode=ParseMode.HTML,
        )
        return

    rows = "\n".join(
        f"{i}. <code>{escape(s)}</code>"
        for i, s in enumerate(stickers[:MAX_MENU_ROWS], 1)
    )
    more = len(stickers) - MAX_MENU_ROWS
    tail = f"\n… and {more} more" if more > 0 else ""
    await reply_text(
        message,
        f"{E.ALERT} <b>Blacklisted stickers ({len(stickers)}):</b>\n"
        f"{rows}{tail}",
        parse_mode=ParseMode.HTML,
    )


async def add_blacklist_sticker(message: Message, bot: Bot) -> None:
    """/addblsticker — reply to a sticker to blacklist it."""
    if not await _require_admin(message, bot):
        return

    reply = message.reply_to_message
    sticker = getattr(reply, "sticker", None) if reply else None
    if sticker is None:
        await reply_text(
            message,
            f"{E.INFO} Reply to a sticker with /addblsticker to blacklist it.",
            parse_mode=ParseMode.HTML,
        )
        return

    sticker_id = sticker.file_unique_id
    chat_id = message.chat.id
    existing: List[str] = await adb(db.get_blacklist_stickers(chat_id))
    if sticker_id in existing:
        await reply_text(
            message,
            f"{E.INFO} That sticker is already blacklisted.\n"
            f"<code>{escape(sticker_id)}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    await adb(db.add_blacklist_sticker(chat_id, sticker_id))
    await reply_text(
        message,
        f"{E.CHECK} Sticker blacklisted.\n<code>{escape(sticker_id)}</code>",
        parse_mode=ParseMode.HTML,
    )


async def remove_blacklist_sticker(message: Message, bot: Bot, args: list) -> None:
    """/unblsticker — reply to a sticker, or pass its file_unique_id."""
    if not await _require_admin(message, bot):
        return

    reply = message.reply_to_message
    sticker = getattr(reply, "sticker", None) if reply else None
    sticker_id = (
        sticker.file_unique_id if sticker else (str(args[0]) if args else "")
    )
    if not sticker_id:
        await reply_text(
            message,
            f"{E.INFO} Reply to a sticker with /unblsticker, or pass its "
            "<code>file_unique_id</code>.",
            parse_mode=ParseMode.HTML,
        )
        return

    chat_id = message.chat.id
    if not await adb(db.remove_blacklist_sticker(chat_id, sticker_id)):
        await reply_text(
            message,
            f"{E.ERROR} That sticker is not blacklisted.\n"
            f"<code>{escape(sticker_id)}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    await reply_text(
        message,
        f"{E.CHECK} Sticker removed.\n<code>{escape(sticker_id)}</code>",
        parse_mode=ParseMode.HTML,
    )


# ── Auto-detection ─────────────────────────────────────────────────

def matches_blacklist(text: str, words: Sequence[str]) -> Optional[str]:
    """First whole-word hit in *text*, or None.

    ``\\b`` boundaries stop ``spam`` matching inside ``spammy``; the
    words are stored lower-cased and matched case-insensitively.
    """
    if not text or not words:
        return None
    for word in words:
        try:
            if re.search(rf"\b{re.escape(word)}\b", text, flags=re.IGNORECASE):
                return word
        except re.error as e:  # a pathological word must not kill the handler
            logger.debug(f"blacklist regex failed for {word!r}: {e}")
    return None


def _count_action(chat_id: int, user_id: int) -> None:
    """Blocking reputation/mod-counter write — worker thread only."""
    try:
        db.bump_mod_actions(chat_id, 1)
        db.record_reputation_event(user_id, "warning", 1)
    except Exception as e:
        logger.debug(f"blacklist counters: {e}")


def _hit_card(
    user,
    matched_label: str,
    matched: str,
    mode: str,
    *,
    detail: str = "",
) -> str:
    fields = [
        field_user(user),
        field_extra(E.INFO, matched_label, escape(matched)),
        field_extra(MODE_ICONS.get(mode, E.INFO), "Action",
                    escape(MODE_LABELS.get(mode, mode))),
    ]
    if detail:
        fields.append(field_extra(E.ALERT, "Result", escape(detail)))
    return action_card("Blacklisted Content", fields, icon=E.ALERT)


async def _warn(message: Message, bot: Bot, matched_label: str, matched: str) -> str:
    """Warn route — same warn limit / warn mode the /warn command uses."""
    from bot.modules.moderation import (
        WarningAction,
        add_warning,
        execute_action,
        get_chat_settings,
        reset_warnings,
    )

    chat_id = message.chat.id
    user = message.from_user
    settings = await get_chat_settings(chat_id)
    limit = int(settings.get("warn_limit") or 3)
    reason = f"Blacklisted {matched_label}: {matched}"

    count = await add_warning(chat_id, user.id, reason)
    try:
        await asyncio.to_thread(_count_action, chat_id, user.id)
    except Exception:
        pass

    if count >= limit:
        action = settings.get("warn_mode") or WarningAction.MUTE
        duration = settings.get("warn_mode_duration")
        ok, detail = await execute_action(
            message, bot, user.id, action, duration, reason
        )
        await reset_warnings(chat_id, user.id)
        label = {
            WarningAction.MUTE: "Muted",
            WarningAction.KICK: "Kicked",
            WarningAction.BAN: "Banned",
            WarningAction.TIMEOUT: "Timed out",
        }.get(action, "Acted on")
        result = f"{label} ({detail})" if ok else f"Failed ({detail})"
        return _hit_card(user, matched_label, matched, "warn", detail=result)

    return _hit_card(
        user, matched_label, matched, "warn",
        detail=f"Warnings {count}/{limit}",
    )


async def _enforce(
    message: Message, bot: Bot, mode: str, matched_label: str, matched: str
) -> None:
    """Delete the hit, then apply the configured mode."""
    from bot.modules.moderation import WarningAction, execute_action

    chat_id = message.chat.id
    user = message.from_user

    try:
        await message.delete()
    except Exception as e:
        logger.debug(f"blacklist delete failed: {e}")

    if mode in ("off", "del"):
        return

    card: Optional[str] = None
    if mode == "warn":
        card = await _warn(message, bot, matched_label, matched)
    else:
        action = {
            "mute": WarningAction.MUTE,
            "kick": WarningAction.KICK,
            "ban": WarningAction.BAN,
        }.get(mode)
        if action is None:
            return
        try:
            await asyncio.to_thread(_count_action, chat_id, user.id)
        except Exception:
            pass
        ok, detail = await execute_action(
            message, bot, user.id, action, None,
            f"Blacklisted {matched_label}: {matched}",
        )
        card = _hit_card(user, matched_label, matched, mode,
                         detail=(detail if ok else f"Failed ({detail})"))

    if not card:
        return
    try:
        await bot.send_message(
            chat_id, card, parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.warning(f"blacklist card send failed: {e}")


async def blacklist_check(message: Message, bot: Bot) -> None:
    """Enforce the blacklist on incoming group messages."""
    if not message or not message.chat or message.chat.type == "private":
        return
    # Status updates are not activity (and StopChain would starve
    # welcome/goodbye in group 10 — belt and braces with ~SERVICE).
    if is_service_update(message):
        return

    user = message.from_user
    if user is None or getattr(user, "is_bot", False):
        return
    # Linked-channel posts / anonymous admins: no human to act on.
    if getattr(message, "sender_chat", None) is not None:
        return

    chat_id = message.chat.id
    mode_state: Dict[str, Any] = await adb(db.get_blacklist_mode(chat_id))
    mode = str(mode_state.get("mode") or "off")
    if mode == "off":
        return

    words: List[str] = await adb(db.get_blacklist_words(chat_id))
    stickers: List[str] = await adb(db.get_blacklist_stickers(chat_id))
    if not words and not stickers:
        return

    matched_label = ""
    matched = ""
    text = (message.text or message.caption or "")
    hit = matches_blacklist(text, words)
    if hit is not None:
        matched_label, matched = "Word", hit
    else:
        sticker = getattr(message, "sticker", None)
        uid = getattr(sticker, "file_unique_id", None) if sticker else None
        if uid and uid in stickers:
            matched_label, matched = "Sticker", uid

    if not matched:
        return

    # Admins configure this list; they are not subject to it.
    if await _is_admin(message, bot):
        return

    await _enforce(message, bot, mode, matched_label, matched)


# ── Callbacks ──────────────────────────────────────────────────────

async def blacklist_callback(callback_query: CallbackQuery, bot: Bot) -> None:
    """Route ``bl:*`` callbacks — mode switches, refresh, clear, close."""
    query = callback_query
    if query is None:
        return
    data = query.data or ""
    if not data.startswith(f"{_CB}:"):
        return
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    msg = query.message
    if msg is None:
        try:
            await query.answer()
        except Exception:
            pass
        return

    # Resolve everything that can reject *before* the single answer —
    # Telegram only honours one answer per callback query.
    mode = parts[2] if len(parts) > 2 else ""
    if action == "mode" and mode not in MODES:
        action = "unknown"
    elif action not in ("mode", "refresh", "clear", "close"):
        action = "unknown"

    if not await _admin_in(bot, msg.chat.id, getattr(query.from_user, "id", None)):
        try:
            await query.answer("Admins only.", show_alert=True)
        except Exception:
            pass
        return

    try:
        if action == "unknown":
            await query.answer("Unknown option.", show_alert=True)
        else:
            await query.answer()
    except Exception:
        pass

    if action == "unknown":
        return

    if action == "mode":
        await adb(db.set_blacklist_mode(msg.chat.id, mode, 0))
        await _refresh_menu(query, bot)
        return

    if action == "refresh":
        await _refresh_menu(query, bot)
        return

    if action == "clear":
        await adb(db.clear_blacklist_words(msg.chat.id))
        await _refresh_menu(query, bot)
        return

    # action == "close"
    try:
        await msg.delete()
    except Exception:
        try:
            await msg.edit_reply_markup(reply_markup=None)
        except Exception as e:
            logger.debug(f"blacklist close failed: {e}")


# ── Module setup ───────────────────────────────────────────────────

def setup() -> list:
    """Register commands, the callback router and the auto-detect handler."""
    on("message", blacklist_command, flt=cmd("blacklist"))
    on("message", add_blacklist, flt=cmd("addblacklist"))
    on("message", remove_blacklist, flt=cmd("unblacklist"))
    on("message", blacklist_mode, flt=cmd("blacklistmode"))
    on("message", list_blacklist_stickers, flt=cmd("blsticker"))
    on("message", add_blacklist_sticker, flt=cmd("addblsticker"))
    on("message", remove_blacklist_sticker, flt=cmd("unblsticker"))
    on("callback_query", blacklist_callback,
       flt=F.data.regexp(re.compile(rf"^{_CB}:")))
    on(
        "message",
        blacklist_check,
        group=BLACKLIST_GROUP,
        flt=and_f(
            GROUPS,
            ~SERVICE,
            ~COMMAND,
            or_f(F.text, F.caption, F.sticker),
        ),
    )

    return [
        "/blacklist", "/addblacklist", "/unblacklist", "/blacklistmode",
        "/blsticker", "/addblsticker", "/unblsticker",
        "bl:* callbacks",
        f"auto-detect (group {BLACKLIST_GROUP})",
    ]
