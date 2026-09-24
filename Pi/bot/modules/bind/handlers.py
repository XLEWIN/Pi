"""Bind module handlers — /bind, /bindmenu, force-join gate, join tracking."""

import asyncio
import logging
from typing import Any, Dict, Optional

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, filters

from bot.emojis import E

from . import database as bdb
from .checks import (
    in_grace,
    is_bot_admin,
    is_channel_member,
    is_group_admin,
    should_enforce,
)
from .config import (
    HANDLER_GROUP,
    JOIN_TRACKER_GROUP,
    PLACEHOLDERS_HELP,
)
from .keyboards import bind_main_menu, force_join_keyboard, help_bind_text, replace_confirm_menu
from .utils import (
    channel_display,
    format_grace,
    parse_channel_ref,
    render_custom_message,
    spawn,
    user_mention_html,
)

logger = logging.getLogger(__name__)


async def _resolve_channel(context: ContextTypes.DEFAULT_TYPE, ref: str):
    """Resolve a channel reference to a Chat-like object, or raise ValueError."""
    parsed = parse_channel_ref(ref)
    if not parsed:
        raise ValueError("Could not parse that channel reference.")
    channel_id, username, _link = parsed

    if username:
        try:
            return await context.bot.get_chat(username)
        except Exception as e:
            raise ValueError(f"Channel @{username} not found or bot can't access it. ({e})") from e

    try:
        return await context.bot.get_chat(channel_id)
    except Exception as e:
        raise ValueError(f"Chat {channel_id} not found or bot can't access it. ({e})") from e


async def _bot_is_channel_admin(context: ContextTypes.DEFAULT_TYPE, chat) -> bool:
    """Bot must be able to call getChatMember on the channel."""
    try:
        me = await context.bot.get_me()
        member = await context.bot.get_chat_member(chat.id, me.id)
        return member.status in ("administrator", "creator", "member")
    except Exception:
        # Some public channels allow member checks without admin — try anyway.
        return True


def _channel_link(chat) -> Optional[str]:
    if getattr(chat, "username", None):
        return f"https://t.me/{chat.username}"
    # Private invite links are not available via Bot API without export.
    return None


