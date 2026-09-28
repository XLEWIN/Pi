"""Blocklist module — Blacklisted words with delete/warn/mute/ban/kick actions.

Uses SQLite database for storage.
"""

import asyncio
import logging

from aiogram import Bot, F
from aiogram.enums import ParseMode
from aiogram.filters.logic import and_f
from aiogram.types import ChatPermissions, Message

from bot.command_handler import COMMAND
from bot.pipeline import GROUPS, cmd, on
from bot.reply import reply_text
from bot.database import db
from bot.emojis import E

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


async def _take_action(
    message: Message,
    bot: Bot,
    user_id: int,
    action: str,
    reason: str,
):
    """Execute blocklist action on a user."""
    chat_id = message.chat.id

    try:
        if action == "delete":
            await message.delete()
            return

        elif action == "warn":
            await message.delete()
            await bot.send_message(
                chat_id,
                f"{E.WARN} <a href='tg://user?id={user_id}'>{user_id}</a> used a blocked word.\n"
                f"<b>Action:</b> Warning issued.\n<b>Reason:</b> {reason}",
                parse_mode=ParseMode.HTML,
            )
            return

        elif action == "mute":
            await message.delete()
            permissions = ChatPermissions(can_send_messages=False)
            await bot.restrict_chat_member(chat_id, user_id, permissions)
            await bot.send_message(
                chat_id,
                f"{E.MUTE} <a href='tg://user?id={user_id}'>{user_id}</a> muted for using a blocked word.\n"
                f"<b>Reason:</b> {reason}",
                parse_mode=ParseMode.HTML,
            )
            return

        elif action == "kick":
            await message.delete()
            await bot.ban_chat_member(chat_id, user_id)
            await bot.unban_chat_member(chat_id, user_id)
            await bot.send_message(
                chat_id,
                f"{E.KICK} <a href='tg://user?id={user_id}'>{user_id}</a> kicked for using a blocked word.\n"
                f"<b>Reason:</b> {reason}",
                parse_mode=ParseMode.HTML,
            )
            return

        elif action == "ban":
            await message.delete()
            await bot.ban_chat_member(chat_id, user_id)
            await bot.send_message(
                chat_id,
                f"{E.BAN} <a href='tg://user?id={user_id}'>{user_id}</a> banned for using a blocked word.\n"
                f"<b>Reason:</b> {reason}",
                parse_mode=ParseMode.HTML,
            )
            return

    except Exception as e:
        logger.warning(f"Blocklist action error: {e}")


# ── Command handlers ─────────────────────────────────────
async def add_blocklist(message: Message, bot: Bot, args: list):
    """Handle /blocklist — add words to the blocklist."""
    if message.chat.type == "private":
        await reply_text(message, f"{E.ERROR} This command only works in groups.",
            parse_mode=ParseMode.HTML)
        return

    if not await _is_admin(message, bot):
        await reply_text(message, f"{E.ERROR} You need admin rights to manage the blocklist.",
            parse_mode=ParseMode.HTML)
        return

    if not args:
        await reply_text(message,
            f"{E.INFO} <b>Usage:</b>\n"
            "• /blocklist &lt;word1&gt; &lt;word2&gt; ... — Add words\n"
            "• /unblocklist &lt;word1&gt; &lt;word2&gt; ... — Remove words\n"
            "• /blocklistview — View blocked words\n"
            "• /setblocklistaction &lt;delete|warn|mute|kick|ban&gt; — Set action\n"
            "• /blocklistreason &lt;reason&gt; — Set reason\n"
            "• /unblocklistall — Clear all blocked words",
            parse_mode=ParseMode.HTML,
        )
        return

    chat_id = message.chat.id

    # Get current action/reason from first word or use defaults
    action = "delete"
    reason = "Blocked word"

    words = {w.lower() for w in args}
    added = []
    for word in words:
        if await asyncio.to_thread(db.add_blocklist_word, chat_id, word, action, reason):
            added.append(word)

    if added:
        word_list = ", ".join(f"<code>{w}</code>" for w in sorted(added))
        await reply_text(message, f"{E.CHECK} Added to blocklist: {word_list}", parse_mode=ParseMode.HTML)
    else:
        await reply_text(message, f"{E.ERROR} Failed to add words.",
            parse_mode=ParseMode.HTML)


async def remove_blocklist(message: Message, bot: Bot, args: list):
    """Handle /unblocklist — remove words from the blocklist."""
    if message.chat.type == "private":
        await reply_text(message, f"{E.ERROR} This command only works in groups.",
            parse_mode=ParseMode.HTML)
        return

    if not await _is_admin(message, bot):
        await reply_text(message, f"{E.ERROR} You need admin rights to manage the blocklist.",
            parse_mode=ParseMode.HTML)
        return

    if not args:
        await reply_text(message, f"{E.INFO} Usage: /unblocklist &lt;word1&gt; &lt;word2&gt; ...", parse_mode=ParseMode.HTML)
        return

    chat_id = message.chat.id
    words = {w.lower() for w in args}
    removed = []
    for word in words:
        if await asyncio.to_thread(db.remove_blocklist_word, chat_id, word):
            removed.append(word)

    if removed:
        word_list = ", ".join(f"<code>{w}</code>" for w in sorted(removed))
        await reply_text(message, f"{E.CHECK} Removed from blocklist: {word_list}", parse_mode=ParseMode.HTML)
    else:
        await reply_text(message, f"{E.ERROR} None of those words were in the blocklist.",
            parse_mode=ParseMode.HTML)


