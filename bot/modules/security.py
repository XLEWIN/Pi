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

from aiogram import Bot, F
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.filters.logic import and_f
from aiogram.types import ChatPermissions, Message

from bot.command_handler import COMMAND
from bot.database import db
from bot.emojis import E
from bot.pipeline import GROUPS, SERVICE, cmd, on
from bot.reply import reply_text
from bot.responses import action_card, field_extra, field_user, reply_card
from bot.async_bridge import adb

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

# ── Cached admin check ──────────────────────────────────
# get_chat_member is a Telegram API round-trip; the message-burst handler
# used to make it on EVERY group message. 30s cache → first message pays
# the API call, the rest are a dict lookup. Errors are never cached.
_admin_cache: Dict[Tuple[int, int], Tuple[float, bool]] = {}
_ADMIN_CACHE_TTL = 30.0
_ADMIN_CACHE_MAX = 10000


async def _member_is_admin(bot, chat_id: int, user_id: int) -> Optional[bool]:
    """True/False = known admin status, None = unknown (API error)."""
    now = time.time()
    hit = _admin_cache.get((chat_id, user_id))
    if hit is not None and hit[0] > now:
        return hit[1]
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except Exception:
        return None  # callers keep the old bare-except semantics
    ok = member.status in (
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.CREATOR,
        "administrator",
        "creator",
    )
    if len(_admin_cache) >= _ADMIN_CACHE_MAX:
        _admin_cache.clear()
    _admin_cache[(chat_id, user_id)] = (now + _ADMIN_CACHE_TTL, ok)
    return ok


async def _is_admin(message: Message, bot: Bot) -> bool:
    return await _member_is_admin(
        bot, message.chat.id, message.from_user.id
    ) is True


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
    message: Message,
    bot: Bot,
    user_id: int,
    action: str,
    reason: str,
) -> bool:
    chat_id = message.chat.id
    try:
        if action == "mute":
            await bot.restrict_chat_member(
                chat_id, user_id, ChatPermissions(can_send_messages=False)
            )
        elif action == "kick":
            await bot.ban_chat_member(chat_id, user_id)
            await bot.unban_chat_member(chat_id, user_id)
        elif action == "ban":
            await bot.ban_chat_member(chat_id, user_id)
        else:
            return False
        await adb(db.bump_mod_actions(chat_id, 1))
        await adb(db.record_reputation_event(user_id, "restriction", 1))
        return True
    except Exception as e:
        logger.warning(f"shield action failed: {e}")
        return False


async def _set_lockdown(
    chat_id: int, enabled: bool
) -> bool:
    """Toggle lockdown flag only.

    Bot API cannot list all members to bulk-restrict, so enforcement is:
    while lockdown=1, enforce_lockdown() deletes non-admin messages.
    """
    try:
        await adb(db.set_shield_settings(chat_id, lockdown=1 if enabled else 0
        ))
        return True
    except Exception as e:
        logger.warning(f"lockdown toggle failed: {e}")
        return False


async def enforce_lockdown(
    message: Message, bot: Bot
) -> None:
    """While lockdown is ON, delete messages from non-admins."""
    if not message or message.chat.type == "private":
        return
    chat_id = message.chat.id
    settings = await adb(db.get_shield_settings(chat_id))
    if not settings.get("lockdown"):
        return
    user = message.from_user
    if not user or user.is_bot or user.id == bot.id:
        return
    # None (API error) and True (admin) both keep the old early-return.
    if await _member_is_admin(bot, chat_id, user.id) is not False:
        return
    try:
        await message.delete()
    except Exception:
        pass


# ── Detection ───────────────────────────────────────────

