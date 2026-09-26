"""Tagging handlers — /all, /tagabort, /allsettings, /tagstats + observers.

Group placement (see config.py):
    0   commands + tag:* callbacks
    15  message activity observer
    16  join/leave service messages + chat_member updates
    17  callback activity observer (catch-all)
"""

from __future__ import annotations

import asyncio
import time

from telegram import Update
from telegram.ext import ContextTypes

from bot.emojis import E
from bot.logger import logger
from bot.responses import action_card, plain_error, plain_ok

from . import (
    activity_tracker,
    config,
    database as tdb,
    member_registry,
    permissions,
    sender,
    session as sess_mod,
    settings as settings_mod,
)
from .exceptions import AdminFetchError, AlreadyRunningError
from .keyboards import settings_keyboard, stats_keyboard
from .models import TagSettings
from .presence import get_manager

# Icons for the settings card rows.
_ROW_ICONS = {
    "mode": E.SETTINGS,
    "window": E.TIME,
    "max": E.USER,
    "batch": E.SPEAKER,
    "send": E.FORWARD,
    "registry": E.FOLDER,
}
_VALUE_OVERRIDES = {
    "mode": lambda s: settings_mod.MODE_LABELS.get(s.mode, s.mode),
    "window": lambda s: settings_mod.window_label(s.window_hours),
    "max": lambda s: settings_mod.max_label(s.max_mentions),
    "batch": lambda s: str(s.batch_size),
    "send": lambda s: settings_mod.SEND_LABELS.get(s.send_mode, s.send_mode),
    "registry": lambda s: settings_mod.REGISTRY_LABELS.get(
        s.registry_mode, s.registry_mode
    ),
}


async def _reply(message, text: str, reply_markup=None):
    """HTML reply helper (E.* are tg-emoji HTML)."""
    return await message.reply_text(
        text, parse_mode="HTML", reply_markup=reply_markup
    )


def _is_group(chat) -> bool:
    return chat is not None and chat.type in ("group", "supergroup")


def settings_card(st: TagSettings) -> str:
    rows = []
    for key, label in (
        ("mode", "Mode"),
        ("window", "Window"),
        ("max", "Max"),
        ("batch", "Batch"),
        ("send", "Send"),
        ("registry", "Registry"),
    ):
        rows.append((_ROW_ICONS[key], label, _VALUE_OVERRIDES[key](st)))
    return action_card("Mass Tag Settings", rows, icon=E.SETTINGS)


# ── /all ──────────────────────────────────────────────────────────

async def all_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if msg is None or chat is None or user is None:
        return

    if not _is_group(chat):
        await _reply(msg, plain_error(config.MSG_NOT_GROUP))
        return
    if msg.reply_to_message is None:
        await _reply(msg, plain_error(config.MSG_NO_REPLY))
        return
    if sess_mod.is_running(chat.id):
        await _reply(msg, plain_error(config.MSG_RUNNING))
        return
    if not await permissions.is_admin(chat.id, user.id, context.bot):
        await _reply(msg, plain_error(config.MSG_NOT_ADMIN))
        return

    get_manager().kick()  # deferred MTProto startup (needs a running loop)

    # Authoritative admin exclusion — fetched fresh for every /all.
    try:
        admins = await permissions.admin_ids(chat.id, context.bot)
    except AdminFetchError:
        await _reply(msg, plain_error(config.MSG_ADMIN_FETCH_FAIL))
        return

    st = settings_mod.get(chat.id)
    source = msg.reply_to_message

    # Status card exists before the session (session needs its message id).
    status = await _reply(msg, sender.start_card(st))

    db_id = 0
    try:
        db_id = tdb.create_session(
            chat.id, user.id, mode=st.mode, window_hours=st.window_hours
        )
        session = sess_mod.create(
            chat_id=chat.id,
            session_id=db_id,
            invoker_id=user.id,
            source_message=source,
            status_message=status,
            settings=st,
            admin_ids=admins,
        )
    except AlreadyRunningError:
        # Lost a race at an await point — clean up the just-made row.
        if db_id:
            try:
                tdb.finish_session(db_id, "aborted", error="duplicate start")
            except Exception:
                pass
        try:
            await status.delete()
        except Exception:
            pass
        await _reply(msg, plain_error(config.MSG_RUNNING))
        return
    except Exception as e:
        logger.error(f"Tagging session create failed chat={chat.id}", exc_info=e)
        if db_id:
            try:
                tdb.finish_session(db_id, "failed", error=type(e).__name__)
            except Exception:
                pass
        try:
            await status.delete()
        except Exception:
            pass
        await _reply(msg, plain_error("Could not start tagging — try again."))
        return

    session.task = asyncio.create_task(sender.run(session, context))


