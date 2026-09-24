"""Security & Anti-Raid — Group Shield for sudden attacks.

Detects join bursts and message floods, optional auto-action, lockdown.
Commands (groups, admin):
  /shield [on|off]           — view / toggle shield
  /shieldcfg joins N SEC     — join-burst threshold
  /shieldcfg msgs N SEC      — message-burst threshold
  /shieldcfg action ALERT|MUTE|KICK|BAN
  /lockdown [on|off]         — freeze non-admin messaging
  /raidlog [n]               — recent raid events
"""

import logging
import time
from collections import defaultdict, deque
from html import escape
from typing import Deque, Dict, Optional, Tuple

from telegram import ChatMember, ChatPermissions, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bot.database import db
from bot.emojis import E
from bot.responses import action_card, field_extra, field_user, reply_card

logger = logging.getLogger(__name__)

# ── In-memory sliding windows ───────────────────────────
# chat_id -> deque[timestamps]
_join_times: Dict[int, Deque[float]] = defaultdict(deque)
# (chat_id, user_id) -> deque[timestamps]
_msg_times: Dict[Tuple[int, int], Deque[float]] = defaultdict(deque)
# chat_id -> last alert unix time (cooldown so we don't spam)
_alert_cooldown: Dict[int, float] = {}
ALERT_COOLDOWN_SEC = 30

# Cap tracked users so memory stays bounded.
_MAX_MSG_KEYS = 2000


async def _is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        member = await context.bot.get_chat_member(
            update.effective_chat.id, update.effective_user.id
        )
        return member.status in (
            ChatMember.ADMINISTRATOR,
            ChatMember.OWNER,
            "administrator",
            "creator",
        )
    except Exception:
        return False


def _on_cooldown(chat_id: int) -> bool:
    now = time.time()
    last = _alert_cooldown.get(chat_id, 0)
    if now - last < ALERT_COOLDOWN_SEC:
        return True
    _alert_cooldown[chat_id] = now
    return False


def _prune_deque(d: Deque[float], window: int) -> None:
    cutoff = time.time() - window
    while d and d[0] < cutoff:
        d.popleft()


async def _take_action(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    action: str,
    reason: str,
) -> bool:
    chat_id = update.effective_chat.id
    try:
        if action == "mute":
            await context.bot.restrict_chat_member(
                chat_id, user_id, ChatPermissions(can_send_messages=False)
            )
        elif action == "kick":
            await context.bot.ban_chat_member(chat_id, user_id)
            await context.bot.unban_chat_member(chat_id, user_id)
        elif action == "ban":
            await context.bot.ban_chat_member(chat_id, user_id)
        else:
            return False
        db.bump_mod_actions(chat_id, 1)
        db.record_reputation_event(user_id, "restriction", 1)
        return True
    except Exception as e:
        logger.warning(f"shield action failed: {e}")
        return False


async def _set_lockdown(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, enabled: bool
) -> bool:
    """Toggle lockdown flag only.

    Bot API cannot list all members to bulk-restrict, so enforcement is:
    while lockdown=1, enforce_lockdown() deletes non-admin messages.
    """
    try:
        db.set_shield_settings(chat_id, lockdown=1 if enabled else 0)
        return True
    except Exception as e:
        logger.warning(f"lockdown toggle failed: {e}")
        return False