async def bind_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /bind [channel] — bind this group to a channel."""
    if not update.message or not update.effective_chat or update.effective_chat.type == "private":
        if update.message:
            await update.message.reply_text(f"{E.ERROR} This command only works in groups.", parse_mode=ParseMode.HTML)
        return

    chat_id = update.effective_chat.id
    user = update.effective_user

    if not await is_group_admin(context.bot, chat_id, user.id):
        await update.message.reply_text(
            f"{E.ERROR} Only group admins can bind a channel.",
            parse_mode=ParseMode.HTML,
        )
        return

    existing = bdb.get_settings(chat_id)

    # No argument → show status / usage.
    if not context.args:
        if existing:
            link = existing.get("channel_link") or ""
            btn = bind_main_menu(existing, group_title=update.effective_chat.title or "")
            await update.message.reply_text(
                f"{E.CHECK} <b>Already bound</b> to {channel_display(existing)}.\n"
                f"Force Join: <b>{'ON' if existing.get('force_join') else 'OFF'}</b> • "
                f"Grace: <b>{format_grace(existing.get('grace_minutes') or 0)}</b>\n\n"
                f"Use <code>/bind @newchannel</code> to replace, or <code>/bindmenu</code> to configure.",
                parse_mode=ParseMode.HTML,
                reply_markup=btn,
                disable_web_page_preview=True,
            )
        else:
            await update.message.reply_text(
                f"{E.INFO} Not bound yet.\n\n{help_bind_text()}",
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        return

    ref = context.args[0]
    try:
        channel = await _resolve_channel(context, ref)
    except ValueError as e:
        await update.message.reply_text(f"{E.ERROR} {e}", parse_mode=ParseMode.HTML)
        return

    if channel.type not in ("channel", "supergroup", "group"):
        await update.message.reply_text(
            f"{E.ERROR} Target must be a channel or supergroup.",
            parse_mode=ParseMode.HTML,
        )
        return

    if not await _bot_is_channel_admin(context, channel):
        await update.message.reply_text(
            f"{E.ERROR} I can't check membership in that chat. "
            "Add me as an admin of the channel (or make sure it's public).",
            parse_mode=ParseMode.HTML,
        )
        return

    # One channel ↔ one group: reject if another GC already holds this channel.
    other = bdb.find_other_binding(channel.id, chat_id)
    if other:
        await update.message.reply_text(_channel_taken_text(), parse_mode=ParseMode.HTML)
        return

    # Already bound to a different channel → confirm replace.
    if existing and existing.get("channel_id") and existing["channel_id"] != channel.id:
        # Stash pending target in chat_data for confirm callback.
        context.chat_data["bind_pending_channel"] = {
            "id": channel.id,
            "username": channel.username,
            "title": channel.title,
            "link": _channel_link(channel),
        }
        await update.message.reply_text(
            f"{E.WARNING} Already bound to {channel_display(existing)}.\n"
            f"Replace with <b>{channel.title or channel.username or channel.id}</b>?",
            parse_mode=ParseMode.HTML,
            reply_markup=replace_confirm_menu(),
        )
        return

    _apply_binding(update, context, channel, bound_by=user.id)


def _channel_taken_text() -> str:
    return (
        f"{E.ERROR} <b>Channel already bound.</b>\n\n"
        "One channel can only be bound to one group at a time.\n"
        "Unbind it from the other group first, or pick a different channel."
    )


def _apply_binding(update: Update, context: ContextTypes.DEFAULT_TYPE, channel, bound_by: int) -> None:
    chat_id = update.effective_chat.id
    link = _channel_link(channel)
    try:
        bdb.upsert_binding(
            chat_id,
            channel.id,
            channel_username=channel.username,
            channel_title=channel.title,
            channel_link=link,
            bound_by=bound_by,
        )
    except ValueError as e:
        if str(e) == "CHANNEL_TAKEN":
            spawn(update.message.reply_text(_channel_taken_text(), parse_mode=ParseMode.HTML))
            return
        raise
    settings = bdb.get_settings(chat_id)
    title = channel.title or (f"@{channel.username}" if channel.username else str(channel.id))
    text = (
        f"{E.CHECK} <b>Group bound successfully!</b>\n\n"
        f"{E.ANNOUNCE} Channel: <b>{title}</b>\n"
        f"{E.CHECK} Force Join: <b>{'ON' if settings.get('force_join') else 'OFF'}</b>\n"
        f"{E.CROWN} Admin bypass: <b>{'ON' if settings.get('admin_bypass') else 'OFF'}</b>\n"
        f"{E.CLOCK} Grace: <b>{format_grace(settings.get('grace_minutes') or 0)}</b>\n\n"
        f"{E.SETTINGS} Open <code>/bindmenu</code> to configure gates and the message."
    )
    # Fire reply first; heavy work already done above (sync SQLite is fine here).
    spawn(
        update.message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=bind_main_menu(settings, group_title=update.effective_chat.title or ""),
            disable_web_page_preview=True,
        )
    )


async def bindmenu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /bindmenu — open the configuration panel."""
    if not update.message or not update.effective_chat or update.effective_chat.type == "private":
        if update.message:
            await update.message.reply_text(f"{E.ERROR} This command only works in groups.", parse_mode=ParseMode.HTML)
        return

    chat_id = update.effective_chat.id
    user = update.effective_user

    # Opening /bindmenu cancels any pending custom-message / channel prompt.
    context.chat_data.pop("bind_wait", None)

    if not await is_group_admin(context.bot, chat_id, user.id):
        await update.message.reply_text(
            f"{E.ERROR} Only group admins can open the bind menu.",
            parse_mode=ParseMode.HTML,
        )
        return

    settings = bdb.get_settings(chat_id)
    if not settings:
        await update.message.reply_text(
            f"{E.INFO} Not bound yet.\n\n{help_bind_text()}",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return

    await update.message.reply_text(
        f"{E.SETTINGS} <b>Bind Menu</b>\n"
        f"{E.ANNOUNCE} {channel_display(settings)}\n"
        f"Force Join <b>{'✅ ON' if settings.get('force_join') else '❌ OFF'}</b>\n"
        f"Admin bypass <b>{'✅ ON' if settings.get('admin_bypass') else '❌ OFF'}</b>\n"
        f"Gates active: <b>{_active_gate_count(settings)}</b>\n"
        f"{E.CLOCK} Grace: <b>{format_grace(settings.get('grace_minutes') or 0)}</b> • "
        f"Auto-delete: <b>{settings.get('auto_delete_seconds') or 0}s</b>\n\n"
        f"<i>Placeholders:</i> <code>{PLACEHOLDERS_HELP}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=bind_main_menu(settings, group_title=update.effective_chat.title or ""),
        disable_web_page_preview=True,
    )


def _active_gate_count(settings: Dict[str, Any]) -> int:
    from .config import GATES

    return sum(1 for col in GATES.values() if int(settings.get(col) or 0))


async def gate_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Enforce force-join / message-type gates on incoming group messages."""
    message = update.message
    if not message:
        return
    chat = update.effective_chat
    if not chat or chat.type == "private":
        return

    chat_id = chat.id
    settings = bdb.get_settings(chat_id)
    if not settings or not settings.get("channel_id"):
        return

    # Never gate service/pin/poll-only noise we don't handle; still allow text/media.
    user = message.from_user
    if user is None:
        return

    # Quick path: record first-seen for grace bookkeeping.
    if not bdb.get_join_time(chat_id, user.id):
        # Do NOT invent a grace window — leave unknown → no grace (see in_grace).
        pass

    is_admin = False
    if int(settings.get("admin_bypass") or 1):
        is_admin = await is_group_admin(context.bot, chat_id, user.id)

    join_ts = bdb.get_join_time(chat_id, user.id)
    grace_ok = in_grace(join_ts, int(settings.get("grace_minutes") or 0))

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
    if await is_channel_member(context.bot, channel_id, user.id, fresh=False):
        # Cache said member — allow. Also persist join for future grace math.
        bdb.record_join(chat_id, user.id)
        return

    # Not a member → gate: delete + warn.
    try:
        from bot.database import db as _pdb
        _pdb.bump_bind_fails(chat_id, 1)
        _pdb.record_reputation_event(user.id, "warning", 1)
    except Exception as _e:
        logger.debug(f"bind fail counter: {_e}")

    if not await is_bot_admin(context.bot, chat_id):
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
        warning = await context.bot.send_message(
            chat_id=chat_id,
            text=f"{E.ANNOUNCE} {body}",
            parse_mode=ParseMode.HTML,
            reply_to_message_id=None,
            reply_markup=force_join_keyboard(channel_link, channel_title),
            disable_web_page_preview=True,
        )
        bdb.add_warning(chat_id, warning.message_id, user.id)
        delay = int(settings.get("auto_delete_seconds") or 0)
        if delay > 0:
            spawn(_auto_delete(context, chat_id, warning.message_id, delay))
    except Exception as e:
        logger.warning(f"bind warning send failed: {e}")


async def _auto_delete(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, delay: int) -> None:
    try:
        await asyncio.sleep(delay)
        await context.bot.delete_message(chat_id, message_id)
    except Exception:
        pass
    finally:
        try:
            bdb.remove_warning(chat_id, message_id)
        except Exception:
            pass


async def join_tracker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Record group-join timestamps for grace period math."""
    message = update.message
    if not message or not update.effective_chat or update.effective_chat.type == "private":
        return

    chat_id = update.effective_chat.id
    if not message.new_chat_members:
        return

    # Only track when this chat is bound (saves writes).
    if not bdb.get_settings(chat_id):
        return

    import time

    now = time.time()
    for member in message.new_chat_members:
        if member.is_bot:
            continue
        if member.id == context.bot.id:
            continue
        bdb.record_join(chat_id, member.id, joined_at=now)

    # left_chat_member → clear so rejoin gets a fresh stamp.
    if message.left_chat_member and not message.left_chat_member.is_bot:
        bdb.clear_join(chat_id, message.left_chat_member.id)


async def waiting_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Consume admin text when waiting for custom message or new channel."""
    message = update.message
    if not message or not update.effective_chat or update.effective_chat.type == "private":
        return

    wait = context.chat_data.get("bind_wait")
    if not wait:
        return

    chat_id = update.effective_chat.id
    user = update.effective_user
    if not await is_group_admin(context.bot, chat_id, user.id):
        return

    settings = bdb.get_settings(chat_id)
    if not settings:
        context.chat_data.pop("bind_wait", None)
        return

    if wait == "custom":
        context.chat_data.pop("bind_wait", None)
        text = (message.text or "").strip()
        if not text:
            await message.reply_text(f"{E.ERROR} Empty message — send non-empty text or /bindmenu.", parse_mode=ParseMode.HTML)
            return
        # Show placeholders that survived / were typed.
        bdb.update_field(chat_id, "custom_message", text)
        try:
            await message.delete()
        except Exception:
            pass
        await update.effective_chat.send_message(
            f"{E.CHECK} Custom force-join message saved.\n"
            f"<i>Placeholders:</i> <code>{PLACEHOLDERS_HELP}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    if wait == "channel":
        context.chat_data.pop("bind_wait", None)
        raw = (message.text or "").strip()
        try:
            channel = await _resolve_channel(context, raw)
        except ValueError as e:
            await update.effective_chat.send_message(f"{E.ERROR} {e}", parse_mode=ParseMode.HTML)
            return
        if channel.type not in ("channel", "supergroup", "group"):
            await update.effective_chat.send_message(
                f"{E.ERROR} Target must be a channel or supergroup.", parse_mode=ParseMode.HTML
            )
            return
        if bdb.find_other_binding(channel.id, chat_id):
            await update.effective_chat.send_message(_channel_taken_text(), parse_mode=ParseMode.HTML)
            return
        link = _channel_link(channel)
        try:
            bdb.upsert_binding(
                chat_id,
                channel.id,
                channel_username=channel.username,
                channel_title=channel.title,
                channel_link=link,
                bound_by=user.id,
            )
        except ValueError as e:
            if str(e) == "CHANNEL_TAKEN":
                await update.effective_chat.send_message(_channel_taken_text(), parse_mode=ParseMode.HTML)
                return
            raise
        fresh = bdb.get_settings(chat_id)
        await update.effective_chat.send_message(
            f"{E.CHECK} Channel replaced with <b>{channel.title or channel.username or channel.id}</b>.",
            parse_mode=ParseMode.HTML,
            reply_markup=bind_main_menu(fresh, group_title=update.effective_chat.title or ""),
        )
        return
