"""Phi pi - Pure Bot API entry point.

Runs python-telegram-bot (Bot API) with colored inline buttons.
No Telethon dependency.
"""

import asyncio
import logging
import signal
import sys
import traceback
from datetime import datetime

from telegram import Update
from telegram.error import NetworkError
from telegram.ext import Application, ContextTypes

from bot.config import settings
from bot.constants import BOT_NAME
from bot.loader import load_modules
from bot.logger import logger
from bot.database import DB_DIR, db


# ── Startup log ──────────────────────────────────────────
LOG_CHANNEL_ID = -1003845687680  # Pi_Logs
HANDLER_ERRORS_FILE = DB_DIR / "handler_errors.log"


def _persist_handler_error(update: object, err: Exception) -> None:
    """Append full traceback to a log file (diagnosable without a paste)."""
    try:
        uid = getattr(update, "update_id", "?")
        chat = "?"
        user = "?"
        if isinstance(update, Update) and update.effective_chat:
            chat = update.effective_chat.id
            user = getattr(update.effective_user, "id", "?")
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        tb = "".join(
            traceback.format_exception(type(err), err, err.__traceback__)
        )
        with open(HANDLER_ERRORS_FILE, "a", encoding="utf-8") as f:
            f.write(
                f"=== {ts} update={uid} chat={chat} user={user} "
                f"{type(err).__name__}: {err}\n{tb}\n"
            )
    except Exception:
        pass  # diagnosis must never raise


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log handler exceptions so failures never disappear silently."""
    err = context.error
    # Expected/soft failures — one line, no traceback noise. isinstance
    # (not just the type name) so every NetworkError subclass — TimedOut,
    # InternalServerError, httpx wrapping — is treated as network.
    if isinstance(err, Exception) and (
        isinstance(err, (NetworkError, TimeoutError))
        or type(err).__name__ in {"TimedOut", "BadRequest"}
    ):
        logger.warning(f"Handler network/API issue: {err}")
        return
    logger.error("Handler error", exc_info=err)
    if isinstance(update, Update) and update.effective_message:
        logger.error(
            "  update=%s chat=%s user=%s",
            update.update_id,
            getattr(update.effective_chat, "id", "?"),
            getattr(update.effective_user, "id", "?"),
        )
    if isinstance(err, Exception):
        _persist_handler_error(update, err)


async def post_init(app: Application) -> None:
    """Fetch bot identity; never block polling on the startup log."""
    me = await app.bot.get_me()
    app.bot_data["username"] = me.username
    app.bot_data["name"] = me.full_name
    logger.info(f"Bot API connected as @{me.username} — {me.full_name}")

    async def _startup_log() -> None:
        try:
            startup_msg = (
                f"<b>Bot Started Successfully!</b>\n\n"
                f"<b>Bot:</b> @{me.username}\n"
                f"<b>Bot ID:</b> <code>{me.id}</code>\n"
                f"<b>Time:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"<b>Modules:</b> Loaded\n"
                f"<b>Database:</b> Connected\n"
                f"<b>Colored Buttons:</b> Active (Pure PTB)\n"
                f"<b>Logging:</b> Active\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━"
            )
            await app.bot.send_message(
                chat_id=LOG_CHANNEL_ID,
                text=startup_msg,
                parse_mode="HTML",
            )
            logger.info("Startup log sent to channel")
        except Exception as e:
            logger.warning(f"Startup log not sent: {e}")

    # Fire-and-forget: start listening for updates immediately.
    asyncio.create_task(_startup_log())


async def post_shutdown(app: Application) -> None:
    """Cleanup on shutdown."""
    logger.info("Shutdown complete")


def main() -> None:
    logger.info(f"Starting {BOT_NAME} (Pure Bot API Mode)...")

    app = (
        Application.builder()
        .token(settings.bot_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        # Regular Bot API HTTP timeouts (sendMessage, etc.) — PTB defaults
        # are often ~5s and fail on slow routes to api.telegram.org.
        .read_timeout(30)
        .write_timeout(30)
        .connect_timeout(30)
        .pool_timeout(30)
        .connection_pool_size(20)
        # Long-poll getUpdates (separate from ordinary API calls).
        .get_updates_read_timeout(30)
        .get_updates_connect_timeout(30)
        .get_updates_write_timeout(30)
        .get_updates_pool_timeout(30)
        .build()
    )
    app.add_error_handler(on_error)

    count = load_modules(app)
    logger.info(f"Loaded {count} module(s) — {BOT_NAME} is ready")

    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
