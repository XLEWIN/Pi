"""Blocklist module — Blacklisted words with delete/warn/mute/ban/kick actions.

Uses SQLite database for storage.
"""

import logging

from telegram import Update, ChatPermissions
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode

from bot.database import db

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


async def _take_action(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    action: str,
    reason: str,
):
    """Execute blocklist action on a user."""
    chat_id = update.effective_chat.id

    try:
        if action == "delete":
            await update.message.delete()
            return

        elif action == "warn":
            await update.message.delete()
            await context.bot.send_message(
                chat_id,
                f"⚠️ <a href='tg://user?id={user_id}'>{user_id}</a> used a blocked word.\n"
                f"<b>Action:</b> Warning issued.\n<b>Reason:</b> {reason}",
                parse_mode=ParseMode.HTML,
            )
            return

        elif action == "mute":
            await update.message.delete()
            permissions = ChatPermissions(can_send_messages=False)
            await context.bot.restrict_chat_member(chat_id, user_id, permissions)
            await context.bot.send_message(
                chat_id,
                f"🔇 <a href='tg://user?id={user_id}'>{user_id}</a> muted for using a blocked word.\n"
                f"<b>Reason:</b> {reason}",
                parse_mode=ParseMode.HTML,
            )
            return

        elif action == "kick":
            await update.message.delete()
            await context.bot.ban_chat_member(chat_id, user_id)
            await context.bot.unban_chat_member(chat_id, user_id)
            await context.bot.send_message(
                chat_id,
                f"👢 <a href='tg://user?id={user_id}'>{user_id}</a> kicked for using a blocked word.\n"
                f"<b>Reason:</b> {reason}",
                parse_mode=ParseMode.HTML,
            )
            return

        elif action == "ban":
            await update.message.delete()
            await context.bot.ban_chat_member(chat_id, user_id)
            await context.bot.send_message(
                chat_id,
                f"🔨 <a href='tg://user?id={user_id}'>{user_id}</a> banned for using a blocked word.\n"
                f"<b>Reason:</b> {reason}",
                parse_mode=ParseMode.HTML,
            )
            return

    except Exception as e:
        logger.error(f"Blocklist action error: {e}")


