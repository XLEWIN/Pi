"""Bind module — force-join & message-type gates bound to a channel.

Auto-discovered by bot.loader as package `bot.modules.bind`.
setup(app) must live here so the top-level module exposes setup().
"""

from telegram.ext import Application, CallbackQueryHandler, MessageHandler, filters

from bot.command_handler import COMMAND, CommandHandler
from bot.logger import logger

from . import database as bdb
from .callbacks import bind_callback
from .config import CB_PREFIX, HANDLER_GROUP, JOIN_TRACKER_GROUP
from .handlers import (
    bind_command,
    bindmenu_command,
    gate_message_handler,
    join_tracker,
    waiting_text_handler,
)


def setup(app: Application) -> list[str]:
    """Register Bind commands, callbacks, gate + join trackers."""
    try:
        bdb.ensure_tables()
    except Exception as e:
        logger.warning(f"Bind table init failed: {e}")

    group_filter = filters.ChatType.GROUPS

    # Commands (default group 0 — same as other CommandHandlers).
    app.add_handler(CommandHandler("bind", bind_command, filters=group_filter))
    app.add_handler(CommandHandler("bindmenu", bindmenu_command, filters=group_filter))

    # Callbacks.
    app.add_handler(CallbackQueryHandler(bind_callback, pattern=rf"^{CB_PREFIX}:"))

    # Force-join / message gates — own group so we never collide with
    # filters(1)/blocklist(2)/watchwords(3). Do NOT exclude commands: when
    # force_join is on, non-member commands should be gated too (start.py
    # already ran in group 0 if it matched; we still delete the message).
    # StatusUpdate is a factory class — negate its ALL member, not the type.
    app.add_handler(
        MessageHandler(group_filter & ~filters.StatusUpdate.ALL, gate_message_handler),
        group=HANDLER_GROUP,
    )

    # Waiting-for-input text (custom message / change channel) — after gates
    # would have already deleted non-member messages; admins only anyway.
    app.add_handler(
        MessageHandler(group_filter & filters.TEXT & ~COMMAND, waiting_text_handler),
        group=HANDLER_GROUP + 1,
    )

    # Join timestamps for grace period (welcome uses group=10).
    app.add_handler(
        MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS | filters.StatusUpdate.LEFT_CHAT_MEMBER, join_tracker),
        group=JOIN_TRACKER_GROUP,
    )

    logger.info("Bind module registered")
    return ["/bind", "/bindmenu", "bind:* callbacks", "gate handler"]
