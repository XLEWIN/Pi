"""Bind module handlers — /bind, /bindmenu, force-join gate, join tracking.

Every ``bdb.*`` call is synchronous, UNCACHED MongoDB I/O (``_find_one``
bypasses bot.database's read cache).  This module therefore routes all of
them through ``asyncio.to_thread`` — ``gate_message_handler`` runs for
*every* group message in *every* chat, so a single blocking round trip
here stalls the whole event loop and with it long-polling itself.
"""

import asyncio
import logging
import time
from html import escape
from typing import Any, Dict, Optional

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.types import Message

from bot.emojis import E
from bot.reply import reply_text

from . import database as bdb
from .checks import (
    gate_enabled,
    in_grace,
    is_bot_admin,
    is_channel_member,
    is_group_admin,
    message_gates,
    should_enforce,
)
from .config import PLACEHOLDERS_HELP
from .keyboards import bind_main_menu, force_join_keyboard, help_bind_text, replace_confirm_menu
from .utils import (
    channel_display,
    format_grace,
    parse_channel_ref,
    render_custom_message,
    spawn,
    user_mention_html,
)
from bot.async_bridge import adb

logger = logging.getLogger(__name__)


async def _resolve_channel(bot: Bot, ref: str):
    """Resolve a channel reference to a Chat-like object, or raise ValueError."""
    parsed = parse_channel_ref(ref)
    if not parsed:
        raise ValueError("Could not parse that channel reference.")
    channel_id, username, _link = parsed

    if username:
        try:
            return await bot.get_chat(username)
        except Exception as e:
            raise ValueError(f"Channel @{username} not found or bot can't access it. ({e})") from e

    try:
        return await bot.get_chat(channel_id)
    except Exception as e:
        # Telegram only accepts NEGATIVE ids for channels/supergroups
        # (-100xxxxxxxxxx).  A lot of tools show the id without the sign
        # (``1002807915293``), which getChat() rejects with
        # "Bad Request: chat not found" even when the bot is an admin.
        # Retry the sign-restored form before giving up.
        signed = -abs(channel_id)
        if signed != channel_id:
            try:
                return await bot.get_chat(signed)
            except Exception:
                pass
        raise ValueError(
            f"Chat {channel_id} not found or bot can't access it. "
            f"({escape(str(e))}) — try <code>@username</code>, a <code>t.me/…</code> link, "
            "or the full id including the <code>-100</code> prefix."
        ) from e



async def _bot_is_channel_admin(bot: Bot, chat) -> bool:
    """Bot must be able to call getChatMember on the channel."""
    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(chat.id, me.id)
        return member.status in ("administrator", "creator", "member")
    except Exception:
        # Some public channels allow member checks without admin — try anyway.
        return True


def _channel_link(chat) -> Optional[str]:
    if getattr(chat, "username", None):
        return f"https://t.me/{chat.username}"
    # Private invite links are not available via Bot API without export.
    return None


