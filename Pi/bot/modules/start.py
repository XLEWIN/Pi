"""Start module — /start command with colored inline buttons.

Works in both private chats and groups. Pure PTB implementation.
"""

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes
from telegram.constants import ParseMode

from bot.constants import BOT_DESCRIPTION, START_TEXT, URL_ADD_TO_GROUP, URL_OFFICIAL_CHANNEL, URL_NETWORK
from bot.database import db
from bot.keyboards.colored import btn_primary, btn_success, btn_url, build_keyboard

# Log channel configuration
LOG_CHANNEL_ID = -1003963429635


async def send_log(context: ContextTypes.DEFAULT_TYPE, message: str):
    """Send a log message to the log channel."""
    try:
        await context.bot.send_message(
            chat_id=LOG_CHANNEL_ID,
            text=message,
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        print(f"Error sending log: {e}")


def format_user_log(user, action: str, chat_title: str = None) -> str:
    """Format user log message."""
    username = f"@{user.username}" if user.username else "No username"
    name = user.full_name or user.first_name or "Unknown"

    log_text = f"👤 <b>{name.upper()}</b> {action}\n"
    log_text += f"🆔 User ID: <code>{user.id}</code>\n"
    log_text += f"📛 Username: {username}"

    if chat_title:
        log_text += f"\n💬 Chat: {chat_title}"

    return log_text


def build_start_keyboard():
    """Build the start menu keyboard with colored buttons."""
    buttons = [
        [
            btn_url("➕ Add Bot To Chat", URL_ADD_TO_GROUP),
            btn_primary("📖 Help Menu", "start:help"),
        ],
        [
            btn_success("🌐 Dashboard", "start:dashboard"),
        ],
        [
            btn_url("📢 Channel", URL_OFFICIAL_CHANNEL),
            btn_url("🕸️ Network", URL_NETWORK),
        ],
    ]
    return build_keyboard(buttons)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start — show the welcome message with colored buttons."""
    user = update.effective_user
    chat = update.effective_chat
    
    # Register user in database
    db.add_user(
        user_id=user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
        is_bot=user.is_bot
    )
    
    # Log user activity
    if chat.type == "private":
        db.update_user_activity(
            user_id=user.id,
            action="started the bot (DM)",
            chat_id=chat.id,
            chat_title="DM"
        )
        log_message = format_user_log(user, "started the bot (DM)")
        await send_log(context, log_message)
    else:
        db.update_user_activity(
            user_id=user.id,
            action=f"used /start in {chat.title}",
            chat_id=chat.id,
            chat_title=chat.title
        )
    
    # Send with colored buttons
    username = context.bot_data.get("username", "Phi π")
    text = START_TEXT.format(username=f"@{username}", description=BOT_DESCRIPTION)
    
    try:
        await update.message.reply_text(
            text,
            reply_markup=build_start_keyboard(),
            parse_mode=ParseMode.HTML,
        )
        await update.message.delete()
    except Exception as e:
        print(f"Failed to send start message: {e}")


async def start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the start callback buttons."""
    query = update.callback_query
    await query.answer("Coming soon!", show_alert=False)


def setup(app: Application) -> list[str]:
    """Register this module's handlers."""
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CallbackQueryHandler(start_callback, pattern=r"^start:"))
    return ["/start", "start:* callbacks"]
