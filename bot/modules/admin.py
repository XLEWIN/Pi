"""Admin module — Promote, demote, pin, admin list, and admin-only actions.

Adapted from boa2's admin for Pi bot (python-telegram-bot).
Success replies use bot.responses action cards (Pi emoji set).
"""

import asyncio
import logging
from html import escape

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters.logic import and_f
from aiogram.types import Message

from bot.pipeline import GROUPS, cmd, on
from bot.reply import reply_text
from bot.emojis import E
from bot.responses import (
    action_card,
    bot_rights_error,
    failed,
    field_by,
    field_extra,
    field_title,
    field_user,
    is_rights_error,
    plain_error,
    reply_card,
)

logger = logging.getLogger(__name__)

# ── Helpers ──────────────────────────────────────────────
async def _is_admin(message: Message, bot: Bot) -> bool:
    user_id = message.from_user.id
    chat_id = message.chat.id
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ["administrator", "creator"]
    except Exception:
        return False


async def _is_owner(message: Message, bot: Bot) -> bool:
    user_id = message.from_user.id
    chat_id = message.chat.id
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status == "creator"
    except Exception:
        return False


async def _can_promote(message: Message, bot: Bot) -> bool:
    """Creator always; administrators only with the add-admins right."""
    user_id = message.from_user.id
    chat_id = message.chat.id
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except Exception:
        return False
    if member.status == "creator":
        return True
    if member.status == "administrator":
        return bool(getattr(member, "can_promote_members", False))
    return False


async def _is_bot_admin(message: Message, bot: Bot) -> bool:
    try:
        member = await bot.get_chat_member(message.chat.id, bot.id)
        return member.status in ["administrator", "creator"]
    except Exception:
        return False


async def _get_target_user(message: Message, bot: Bot, args: list):
    """Extract target user from reply or args."""
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user
    if args:
        try:
            user_id = int(args[0])
            member = await bot.get_chat_member(message.chat.id, user_id)
            return member.user
        except (ValueError, Exception):
            pass
    return None


