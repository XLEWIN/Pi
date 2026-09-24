"""Shared action-card callbacks — Close dismisses the card buttons."""

import logging

from telegram import Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

logger = logging.getLogger(__name__)


async def card_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle card:close — delete the card (or strip its buttons)."""
    query = update.callback_query
    data = query.data or ""
    if not data.startswith("card:"):
        return

    action = data.split(":", 1)[1]
    if action == "close":
        try:
            await query.answer()
        except Exception:
            pass
        try:
            await query.message.delete()
        except Exception:
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception as e:
                logger.debug("card close failed: %s", e)
        return

    try:
        await query.answer()
    except Exception:
        pass


def setup(app: Application) -> list:
    app.add_handler(CallbackQueryHandler(card_callback, pattern=r"^card:"))
    return ["card:close"]