async def detect_join_burst(
    message: Message, bot: Bot
) -> None:
    """Count new-member events per chat in a sliding window."""
    if not message or not message.new_chat_members:
        return
    if message.chat.type == "private":
        return

    chat_id = message.chat.id
    settings = await adb(db.get_shield_settings(chat_id))
    if not settings.get("shield_enabled", 1):
        return

    window = int(settings.get("join_window") or 15)
    limit = int(settings.get("join_limit") or 8)
    now = time.time()
    dq = _join_times[chat_id]
    for _ in message.new_chat_members:
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
    await adb(db.log_raid_event(chat_id, "join_burst", detail, count))

    names = ", ".join(
        (m.first_name or m.username or str(m.id))
        for m in message.new_chat_members[:5]
    )
    action = str(settings.get("action") or "alert").lower()
    text = action_card(
        "Join Burst Detected",
        [
            field_extra(E.ALERT, "Detail", escape(detail)),
            field_extra(E.USER, "Sample", escape(names[:120])),
            field_extra(E.SETTINGS, "Action", escape(action.capitalize())),
        ],
        icon=E.ALERT,
    )
    try:
        await reply_text(
            message,
            text, parse_mode=ParseMode.HTML, quote=False
        )
    except Exception as e:
        logger.warning(f"raid alert send failed: {e}")

    if action in ("mute", "kick", "ban"):
        for member in message.new_chat_members:
            if member.is_bot or member.id == bot.id:
                continue
            await _take_action(message, bot, member.id, action, "Raid join burst")