# ── /tagabort ─────────────────────────────────────────────────────

async def tagabort_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if msg is None or chat is None or user is None:
        return

    s = sess_mod.get(chat.id)
    if s is None or not s.running:
        await _reply(msg, plain_error(config.MSG_NO_SESSION))
        return
    if not await permissions.is_admin(chat.id, user.id, context.bot):
        await _reply(msg, plain_error(config.MSG_NOT_ADMIN))
        return

    s.token.cancel()
    try:
        await asyncio.wait_for(s.done.wait(), timeout=8.0)
    except asyncio.TimeoutError:
        await _reply(msg, plain_ok("Stopping after the current message…"))
        return

    if s.state == "completed":
        await _reply(
            msg,
            plain_ok(f"Tagging already finished. Tagged: {s.tagged} / {s.total} users"),
        )
        return
    await _reply(
        msg, plain_ok(config.MSG_STOPPED_FMT.format(tagged=s.tagged, total=s.total))
    )


# ── /allsettings ──────────────────────────────────────────────────

async def allsettings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if msg is None or chat is None or user is None:
        return

    if not _is_group(chat):
        await _reply(msg, plain_error(config.MSG_NOT_GROUP))
        return
    if not await permissions.is_admin(chat.id, user.id, context.bot):
        await _reply(msg, plain_error(config.MSG_NOT_ADMIN))
        return

    args = context.args or []
    if not args:
        st = settings_mod.get(chat.id)
        await _reply(msg, settings_card(st), reply_markup=settings_keyboard(st))
        return

    key_raw = args[0].lower()
    value = " ".join(args[1:]).strip()
    try:
        st = settings_mod.apply_arg(chat.id, key_raw, value)
    except ValueError as e:
        await _reply(msg, plain_error(str(e)))
        return
    await _reply(
        msg,
        action_card(
            "Settings Updated",
            [(E.CHECK, "Setting", f"<b>{key_raw}</b> → {value or 'next'}")],
            icon=E.SETTINGS,
        ),
        reply_markup=settings_keyboard(st),
    )


# ── /tagstats ─────────────────────────────────────────────────────

async def tagstats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if msg is None or chat is None or user is None:
        return

    if not _is_group(chat):
        await _reply(msg, plain_error(config.MSG_NOT_GROUP))
        return
    if not await permissions.is_admin(chat.id, user.id, context.bot):
        await _reply(msg, plain_error(config.MSG_NOT_ADMIN))
        return

    st = settings_mod.get(chat.id)
    stats = tdb.session_stats(chat.id)
    members = tdb.count_members(chat.id)
    now = time.time()
    active_now = tdb.count_active(chat.id, now - config.PRESENCE_TTL)
    active_day = tdb.count_active(chat.id, now - config.ACTIVITY_RANK_DAY)

    card = action_card(
        "Mass Tag Stats",
        [
            (E.USER, "Members known", f"{active_day:,} active 24h / {members:,} total"),
            (E.EYES, "Active now", str(active_now)),
            (E.ANNOUNCE, "Sessions",
             f"{stats['sessions']} total · {stats['completed']} done · "
             f"{stats['stopped']} stopped · {stats['failed']} failed"),
            (E.CHECK, "Users tagged", f"{stats['tagged']:,}"),
            (E.SPEAKER, "Messages sent", f"{stats['messages']:,}"),
            (E.INFO, "Presence source", get_manager().source_label),
            (E.SETTINGS, "Default",
             f"{settings_mod.MODE_LABELS.get(st.mode, st.mode)} · "
             f"{settings_mod.window_label(st.window_hours)} window"),
        ],
        icon=E.ANNOUNCE,
    )
    await _reply(msg, card, reply_markup=stats_keyboard())


