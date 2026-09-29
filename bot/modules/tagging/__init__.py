"""Mass Tagging module — /all, /tagabort, /allsettings, /tagstats,
plus Boa-style /tagall, /etagall, @all, @eall (Yumeko port).

Auto-discovered by bot.loader as package `bot.modules.tagging`.
setup() must live here so the top-level module exposes setup().

Handler groups (see config.py for the full rationale):
    0   commands + tag:* callbacks + @all/@eall trigger
    15  message activity observer (write-behind, no DB in hot path)
    16  joins/leaves (service messages) + chat_member updates
    17  catch-all callback activity observer

NEVER put join/leave handlers in group 0: this package loads
alphabetically before `users`/`welcome` and would shadow them.
"""

import re

from aiogram import F
from aiogram.filters.logic import and_f

from bot.command_handler import COMMAND, cmd
from bot.logger import logger
from bot.pipeline import GROUPS, SERVICE, on

from . import database as tdb
from .config import (
    ACTIVITY_GROUP,
    CALLBACK_ACTIVITY_GROUP,
    MEMBER_GROUP,
)
from .handler import (
    activity_observer,
    all_command,
    allsettings_command,
    callback_activity_observer,
    chat_member_observer,
    member_observer,
    tag_callback,
    tagabort_command,
    tagstats_command,
)
from .tagall import at_trigger, etagall_command, tagall_command


def setup() -> list[str]:
    """Register tagging commands, callbacks and activity observers."""
    try:
        tdb.ensure_tables()
        tdb.defer_interrupted()  # crash recovery: never auto-resume
    except Exception as e:
        logger.warning(f"Tagging table init failed: {e}")

    # Commands + callbacks — default group 0 (no name collisions).
    on("message", all_command, flt=cmd("all"))
    on("message", tagabort_command, flt=cmd("tagabort"))
    on("message", allsettings_command, flt=cmd("allsettings"))
    on("message", tagstats_command, flt=cmd("tagstats"))
    # Boa-style tagall (Yumeko port): /cancel is boabot's stop command
    # and shares Pi's session — same handler as /tagabort.
    on("message", tagall_command, flt=cmd("tagall"))
    on("message", etagall_command, flt=cmd("etagall"))
    on("message", tagabort_command, flt=cmd("cancel"))
    on("callback_query", tag_callback, flt=F.data.regexp(re.compile(r"^tag:")))
    # @all / @eall without a slash (boabot's second pattern). Regex is
    # narrow, so plain messages fall through to the other group-0
    # handlers exactly as before.
    on(
        "message",
        at_trigger,
        flt=F.text.regexp(re.compile(r"^@(all|eall)(?:\s|$)")) & GROUPS,
    )

    # Message activity — own group so filters(1)/blocklist(2)/watch(3)
    # and analytics(7/13) keep their own matches (one handler per group).
    on(
        "message",
        activity_observer,
        group=ACTIVITY_GROUP,
        flt=and_f(GROUPS, ~COMMAND, ~SERVICE),
    )

    # Membership tracking — separate group (welcome uses 10, bind 11).
    on(
        "message",
        member_observer,
        group=MEMBER_GROUP,
        flt=F.new_chat_members | F.left_chat_member,
    )
    on("chat_member", chat_member_observer, group=MEMBER_GROUP)

    # Callback activity — catch-all in its own group (group 0 owns the
    # real tag: handling; this only records who pressed what).
    on("callback_query", callback_activity_observer, group=CALLBACK_ACTIVITY_GROUP)

    # Optional MTROTO presence starts lazily: setup() runs before
    # run_polling creates the event loop, so handlers call
    # presence_manager.kick() on the first update instead.

    logger.info("Tagging module registered")
    return [
        "/all", "/tagabort", "/allsettings", "/tagstats",
        "/tagall", "/etagall", "/cancel", "@all", "@eall",
        "tag:* callbacks",
    ]