async def detect_message_burst(
    message: Message, bot: Bot
) -> None:
    """Count messages per user in a sliding window."""
    if not message or not message.from_user:
        return
    if message.chat.type == "private":
        return

    user = message.from_user
    if user.is_bot:
        return

    chat_id = message.chat.id
    settings = await adb(db.get_shield_settings(chat_id))
    if not settings.get("shield_enabled", 1):
        return

    window = int(settings.get("msg_window") or 5)
    limit = int(settings.get("msg_limit") or 10)
    key = (chat_id, user.id)
    if len(_msg_times) > _MAX_MSG_KEYS:
        _msg_times.clear()
    dq = _msg_times[key]
    dq.append(time.time())
    _prune_deque(dq, window)

    # Count FIRST, then bail. The admin lookup and the alert cooldown both
    # used to run before this early-out, so every message in a shielded
    # group paid a Telegram round-trip (first message per (chat,user) in
    # each 30s window) and, worse, _on_cooldown *armed* itself on the
    # way past. Order below matches the original: threshold -> admin ->
    # cooldown, so an admin's burst still never arms the alert cooldown.
    if len(dq) < limit:
        return

    # Admins are never flood-punished (cached — was a TG API call per message).
    if await _member_is_admin(bot, chat_id, user.id) is True:
        return
    if _on_cooldown(chat_id):
        return

    dq.clear()
    detail = f"{limit} msgs in {window}s by {user.id}"
    await adb(db.log_raid_event(chat_id, "message_burst", detail, limit))
    action = str(settings.get("action") or "alert").lower()

    text = action_card(
        "Message Burst Detected",
        [
            field_user(user),
            field_extra(E.ALERT, "Detail", escape(detail)),
            field_extra(E.SETTINGS, "Action", escape(action.capitalize())),
        ],
        icon=E.ALERT,
    )
    try:
        await reply_text(message, text, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.warning(f"flood alert send failed: {e}")

    if action in ("mute", "kick", "ban"):
        await _take_action(message, bot, user.id, action, "Message flood")


# ── Commands ────────────────────────────────────────────

async def shield_command(message: Message, bot: Bot, args: list):
    if message.chat.type == "private":
        await reply_text(
            message,
            f"{E.INFO} This command only works in groups.", parse_mode=ParseMode.HTML
        )
        return
    if not await _is_admin(message, bot):
        await reply_text(
            message,
            f"{E.ERROR} Only admins can manage the shield.", parse_mode=ParseMode.HTML
        )
        return

    chat_id = message.chat.id
    settings = await adb(db.get_shield_settings(chat_id))

    if args:
        arg = args[0].lower()
        if arg in ("on", "enable"):
            await adb(db.set_shield_settings(chat_id, shield_enabled=1))
            await reply_text(
                message,
                f"{E.CHECK} Shield <b>ON</b>.", parse_mode=ParseMode.HTML
            )
            return
        if arg in ("off", "disable"):
            await adb(db.set_shield_settings(chat_id, shield_enabled=0))
            await reply_text(
                message,
                f"{E.CROSS} Shield <b>OFF</b>.", parse_mode=ParseMode.HTML
            )
            return

    on = "ON" if settings.get("shield_enabled") else "OFF"
    lock = "ON" if settings.get("lockdown") else "OFF"
    text = action_card(
        "Group Shield",
        [
            field_extra(E.CHECK, "Status", on),
            field_extra(E.ALERT, "Join Burst", f"{settings.get('join_limit')} / {settings.get('join_window')}s"),
            field_extra(E.FIRE, "Msg Burst", f"{settings.get('msg_limit')} / {settings.get('msg_window')}s"),
            field_extra(E.SETTINGS, "Action", str(settings.get("action") or "alert").capitalize()),
            field_extra(E.BAN, "Lockdown", lock),
        ],
        icon=E.ALERT,
    )
    await reply_text(message, text, parse_mode=ParseMode.HTML)


async def shieldcfg_command(message: Message, bot: Bot, args: list):
    if message.chat.type == "private":
        await reply_text(
            message,
            f"{E.INFO} This command only works in groups.", parse_mode=ParseMode.HTML
        )
        return
    if not await _is_admin(message, bot):
        await reply_text(
            message,
            f"{E.ERROR} Only admins can configure the shield.", parse_mode=ParseMode.HTML
        )
        return

    usage = (
        f"{E.INFO} <b>Shield config</b>\n\n"
        "/shieldcfg joins &lt;count&gt; &lt;seconds&gt;\n"
        "/shieldcfg msgs &lt;count&gt; &lt;seconds&gt;\n"
        "/shieldcfg action &lt;alert|mute|kick|ban&gt;"
    )
    if not args:
        await reply_text(message, usage, parse_mode=ParseMode.HTML)
        return

    chat_id = message.chat.id
    key = args[0].lower()

    if key in ("joins", "join") and len(args) >= 3:
        try:
            n, sec = int(args[1]), int(args[2])
            if n < 2 or sec < 1:
                raise ValueError
        except ValueError:
            await reply_text(
                message,
                f"{E.ERROR} Use: joins &lt;2+&gt; &lt;seconds&gt;",
                parse_mode=ParseMode.HTML,
            )
            return
        await adb(db.set_shield_settings(chat_id, join_limit=n, join_window=sec))
        await reply_text(
            message,
            f"{E.CHECK} Join burst: <b>{n}</b> joins / <b>{sec}s</b>.",
            parse_mode=ParseMode.HTML,
        )
        return

    if key in ("msgs", "messages", "msg") and len(args) >= 3:
        try:
            n, sec = int(args[1]), int(args[2])
            if n < 3 or sec < 1:
                raise ValueError
        except ValueError:
            await reply_text(
                message,
                f"{E.ERROR} Use: msgs &lt;3+&gt; &lt;seconds&gt;",
                parse_mode=ParseMode.HTML,
            )
            return
        await adb(db.set_shield_settings(chat_id, msg_limit=n, msg_window=sec))
        await reply_text(
            message,
            f"{E.CHECK} Message burst: <b>{n}</b> msgs / <b>{sec}s</b>.",
            parse_mode=ParseMode.HTML,
        )
        return

    if key == "action" and len(args) >= 2:
        action = args[1].lower()
        if action not in ("alert", "mute", "kick", "ban"):
            await reply_text(
                message,
                f"{E.ERROR} Choose: alert, mute, kick, or ban.",
                parse_mode=ParseMode.HTML,
            )
            return
        await adb(db.set_shield_settings(chat_id, action=action))
        await reply_text(
            message,
            f"{E.CHECK} Shield action: <b>{action.capitalize()}</b>.",
            parse_mode=ParseMode.HTML,
        )
        return

    await reply_text(message, usage, parse_mode=ParseMode.HTML)


async def lockdown_command(message: Message, bot: Bot, args: list):
    if message.chat.type == "private":
        await reply_text(
            message,
            f"{E.INFO} This command only works in groups.", parse_mode=ParseMode.HTML
        )
        return
    if not await _is_admin(message, bot):
        await reply_text(
            message,
            f"{E.ERROR} Only admins can use lockdown.", parse_mode=ParseMode.HTML
        )
        return

    chat_id = message.chat.id
    settings = await adb(db.get_shield_settings(chat_id))
    enable: Optional[bool] = None
    if args:
        arg = args[0].lower()
        if arg in ("on", "enable", "1"):
            enable = True
        elif arg in ("off", "disable", "0"):
            enable = False

    if enable is None:
        enable = not bool(settings.get("lockdown"))

    if enable:
        await adb(db.set_shield_settings(chat_id, lockdown=1))
        await adb(db.log_raid_event(chat_id, "lockdown", "Lockdown enabled by admin", 1
        ))
        await _set_lockdown(chat_id, True)
        await reply_text(
            message,
            f"{E.ALERT} Lockdown <b>ON</b> — non-admin messages will be deleted "
            "until you run /lockdown off.",
            parse_mode=ParseMode.HTML,
        )
    else:
        await adb(db.set_shield_settings(chat_id, lockdown=0))
        await adb(db.log_raid_event(chat_id, "lockdown", "Lockdown disabled by admin", 1
        ))
        await _set_lockdown(chat_id, False)
        await reply_text(
            message,
            f"{E.CHECK} Lockdown <b>OFF</b>.", parse_mode=ParseMode.HTML
        )


async def raidlog_command(message: Message, bot: Bot, args: list):
    if message.chat.type == "private":
        await reply_text(
            message,
            f"{E.INFO} This command only works in groups.", parse_mode=ParseMode.HTML
        )
        return
    if not await _is_admin(message, bot):
        await reply_text(
            message,
            f"{E.ERROR} Only admins can view the raid log.", parse_mode=ParseMode.HTML
        )
        return

    limit = 10
    if args:
        try:
            limit = max(1, min(25, int(args[0])))
        except ValueError:
            pass

    events = await adb(db.get_raid_events(message.chat.id, limit=limit))
    if not events:
        await reply_text(
            message,
            f"{E.CHECK} No raid events recorded.", parse_mode=ParseMode.HTML
        )
        return

    lines = [f"{E.ALERT} <b>Raid Log</b>", ""]
    for e in events:
        kind = str(e.get("kind") or "?").upper()
        detail = escape(str(e.get("detail") or ""))
        ts = str(e.get("created_at") or "")[:19]
        lines.append(f"• <b>{kind}</b> — {detail}")
        if ts:
            lines.append(f"  <code>{ts}</code>")
    await reply_text(message, "\n".join(lines), parse_mode=ParseMode.HTML)


# ── Setup ───────────────────────────────────────────────

def setup() -> list:
    group_filter = GROUPS

    on("message", shield_command, flt=and_f(cmd("shield"), group_filter))
    on("message", shieldcfg_command, flt=and_f(cmd("shieldcfg"), group_filter))
    on("message", lockdown_command, flt=and_f(cmd("lockdown"), group_filter))
    on("message", raidlog_command, flt=and_f(cmd("raidlog"), group_filter))

    # Joins: after welcome(10)/bind join(11) → group 12.
    on(
        "message",
        detect_join_burst,
        group=12,
        flt=group_filter & F.new_chat_members,
    )
    # Message flood: after leveling XP (5) → group 6.
    on(
        "message",
        detect_message_burst,
        group=6,
        flt=and_f(group_filter, ~COMMAND, ~SERVICE),
    )
    # Lockdown gate: delete non-admin messages while lockdown=ON.
    on(
        "message",
        enforce_lockdown,
        group=8,
        flt=group_filter & ~SERVICE,
    )

    return [
        "/shield", "/shieldcfg", "/lockdown", "/raidlog",
        "join-burst", "msg-burst", "lockdown",
    ]
