"""Shared action-card callbacks — Close dismisses the card buttons."""

import logging
import re

from aiogram import F
from aiogram.types import CallbackQuery

from bot.pipeline import on

logger = logging.getLogger(__name__)


async def card_callback(callback_query: CallbackQuery) -> None:
    """Handle card:close — delete the card (or strip its buttons)."""
    query = callback_query
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
                await query.message.edit_reply_markup(reply_markup=None)
            except Exception as e:
                logger.debug("card close failed: %s", e)
        return

    try:
        await query.answer()
    except Exception:
        pass


def setup() -> list:
    on("callback_query", card_callback, flt=F.data.regexp(re.compile(r"^card:")))
    return ["card:close"]