# ── Command handlers ─────────────────────────────────────
async def add_blocklist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /blocklist — add words to the blocklist."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("❌ This command only works in groups.")
        return

    if not await _is_admin(update, context):
        await update.message.reply_text("❌ You need admin rights to manage the blocklist.")
        return

    if not context.args:
        await update.message.reply_text(
            "ℹ️ <b>Usage:</b>\n"
            "• /blocklist &lt;word1&gt; &lt;word2&gt; ... — Add words\n"
            "• /unblocklist &lt;word1&gt; &lt;word2&gt; ... — Remove words\n"
            "• /blocklistview — View blocked words\n"
            "• /setblocklistaction &lt;delete|warn|mute|kick|ban&gt; — Set action\n"
            "• /blocklistreason &lt;reason&gt; — Set reason\n"
            "• /unblocklistall — Clear all blocked words",
            parse_mode=ParseMode.HTML,
        )
        return

    chat_id = update.effective_chat.id

    # Get current action/reason from first word or use defaults
    action = "delete"
    reason = "Blocked word"

    words = {w.lower() for w in context.args}
    added = []
    for word in words:
        if db.add_blocklist_word(chat_id, word, action, reason):
            added.append(word)

    if added:
        word_list = ", ".join(f"<code>{w}</code>" for w in sorted(added))
        await update.message.reply_text(f"✅ Added to blocklist: {word_list}", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text("❌ Failed to add words.")


async def remove_blocklist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /unblocklist — remove words from the blocklist."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("❌ This command only works in groups.")
        return

    if not await _is_admin(update, context):
        await update.message.reply_text("❌ You need admin rights to manage the blocklist.")
        return

    if not context.args:
        await update.message.reply_text("ℹ️ Usage: /unblocklist &lt;word1&gt; &lt;word2&gt; ...", parse_mode=ParseMode.HTML)
        return

    chat_id = update.effective_chat.id
    words = {w.lower() for w in context.args}
    removed = []
    for word in words:
        if db.remove_blocklist_word(chat_id, word):
            removed.append(word)

    if removed:
        word_list = ", ".join(f"<code>{w}</code>" for w in sorted(removed))
        await update.message.reply_text(f"✅ Removed from blocklist: {word_list}", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text("❌ None of those words were in the blocklist.")


async def view_blocklist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /blocklistview — view blocked words."""
    chat_id = update.effective_chat.id
    blocklist = db.get_blocklist(chat_id)

    if not blocklist:
        await update.message.reply_text("ℹ️ No blocked words in this chat.")
        return

    word_list = "\n".join([f"• <code>{b['word']}</code> ({b['action']})" for b in blocklist])
    await update.message.reply_text(
        f"🚫 <b>Blocked Words ({len(blocklist)}):</b>\n{word_list}",
        parse_mode=ParseMode.HTML,
    )


async def clear_blocklist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /unblocklistall — clear all blocked words."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("❌ This command only works in groups.")
        return

    if not await _is_admin(update, context):
        await update.message.reply_text("❌ You need admin rights.")
        return

    chat_id = update.effective_chat.id
    count = db.clear_blocklist(chat_id)
    if count > 0:
        await update.message.reply_text(f"✅ Cleared {count} blocked words.")
    else:
        await update.message.reply_text("ℹ️ No blocklist found.")


async def set_blocklist_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /setblocklistaction — set the action for blocked words."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("❌ This command only works in groups.")
        return

    if not await _is_admin(update, context):
        await update.message.reply_text("❌ You need admin rights.")
        return

    if not context.args or context.args[0].lower() not in ["delete", "warn", "mute", "kick", "ban"]:
        await update.message.reply_text(
            "❌ Choose action: <b>delete</b>, <b>warn</b>, <b>mute</b>, <b>kick</b>, <b>ban</b>",
            parse_mode=ParseMode.HTML,
        )
        return

    chat_id = update.effective_chat.id
    db.set_blocklist_action(chat_id, context.args[0].lower())
    await update.message.reply_text(f"✅ Blocklist action set to: <b>{context.args[0].lower()}</b>", parse_mode=ParseMode.HTML)


async def set_blocklist_reason(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /blocklistreason — set the reason for blocked words."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("❌ This command only works in groups.")
        return

    if not await _is_admin(update, context):
        await update.message.reply_text("❌ You need admin rights.")
        return

    if not context.args:
        await update.message.reply_text("ℹ️ Usage: /blocklistreason &lt;reason&gt;", parse_mode=ParseMode.HTML)
        return

    chat_id = update.effective_chat.id
    reason = " ".join(context.args)
    db.set_blocklist_reason(chat_id, reason)
    await update.message.reply_text(f"✅ Blocklist reason set to: <b>{reason}</b>", parse_mode=ParseMode.HTML)


async def blocklist_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check incoming messages against blocklist."""
    if not update.message or update.effective_chat.type == "private":
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    # Check exemptions
    if db.is_blocklist_exempt(chat_id, user_id):
        return

    blocklist = db.get_blocklist(chat_id)
    if not blocklist:
        return

    text = (update.message.text or update.message.caption or "").lower()

    # Get the action/reason from the first entry (they're all the same per chat)
    action = blocklist[0]["action"]
    reason = blocklist[0]["reason"]

    for b in blocklist:
        if b["word"] in text:
            await _take_action(update, context, user_id, action, reason)
            return


# ── Module setup ─────────────────────────────────────────
def setup(app: Application) -> list:
    """Register blocklist commands and message handler."""
    app.add_handler(CommandHandler("blocklist", add_blocklist, filters=filters.ChatType.GROUPS))
    app.add_handler(CommandHandler("unblocklist", remove_blocklist, filters=filters.ChatType.GROUPS))
    app.add_handler(CommandHandler("blocklistview", view_blocklist, filters=filters.ChatType.GROUPS))
    app.add_handler(CommandHandler("unblocklistall", clear_blocklist, filters=filters.ChatType.GROUPS))
    app.add_handler(CommandHandler("setblocklistaction", set_blocklist_action, filters=filters.ChatType.GROUPS))
    app.add_handler(CommandHandler("blocklistreason", set_blocklist_reason, filters=filters.ChatType.GROUPS))
    app.add_handler(MessageHandler(filters.TEXT | filters.CAPTION, blocklist_check), group=2)

    return ["blocklist", "unblocklist", "blocklistview", "unblocklistall", "setblocklistaction", "blocklistreason"]