# ── Command handlers ─────────────────────────────────────
async def promote_command(message: Message, bot: Bot, args: list):
    """Handle /promote — promote a user to admin."""
    if message.chat.type == "private":
        await reply_text(message, f"{E.ERROR} This command only works in groups.",
            parse_mode=ParseMode.HTML)
        return

    if not await _can_promote(message, bot):
        return

    if not await _is_bot_admin(message, bot):
        await reply_text(message, bot_rights_error("promote users", "Add Admins"),
            parse_mode=ParseMode.HTML)
        return

    target = await _get_target_user(message, bot, args)
    if not target:
        await reply_text(message,
            f"{E.ERROR} Reply to a user or provide their ID.\n\n<b>Usage:</b> /promote @user",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        await bot.promote_chat_member(
            message.chat.id,
            target.id,
            can_change_info=True,
            can_delete_messages=True,
            can_invite_users=True,
            can_restrict_members=True,
            can_pin_messages=True,
            can_promote_members=False,
            can_manage_video_chats=True,
        )
        await reply_card(
            message,
            action_card(
                "Promotion Successful",
                [
                    field_user(target),
                    field_by(message.from_user, "PROMOTED BY"),
                    field_title("ADMIN"),
                ],
                icon=E.CHECK,
            ),
            user=target,
        )
    except Exception as e:
        await reply_text(message, failed("promote that user", e, permission="Add Admins"),
            parse_mode=ParseMode.HTML)


async def demote_command(message: Message, bot: Bot, args: list):
    """Handle /demote — demote an admin."""
    if message.chat.type == "private":
        await reply_text(message, f"{E.ERROR} This command only works in groups.",
            parse_mode=ParseMode.HTML)
        return

    if not await _is_owner(message, bot):
        return

    target = await _get_target_user(message, bot, args)
    if not target:
        await reply_text(message,
            f"{E.ERROR} Reply to a user or provide their ID.\n\n<b>Usage:</b> /demote @user",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        await bot.promote_chat_member(
            message.chat.id,
            target.id,
            can_change_info=False,
            can_delete_messages=False,
            can_invite_users=False,
            can_restrict_members=False,
            can_pin_messages=False,
            can_promote_members=False,
            can_manage_video_chats=False,
        )
        await reply_card(
            message,
            action_card(
                "Demotion Successful",
                [
                    field_user(target),
                    field_by(message.from_user, "DEMOTED BY"),
                    field_title("MEMBER"),
                ],
                icon=E.CROSS,
            ),
            user=target,
        )
    except Exception as e:
        await reply_text(message, failed("demote that user", e, permission="Add Admins"),
            parse_mode=ParseMode.HTML)


async def pin_command(message: Message, bot: Bot, args: list):
    """Handle /pin — pin a message."""
    if message.chat.type == "private":
        await reply_text(message, f"{E.ERROR} This command only works in groups.",
            parse_mode=ParseMode.HTML)
        return

    if not await _is_admin(message, bot):
        return

    if not message.reply_to_message:
        await reply_text(message, f"{E.ERROR} Reply to a message to pin it.",
            parse_mode=ParseMode.HTML)
        return

    try:
        silent = args and args[0].lower() in ["loud", "True", "1"]
        await bot.pin_chat_message(
            message.chat.id,
            message.reply_to_message.message_id,
            disable_notification=silent,
        )
        if silent:
            await reply_card(
                message,
                action_card(
                    "Message Pinned",
                    [
                        field_by(message.from_user, "PINNED BY"),
                        field_extra(E.INFO, "Mode", "SILENT"),
                    ],
                    icon=E.PIN,
                ),
            )
        else:
            await reply_card(
                message,
                action_card(
                    "Message Pinned",
                    [
                        field_by(message.from_user, "PINNED BY"),
                        field_extra(E.INFO, "Mode", "NOTIFICATION ON"),
                    ],
                    icon=E.PIN,
                ),
            )
    except Exception as e:
        await reply_text(message, failed("pin that message", e, permission="Pin Messages"),
            parse_mode=ParseMode.HTML)


async def unpin_command(message: Message, bot: Bot):
    """Handle /unpin — unpin a message."""
    if message.chat.type == "private":
        await reply_text(message, f"{E.ERROR} This command only works in groups.",
            parse_mode=ParseMode.HTML)
        return

    if not await _is_admin(message, bot):
        return

    try:
        if message.reply_to_message:
            await bot.unpin_chat_message(
                message.chat.id,
                message.reply_to_message.message_id,
            )
        else:
            await bot.unpin_all_chat_messages(message.chat.id)
        await reply_card(
            message,
            action_card(
                "Message Unpinned",
                [
                    field_by(message.from_user, "UNPINNED BY"),
                    field_extra(
                        E.INFO,
                        "Scope",
                        "Single message" if message.reply_to_message else "All pins",
                    ),
                ],
                icon=E.PIN,
            ),
        )
    except Exception as e:
        await reply_text(message, failed("unpin that message", e, permission="Pin Messages"),
            parse_mode=ParseMode.HTML)


async def adminlist_command(message: Message, bot: Bot):
    """Handle /adminlist — show owner, human admins and bots, each counted."""
    if message.chat.type == "private":
        await reply_text(message, f"{E.ERROR} This command only works in groups.",
            parse_mode=ParseMode.HTML)
        return

    try:
        admins = await bot.get_chat_administrators(message.chat.id)
        owner = None
        human_admins = []
        bot_admins = []

        for admin in admins:
            if admin.status == "creator":
                owner = admin.user
            elif admin.user.is_bot:
                bot_admins.append(admin.user)
            else:
                human_admins.append(admin.user)

        text = action_card(
            "Admin List",
            [
                field_extra(E.INFO, "Group", escape(message.chat.title or "")),
                field_extra(E.ADMIN, "Admins", str(len(human_admins))),
                field_extra("🤖", "Bots", str(len(bot_admins))),
                field_extra(E.USER, "Total", str(len(admins))),
            ],
            icon=E.CROWN,
        ) + "\n\n"

        if owner:
            name = f"@{escape(owner.username)}" if owner.username else escape(owner.first_name)
            text += f"{E.CROWN} <b>Owner:</b> <a href='tg://user?id={owner.id}'>{name}</a>\n"

        if human_admins:
            text += f"\n{E.ADMIN} <b>Administrators:</b>\n"
            for admin in human_admins:
                name = f"@{escape(admin.username)}" if admin.username else escape(admin.first_name)
                text += f"• <a href='tg://user?id={admin.id}'>{name}</a>\n"

        if bot_admins:
            text += "\n🤖 <b>Bots:</b>\n"
            for bot_user in bot_admins:
                name = f"@{escape(bot_user.username)}" if bot_user.username else escape(bot_user.first_name or str(bot_user.id))
                text += f"• <a href='tg://user?id={bot_user.id}'>{name}</a>\n"

        await reply_card(message, text)
    except Exception as e:
        await reply_text(message, failed("load the admin list", e),
            parse_mode=ParseMode.HTML)


async def admin_count_command(message: Message, bot: Bot):
    """Handle /admincount — count owners, admins and bot admins separately."""
    if message.chat.type == "private":
        await reply_text(message, f"{E.ERROR} This command only works in groups.",
            parse_mode=ParseMode.HTML)
        return

    try:
        admins = await bot.get_chat_administrators(message.chat.id)
        owner_count = sum(1 for a in admins if a.status == "creator")
        bot_count = sum(1 for a in admins if a.user.is_bot)
        human_admin_count = sum(
            1 for a in admins if a.status != "creator" and not a.user.is_bot
        )

        await reply_card(
            message,
            action_card(
                "Admin Count",
                [
                    field_extra(E.INFO, "Group", escape(message.chat.title or "")),
                    field_extra(E.CROWN, "Owner", str(owner_count)),
                    field_extra(E.ADMIN, "Admins", str(human_admin_count)),
                    field_extra("🤖", "Bots", str(bot_count)),
                    field_extra(E.USER, "Total", str(len(admins))),
                ],
                icon=E.ADMIN,
            ),
        )
    except Exception as e:
        await reply_text(message, failed("count admins", e),
            parse_mode=ParseMode.HTML)


async def setchatphoto_command(message: Message, bot: Bot):
    """Handle /setchatphoto — set chat photo (reply to a photo)."""
    if message.chat.type == "private":
        await reply_text(message, f"{E.ERROR} This command only works in groups.",
            parse_mode=ParseMode.HTML)
        return

    if not await _is_admin(message, bot):
        return

    if not message.reply_to_message or not message.reply_to_message.photo:
        await reply_text(message, f"{E.ERROR} Reply to a photo to set it as chat photo.",
            parse_mode=ParseMode.HTML)
        return

    try:
        photo = message.reply_to_message.photo[-1]
        await bot.set_chat_photo(message.chat.id, photo.file_id)
        await reply_card(
            message,
            action_card(
                "Chat Photo Updated",
                [
                    field_by(message.from_user, "UPDATED BY"),
                    field_extra(E.INFO, "Chat", escape(message.chat.title or "")),
                ],
                icon=E.CHECK,
            ),
        )
    except Exception as e:
        await reply_text(message, failed("set the group photo", e, permission="Change Group Info"),
            parse_mode=ParseMode.HTML)


async def setchatname_command(message: Message, bot: Bot, args: list):
    """Handle /setchatname — set chat name."""
    if message.chat.type == "private":
        await reply_text(message, f"{E.ERROR} This command only works in groups.",
            parse_mode=ParseMode.HTML)
        return

    if not await _is_admin(message, bot):
        return

    if not args:
        await reply_text(message, f"{E.INFO} Usage: /setchatname &lt;new name&gt;", parse_mode=ParseMode.HTML)
        return

    name = " ".join(args)
    try:
        await bot.set_chat_title(message.chat.id, name)
        await reply_card(
            message,
            action_card(
                "Chat Name Updated",
                [
                    field_by(message.from_user, "UPDATED BY"),
                    field_extra(E.INFO, "New Name", escape(name)),
                ],
                icon=E.CHECK,
            ),
        )
    except Exception as e:
        await reply_text(message, failed("set the group name", e, permission="Change Group Info"),
            parse_mode=ParseMode.HTML)


async def setchatdescription_command(message: Message, bot: Bot, args: list):
    """Handle /setchatdescription — set chat description."""
    if message.chat.type == "private":
        await reply_text(message, f"{E.ERROR} This command only works in groups.",
            parse_mode=ParseMode.HTML)
        return

    if not await _is_admin(message, bot):
        return

    if not args:
        await reply_text(message, f"{E.INFO} Usage: /setchatdescription &lt;description&gt;", parse_mode=ParseMode.HTML)
        return

    desc = " ".join(args)
    try:
        await bot.set_chat_description(message.chat.id, desc)
        await reply_card(
            message,
            action_card(
                "Chat Description Updated",
                [
                    field_by(message.from_user, "UPDATED BY"),
                    field_extra(E.INFO, "Description", escape(desc[:200])),
                ],
                icon=E.CHECK,
            ),
        )
    except Exception as e:
        await reply_text(message, failed("set the group description", e, permission="Change Group Info"),
            parse_mode=ParseMode.HTML)


# ============================================
# PURGE / SPURGE  (from boa2, Pi-branded)
# ============================================
#
# /purge   delete from the message you reply to, through the command,
#          then post a card that removes itself (and the command) after
#          _PURGE_CONFIRM_TTL seconds.
# /spurge  identical range, but silent: nothing is announced, and the
#          command message is deleted too.
#
# Both were carried over from boa2's admin module.  boa2 runs on
# Pyrogram, where delete_messages() takes a list and ships it in one
# round trip; the Bot API has no batch delete, so this walks the range
# one id at a time and paces itself instead.

#: Delete this many messages, then breathe — a burst of Bot API deletes
#: trips flood control far sooner than Telegram's documented rate.
_PURGE_CHUNK = 100
_PURGE_PAUSE = 0.1
#: Seconds the purge card stays before it removes itself and the command.
_PURGE_CONFIRM_TTL = 10

#: A refusal that applies to the WHOLE range (rights), not to one message
#: (too old / already deleted).
_FATAL_DELETE_MARKERS = (
    "not enough rights",
    "not enough permissions",
    "have no rights",
    "message can't be deleted",
    "message cannot be deleted",
    "chat admin required",
    "bot is not a member",
    "forbidden",
)


def _fatal_delete(exc: BaseException) -> bool:
    """True when Telegram said 'you may not delete here at all'."""
    if isinstance(exc, TelegramForbiddenError):
        return True
    if is_rights_error(exc):
        return True
    text = str(exc).lower()
    return any(marker in text for marker in _FATAL_DELETE_MARKERS)


async def _bot_can_delete(message: Message, bot: Bot) -> bool:
    """True when the bot itself may delete messages in this chat."""
    try:
        member = await bot.get_chat_member(message.chat.id, bot.id)
        if member.status not in ("administrator", "creator"):
            return False
        return bool(getattr(member, "can_delete_messages", True))
    except Exception:
        return False


def _purge_ids(message: Message) -> list:
    """Ids from the replied message up to (excluding) the command itself."""
    start = message.reply_to_message.message_id
    end = message.message_id
    if start >= end:
        # Replying to something at/after the command: purge that one only.
        return [start]
    return list(range(start, end))


async def _delete_range(bot: Bot, chat_id: int, message_ids: list) -> tuple:
    """Delete ``message_ids`` oldest-first; returns ``(deleted, fatal)``.

    A single message that is too old or already gone is skipped so a
    partial purge still reports honestly; a refusal that would apply to
    the whole range stops the run so the caller can explain it instead
    of claiming success.
    """
    deleted = 0
    for index, mid in enumerate(message_ids, start=1):
        try:
            await bot.delete_message(chat_id, mid)
            deleted += 1
        except TelegramRetryAfter as e:
            await asyncio.sleep(getattr(e, "retry_after", 1) or 1)
            try:
                await bot.delete_message(chat_id, mid)
                deleted += 1
            except Exception as exc:
                if _fatal_delete(exc):
                    return deleted, exc
        except Exception as exc:
            if _fatal_delete(exc):
                return deleted, exc
        if index % _PURGE_CHUNK == 0:
            await asyncio.sleep(_PURGE_PAUSE)
    return deleted, None


async def _run_purge(message: Message, bot: Bot, *, silent: bool) -> None:
    usage = "spurge" if silent else "purge"

    if not message or not message.chat or message.chat.type == "private":
        if message:
            await reply_text(message, f"{E.ERROR} This command only works in groups.",
                parse_mode=ParseMode.HTML)
        return

    if message.chat.type != "supergroup":
        await reply_text(message, plain_error("Cannot purge messages in a basic group."),
            parse_mode=ParseMode.HTML)
        return

    if not await _is_admin(message, bot):
        return

    if not message.reply_to_message:
        await reply_text(
            message,
            f"{E.ERROR} Reply to the message to start from.\n\n"
            f"<b>Usage:</b> reply to a message with <code>/{usage}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    if not await _bot_can_delete(message, bot):
        await reply_text(message, bot_rights_error("delete messages", "Can Delete Messages"),
            parse_mode=ParseMode.HTML)
        return

    deleted, fatal = await _delete_range(bot, message.chat.id, _purge_ids(message))
    if fatal is not None:
        logger.warning(f"purge aborted chat={message.chat.id} deleted={deleted}: {fatal}")
        await reply_text(message, bot_rights_error("delete messages", "Can Delete Messages"),
            parse_mode=ParseMode.HTML)
        return

    if silent:
        # /spurge — the whole point is that nothing is left to read.
        try:
            await message.delete()
        except Exception:
            pass
        return

    count = f"{deleted} message" if deleted == 1 else f"{deleted} messages"
    sent = await reply_text(
        message,
        action_card(
            "Purge Complete",
            [
                field_extra(E.CROSS, "Deleted", count),
                field_by(message.from_user, "PURGED BY"),
                field_extra(E.INFO, "Chat", escape(message.chat.title or "")),
            ],
            icon=E.CROSS,
        ),
        parse_mode=ParseMode.HTML,
    )

    await asyncio.sleep(_PURGE_CONFIRM_TTL)
    for target in (sent, message):
        try:
            if target is not None:
                await target.delete()
        except Exception:
            pass


async def purge_command(message: Message, bot: Bot) -> None:
    """Handle /purge — delete from the replied message through this command."""
    await _run_purge(message, bot, silent=False)


async def spurge_command(message: Message, bot: Bot) -> None:
    """Handle /spurge — silent purge: no confirmation, nothing left behind."""
    await _run_purge(message, bot, silent=True)


# ── Module setup ─────────────────────────────────────────
def setup() -> list:
    """Register admin commands."""
    on("message", promote_command, flt=and_f(cmd("promote"), GROUPS))
    on("message", demote_command, flt=and_f(cmd("demote"), GROUPS))
    on("message", pin_command, flt=and_f(cmd("pin"), GROUPS))
    on("message", unpin_command, flt=and_f(cmd("unpin"), GROUPS))
    on("message", adminlist_command, flt=and_f(cmd("adminlist"), GROUPS))
    on("message", adminlist_command, flt=and_f(cmd("admins"), GROUPS))
    on("message", admin_count_command, flt=and_f(cmd("admincount"), GROUPS))
    on("message", setchatphoto_command, flt=and_f(cmd("setchatphoto"), GROUPS))
    on("message", setchatname_command, flt=and_f(cmd("setchatname"), GROUPS))
    on("message", setchatdescription_command, flt=and_f(cmd("setchatdescription"), GROUPS))
    on("message", purge_command, flt=and_f(cmd("purge"), GROUPS))
    on("message", spurge_command, flt=and_f(cmd("spurge"), GROUPS))

    return ["promote", "demote", "pin", "unpin", "adminlist", "admins", "admincount", "setchatphoto", "setchatname", "setchatdescription", "purge", "spurge"]