async def enforce_lockdown(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """While lockdown is ON, delete messages from non-admins."""
    if not update.message or update.effective_chat.type == "private":
        return
    chat_id = update.effective_chat.id
    settings = db.get_shield_settings(chat_id)
    if not settings.get("lockdown"):
        return
    user = update.effective_user
    if not user or user.is_bot or user.id == context.bot.id:
        return
    try:
        member = await context.bot.get_chat_member(chat_id, user.id)
        if member.status in (
            ChatMember.ADMINISTRATOR,
            ChatMember.OWNER,
            "administrator",
            "creator",
        ):
            return
    except Exception:
        return
    try:
        await update.message.delete()
    except Exception:
        pass


# ── Detection ───────────────────────────────────────────

async def detect_join_burst(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Count new-member events per chat in a sliding window."""
    if not update.message or not update.message.new_chat_members:
        return
    if update.effective_chat.type == "private":
        return

    chat_id = update.effective_chat.id
    settings = db.get_shield_settings(chat_id)
    if not settings.get("shield_enabled", 1):
        return

    window = int(settings.get("join_window") or 15)
    limit = int(settings.get("join_limit") or 8)
    now = time.time()
    dq = _join_times[chat_id]
    for _ in update.message.new_chat_members:
        dq.append(now)
    _prune_deque(dq, window)

    if len(dq) < limit:
        return
    if _on_cooldown(chat_id):
        return

    # Clear window so one burst = one alert.
    dq.clear()
    count = limit
    detail = f"{count} joins in {window}s"
    db.log_raid_event(chat_id, "join_burst", detail, count)

    names = ", ".join(
        (m.first_name or m.username or str(m.id))
        for m in update.message.new_chat_members[:5]
    )
    action = str(settings.get("action") or "alert").lower()
    text = action_card(
        "JOIN BURST DETECTED",
        [
            field_extra(E.ALERT, "DETAIL", escape(detail)),
            field_extra(E.USER, "SAMPLE", escape(names[:120])),
            field_extra(E.SETTINGS, "ACTION", escape(action.upper())),
        ],
        icon=E.ALERT,
    )
    try:
        await update.message.reply_text(
            text, parse_mode=ParseMode.HTML, quote=False
        )
    except Exception as e:
        logger.warning(f"raid alert send failed: {e}")

    if action in ("mute", "kick", "ban"):
        for member in update.message.new_chat_members:
            if member.is_bot or member.id == context.bot.id:
                continue
            await _take_action(update, context, member.id, action, "Raid join burst")


async def detect_message_burst(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Count messages per user in a sliding window."""
    if not update.message or not update.effective_user:
        return
    if update.effective_chat.type == "private":
        return

    user = update.effective_user
    if user.is_bot:
        return

    chat_id = update.effective_chat.id
    settings = db.get_shield_settings(chat_id)
    if not settings.get("shield_enabled", 1):
        return

    # Admins are never flood-punished.
    try:
        member = await context.bot.get_chat_member(chat_id, user.id)
        if member.status in (
            ChatMember.ADMINISTRATOR,
            ChatMember.OWNER,
            "administrator",
            "creator",
        ):
            return
    except Exception:
        pass

    window = int(settings.get("msg_window") or 5)
    limit = int(settings.get("msg_limit") or 10)
    key = (chat_id, user.id)
    if len(_msg_times) > _MAX_MSG_KEYS:
        _msg_times.clear()
    dq = _msg_times[key]
    dq.append(time.time())
    _prune_deque(dq, window)

    if len(dq) < limit:
        return
    if _on_cooldown(chat_id):
        return

    dq.clear()
    detail = f"{limit} msgs in {window}s by {user.id}"
    db.log_raid_event(chat_id, "message_burst", detail, limit)
    action = str(settings.get("action") or "alert").lower()

    text = action_card(
        "MESSAGE BURST DETECTED",
        [
            field_user(user),
            field_extra(E.ALERT, "DETAIL", escape(detail)),
            field_extra(E.SETTINGS, "ACTION", escape(action.upper())),
        ],
        icon=E.ALERT,
    )
    try:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.warning(f"flood alert send failed: {e}")

    if action in ("mute", "kick", "ban"):
        await _take_action(update, context, user.id, action, "Message flood")


# ── Commands ────────────────────────────────────────────

async def shield_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await update.message.reply_text(
            f"{E.INFO} This command only works in groups.", parse_mode=ParseMode.HTML
        )
        return
    if not await _is_admin(update, context):
        await update.message.reply_text(
            f"{E.ERROR} Only admins can manage the shield.", parse_mode=ParseMode.HTML
        )
        return

    chat_id = update.effective_chat.id
    settings = db.get_shield_settings(chat_id)

    if context.args:
        arg = context.args[0].lower()
        if arg in ("on", "enable"):
            db.set_shield_settings(chat_id, shield_enabled=1)
            await update.message.reply_text(
                f"{E.CHECK} Shield <b>ON</b>.", parse_mode=ParseMode.HTML
            )
            return
        if arg in ("off", "disable"):
            db.set_shield_settings(chat_id, shield_enabled=0)
            await update.message.reply_text(
                f"{E.CROSS} Shield <b>OFF</b>.", parse_mode=ParseMode.HTML
            )
            return

    on = "ON" if settings.get("shield_enabled") else "OFF"
    lock = "ON" if settings.get("lockdown") else "OFF"
    text = action_card(
        "GROUP SHIELD",
        [
            field_extra(E.CHECK, "STATUS", on),
            field_extra(E.ALERT, "JOIN BURST", f"{settings.get('join_limit')} / {settings.get('join_window')}s"),
            field_extra(E.FIRE, "MSG BURST", f"{settings.get('msg_limit')} / {settings.get('msg_window')}s"),
            field_extra(E.SETTINGS, "ACTION", str(settings.get("action") or "alert").upper()),
            field_extra(E.BAN, "LOCKDOWN", lock),
        ],
        icon=E.ALERT,
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def shieldcfg_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await update.message.reply_text(
            f"{E.INFO} This command only works in groups.", parse_mode=ParseMode.HTML
        )
        return
    if not await _is_admin(update, context):
        await update.message.reply_text(
            f"{E.ERROR} Only admins can configure the shield.", parse_mode=ParseMode.HTML
        )
        return

    usage = (
        f"{E.INFO} <b>Shield config</b>\n\n"
        "/shieldcfg joins &lt;count&gt; &lt;seconds&gt;\n"
        "/shieldcfg msgs &lt;count&gt; &lt;seconds&gt;\n"
        "/shieldcfg action &lt;alert|mute|kick|ban&gt;"
    )
    if not context.args:
        await update.message.reply_text(usage, parse_mode=ParseMode.HTML)
        return

    chat_id = update.effective_chat.id
    key = context.args[0].lower()

    if key in ("joins", "join") and len(context.args) >= 3:
        try:
            n, sec = int(context.args[1]), int(context.args[2])
            if n < 2 or sec < 1:
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                f"{E.ERROR} Use: joins &lt;2+&gt; &lt;seconds&gt;",
                parse_mode=ParseMode.HTML,
            )
            return
        db.set_shield_settings(chat_id, join_limit=n, join_window=sec)
        await update.message.reply_text(
            f"{E.CHECK} Join burst: <b>{n}</b> joins / <b>{sec}s</b>.",
            parse_mode=ParseMode.HTML,
        )
        return

    if key in ("msgs", "messages", "msg") and len(context.args) >= 3:
        try:
            n, sec = int(context.args[1]), int(context.args[2])
            if n < 3 or sec < 1:
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                f"{E.ERROR} Use: msgs &lt;3+&gt; &lt;seconds&gt;",
                parse_mode=ParseMode.HTML,
            )
            return
        db.set_shield_settings(chat_id, msg_limit=n, msg_window=sec)
        await update.message.reply_text(
            f"{E.CHECK} Message burst: <b>{n}</b> msgs / <b>{sec}s</b>.",
            parse_mode=ParseMode.HTML,
        )
        return

    if key == "action" and len(context.args) >= 2:
        action = context.args[1].lower()
        if action not in ("alert", "mute", "kick", "ban"):
            await update.message.reply_text(
                f"{E.ERROR} Choose: alert, mute, kick, or ban.",
                parse_mode=ParseMode.HTML,
            )
            return
        db.set_shield_settings(chat_id, action=action)
        await update.message.reply_text(
            f"{E.CHECK} Shield action: <b>{action.upper()}</b>.",
            parse_mode=ParseMode.HTML,
        )
        return

    await update.message.reply_text(usage, parse_mode=ParseMode.HTML)


async def lockdown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await update.message.reply_text(
            f"{E.INFO} This command only works in groups.", parse_mode=ParseMode.HTML
        )
        return
    if not await _is_admin(update, context):
        await update.message.reply_text(
            f"{E.ERROR} Only admins can use lockdown.", parse_mode=ParseMode.HTML
        )
        return

    chat_id = update.effective_chat.id
    settings = db.get_shield_settings(chat_id)
    enable: Optional[bool] = None
    if context.args:
        arg = context.args[0].lower()
        if arg in ("on", "enable", "1"):
            enable = True
        elif arg in ("off", "disable", "0"):
            enable = False

    if enable is None:
        enable = not bool(settings.get("lockdown"))

    if enable:
        db.set_shield_settings(chat_id, lockdown=1)
        db.log_raid_event(chat_id, "lockdown", "Lockdown enabled by admin", 1)
        await _set_lockdown(context, chat_id, True)
        await update.message.reply_text(
            f"{E.ALERT} Lockdown <b>ON</b> — non-admin messages will be deleted "
            "until you run /lockdown off.",
            parse_mode=ParseMode.HTML,
        )
    else:
        db.set_shield_settings(chat_id, lockdown=0)
        db.log_raid_event(chat_id, "lockdown", "Lockdown disabled by admin", 1)
        await _set_lockdown(context, chat_id, False)
        await update.message.reply_text(
            f"{E.CHECK} Lockdown <b>OFF</b>.", parse_mode=ParseMode.HTML
        )


async def raidlog_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await update.message.reply_text(
            f"{E.INFO} This command only works in groups.", parse_mode=ParseMode.HTML
        )
        return
    if not await _is_admin(update, context):
        await update.message.reply_text(
            f"{E.ERROR} Only admins can view the raid log.", parse_mode=ParseMode.HTML
        )
        return

    limit = 10
    if context.args:
        try:
            limit = max(1, min(25, int(context.args[0])))
        except ValueError:
            pass

    events = db.get_raid_events(update.effective_chat.id, limit=limit)
    if not events:
        await update.message.reply_text(
            f"{E.CHECK} No raid events recorded.", parse_mode=ParseMode.HTML
        )
        return

    lines = [f"{E.ALERT} <b>RAID LOG~</b>", ""]
    for e in events:
        kind = str(e.get("kind") or "?").upper()
        detail = escape(str(e.get("detail") or ""))
        ts = str(e.get("created_at") or "")[:19]
        lines.append(f"• <b>{kind}</b> — {detail}")
        if ts:
            lines.append(f"  <code>{ts}</code>")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ── Setup ───────────────────────────────────────────────

def setup(app: Application) -> list:
    group_filter = filters.ChatType.GROUPS

    app.add_handler(CommandHandler("shield", shield_command, filters=group_filter))
    app.add_handler(CommandHandler("shieldcfg", shieldcfg_command, filters=group_filter))
    app.add_handler(CommandHandler("lockdown", lockdown_command, filters=group_filter))
    app.add_handler(CommandHandler("raidlog", raidlog_command, filters=group_filter))

    # Joins: after welcome(10)/bind join(11) → group 12.
    app.add_handler(
        MessageHandler(
            group_filter & filters.StatusUpdate.NEW_CHAT_MEMBERS,
            detect_join_burst,
        ),
        group=12,
    )
    # Message flood: after leveling XP (5) → group 6.
    app.add_handler(
        MessageHandler(
            group_filter & ~filters.COMMAND & ~filters.StatusUpdate.ALL,
            detect_message_burst,
        ),
        group=6,
    )
    # Lockdown gate: delete non-admin messages while lockdown=ON.
    app.add_handler(
        MessageHandler(
            group_filter & ~filters.StatusUpdate.ALL,
            enforce_lockdown,
        ),
        group=8,
    )

    return [
        "/shield", "/shieldcfg", "/lockdown", "/raidlog",
        "join-burst", "msg-burst", "lockdown",
    ]
