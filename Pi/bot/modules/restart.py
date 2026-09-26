"""Owner-only /restart — re-exec the bot process in place.

Works wherever the bot is deployed:

* Railway / containers — ``os.execvp`` re-executes the same command
  via a real exec (Linux): PID 1 keeps its PID and environment, so the
  platform sees one continuous process and the fresh bot is back
  immediately — no container restart, no backoff.
* Terminal (VS Code, PowerShell, bash) — on Windows exec overlays the
  current process (``_P_OVERLAY``), so the new run continues in the
  same console with its logs visible.

A failed re-exec must NEVER take the bot down: if execvp raises, the
current process stays alive and the owner gets an error card saying so.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from html import escape
from typing import List, Optional

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, ContextTypes

from bot.command_handler import CommandHandler
from bot.config import settings
from bot.emojis import E
from bot.responses import action_card, field_extra, plain_error

logger = logging.getLogger(__name__)

_NOTICE_WAIT = 0.5  # let the "restarting" notice reach Telegram before exec


def _restart_argv() -> List[str]:
    """argv of the current run — prefer sys.orig_argv (3.10+), which also
    covers ``python -m package`` launches that sys.argv alone can't."""
    orig = list(getattr(sys, "orig_argv", None) or [])
    if orig:
        return orig
    return [sys.executable, *sys.argv]


def _exec_self() -> Optional[str]:
    """Replace this process with a fresh one.

    On success this never returns (a new interpreter takes over).
    On failure the current process keeps running and the returned
    string explains what went wrong.
    """
    argv = _restart_argv()
    logger.warning("restart: re-exec %r", argv)
    # Buffered stdout/stderr would be lost across exec — flush first.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:  # noqa: BLE001 — flushing is best-effort
            pass
    try:
        os.execvp(argv[0], argv)
    except Exception as e:  # noqa: BLE001 — must not kill the bot
        logger.exception("restart: re-exec failed")
        return f"{type(e).__name__}: {e}"
    return "exec returned without replacing the process"


async def restart_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None:
        return
    user = update.effective_user
    if user is None or not settings.owner_id or user.id != settings.owner_id:
        await msg.reply_text(
            f"{E.CROWN} Only the bot owner can restart me.",
            parse_mode=ParseMode.HTML,
        )
        return

    card = action_card(
        "Restarting the bot",
        [field_extra(E.TIME, "Mode", "in-place re-exec")],
        icon=E.CROWN,
    )
    prog = await msg.reply_text(card, parse_mode=ParseMode.HTML)
    await asyncio.sleep(_NOTICE_WAIT)

    err = _exec_self()
    # Only reached when re-exec failed — stay up and explain why.
    await prog.edit_text(
        plain_error(
            f"Restart failed ({escape(err)}). The bot is still running."
        ),
        parse_mode=ParseMode.HTML,
    )


def setup(app: Application) -> List[str]:
    app.add_handler(CommandHandler(["restart", "reboot"], restart_command))
    return ["/restart", "/reboot"]
