"""Help module — /help command listing all available commands."""

import re

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode

from bot.constants import HELP_TEXT
from bot.emojis import E
from bot.logger import logger

_TG_EMOJI_RE = re.compile(r'<tg-emoji emoji-id="\d+">.*?</tg-emoji>')


def _strip_custom_emoji(text: str) -> str:
    """Replace <tg-emoji> tags with their inner fallback emoji."""
    return _TG_EMOJI_RE.sub(lambda m: m.group(0).split(">", 1)[1].rsplit("</", 1)[0], text)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help — brand custom emoji first; plain only on BadRequest."""
    text = HELP_TEXT.format(
        fire=E.FIRE,
        info=E.INFO,
        wave=E.WAVE,
        mute=E.MUTE,
        settings=E.SETTINGS,
        cross=E.CROSS,
        sparkle=E.SPARKLE,
        crown=E.CROWN,
        alert=E.ALERT,
        eyes=E.EYES,
        web=E.WEB,
        announce=E.ANNOUNCE,
    )
    try:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)
    except Exception as e:
        # Only fall back when Telegram rejected the payload — not on timeout
        # (retrying a timeout doubles the user-visible wait).
        if type(e).__name__ in {"TimedOut", "NetworkError"}:
            logger.warning(f"Help reply network issue: {e}")
            return
        try:
            await update.message.reply_text(
                _strip_custom_emoji(text), parse_mode=ParseMode.HTML
            )
        except Exception as e2:
            logger.warning(f"Failed to send help: {e2}")


def setup(app: Application) -> list[str]:
    """Register this module's handlers. Returns route descriptions for the log."""
    app.add_handler(CommandHandler("help", help_command))
    return ["/help"]
