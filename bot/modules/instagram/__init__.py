"""Instagram media downloader — auto-detect links + manual commands.

Auto-discovered by bot.loader as package `bot.modules.instagram`.
setup() lives here so the top-level package exposes setup().

Commands:
  /igdl <url> / /igsettings / /igstats / /igcache / /igbenchmark
Auto-detect: group 14 (text messages containing instagram.com URLs).
"""

from __future__ import annotations

import re

from aiogram import F
from aiogram.filters.logic import and_f

from bot.command_handler import COMMAND, cmd
from bot.logger import logger
from bot.pipeline import on

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


def setup() -> list[str]:
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
    on("message", igdl_command, flt=cmd("igdl"))
    on("message", igsettings_command, flt=cmd("igsettings"))
    on("message", igstats_command, flt=cmd("igstats"))
    on("message", igcache_command, flt=cmd("igcache"))
    on("message", igbenchmark_command, flt=cmd("igbenchmark"))

    # Settings callbacks.
    on("callback_query", ig_callback, flt=F.data.regexp(re.compile(r"^ig:")))

    # Auto-detect — dedicated high group so we never steal updates from
    # filters(1)/blocklist(2)/watchwords(3)/bind(4)/leveling(5)/…/analytics(13).
    # Narrow filter: only non-command text that contains an Instagram host.
    ig_filter = and_f(
        F.text,
        ~COMMAND,
        F.text.regexp(re.compile(r"(?i)(instagram\.com|instagr\.am)/")),
    )
    on("message", auto_download_handler, group=HANDLER_GROUP, flt=ig_filter)

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
