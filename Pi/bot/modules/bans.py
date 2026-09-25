"""Bans module - Global ban (gban), sudo users, and mass-ban features.

Uses SQLite database for storage. Success replies use bot.responses cards.
"""

import logging
from html import escape

from telegram import Update
from telegram.ext import Application, ContextTypes
from telegram.constants import ParseMode

from bot.command_handler import CommandHandler
from bot.database import db
from bot.emojis import E
from bot.responses import (
    action_card,
    field_by,
    field_extra,
    field_reason,
    field_user,
    plain_error,
    plain_ok,
    reply_card,
)

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
        await update.message.reply_text(f"{E.CROWN} Only the bot owner can manage sudo users.",
            parse_mode=ParseMode.HTML)
        return
    target = await _get_target_user(update, context)
    if not target:
        await update.message.reply_text(f"{E.ERROR} Specify a user: /addsudo @user or /addsudo USER_ID",
            parse_mode=ParseMode.HTML)
        return
    db.add_sudo_user(target.id, update.effective_user.id)
    await reply_card(
        update.message,
        action_card(
            "Sudo Added",
            [
                field_user(target),
                field_by(update.effective_user, "ADDED BY"),
                field_extra(E.SETTINGS, "Role", "SUDO"),
            ],
            icon=E.CHECK,
        ),
        user=target,
    )


async def rmsudo_command(update, context):
    if not is_owner(update.effective_user.id):
        await update.message.reply_text(f"{E.CROWN} Only the bot owner can manage sudo users.",
            parse_mode=ParseMode.HTML)
        return
    target = await _get_target_user(update, context)
    if not target:
        await update.message.reply_text(f"{E.ERROR} Specify a user: /rmsudo @user or /rmsudo USER_ID",
            parse_mode=ParseMode.HTML)
        return
    if db.remove_sudo_user(target.id):
        await reply_card(
            update.message,
            action_card(
                "Sudo Removed",
                [
                    field_user(target),
                    field_by(update.effective_user, "REMOVED BY"),
                ],
                icon=E.CROSS,
            ),
            user=target,
        )
    else:
        await update.message.reply_text(plain_error("User is not a sudo user."),
            parse_mode=ParseMode.HTML)


async def sudolist_command(update, context):
    if not is_owner(update.effective_user.id):
        await update.message.reply_text(f"{E.CROWN} Only the bot owner can view sudo users.",
            parse_mode=ParseMode.HTML)
        return
    sudo_ids = db.get_sudo_users()
    if not sudo_ids:
        await update.message.reply_text(f"{E.INFO} No sudo users configured.",
            parse_mode=ParseMode.HTML)
        return

    async def _user_label(uid: int) -> str:
        """Clickable @username/name for uid; plain code id if lookup fails."""
        try:
            chat = await context.bot.get_chat(uid)
            name = getattr(chat, "first_name", None) or getattr(chat, "title", None)
            uname = getattr(chat, "username", None)
            if name:
                label = f"@{escape(uname)}" if uname else escape(name)
                return f'<a href="tg://user?id={uid}">{label}</a>'
        except Exception:
            pass
        return f"<code>{uid}</code>"

    # List comp (eager, awaited here) — a genexp with `await` silently
    # becomes an async_generator, which str.join cannot iterate.
    sudo_list = "\n".join(
        [f"  - {await _user_label(uid)}" for uid in sorted(sudo_ids)]
    )
    owner_label = await _user_label(OWNER_ID)
    await update.message.reply_text(
        f"{E.ADMIN} <b>Sudo Users:</b>\n{sudo_list}\n\n"
        f"{E.CROWN} <b>Owner:</b> {owner_label}",
        parse_mode=ParseMode.HTML,
    )


