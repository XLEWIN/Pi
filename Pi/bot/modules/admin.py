"""Admin module — Promote, demote, pin, admin list, and admin-only actions.

Adapted from boa2's admin for Pi bot (python-telegram-bot).
"""

import logging

from telegram import Update, ChatMember, ChatPermissions
from telegram.ext import Application, CommandHandler, ContextTypes, filters
from telegram.constants import ParseMode

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
        await update.message.reply_text("❌ This command only works in groups.")
        return

    if not await _is_owner(update, context):
        await update.message.reply_text("❌ Only the group creator can promote admins.")
        return

    if not await _is_bot_admin(update, context):
        await update.message.reply_text("❌ I need admin rights to promote users.")
        return

    target = await _get_target_user(update, context)
    if not target:
        await update.message.reply_text(
            "❌ Reply to a user or provide their ID.\n\n<b>Usage:</b> /promote @user",
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
        await update.message.reply_text(
            f"✅ Promoted <a href='tg://user?id={target.id}'>{target.first_name}</a> to admin.",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to promote: {e}")


async def demote_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /demote — demote an admin."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("❌ This command only works in groups.")
        return

    if not await _is_owner(update, context):
        await update.message.reply_text("❌ Only the group creator can demote admins.")
        return

    target = await _get_target_user(update, context)
    if not target:
        await update.message.reply_text(
            "❌ Reply to a user or provide their ID.\n\n<b>Usage:</b> /demote @user",
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
        await update.message.reply_text(
            f"✅ Demoted <a href='tg://user?id={target.id}'>{target.first_name}</a>.",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to demote: {e}")


async def pin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /pin — pin a message."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("❌ This command only works in groups.")
        return

    if not await _is_admin(update, context):
        await update.message.reply_text("❌ You need admin rights to pin messages.")
        return

    if not update.message.reply_to_message:
        await update.message.reply_text("❌ Reply to a message to pin it.")
        return

    try:
        silent = context.args and context.args[0].lower() in ["loud", "True", "1"]
        await context.bot.pin_chat_message(
            update.effective_chat.id,
            update.message.reply_to_message.message_id,
            disable_notification=silent,
        )
        if silent:
            await update.message.reply_text("📌 Message pinned silently.")
        else:
            await update.message.reply_text("📌 Message pinned.")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to pin: {e}")


async def unpin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /unpin — unpin a message."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("❌ This command only works in groups.")
        return

    if not await _is_admin(update, context):
        await update.message.reply_text("❌ You need admin rights to unpin messages.")
        return

    try:
        if update.message.reply_to_message:
            await context.bot.unpin_chat_message(
                update.effective_chat.id,
                update.message.reply_to_message.message_id,
            )
        else:
            await context.bot.unpin_all_chat_messages(update.effective_chat.id)
        await update.message.reply_text("📌 Message unpinned.")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to unpin: {e}")


async def adminlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /adminlist — show all admins."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("❌ This command only works in groups.")
        return

    try:
        admins = await context.bot.get_chat_administrators(update.effective_chat.id)
        owner = None
        admin_list = []

        for admin in admins:
            if admin.status == "creator":
                owner = admin.user
            else:
                admin_list.append(admin.user)

        text = f"👑 <b>Admins in {update.effective_chat.title}:</b>\n\n"

        if owner:
            name = f"@{owner.username}" if owner.username else owner.first_name
            text += f"👑 <b>Owner:</b> <a href='tg://user?id={owner.id}'>{name}</a>\n"

        if admin_list:
            text += "\n🔧 <b>Administrators:</b>\n"
            for admin in admin_list:
                name = f"@{admin.username}" if admin.username else admin.first_name
                text += f"• <a href='tg://user?id={admin.id}'>{name}</a>\n"

        await update.message.reply_text(text, parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to get admin list: {e}")


async def admin_count_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /admincount — count admins in chat."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("❌ This command only works in groups.")
        return

    try:
        admins = await context.bot.get_chat_administrators(update.effective_chat.id)
        owner_count = sum(1 for a in admins if a.status == "creator")
        admin_count = len(admins) - owner_count

        await update.message.reply_text(
            f"📊 <b>Admin Count for {update.effective_chat.title}:</b>\n\n"
            f"👑 Owner: {owner_count}\n"
            f"🔧 Admins: {admin_count}\n"
            f"👤 Total: {len(admins)}",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to count admins: {e}")


async def setchatphoto_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /setchatphoto — set chat photo (reply to a photo)."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("❌ This command only works in groups.")
        return

    if not await _is_admin(update, context):
        await update.message.reply_text("❌ You need admin rights.")
        return

    if not update.message.reply_to_message or not update.message.reply_to_message.photo:
        await update.message.reply_text("❌ Reply to a photo to set it as chat photo.")
        return

    try:
        photo = update.message.reply_to_message.photo[-1]
        await context.bot.set_chat_photo(update.effective_chat.id, photo.file_id)
        await update.message.reply_text("✅ Chat photo updated.")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to set photo: {e}")


async def setchatname_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /setchatname — set chat name."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("❌ This command only works in groups.")
        return

    if not await _is_admin(update, context):
        await update.message.reply_text("❌ You need admin rights.")
        return

    if not context.args:
        await update.message.reply_text("ℹ️ Usage: /setchatname &lt;new name&gt;", parse_mode=ParseMode.HTML)
        return

    name = " ".join(context.args)
    try:
        await context.bot.set_chat_title(update.effective_chat.id, name)
        await update.message.reply_text(f"✅ Chat name set to: <b>{name}</b>", parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to set name: {e}")


async def setchatdescription_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /setchatdescription — set chat description."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("❌ This command only works in groups.")
        return

    if not await _is_admin(update, context):
        await update.message.reply_text("❌ You need admin rights.")
        return

    if not context.args:
        await update.message.reply_text("ℹ️ Usage: /setchatdescription &lt;description&gt;", parse_mode=ParseMode.HTML)
        return

    desc = " ".join(context.args)
    try:
        await context.bot.set_chat_description(update.effective_chat.id, desc)
        await update.message.reply_text("✅ Chat description updated.")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to set description: {e}")


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
