"""Admin module — Promote, demote, pin, admin list, and admin-only actions.

Adapted from boa2's admin for Pi bot (python-telegram-bot).
Success replies use bot.responses action cards (Pi emoji set).
"""

import logging
from html import escape

from telegram import Update, ChatMember, ChatPermissions
from telegram.ext import Application, ContextTypes, filters
from telegram.constants import ParseMode

from bot.command_handler import CommandHandler
from bot.emojis import E
from bot.responses import (
    action_card,
    actor_label,
    field_by,
    field_extra,
    field_title,
    field_user,
    mention,
    plain_error,
    reply_card,
    user_label,
)

logger = logging.getLogger(__name__)

# ── Helpers ──────────────────────────────────────────────
async def _is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        return member.status in ["administrator", "creator"]
    except Exception:
        return False


async def _is_owner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        return member.status == "creator"
    except Exception:
        return False


async def _is_bot_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        member = await context.bot.get_chat_member(update.effective_chat.id, context.bot.id)
        return member.status in ["administrator", "creator"]
    except Exception:
        return False


async def _get_target_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Extract target user from reply or args."""
    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        return update.message.reply_to_message.from_user
    if context.args:
        try:
            user_id = int(context.args[0])
            member = await context.bot.get_chat_member(update.effective_chat.id, user_id)
            return member.user
        except (ValueError, Exception):
            pass
    return None


# ── Command handlers ─────────────────────────────────────
async def promote_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /promote — promote a user to admin."""
    if update.effective_chat.type == "private":
        await update.message.reply_text(f"{E.ERROR} This command only works in groups.",
            parse_mode=ParseMode.HTML)
        return

    if not await _is_owner(update, context):
        await update.message.reply_text(f"{E.ERROR} Only the group creator can promote admins.",
            parse_mode=ParseMode.HTML)
        return

    if not await _is_bot_admin(update, context):
        await update.message.reply_text(f"{E.ERROR} I need admin rights to promote users.",
            parse_mode=ParseMode.HTML)
        return

    target = await _get_target_user(update, context)
    if not target:
        await update.message.reply_text(
            f"{E.ERROR} Reply to a user or provide their ID.\n\n<b>Usage:</b> /promote @user",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        await context.bot.promote_chat_member(
            update.effective_chat.id,
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
            update.message,
            action_card(
                "Promotion Successful",
                [
                    field_user(target),
                    field_by(update.effective_user, "PROMOTED BY"),
                    field_title("ADMIN"),
                ],
                icon=E.CHECK,
            ),
            user=target,
        )
    except Exception as e:
        await update.message.reply_text(plain_error(f"Failed to promote: {e}"),
            parse_mode=ParseMode.HTML)


async def demote_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /demote — demote an admin."""
    if update.effective_chat.type == "private":
        await update.message.reply_text(f"{E.ERROR} This command only works in groups.",
            parse_mode=ParseMode.HTML)
        return

    if not await _is_owner(update, context):
        await update.message.reply_text(f"{E.ERROR} Only the group creator can demote admins.",
            parse_mode=ParseMode.HTML)
        return

    target = await _get_target_user(update, context)
    if not target:
        await update.message.reply_text(
            f"{E.ERROR} Reply to a user or provide their ID.\n\n<b>Usage:</b> /demote @user",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        await context.bot.promote_chat_member(
            update.effective_chat.id,
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
            update.message,
            action_card(
                "Demotion Successful",
                [
                    field_user(target),
                    field_by(update.effective_user, "DEMOTED BY"),
                    field_title("MEMBER"),
                ],
                icon=E.CROSS,
            ),
            user=target,
        )
    except Exception as e:
        await update.message.reply_text(plain_error(f"Failed to demote: {e}"),
            parse_mode=ParseMode.HTML)


async def pin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /pin — pin a message."""
    if update.effective_chat.type == "private":
        await update.message.reply_text(f"{E.ERROR} This command only works in groups.",
            parse_mode=ParseMode.HTML)
        return

    if not await _is_admin(update, context):
        await update.message.reply_text(f"{E.ERROR} You need admin rights to pin messages.",
            parse_mode=ParseMode.HTML)
        return

    if not update.message.reply_to_message:
        await update.message.reply_text(f"{E.ERROR} Reply to a message to pin it.",
            parse_mode=ParseMode.HTML)
        return

    try:
        silent = context.args and context.args[0].lower() in ["loud", "True", "1"]
        await context.bot.pin_chat_message(
            update.effective_chat.id,
            update.message.reply_to_message.message_id,
            disable_notification=silent,
        )
        if silent:
            await reply_card(
                update.message,
                action_card(
                    "Message Pinned",
                    [
                        field_by(update.effective_user, "PINNED BY"),
                        field_extra(E.INFO, "Mode", "SILENT"),
                    ],
                    icon=E.PIN,
                ),
            )
        else:
            await reply_card(
                update.message,
                action_card(
                    "Message Pinned",
                    [
                        field_by(update.effective_user, "PINNED BY"),
                        field_extra(E.INFO, "Mode", "NOTIFICATION ON"),
                    ],
                    icon=E.PIN,
                ),
            )
    except Exception as e:
        await update.message.reply_text(plain_error(f"Failed to pin: {e}"),
            parse_mode=ParseMode.HTML)


async def unpin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /unpin — unpin a message."""
    if update.effective_chat.type == "private":
        await update.message.reply_text(f"{E.ERROR} This command only works in groups.",
            parse_mode=ParseMode.HTML)
        return

    if not await _is_admin(update, context):
        await update.message.reply_text(f"{E.ERROR} You need admin rights to unpin messages.",
            parse_mode=ParseMode.HTML)
        return

    try:
        if update.message.reply_to_message:
            await context.bot.unpin_chat_message(
                update.effective_chat.id,
                update.message.reply_to_message.message_id,
            )
        else:
            await context.bot.unpin_all_chat_messages(update.effective_chat.id)
        await reply_card(
            update.message,
            action_card(
                "Message Unpinned",
                [
                    field_by(update.effective_user, "UNPINNED BY"),
                    field_extra(
                        E.INFO,
                        "Scope",
                        "Single message" if update.message.reply_to_message else "All pins",
                    ),
                ],
                icon=E.PIN,
            ),
        )
    except Exception as e:
        await update.message.reply_text(plain_error(f"Failed to unpin: {e}"),
            parse_mode=ParseMode.HTML)


async def adminlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /adminlist — show owner, human admins and bots, each counted."""
    if update.effective_chat.type == "private":
        await update.message.reply_text(f"{E.ERROR} This command only works in groups.",
            parse_mode=ParseMode.HTML)
        return

    try:
        admins = await context.bot.get_chat_administrators(update.effective_chat.id)
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
                field_extra(E.INFO, "Group", escape(update.effective_chat.title or "")),
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
            text += f"\n🤖 <b>Bots:</b>\n"
            for bot_user in bot_admins:
                name = f"@{escape(bot_user.username)}" if bot_user.username else escape(bot_user.first_name or str(bot_user.id))
                text += f"• <a href='tg://user?id={bot_user.id}'>{name}</a>\n"

        await reply_card(update.message, text)
    except Exception as e:
        await update.message.reply_text(f"{E.ERROR} Failed to get admin list: {e}",
            parse_mode=ParseMode.HTML)


async def admin_count_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /admincount — count owners, admins and bot admins separately."""
    if update.effective_chat.type == "private":
        await update.message.reply_text(f"{E.ERROR} This command only works in groups.",
            parse_mode=ParseMode.HTML)
        return

    try:
        admins = await context.bot.get_chat_administrators(update.effective_chat.id)
        owner_count = sum(1 for a in admins if a.status == "creator")
        bot_count = sum(1 for a in admins if a.user.is_bot)
        human_admin_count = sum(
            1 for a in admins if a.status != "creator" and not a.user.is_bot
        )

        await reply_card(
            update.message,
            action_card(
                "Admin Count",
                [
                    field_extra(E.INFO, "Group", escape(update.effective_chat.title or "")),
                    field_extra(E.CROWN, "Owner", str(owner_count)),
                    field_extra(E.ADMIN, "Admins", str(human_admin_count)),
                    field_extra("🤖", "Bots", str(bot_count)),
                    field_extra(E.USER, "Total", str(len(admins))),
                ],
                icon=E.ADMIN,
            ),
        )
    except Exception as e:
        await update.message.reply_text(f"{E.ERROR} Failed to count admins: {e}",
            parse_mode=ParseMode.HTML)


async def setchatphoto_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /setchatphoto — set chat photo (reply to a photo)."""
    if update.effective_chat.type == "private":
        await update.message.reply_text(f"{E.ERROR} This command only works in groups.",
            parse_mode=ParseMode.HTML)
        return

    if not await _is_admin(update, context):
        await update.message.reply_text(f"{E.ERROR} You need admin rights.",
            parse_mode=ParseMode.HTML)
        return

    if not update.message.reply_to_message or not update.message.reply_to_message.photo:
        await update.message.reply_text(f"{E.ERROR} Reply to a photo to set it as chat photo.",
            parse_mode=ParseMode.HTML)
        return

    try:
        photo = update.message.reply_to_message.photo[-1]
        await context.bot.set_chat_photo(update.effective_chat.id, photo.file_id)
        await reply_card(
            update.message,
            action_card(
                "Chat Photo Updated",
                [
                    field_by(update.effective_user, "UPDATED BY"),
                    field_extra(E.INFO, "Chat", escape(update.effective_chat.title or "")),
                ],
                icon=E.CHECK,
            ),
        )
    except Exception as e:
        await update.message.reply_text(plain_error(f"Failed to set photo: {e}"),
            parse_mode=ParseMode.HTML)


async def setchatname_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /setchatname — set chat name."""
    if update.effective_chat.type == "private":
        await update.message.reply_text(f"{E.ERROR} This command only works in groups.",
            parse_mode=ParseMode.HTML)
        return

    if not await _is_admin(update, context):
        await update.message.reply_text(f"{E.ERROR} You need admin rights.",
            parse_mode=ParseMode.HTML)
        return

    if not context.args:
        await update.message.reply_text(f"{E.INFO} Usage: /setchatname &lt;new name&gt;", parse_mode=ParseMode.HTML)
        return

    name = " ".join(context.args)
    try:
        await context.bot.set_chat_title(update.effective_chat.id, name)
        await reply_card(
            update.message,
            action_card(
                "Chat Name Updated",
                [
                    field_by(update.effective_user, "UPDATED BY"),
                    field_extra(E.INFO, "New Name", escape(name)),
                ],
                icon=E.CHECK,
            ),
        )
    except Exception as e:
        await update.message.reply_text(plain_error(f"Failed to set name: {e}"),
            parse_mode=ParseMode.HTML)


async def setchatdescription_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /setchatdescription — set chat description."""
    if update.effective_chat.type == "private":
        await update.message.reply_text(f"{E.ERROR} This command only works in groups.",
            parse_mode=ParseMode.HTML)
        return

    if not await _is_admin(update, context):
        await update.message.reply_text(f"{E.ERROR} You need admin rights.",
            parse_mode=ParseMode.HTML)
        return

    if not context.args:
        await update.message.reply_text(f"{E.INFO} Usage: /setchatdescription &lt;description&gt;", parse_mode=ParseMode.HTML)
        return

    desc = " ".join(context.args)
    try:
        await context.bot.set_chat_description(update.effective_chat.id, desc)
        await reply_card(
            update.message,
            action_card(
                "Chat Description Updated",
                [
                    field_by(update.effective_user, "UPDATED BY"),
                    field_extra(E.INFO, "Description", escape(desc[:200])),
                ],
                icon=E.CHECK,
            ),
        )
    except Exception as e:
        await update.message.reply_text(plain_error(f"Failed to set description: {e}"),
            parse_mode=ParseMode.HTML)


# ── Module setup ─────────────────────────────────────────
def setup(app: Application) -> list:
    """Register admin commands."""
    app.add_handler(CommandHandler("promote", promote_command, filters=filters.ChatType.GROUPS))
    app.add_handler(CommandHandler("demote", demote_command, filters=filters.ChatType.GROUPS))
    app.add_handler(CommandHandler("pin", pin_command, filters=filters.ChatType.GROUPS))
    app.add_handler(CommandHandler("unpin", unpin_command, filters=filters.ChatType.GROUPS))
    app.add_handler(CommandHandler("adminlist", adminlist_command, filters=filters.ChatType.GROUPS))
    app.add_handler(CommandHandler("admins", adminlist_command, filters=filters.ChatType.GROUPS))
    app.add_handler(CommandHandler("admincount", admin_count_command, filters=filters.ChatType.GROUPS))
    app.add_handler(CommandHandler("setchatphoto", setchatphoto_command, filters=filters.ChatType.GROUPS))
    app.add_handler(CommandHandler("setchatname", setchatname_command, filters=filters.ChatType.GROUPS))
    app.add_handler(CommandHandler("setchatdescription", setchatdescription_command, filters=filters.ChatType.GROUPS))

    return ["promote", "demote", "pin", "unpin", "adminlist", "admins", "admincount", "setchatphoto", "setchatname", "setchatdescription"]
