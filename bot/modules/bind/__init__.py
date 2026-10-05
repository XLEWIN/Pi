"""Bind module — force-join & message-type gates bound to a channel.

Auto-discovered by bot.loader as package `bot.modules.bind`.
setup() must live here so the top-level module exposes setup().
"""

import re

from aiogram import F
from aiogram.filters.logic import and_f

from bot.command_handler import COMMAND, cmd
from bot.logger import logger
from bot.pipeline import GROUPS, SERVICE, on

from . import database as bdb
from .callbacks import bind_callback
from .config import CB_PREFIX, HANDLER_GROUP, JOIN_TRACKER_GROUP, WAITING_TEXT_GROUP
from .handlers import (
    bind_command,
    bindmenu_command,
    gate_message_handler,
    join_tracker,
    waiting_text_handler,
)


def setup() -> list[str]:
    """Register Bind commands, callbacks, gate + join trackers."""
    try:
        bdb.ensure_tables()
    except Exception as e:
        logger.warning(f"Bind table init failed: {e}")

    # Commands (default group 0 — same as other CommandHandlers).
    on("message", bind_command, flt=and_f(cmd("bind"), GROUPS))
    on("message", bindmenu_command, flt=and_f(cmd("bindmenu"), GROUPS))

    # Callbacks.
    on("callback_query", bind_callback, flt=F.data.regexp(re.compile(rf"^{CB_PREFIX}:")))

    # Force-join / message gates.  Group -1: the gate must dispatch
    # BEFORE group 0 so a blocked message never reaches a command handler
    # (a non-member's /help would otherwise answer them), a counter or an
    # XP tracker.  On a block it raises pipeline.StopChain, which ends the
    # whole chain for that update.  Do NOT exclude commands: when
    # force_join is on, non-member commands are gated too.  StatusUpdate
    # is a factory class - negate its ALL member, not the type.
    on("message", gate_message_handler, group=HANDLER_GROUP, flt=GROUPS & ~SERVICE)

    # Waiting-for-input text (custom message / change channel) - own
    # group 20 (not 0: that would race every command; not a low number:
    # the waiting admin's text must only be consumed once the gate has
    # decided it is allowed through).  Admins only anyway.
    on("message", waiting_text_handler, group=WAITING_TEXT_GROUP, flt=and_f(GROUPS, F.text, ~COMMAND))

    # Join timestamps for grace period (welcome uses group=10).
    on(
        "message",
        join_tracker,
        group=JOIN_TRACKER_GROUP,
        flt=F.new_chat_members | F.left_chat_member,
    )

    logger.info("Bind module registered")
    return ["/bind", "/bindmenu", "bind:* callbacks", "gate handler"]