async def view_blocklist(message: Message):
    """Handle /blocklistview — view blocked words."""
    chat_id = message.chat.id
    blocklist = await asyncio.to_thread(db.get_blocklist, chat_id)

    if not blocklist:
        await reply_text(message, f"{E.INFO} No blocked words in this chat.",
            parse_mode=ParseMode.HTML)
        return

    word_list = "\n".join([f"• <code>{b['word']}</code> ({b['action']})" for b in blocklist])
    await reply_text(message,
        f"{E.ALERT} <b>Blocked Words ({len(blocklist)}):</b>\n{word_list}",
        parse_mode=ParseMode.HTML,
    )


async def clear_blocklist(message: Message, bot: Bot):
    """Handle /unblocklistall — clear all blocked words."""
    if message.chat.type == "private":
        await reply_text(message, f"{E.ERROR} This command only works in groups.",
            parse_mode=ParseMode.HTML)
        return

    if not await _is_admin(message, bot):
        await reply_text(message, f"{E.ERROR} You need admin rights.",
            parse_mode=ParseMode.HTML)
        return

    chat_id = message.chat.id
    count = await asyncio.to_thread(db.clear_blocklist, chat_id)
    if count > 0:
        await reply_text(message, f"{E.CHECK} Cleared {count} blocked words.",
            parse_mode=ParseMode.HTML)
    else:
        await reply_text(message, f"{E.INFO} No blocklist found.",
            parse_mode=ParseMode.HTML)


async def set_blocklist_action(message: Message, bot: Bot, args: list):
    """Handle /setblocklistaction — set the action for blocked words."""
    if message.chat.type == "private":
        await reply_text(message, f"{E.ERROR} This command only works in groups.",
            parse_mode=ParseMode.HTML)
        return

    if not await _is_admin(message, bot):
        await reply_text(message, f"{E.ERROR} You need admin rights.",
            parse_mode=ParseMode.HTML)
        return

    if not args or args[0].lower() not in ["delete", "warn", "mute", "kick", "ban"]:
        await reply_text(message,
            f"{E.ERROR} Choose action: <b>delete</b>, <b>warn</b>, <b>mute</b>, <b>kick</b>, <b>ban</b>",
            parse_mode=ParseMode.HTML,
        )
        return

    chat_id = message.chat.id
    await asyncio.to_thread(db.set_blocklist_action, chat_id, args[0].lower())
    await reply_text(message, f"{E.CHECK} Blocklist action set to: <b>{args[0].lower()}</b>", parse_mode=ParseMode.HTML)


async def set_blocklist_reason(message: Message, bot: Bot, args: list):
    """Handle /blocklistreason — set the reason for blocked words."""
    if message.chat.type == "private":
        await reply_text(message, f"{E.ERROR} This command only works in groups.",
            parse_mode=ParseMode.HTML)
        return

    if not await _is_admin(message, bot):
        await reply_text(message, f"{E.ERROR} You need admin rights.",
            parse_mode=ParseMode.HTML)
        return

    if not args:
        await reply_text(message, f"{E.INFO} Usage: /blocklistreason &lt;reason&gt;", parse_mode=ParseMode.HTML)
        return

    chat_id = message.chat.id
    reason = " ".join(args)
    await asyncio.to_thread(db.set_blocklist_reason, chat_id, reason)
    await reply_text(message, f"{E.CHECK} Blocklist reason set to: <b>{reason}</b>", parse_mode=ParseMode.HTML)


async def blocklist_check(message: Message, bot: Bot):
    """Check incoming messages against blocklist."""
    if not message or message.chat.type == "private":
        return

    chat_id = message.chat.id
    user_id = message.from_user.id

    # Check exemptions
    if await asyncio.to_thread(db.is_blocklist_exempt, chat_id, user_id):
        return

    blocklist = await asyncio.to_thread(db.get_blocklist, chat_id)
    if not blocklist:
        return

    text = (message.text or message.caption or "").lower()

    # Get the action/reason from the first entry (they're all the same per chat)
    action = blocklist[0]["action"]
    reason = blocklist[0]["reason"]

    for b in blocklist:
        if b["word"] in text:
            def _count_spam() -> None:
                try:
                    db.bump_spam_attempts(chat_id, 1)
                    db.record_reputation_event(user_id, "warning", 1)
                except Exception as e:
                    logger.debug(f"spam counter failed: {e}")

            import asyncio
            asyncio.get_running_loop().run_in_executor(None, _count_spam)
            await _take_action(message, bot, user_id, action, reason)
            return


# ── Module setup ─────────────────────────────────────────
def setup() -> list:
    """Register blocklist commands and message handler."""
    on("message", add_blocklist, flt=and_f(cmd("blocklist"), GROUPS))
    on("message", remove_blocklist, flt=and_f(cmd("unblocklist"), GROUPS))
    on("message", view_blocklist, flt=and_f(cmd("blocklistview"), GROUPS))
    on("message", clear_blocklist, flt=and_f(cmd("unblocklistall"), GROUPS))
    on("message", set_blocklist_action, flt=and_f(cmd("setblocklistaction"), GROUPS))
    on("message", set_blocklist_reason, flt=and_f(cmd("blocklistreason"), GROUPS))
    on("message", blocklist_check, group=2, flt=and_f(F.text | F.caption, ~COMMAND))

    return ["blocklist", "unblocklist", "blocklistview", "unblocklistall", "setblocklistaction", "blocklistreason"]