async def gban_command(update, context):
    if not is_sudo(update.effective_user.id):
        await update.message.reply_text(f"{E.ERROR} Only sudo/owner users can use gban.",
            parse_mode=ParseMode.HTML)
        return
    target = await _get_target_user(update, context)
    if not target:
        await update.message.reply_text(f"{E.ERROR} Specify a user: /gban @user [reason]",
            parse_mode=ParseMode.HTML)
        return
    if target.id == OWNER_ID:
        await update.message.reply_text(f"{E.CROWN} Cannot gban the bot owner.",
            parse_mode=ParseMode.HTML)
        return
    if target.id == context.bot.id:
        await update.message.reply_text(f"{E.ERROR} I cannot gban myself.",
            parse_mode=ParseMode.HTML)
        return
    # Reply target: every arg is a reason word. Otherwise args[0] was
    # the target (@username / numeric ID) — drop it so it never shows
    # up as the reason.
    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        reason_args = list(context.args or [])
    else:
        reason_args = list(context.args or [])[1:]
    reason = " ".join(reason_args).strip() or "No reason provided"
    db.add_gban(target.id, reason, update.effective_user.id)
    try:
        await context.bot.ban_chat_member(update.effective_chat.id, target.id)
    except Exception:
        pass
    total = len(db.get_gbanned_users())
    await reply_card(
        update.message,
        action_card(
            "Global Ban Successful",
            [
                field_user(target),
                field_by(update.effective_user, "BANNED BY"),
                field_reason(reason),
                field_extra(E.BAN, "Total Gbanned", str(total)),
            ],
            icon=E.BAN,
        ),
        user=target,
    )


async def ungban_command(update, context):
    if not is_sudo(update.effective_user.id):
        await update.message.reply_text(f"{E.ERROR} Only sudo/owner users can use ungban.",
            parse_mode=ParseMode.HTML)
        return
    target = await _get_target_user(update, context)
    if not target:
        await update.message.reply_text(f"{E.ERROR} Specify a user: /ungban @user",
            parse_mode=ParseMode.HTML)
        return
    if db.remove_gban(target.id):
        try:
            await context.bot.unban_chat_member(update.effective_chat.id, target.id)
        except Exception:
            pass
        await reply_card(
            update.message,
            action_card(
                "Global Unban Successful",
                [
                    field_user(target),
                    field_by(update.effective_user, "UNBANNED BY"),
                ],
                icon=E.UNBAN,
            ),
            user=target,
        )
    else:
        await update.message.reply_text(plain_error("User is not gbanned."),
            parse_mode=ParseMode.HTML)


async def gbanlist_command(update, context):
    if not is_sudo(update.effective_user.id):
        await update.message.reply_text(f"{E.ERROR} Only sudo/owner users can view gbans.",
            parse_mode=ParseMode.HTML)
        return
    gbanned = db.get_gbanned_users()
    if not gbanned:
        await update.message.reply_text(f"{E.INFO} No gbanned users.",
            parse_mode=ParseMode.HTML)
        return
    ban_list = "\n".join([f"  - <code>{g['user_id']}</code> ({g.get('reason', 'N/A')})" for g in gbanned])
    await update.message.reply_text(
        f"{E.BAN} <b>Gbanned Users ({len(gbanned)}):</b>\n{ban_list}",
        parse_mode=ParseMode.HTML,
    )


async def massban_command(update, context):
    if not is_owner(update.effective_user.id):
        await update.message.reply_text(f"{E.CROWN} Only the bot owner can mass ban.",
            parse_mode=ParseMode.HTML)
        return
    if not context.args:
        await update.message.reply_text(f"{E.ERROR} Provide user IDs: /massban 123456 789012 345678",
            parse_mode=ParseMode.HTML)
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
    await reply_card(
        update.message,
        action_card(
            "Mass Ban Complete",
            [
                field_by(update.effective_user, "BANNED BY"),
                field_extra(E.BAN, "Banned", str(banned)),
                field_extra(E.ERROR, "Failed", str(failed)),
            ],
            icon=E.BAN,
        ),
    )


async def sudopromote_command(update, context):
    if not is_sudo(update.effective_user.id):
        await update.message.reply_text(f"{E.ERROR} Only sudo/owner users can use this.",
            parse_mode=ParseMode.HTML)
        return
    target = await _get_target_user(update, context)
    if not target:
        await update.message.reply_text(f"{E.ERROR} Specify a user: /sudopromote @user",
            parse_mode=ParseMode.HTML)
        return
    try:
        await context.bot.promote_chat_member(
            update.effective_chat.id, target.id,
            can_change_info=True, can_delete_messages=True,
            can_invite_users=True, can_restrict_members=True,
            can_pin_messages=True, can_promote_members=False,
            can_manage_video_chats=True,
        )
        await reply_card(
            update.message,
            action_card(
                "Promotion Successful",
                [
                    field_user(target),
                    field_by(update.effective_user, "PROMOTED BY"),
                    field_extra(E.SETTINGS, "Title", "ADMIN"),
                ],
                icon=E.CHECK,
            ),
            user=target,
        )
    except Exception as e:
        await update.message.reply_text(plain_error(f"Failed to promote: {e}"),
            parse_mode=ParseMode.HTML)


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
