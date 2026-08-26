"""Help module — /help command listing all available commands."""

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from bot.constants import HELP_TEXT


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help — show the full command list."""
    await update.message.reply_text(HELP_TEXT)


def setup(app: Application) -> list[str]:
    """Register this module's handlers. Returns route descriptions for the log."""
    app.add_handler(CommandHandler("help", help_command))
    return ["/help"]