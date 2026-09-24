"""Instagram media downloader — auto-detect links + manual commands.

Auto-discovered by bot.loader as package `bot.modules.instagram`.
setup(app) lives here so the top-level package exposes setup().

Commands:
  /igdl <url> / /igsettings / /igstats / /igcache / /igbenchmark
Auto-detect: group 14 (text messages containing instagram.com URLs).
"""

from __future__ import annotations

from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from bot.logger import logger

from .config import HANDLER_GROUP, ig_config
from .downloader import sweep_stale
from .handlers import (
    auto_download_handler,
    igbenchmark_command,
    igcache_command,
    ig_callback,
    igdl_command,
    igsettings_command,
    igstats_command,
)


def setup(app: Application) -> list[str]:
    """Register Instagram commands, callbacks, and auto-detect handler."""
    if not ig_config.enabled:
        logger.info("[IG] module disabled via IG_ENABLED=0")
        return ["instagram: disabled"]

    # Startup: sweep leftover temp downloads (hard-guarded inside sweep_stale).
    try:
        sweep_stale()
    except Exception as e:
        logger.warning(f"[IG] temp sweep failed: {e}")

    # Commands — default group 0 (same as other CommandHandlers).
    app.add_handler(CommandHandler("igdl", igdl_command))
    app.add_handler(CommandHandler("igsettings", igsettings_command))
    app.add_handler(CommandHandler("igstats", igstats_command))
    app.add_handler(CommandHandler("igcache", igcache_command))
    app.add_handler(CommandHandler("igbenchmark", igbenchmark_command))

    # Settings callbacks.
    app.add_handler(CallbackQueryHandler(ig_callback, pattern=r"^ig:"))

    # Auto-detect — dedicated high group so we never steal updates from
    # filters(1)/blocklist(2)/watchwords(3)/bind(4)/leveling(5)/…/analytics(13).
    # Narrow filter: only non-command text that contains an Instagram host.
    ig_filter = (
        filters.TEXT
        & ~filters.COMMAND
        & filters.Regex(r"(?i)(instagram\.com|instagr\.am)/")
    )
    app.add_handler(
        MessageHandler(ig_filter, auto_download_handler),
        group=HANDLER_GROUP,
    )

    logger.info(f"[IG] registered (auto group={HANDLER_GROUP})")
    return [
        "/igdl",
        "/igsettings",
        "/igstats",
        "/igcache",
        "/igbenchmark",
        f"auto-detect@{HANDLER_GROUP}",
        "ig:* callbacks",
    ]
