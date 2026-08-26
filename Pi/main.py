"""Phi pi - Pure Bot API entry point.

Runs python-telegram-bot (Bot API) with colored inline buttons.
No Telethon dependency.
"""

import signal
import sys
from datetime import datetime

from telegram import Update
from telegram.ext import Application

from bot.config import settings
from bot.constants import BOT_NAME
from bot.loader import load_modules
from bot.logger import logger
from bot.database import db


# ── Startup log ──────────────────────────────────────────
LOG_CHANNEL_ID = -1003963429635  # Phi_Logs


async def post_init(app: Application) -> None:
    """Fetch bot identity and send startup log."""
    me = await app.bot.get_me()
    app.bot_data["username"] = me.username
    app.bot_data["name"] = me.full_name
    logger.info(f"Bot API connected as @{me.username} — {me.full_name}")

    # Send startup log
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
        logger.error(f"Failed to send startup log: {e}")


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
        .build()
    )

    count = load_modules(app)
    logger.info(f"Loaded {count} module(s) — {BOT_NAME} is ready")

    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        read_timeout=30,
        connect_timeout=30,
        write_timeout=30,
    )


if __name__ == "__main__":
    main()
