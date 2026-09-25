"""Mass Tagging module — /all, /tagabort, /allsettings, /tagstats.

Auto-discovered by bot.loader as package `bot.modules.tagging`.
setup(app) must live here so the top-level module exposes setup().

Handler groups (see config.py for the full rationale):
    0   commands + tag:* callbacks
    15  message activity observer (write-behind, no DB in hot path)
    16  joins/leaves (service messages) + chat_member updates
    17  catch-all callback activity observer

NEVER put join/leave handlers in group 0: this package loads
alphabetically before `users`/`welcome` and would shadow them.
"""

from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    MessageHandler,
    filters,
)

from bot.command_handler import COMMAND, CommandHandler
from bot.logger import logger

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


def setup(app: Application) -> list[str]:
    """Register tagging commands, callbacks and activity observers."""
    try:
        tdb.ensure_tables()
        tdb.mark_interrupted()  # crash recovery: never auto-resume
    except Exception as e:
        logger.warning(f"Tagging table init failed: {e}")

    # Commands + callbacks — default group 0 (no name collisions).
    app.add_handler(CommandHandler("all", all_command))
    app.add_handler(CommandHandler("tagabort", tagabort_command))
    app.add_handler(CommandHandler("allsettings", allsettings_command))
    app.add_handler(CommandHandler("tagstats", tagstats_command))
    app.add_handler(CallbackQueryHandler(tag_callback, pattern=r"^tag:"))

    # Message activity — own group so filters(1)/blocklist(2)/watch(3)
    # and analytics(7/13) keep their own matches (one handler per group).
    app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS
            & ~COMMAND
            & ~filters.StatusUpdate.ALL,
            activity_observer,
        ),
        group=ACTIVITY_GROUP,
    )

    # Membership tracking — separate group (welcome uses 10, bind 11).
    app.add_handler(
        MessageHandler(
            filters.StatusUpdate.NEW_CHAT_MEMBERS
            | filters.StatusUpdate.LEFT_CHAT_MEMBER,
            member_observer,
        ),
        group=MEMBER_GROUP,
    )
    app.add_handler(
        ChatMemberHandler(chat_member_observer, ChatMemberHandler.CHAT_MEMBER),
        group=MEMBER_GROUP,
    )

    # Callback activity — catch-all in its own group (group 0 owns the
    # real tag: handling; this only records who pressed what).
    app.add_handler(
        CallbackQueryHandler(callback_activity_observer),
        group=CALLBACK_ACTIVITY_GROUP,
    )

    # Optional MTROTO presence starts lazily: setup() runs before
    # run_polling creates the event loop, so handlers call
    # presence_manager.kick() on the first update instead.

    logger.info("Tagging module registered")
    return ["/all", "/tagabort", "/allsettings", "/tagstats", "tag:* callbacks"]
