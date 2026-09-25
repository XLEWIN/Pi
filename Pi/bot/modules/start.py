"""Start module — /start command with colored inline buttons.

Works in both private chats and groups. Uses Bot API 9.4+ button styling.
"""

import asyncio
from html import escape

from telegram import Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes
from telegram.constants import ParseMode

from bot.command_handler import CommandHandler
from bot.constants import BOT_DESCRIPTION, START_TEXT, URL_ADD_TO_GROUP, URL_OFFICIAL_CHANNEL, URL_NETWORK
from bot.database import db
from bot.emojis import E
from bot.keyboards.colored import btn_primary, btn_success, btn_url, build_keyboard
from bot.logger import logger

# Log channel configuration
LOG_CHANNEL_ID = -1003845687680
# Background only — never on the reply path. Generous so slow routes still land.
LOG_TIMEOUT_SECONDS = 10
# Hot-path reply: fail fast if the network is dead, but allow one real RTT.
REPLY_TIMEOUT_SECONDS = 20


def _spawn(coro) -> None:
    """Run a coroutine in the background; never block the caller."""
    task = asyncio.create_task(coro)

    def _done(t: asyncio.Task) -> None:
        if t.cancelled():
            return
        exc = t.exception()
        if exc:
            logger.warning(f"Background task failed: {exc}")

    task.add_done_callback(_done)


async def send_log(context: ContextTypes.DEFAULT_TYPE, message: str):
    """Send a log message. Fails soft — never raises to the caller."""
    try:
        await asyncio.wait_for(
            context.bot.send_message(
                chat_id=LOG_CHANNEL_ID,
                text=message,
                parse_mode=ParseMode.HTML,
            ),
            timeout=LOG_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning(f"Log send timed out after {LOG_TIMEOUT_SECONDS}s")
    except Exception as e:
        logger.warning(f"Log send failed: {e}")


def format_user_log(user, action: str, chat_title: str = None) -> str:
    """Format user log message.

    Plain HTML only — custom <tg-emoji> tags are rejected (400) in some chats.
    """
    username = f"@{escape(user.username)}" if user.username else "No username"
    raw_name = user.full_name or user.first_name or "Unknown"
    # Uppercase BEFORE escaping so we don't produce &LT; entities.
    name = escape(raw_name)

    log_text = f"👤 <b>{name}</b> {escape(action)}\n"
    log_text += f"🆔 User ID: <code>{user.id}</code>\n"
    log_text += f"📛 Username: {username}"

    if chat_title:
        log_text += f"\n💬 Chat: {escape(chat_title)}"

    return log_text


def build_start_keyboard(icons: bool = True):
    """Start menu keyboard with colored buttons; icons=False = plain retry."""
    from bot.emojis import EID

    buttons = [
        [
            btn_url("Bot To Chat", URL_ADD_TO_GROUP,
                    icon_emoji_id=EID.ADD if icons else None),
            btn_primary("Help Menu", "start:help",
                        icon_emoji_id=EID.INFO if icons else None),
        ],
        [
            btn_success("Dashboard", "start:dashboard",
                        icon_emoji_id=EID.WEB if icons else None),
        ],
        [
            btn_url("Channel", URL_OFFICIAL_CHANNEL,
                    icon_emoji_id=EID.ANNOUNCE if icons else None),
            btn_url("Network", URL_NETWORK,
                    icon_emoji_id=EID.TRAVEL if icons else None),
        ],
    ]
    return build_keyboard(buttons)


async def _safe_delete(message) -> None:
    try:
        await message.delete()
    except Exception:
        pass


async def _finish_start(context: ContextTypes.DEFAULT_TYPE, user, chat) -> None:
    """Background work after the user already has their reply."""
    def _db_work() -> None:
        try:
            db.add_user(
                user_id=user.id,
                username=user.username,
                first_name=user.first_name,
                last_name=user.last_name,
                is_bot=user.is_bot,
            )
            db.update_user_activity(
                user_id=user.id,
                action="started the bot (DM)" if chat.type == "private" else f"used /start in {chat.title}",
                chat_id=chat.id,
                chat_title="DM" if chat.type == "private" else chat.title,
            )
        except Exception as e:
            logger.warning(f"start DB update failed: {e}")

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _db_work)

    if chat.type == "private":
        await send_log(context, format_user_log(user, "started the bot (DM)"))


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start — one reply first; DB + log only after that."""
    user = update.effective_user
    chat = update.effective_chat

    username = context.bot_data.get("username", "Phi π")
    text = START_TEXT.format(
        fire=E.FIRE,
        username=f"@{username}",
        description=BOT_DESCRIPTION,
        arrow=E.ARROW,
    )
    plain_text = (
        f"🔥 @{username}\n"
        f"{BOT_DESCRIPTION}\n\n"
        f"✈️ /help for the full command list."
    )
    keyboard = build_start_keyboard()
    plain_keyboard = build_start_keyboard(icons=False)

    # Exactly one attempt for the brand payload. Timeouts must not stack
    # (that was the multi-second stall). Only a fast BadRequest gets a
    # plain-HTML retry — never retry a TimedOut.
    try:
        await asyncio.wait_for(
            update.message.reply_text(
                text, reply_markup=keyboard, parse_mode=ParseMode.HTML
            ),
            timeout=REPLY_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.error("Start reply timed out (network/API)")
        return
    except Exception as e:
        # e.g. custom emoji / icon rejected — one plain fallback, no loop.
        logger.warning(f"Start brand reply failed ({e}); sending plain fallback")
        try:
            await asyncio.wait_for(
                update.message.reply_text(
                    plain_text, reply_markup=plain_keyboard, parse_mode=ParseMode.HTML
                ),
                timeout=REPLY_TIMEOUT_SECONDS,
            )
        except Exception as e2:
            logger.error(f"Failed to send start message: {e2}")
            return

    # Reply is on its way — only now do delete / DB / channel log.
    if update.message:
        _spawn(_safe_delete(update.message))

    _spawn(_finish_start(context, user, chat))


async def start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the start callback buttons."""
    query = update.callback_query
    data = query.data or ""
    if data == "start:help":
        # Open the interactive help menu (help.py owns the edit).
        from bot.modules.help import show_main_menu

        await show_main_menu(update, context)
        return
    await query.answer("Coming soon!", show_alert=False)


def setup(app: Application) -> list[str]:
    """Register this module's handlers."""
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CallbackQueryHandler(start_callback, pattern=r"^start:"))
    return ["/start", "start:* callbacks"]
