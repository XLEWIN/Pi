"""Media downloader — auto-detect YouTube/TikTok/Instagram links + commands.

Auto-discovered by bot.loader as package `bot.modules.media`.
setup() lives here so the top-level package exposes setup().

Commands:
  /dl <url> / /mediasettings / /mediastats / /mediacache / /mediabench
  Legacy aliases: /igdl /igsettings /igstats /igcache /igbenchmark
Auto-detect: group 14 (text messages containing supported media URLs).
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
    dl_command,
    ig_callback,
    mediabench_command,
    mediacache_command,
    mediasettings_command,
    mediastats_command,
)
from .platforms import pick_media_url


def _auto_detect(message) -> bool:
    """True when non-command text/caption carries a supported media URL.

    Uses the SAME extractor the handler runs (pick_media_url) — the old
    magic-filter regexp defaulted to match-at-start mode, so it never
    fired on ``https://…`` links and auto-download was dead unless the
    message literally began with the bare host.
    """
    if message is None:
        return False
    return bool(pick_media_url(message.text or message.caption or ""))


#: Module-level so tests can import the exact production filter.
ig_filter = and_f(~COMMAND, F.func(_auto_detect))
#: New-style alias — same filter object.
media_filter = ig_filter


def setup() -> list[str]:
    """Register media commands, callbacks, and auto-detect handler."""
    if not ig_config.enabled:
        logger.info("[MEDIA] module disabled via MEDIA_ENABLED=0")
        return ["media: disabled"]

    # Startup: sweep leftover temp downloads (hard-guarded inside sweep_stale).
    try:
        sweep_stale(ig_config.tmp_cleanup_age)
    except Exception as e:
        logger.warning(f"[MEDIA] temp sweep failed: {e}")

    # Commands — default group 0 (same as other CommandHandlers).
    # cmd() accepts multiple names, so old aliases share one registration.
    on("message", dl_command, flt=cmd("dl", "igdl", "ytdl", "ttdl"))
    on("message", mediasettings_command, flt=cmd("mediasettings", "igsettings"))
    on("message", mediastats_command, flt=cmd("mediastats", "igstats"))
    on("message", mediacache_command, flt=cmd("mediacache", "igcache"))
    on("message", mediabench_command, flt=cmd("mediabench", "igbenchmark"))

    # Settings callbacks.
    on("callback_query", ig_callback, flt=F.data.regexp(re.compile(r"^ig:")))

    # Auto-detect — dedicated high group so we never steal updates from
    # filters(1)/blocklist(2)/watchwords(3)/bind(4)/leveling(5)/…/analytics(13).
    # Narrow filter: only non-command text (or caption) containing a
    # YouTube / TikTok / Instagram URL.
    on("message", auto_download_handler, group=HANDLER_GROUP, flt=ig_filter)

    logger.info(f"[MEDIA] registered (auto group={HANDLER_GROUP})")
    return [
        "/dl",
        "/mediasettings",
        "/mediastats",
        "/mediacache",
        "/mediabench",
        f"auto-detect@{HANDLER_GROUP}",
        "ig:* callbacks",
    ]
