"""Bans module - Global ban (gban), sudo users, and mass-ban features.

Uses SQLite database for storage.
"""

import logging

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode

from bot.database import db

logger = logging.getLogger(__name__)

OWNER_ID = 8301883098


def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID


def is_sudo(user_id: int) -> bool:
    return db.is_sudo_user(user_id) or user_id == OWNER_ID


async def _get_target_user(update, context):
    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        return update.message.reply_to_message.from_user
    if context.args:
        target = context.args[0]
        if target.startswith("@"):
            try:
                member = await context.bot.get_chat_member(update.effective_chat.id, target)
                return member.user
            except Exception:
                return None
        try:
            user_id = int(target)
            member = await context.bot.get_chat_member(update.effective_chat.id, user_id)
            return member.user
        except (ValueError, Exception):
            return None
    return None


async def addsudo_command(update, context):
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("Only the bot owner can manage sudo users.")
        return
    target = await _get_target_user(update, context)
    if not target:
        await update.message.reply_text("Specify a user: /addsudo @user or /addsudo USER_ID")
        return
    db.add_sudo_user(target.id, update.effective_user.id)
    await update.message.reply_text(
        f"Added <a href='tg://user?id={target.id}'>{target.first_name}</a> as sudo user.",
        parse_mode=ParseMode.HTML,
    )


async def rmsudo_command(update, context):
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("Only the bot owner can manage sudo users.")
        return
    target = await _get_target_user(update, context)
    if not target:
        await update.message.reply_text("Specify a user: /rmsudo @user or /rmsudo USER_ID")
        return
    if db.remove_sudo_user(target.id):
        await update.message.reply_text(
            f"Removed <a href='tg://user?id={target.id}'>{target.first_name}</a> from sudo users.",
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text("User is not a sudo user.")


async def sudolist_command(update, context):
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("Only the bot owner can view sudo users.")
        return
    sudo_ids = db.get_sudo_users()
    if not sudo_ids:
        await update.message.reply_text("No sudo users configured.")
        return
    sudo_list = "\n".join([f"  - <code>{uid}</code>" for uid in sorted(sudo_ids)])
    await update.message.reply_text(
        f"<b>Sudo Users:</b>\n{sudo_list}\n\n<b>Owner:</b> <code>{OWNER_ID}</code>",
        parse_mode=ParseMode.HTML,
    )


async def gban_command(update, context):
    if not is_sudo(update.effective_user.id):
        await update.message.reply_text("Only sudo/owner users can use gban.")
        return
    target = await _get_target_user(update, context)
    if not target:
        await update.message.reply_text("Specify a user: /gban @user [reason]")
        return
    if target.id == OWNER_ID:
        await update.message.reply_text("Cannot gban the bot owner.")
        return
    if target.id == context.bot.id:
        await update.message.reply_text("I cannot gban myself.")
        return
    reason = " ".join(context.args[1:]) if len(context.args) > 1 else "No reason provided"
    db.add_gban(target.id, reason, update.effective_user.id)
    try:
        await context.bot.ban_chat_member(update.effective_chat.id, target.id)
    except Exception:
        pass
    total = len(db.get_gbanned_users())
    await update.message.reply_text(
        f"<b>Globally Banned</b> <a href='tg://user?id={target.id}'>{target.first_name}</a>\n"
        f"<b>Reason:</b> {reason}\n<b>Total Gbanned:</b> {total}",
        parse_mode=ParseMode.HTML,
    )


async def ungban_command(update, context):
    if not is_sudo(update.effective_user.id):
        await update.message.reply_text("Only sudo/owner users can use ungban.")
        return
    target = await _get_target_user(update, context)
    if not target:
        await update.message.reply_text("Specify a user: /ungban @user")
        return
    if db.remove_gban(target.id):
        try:
            await context.bot.unban_chat_member(update.effective_chat.id, target.id)
        except Exception:
            pass
        await update.message.reply_text(
            f"<b>Globally Unbanned</b> <a href='tg://user?id={target.id}'>{target.first_name}</a>.",
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text("User is not gbanned.")


async def gbanlist_command(update, context):
    if not is_sudo(update.effective_user.id):
        await update.message.reply_text("Only sudo/owner users can view gbans.")
        return
    gbanned = db.get_gbanned_users()
    if not gbanned:
        await update.message.reply_text("No gbanned users.")
        return
    ban_list = "\n".join([f"  - <code>{g['user_id']}</code> ({g.get('reason', 'N/A')})" for g in gbanned])
    await update.message.reply_text(
        f"<b>Gbanned Users ({len(gbanned)}):</b>\n{ban_list}",
        parse_mode=ParseMode.HTML,
    )


async def massban_command(update, context):
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("Only the bot owner can mass ban.")
        return
    if not context.args:
        await update.message.reply_text("Provide user IDs: /massban 123456 789012 345678")
        return
    banned = 0
    failed = 0
    for arg in context.args:
        try:
            user_id = int(arg)
            await context.bot.ban_chat_member(update.effective_chat.id, user_id)
            db.add_gban(user_id, "Mass ban", update.effective_user.id)
            banned += 1
        except (ValueError, Exception):
            failed += 1
    await update.message.reply_text(f"<b>Mass Ban:</b> Banned: {banned}, Failed: {failed}")


async def sudopromote_command(update, context):
    if not is_sudo(update.effective_user.id):
        await update.message.reply_text("Only sudo/owner users can use this.")
        return
    target = await _get_target_user(update, context)
    if not target:
        await update.message.reply_text("Specify a user: /sudopromote @user")
        return
    try:
        await context.bot.promote_chat_member(
            update.effective_chat.id, target.id,
            can_change_info=True, can_delete_messages=True,
            can_invite_users=True, can_restrict_members=True,
            can_pin_messages=True, can_promote_members=False,
            can_manage_video_chats=True,
        )
        await update.message.reply_text(
            f"Promoted <a href='tg://user?id={target.id}'>{target.first_name}</a>.",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        await update.message.reply_text(f"Failed to promote: {e}")


def setup(app: Application) -> list:
    app.add_handler(CommandHandler("addsudo", addsudo_command))
    app.add_handler(CommandHandler("rmsudo", rmsudo_command))
    app.add_handler(CommandHandler("sudolist", sudolist_command))
    app.add_handler(CommandHandler("gban", gban_command))
    app.add_handler(CommandHandler("ungban", ungban_command))
    app.add_handler(CommandHandler("gbanlist", gbanlist_command))
    app.add_handler(CommandHandler("massban", massban_command))
    app.add_handler(CommandHandler("sudopromote", sudopromote_command))
    return ["addsudo", "rmsudo", "sudolist", "gban", "ungban", "gbanlist", "massban", "sudopromote"]