async def bind_command(message: Message, bot: Bot, args: list, chat_data: dict) -> None:
    """Handle /bind [channel] — bind this group to a channel."""
    if not message or not message.chat or message.chat.type == "private":
        if message:
            await reply_text(message, f"{E.ERROR} This command only works in groups.", parse_mode=ParseMode.HTML)
        return

    chat_id = message.chat.id
    user = message.from_user

    if not await is_group_admin(bot, chat_id, user.id):
        return

    existing = await adb(bdb.get_settings(chat_id))

    # No argument → show status / usage.
    if not args:
        if existing:
            link = existing.get("channel_link") or ""
            btn = bind_main_menu(existing, group_title=message.chat.title or "")
            await reply_text(
                message,
                f"{E.CHECK} <b>Already bound</b> to {channel_display(existing)}.\n"
                f"Force Join: <b>{'ON' if existing.get('force_join') else 'OFF'}</b> • "
                f"Grace: <b>{format_grace(existing.get('grace_minutes') or 0)}</b>\n\n"
                f"Use <code>/bind @newchannel</code> to replace, or <code>/bindmenu</code> to configure.",
                parse_mode=ParseMode.HTML,
                reply_markup=btn,
                disable_web_page_preview=True,
            )
        else:
            await reply_text(
                message,
                f"{E.INFO} Not bound yet.\n\n{help_bind_text()}",
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        return

    ref = args[0]
    try:
        channel = await _resolve_channel(bot, ref)
    except ValueError as e:
        await reply_text(message, f"{E.ERROR} {e}", parse_mode=ParseMode.HTML)
        return

    if channel.type not in ("channel", "supergroup", "group"):
        await reply_text(
            message,
            f"{E.ERROR} Target must be a channel or supergroup.",
            parse_mode=ParseMode.HTML,
        )
        return

    if not await _bot_is_channel_admin(bot, channel):
        await reply_text(
            message,
            f"{E.ERROR} I can't check membership in that chat. "
            "Add me as an admin of the channel (or make sure it's public).",
            parse_mode=ParseMode.HTML,
        )
        return

    # One channel ↔ one group: reject if another GC already holds this channel.
    other = await adb(bdb.find_other_binding(channel.id, chat_id))
    if other:
        await reply_text(message, _channel_taken_text(), parse_mode=ParseMode.HTML)
        return

    # Already bound to a different channel → confirm replace.
    if existing and existing.get("channel_id") and existing["channel_id"] != channel.id:
        # Stash pending target in chat_data for confirm callback.
        chat_data["bind_pending_channel"] = {
            "id": channel.id,
            "username": channel.username,
            "title": channel.title,
            "link": _channel_link(channel),
        }
        await reply_text(
            message,
            f"{E.WARNING} Already bound to {channel_display(existing)}.\n"
            f"Replace with <b>{channel.title or channel.username or channel.id}</b>?",
            parse_mode=ParseMode.HTML,
            reply_markup=replace_confirm_menu(),
        )
        return

    await _apply_binding(message, channel, bound_by=user.id)


def _channel_taken_text() -> str:
    return (
        f"{E.ERROR} <b>Channel already bound.</b>\n\n"
        "One channel can only be bound to one group at a time.\n"
        "Unbind it from the other group first, or pick a different channel."
    )


def _write_binding(message: Message, channel, bound_by: int):
    """Blocking Mongo writes for a bind — runs in a worker thread."""
    chat_id = message.chat.id
    link = _channel_link(channel)
    bdb.upsert_binding(
        chat_id,
        channel.id,
        channel_username=channel.username,
        channel_title=channel.title,
        channel_link=link,
        bound_by=bound_by,
    )
    return chat_id, bdb.get_settings(chat_id)


async def _apply_binding(message: Message, channel, bound_by: int) -> None:
    try:
        chat_id, settings = await asyncio.to_thread(
            _write_binding, message, channel, bound_by
        )
    except ValueError as e:
        if str(e) == "CHANNEL_TAKEN":
            spawn(reply_text(message, _channel_taken_text(), parse_mode=ParseMode.HTML))
            return
        raise
    title = channel.title or (f"@{channel.username}" if channel.username else str(channel.id))
    text = (
        f"{E.CHECK} <b>Group bound successfully!</b>\n\n"
        f"{E.ANNOUNCE} Channel: <b>{title}</b>\n"
        f"{E.CHECK} Force Join: <b>{'ON' if settings.get('force_join') else 'OFF'}</b>\n"
        f"{E.CROWN} Admin bypass: <b>{'ON' if settings.get('admin_bypass') else 'OFF'}</b>\n"
        f"{E.CLOCK} Grace: <b>{format_grace(settings.get('grace_minutes') or 0)}</b>\n\n"
        f"{E.SETTINGS} Open <code>/bindmenu</code> to configure gates and the message."
    )
    spawn(
        reply_text(
            message,
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=bind_main_menu(settings, group_title=message.chat.title or ""),
            disable_web_page_preview=True,
        )
    )


async def bindmenu_command(message: Message, bot: Bot, chat_data: dict) -> None:
    """Handle /bindmenu — open the configuration panel."""
    if not message or not message.chat or message.chat.type == "private":
        if message:
            await reply_text(message, f"{E.ERROR} This command only works in groups.", parse_mode=ParseMode.HTML)
        return

    chat_id = message.chat.id
    user = message.from_user

    # Opening /bindmenu cancels any pending custom-message / channel prompt.
    chat_data.pop("bind_wait", None)

    if not await is_group_admin(bot, chat_id, user.id):
        return

    settings = await adb(bdb.get_settings(chat_id))
    if not settings:
        await reply_text(
            message,
            f"{E.INFO} Not bound yet.\n\n{help_bind_text()}",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return

    # Owner-brand emoji (<tg-emoji>) — never stock ✅/❌ in message HTML.
    force_txt = f"{E.CHECK} ON" if settings.get("force_join") else f"{E.ERROR} OFF"
    bypass_txt = f"{E.CHECK} ON" if settings.get("admin_bypass") else f"{E.ERROR} OFF"

    await reply_text(
        message,
        f"{E.SETTINGS} <b>Bind Menu</b>\n"
        f"{E.ANNOUNCE} {channel_display(settings)}\n"
        f"Force Join <b>{force_txt}</b>\n"
        f"Admin bypass <b>{bypass_txt}</b>\n"
        f"Gates active: <b>{_active_gate_count(settings)}</b>\n"
        f"{E.CLOCK} Grace: <b>{format_grace(settings.get('grace_minutes') or 0)}</b> • "
        f"Auto-delete: <b>{settings.get('auto_delete_seconds') or 0}s</b>\n\n"
        f"<i>Placeholders:</i> <code>{PLACEHOLDERS_HELP}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=bind_main_menu(settings, group_title=message.chat.title or ""),
        disable_web_page_preview=True,
    )


def _active_gate_count(settings: Dict[str, Any]) -> int:
    from .config import GATES

    return sum(1 for col in GATES.values() if int(settings.get(col) or 0))


def _gate_state(chat_id: int, user_id: int):
    """Blocking reads behind one gate decision — MUST run in a worker thread.

    Returns ``(settings, join_ts)``; ``settings`` is ``None`` when the chat
    is unbound, in which case the join stamp is irrelevant.
    """
    settings = bdb.get_settings(chat_id)
    if not settings:
        return None, None
    return settings, bdb.get_join_time(chat_id, user_id)


def _record_gate_fail(chat_id: int, user_id: int) -> None:
    """Blocking counter bumps — MUST run in a worker thread."""
    try:
        from bot.database import db as _pdb
        _pdb.bump_bind_fails(chat_id, 1)
        _pdb.record_reputation_event(user_id, "warning", 1)
    except Exception as e:
        logger.debug(f"bind fail counter: {e}")


async def gate_message_handler(message: Message, bot: Bot) -> None:
    """Enforce force-join / message-type gates on incoming group messages.

    This is the hottest handler in the bot — it is registered at
    ``HANDLER_GROUP`` for *every* group message in *every* chat.  Ordering
    inside it is therefore deliberate:

    1. one ``to_thread`` read (settings + join stamp together — the old
       code issued three separate blocking round trips, one of them a
       no-op), then
    2. a pure-CPU gate precheck, so an unbound chat or a chat whose gates
       cannot match never reaches the ``getChatMember`` round trip or a
       single Mongo write.
    """
    if not message:
        return
    chat = message.chat
    if not chat or chat.type == "private":
        return
    user = message.from_user
    if user is None:
        return

    chat_id = chat.id
    settings, join_ts = await asyncio.to_thread(_gate_state, chat_id, user.id)
    if not settings:
        return

    # Pure CPU — no I/O. Note: there is deliberately no second
    # get_join_time() here; the old "quick path" block was a no-op (`pass`)
    # that still paid for a full Mongo round trip on every message, and an
    # unknown join stamp correctly yields no grace (see in_grace).
    force = bool(int(settings.get("force_join") or 0))
    if not force and not any(
        gate_enabled(settings, g) for g in message_gates(message)
    ):
        return
    if user.is_bot:
        return
    grace_ok = in_grace(join_ts, int(settings.get("grace_minutes") or 0))
    if grace_ok:
        return

    is_admin = False
    if int(settings.get("admin_bypass") or 1):
        is_admin = await is_group_admin(bot, chat_id, user.id)

    if not should_enforce(
        settings,
        message,
        user,
        is_admin=is_admin,
        in_grace_window=grace_ok,
    ):
        return

    # Membership check (cached unless we need a decision).
    channel_id = settings["channel_id"]
    if await is_channel_member(bot, channel_id, user.id, fresh=False):
        # Cache said member — allow. Also persist join for future grace math.
        await adb(bdb.record_join(chat_id, user.id))
        return

    # Not a member → gate: delete + warn.
    await asyncio.to_thread(_record_gate_fail, chat_id, user.id)

    if not await is_bot_admin(bot, chat_id):
        # Can't delete; still try to warn once (best effort).
        pass
    else:
        try:
            await message.delete()
        except Exception as e:
            logger.debug(f"bind delete failed: {e}")

    channel_title = settings.get("channel_title") or (
        f"@{settings['channel_username']}" if settings.get("channel_username") else str(channel_id)
    )
    channel_link = settings.get("channel_link") or (
        f"https://t.me/{settings['channel_username']}" if settings.get("channel_username") else ""
    )

    body = render_custom_message(
        settings.get("custom_message"),
        user_mention=user_mention_html(user),
        user_id=user.id,
        group_title=chat.title or "this group",
        channel_title=channel_title,
        channel_link=channel_link or "the channel",
    )

    # Avoid spamming: delete previous warning for this user if still tracked? Keep simple —
    # send new warning; optional auto-delete below.
    try:
        warning = await bot.send_message(
            chat_id=chat_id,
            text=f"{E.ANNOUNCE} {body}",
            parse_mode=ParseMode.HTML,
            reply_to_message_id=None,
            reply_markup=force_join_keyboard(channel_link, channel_title),
            disable_web_page_preview=True,
        )
        await adb(bdb.add_warning(chat_id, warning.message_id, user.id))
        delay = int(settings.get("auto_delete_seconds") or 0)
        if delay > 0:
            spawn(_auto_delete(bot, chat_id, warning.message_id, delay))
    except Exception as e:
        logger.warning(f"bind warning send failed: {e}")


async def _auto_delete(bot: Bot, chat_id: int, message_id: int, delay: int) -> None:
    try:
        await asyncio.sleep(delay)
        await bot.delete_message(chat_id, message_id)
    except Exception:
        pass
    finally:
        try:
            await adb(bdb.remove_warning(chat_id, message_id))
        except Exception:
            pass


def _track_joins(
    chat_id: int,
    members,
    left_member,
    bot_id: int,
    now: float,
) -> None:
    """Blocking join bookkeeping — MUST run in a worker thread.

    Only writes when the chat is actually bound (saves writes), matching
    the previous behaviour exactly.  Joins and leaves are handled
    independently: a leave-only service message has no ``members`` but
    must still clear the leaver's stamp, otherwise grace-period math
    keeps using a join time from months ago.
    """
    if not bdb.get_settings(chat_id):
        return
    for member in members:
        if member.is_bot or member.id == bot_id:
            continue
        bdb.record_join(chat_id, member.id, joined_at=now)
    if left_member is not None and not left_member.is_bot:
        bdb.clear_join(chat_id, left_member.id)


async def join_tracker(message: Message, bot: Bot) -> None:
    """Record group-join timestamps for grace period math."""
    if not message or not message.chat or message.chat.type == "private":
        return

    members = message.new_chat_members or ()
    left = message.left_chat_member
    # Telegram sends three shapes: join-only, leave-only, and
    # join+leave (a change in username/photo sends neither).  Bail out
    # only when there is genuinely nothing to record — the old
    # `if not message.new_chat_members: return` dropped every
    # leave-only message, so clear_join never ran for them.
    if not members and left is None:
        return

    await asyncio.to_thread(
        _track_joins,
        message.chat.id,
        members,
        left,
        bot.id,
        time.time(),
    )


async def waiting_text_handler(message: Message, bot: Bot, chat_data: dict) -> None:
    """Consume admin text when waiting for custom message or new channel."""
    if not message or not message.chat or message.chat.type == "private":
        return

    wait = chat_data.get("bind_wait")
    if not wait:
        return

    chat_id = message.chat.id
    user = message.from_user
    if not await is_group_admin(bot, chat_id, user.id):
        return

    settings = await adb(bdb.get_settings(chat_id))
    if not settings:
        chat_data.pop("bind_wait", None)
        return

    if wait == "custom":
        chat_data.pop("bind_wait", None)
        text = (message.text or "").strip()
        if not text:
            await reply_text(message, f"{E.ERROR} Empty message — send non-empty text or /bindmenu.", parse_mode=ParseMode.HTML)
            return
        # Show placeholders that survived / were typed.
        await adb(bdb.update_field(chat_id, "custom_message", text))
        try:
            await message.delete()
        except Exception:
            pass
        await bot.send_message(
            chat_id=message.chat.id,
            text=(
                f"{E.CHECK} Custom force-join message saved.\n"
                f"<i>Placeholders:</i> <code>{PLACEHOLDERS_HELP}</code>"
            ),
            parse_mode=ParseMode.HTML,
        )
        return

    if wait == "channel":
        chat_data.pop("bind_wait", None)
        raw = (message.text or "").strip()
        try:
            channel = await _resolve_channel(bot, raw)
        except ValueError as e:
            await bot.send_message(chat_id=message.chat.id, text=f"{E.ERROR} {e}", parse_mode=ParseMode.HTML)
            return
        if channel.type not in ("channel", "supergroup", "group"):
            await bot.send_message(
                chat_id=message.chat.id,
                text=f"{E.ERROR} Target must be a channel or supergroup.",
                parse_mode=ParseMode.HTML,
            )
            return
        if await adb(bdb.find_other_binding(channel.id, chat_id)):
            await bot.send_message(chat_id=message.chat.id, text=_channel_taken_text(), parse_mode=ParseMode.HTML)
            return
        link = _channel_link(channel)
        try:
            await adb(bdb.upsert_binding(
                chat_id,
                channel.id,
                channel_username=channel.username,
                channel_title=channel.title,
                channel_link=link,
                bound_by=user.id,
            ))
        except ValueError as e:
            if str(e) == "CHANNEL_TAKEN":
                await bot.send_message(chat_id=message.chat.id, text=_channel_taken_text(), parse_mode=ParseMode.HTML)
                return
            raise
        fresh = await adb(bdb.get_settings(chat_id))
        await bot.send_message(
            chat_id=message.chat.id,
            text=f"{E.CHECK} Channel replaced with <b>{channel.title or channel.username or channel.id}</b>.",
            parse_mode=ParseMode.HTML,
            reply_markup=bind_main_menu(fresh, group_title=message.chat.title or ""),
        )
        return