# ── tag: callbacks ────────────────────────────────────────────────

async def tag_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """tag:abort + tag:set:<key> (settings cycles)."""
    query = update.callback_query
    if query is None:
        return
    data = query.data or ""
    chat = update.effective_chat
    user = query.from_user
    if chat is None or user is None:
        return

    parts = data.split(":", 2)

    if len(parts) == 2 and parts[1] == "abort":
        await _handle_abort_click(update, context)
        return
    if len(parts) == 3 and parts[1] == "set":
        await _handle_set_click(update, context, parts[2])
        return
    try:
        await query.answer()
    except Exception:
        pass


async def _handle_abort_click(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat = update.effective_chat
    user = query.from_user
    s = sess_mod.get(chat.id) if chat else None
    if s is None or not s.running:
        await _safe_answer(query, config.MSG_NO_SESSION, alert=True)
        return
    if not await permissions.is_admin(chat.id, user.id, context.bot):
        await _safe_answer(query, config.MSG_NOT_ADMIN, alert=True)
        return
    s.token.cancel()
    await _safe_answer(
        query,
        config.MSG_STOPPED_FMT.format(tagged=s.tagged, total=s.total),
    )
    # The sender edits the progress card to the stop card.


async def _handle_set_click(
    update: Update, context: ContextTypes.DEFAULT_TYPE, key: str
) -> None:
    query = update.callback_query
    chat = update.effective_chat
    user = query.from_user
    if chat is None or user is None:
        return
    if not _is_group(chat):
        await _safe_answer(query, config.MSG_NOT_GROUP, alert=True)
        return
    if not await permissions.is_admin(chat.id, user.id, context.bot):
        await _safe_answer(query, config.MSG_NOT_ADMIN, alert=True)
        return
    try:
        st = settings_mod.cycle(chat.id, key)
    except ValueError:
        await _safe_answer(query, "Unknown setting.", alert=True)
        return
    try:
        await query.edit_message_text(
            settings_card(st),
            parse_mode="HTML",
            reply_markup=settings_keyboard(st),
        )
    except Exception as e:
        logger.debug(f"Tagging settings edit failed: {e}")
    await _safe_answer(query, "Updated")


async def _safe_answer(query, text: str, *, alert: bool = False) -> None:
    try:
        await query.answer(text, show_alert=alert)
    except Exception:
        pass


# ── Observers (groups 15/16/17) ───────────────────────────────────

async def activity_observer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Group message → buffered activity touch (no DB in hot path)."""
    get_manager().kick()  # one-time deferred MTProto startup
    msg = update.effective_message
    chat = update.effective_chat
    if msg is None or chat is None or msg.from_user is None:
        return
    if msg.sender_chat is not None:
        return  # anonymous admin / channel post — no real user to tag
    activity_tracker.touch(chat.id, msg.from_user)


async def member_observer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Joins/leaves via service messages → immediate registry writes."""
    msg = update.effective_message
    chat = update.effective_chat
    if msg is None or chat is None:
        return
    for u in msg.new_chat_members or ():
        member_registry.on_join(chat.id, u)
    left = msg.left_chat_member
    if left is not None:
        member_registry.on_leave(chat.id, left)


async def chat_member_observer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """chat_member updates → registry join/leave (large-chat precision)."""
    change = update.chat_member
    if change is None:
        return
    new = change.new_chat_member
    old = change.old_chat_member
    joined = new.status in ("member", "administrator") and old.status in (
        "left", "kicked", "banned", "restricted"
    )
    left = new.status in ("left", "kicked", "banned")
    if joined:
        member_registry.on_join(change.chat.id, new.user)
    elif left:
        member_registry.on_leave(change.chat.id, new.user)


async def callback_activity_observer(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Any callback press counts as activity (group chats only)."""
    query = update.callback_query
    chat = update.effective_chat
    if query is None or chat is None or query.from_user is None:
        return
    if not _is_group(chat):
        return
    activity_tracker.touch(chat.id, query.from_user)
