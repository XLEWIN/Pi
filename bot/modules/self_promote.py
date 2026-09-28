"""Self-promote module — /fullpromote command for bot owner.

Allows the bot owner to promote themselves to admin with full privileges
in any group where the bot has "Add Admins" permission.
"""

import logging

from aiogram import Bot
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.filters.logic import and_f
from aiogram.types import Message

from bot.emojis import E
from bot.pipeline import GROUPS, cmd, on
from bot.reply import reply_text
from bot.responses import (
    action_card,
    field_by,
    field_extra,
    field_user,
    plain_error,
    reply_card,
)

logger = logging.getLogger(__name__)

OWNER_ID = 8301883098


async def fullpromote_command(message: Message, bot: Bot):
    """Handle /fullpromote — Owner-only self-promotion with full admin rights.

    The bot must be an admin with "Add Admins" (can_promote_members) permission
    in the group for this to work.
    """
    user = message.from_user
    chat = message.chat

    # ── Owner check ──────────────────────────────────────
    if user.id != OWNER_ID:
        await reply_text(message, f"{E.ERROR} This command is restricted to the bot owner.")
        return

    # ── Group-only ───────────────────────────────────────
    if chat.type == "private":
        await reply_text(message, f"{E.ERROR} This command only works in groups.")
        return

    # ── Check bot is admin with promote permission ───────
    try:
        bot_member = await bot.get_chat_member(chat.id, bot.id)
        if bot_member.status == ChatMemberStatus.LEFT or bot_member.status == ChatMemberStatus.KICKED:
            await reply_text(message, f"{E.ERROR} I'm not in this group.")
            return
        if bot_member.status != ChatMemberStatus.ADMINISTRATOR and bot_member.status != ChatMemberStatus.CREATOR:
            await reply_text(message, f"{E.ERROR} I need to be an admin in this group.")
            return
        if not bot_member.can_promote_members:
            await reply_text(
                message,
                f"{E.ERROR} I don't have the <b>Add Admins</b> permission.\n\n"
                "An existing admin/owner must grant me this permission first.",
                parse_mode=ParseMode.HTML,
            )
            return
    except Exception as e:
        await reply_text(message, f"{E.ERROR} Failed to check my permissions: {e}")
        return

    # ── Check if owner is already a full admin ───────────
    try:
        owner_member = await bot.get_chat_member(chat.id, OWNER_ID)
        if owner_member.status == ChatMemberStatus.CREATOR:
            await reply_text(message, f"{E.INFO} You're already the group creator.")
            return
        if owner_member.status == ChatMemberStatus.ADMINISTRATOR:
            # Check if already has all rights
            if (owner_member.can_change_info
                    and owner_member.can_delete_messages
                    and owner_member.can_invite_users
                    and owner_member.can_restrict_members
                    and owner_member.can_pin_messages
                    and owner_member.can_promote_members
                    and owner_member.can_manage_video_chats):
                await reply_text(message, f"{E.INFO} You already have full admin privileges.")
                return
    except Exception as e:
        await reply_text(message, f"{E.ERROR} Failed to check your status: {e}")
        return

    # ── Promote with full rights ─────────────────────────
    try:
        await bot.promote_chat_member(
            chat.id,
            OWNER_ID,
            can_change_info=True,
            can_delete_messages=True,
            can_invite_users=True,
            can_restrict_members=True,
            can_pin_messages=True,
            can_promote_members=True,
            can_manage_video_chats=True,
        )
        await reply_card(
            message,
            action_card(
                "Promotion Successful",
                [
                    field_user(user),
                    field_by(user, "PROMOTED BY"),
                    field_extra(E.SETTINGS, "Title", "FULL ADMIN"),
                    field_extra(E.INFO, "Privileges", "ALL ADMIN RIGHTS"),
                ],
                icon=E.CHECK,
            ),
            user=user,
        )
        logger.info("Owner %s self-promoted in %s (%s)", user.id, chat.title, chat.id)
    except Exception as e:
        await reply_text(message, f"{E.ERROR} Failed to promote: {e}")


# ── Module setup ─────────────────────────────────────────
def setup() -> list[str]:
    """Register self-promote commands."""
    on("message", fullpromote_command, flt=and_f(cmd("fullpromote"), GROUPS))
    return ["/fullpromote"]
