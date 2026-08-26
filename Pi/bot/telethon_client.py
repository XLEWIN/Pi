"""Telethon MTProto client — for features not available in Bot API.

Runs alongside python-telegram-bot for hybrid architecture.
"""

import logging
from typing import List, Optional, Callable, Any

from telethon import TelegramClient, events, types
from telethon.tl.custom import Button

from bot.config import settings

logger = logging.getLogger(__name__)

# ── Global client ────────────────────────────────────────
_client: Optional[TelegramClient] = None


def get_client() -> Optional[TelegramClient]:
    """Get the Telethon client instance."""
    return _client


# ── Colored button helpers ───────────────────────────────
def btn_primary(text: str, data: str) -> types.KeyboardButtonCallback:
    """Create a blue/primary colored button."""
    return types.KeyboardButtonCallback(
        text=text,
        data=data.encode("utf-8") if isinstance(data, str) else data,
        style=types.KeyboardButtonStyle(bg_primary=True),
    )


def btn_success(text: str, data: str) -> types.KeyboardButtonCallback:
    """Create a green/success colored button."""
    return types.KeyboardButtonCallback(
        text=text,
        data=data.encode("utf-8") if isinstance(data, str) else data,
        style=types.KeyboardButtonStyle(bg_success=True),
    )


def btn_danger(text: str, data: str) -> types.KeyboardButtonCallback:
    """Create a red/danger colored button."""
    return types.KeyboardButtonCallback(
        text=text,
        data=data.encode("utf-8") if isinstance(data, str) else data,
        style=types.KeyboardButtonStyle(bg_danger=True),
    )


def btn_default(text: str, data: str) -> types.KeyboardButtonCallback:
    """Create a default/white colored button."""
    return types.KeyboardButtonCallback(
        text=text,
        data=data.encode("utf-8") if isinstance(data, str) else data,
        style=types.KeyboardButtonStyle(),
    )


def btn_url(text: str, url: str) -> types.KeyboardButtonUrl:
    """Create a URL button."""
    return types.KeyboardButtonUrl(text=text, url=url)


def build_keyboard(rows: List[List[types.KeyboardButtonCallback]]) -> types.ReplyInlineMarkup:
    """Build a keyboard from rows of buttons."""
    # Each row must be a KeyboardButtonRow containing buttons
    keyboard_rows = []
    for row in rows:
        keyboard_rows.append(types.KeyboardButtonRow(buttons=row))
    return types.ReplyInlineMarkup(rows=keyboard_rows)


# ── Message sending helpers ──────────────────────────────
async def send_colored_buttons(
    chat_id: int,
    text: str,
    buttons: List[List[types.KeyboardButtonCallback]],
    parse_mode: str = "html",
):
    """Send a message with colored buttons via Telethon."""
    global _client
    if not _client:
        logger.error("Telethon client not initialized")
        return None

    keyboard = build_keyboard(buttons)

    try:
        result = await _client.send_message(
            entity=chat_id,
            message=text,
            buttons=keyboard,
            parse_mode=parse_mode,
        )
        return result
    except Exception as e:
        logger.error(f"Failed to send colored buttons: {e}")
        return None


async def send_message(chat_id: int, text: str, parse_mode: str = "html"):
    """Send a plain message via Telethon."""
    global _client
    if not _client:
        return None

    try:
        return await _client.send_message(entity=chat_id, message=text, parse_mode=parse_mode)
    except Exception as e:
        logger.error(f"Failed to send message: {e}")
        return None


async def edit_message(
    chat_id: int,
    message_id: int,
    text: str,
    buttons: Optional[List[List[types.KeyboardButtonCallback]]] = None,
    parse_mode: str = "html",
):
    """Edit a message with optional colored buttons."""
    global _client
    if not _client:
        return None

    try:
        keyboard = build_keyboard(buttons) if buttons else None
        msg = await _client.get_messages(chat_id, ids=message_id)
        if msg:
            await msg.edit(message=text, buttons=keyboard, parse_mode=parse_mode)
        return msg
    except Exception as e:
        logger.error(f"Failed to edit message: {e}")
        return None


async def answer_callback(callback_query_id: str, text: str = None, alert: bool = False):
    """Answer a callback query."""
    global _client
    if not _client:
        return

    try:
        await _client.answer_callback_query(callback_query_id, message=text, alert=alert)
    except Exception as e:
        logger.error(f"Failed to answer callback: {e}")


# ── Callback handler registration ────────────────────────
def on_callback(data_pattern: str):
    """Decorator to register a callback handler for a specific data pattern."""
    def decorator(func: Callable):
        if _client:
            @_client.on(events.CallbackQuery(data=data_pattern.encode("utf-8")))
            async def handler(event):
                try:
                    await func(event)
                except Exception as e:
                    logger.error(f"Callback handler error: {e}")
        return func
    return decorator


# ── Initialize client ────────────────────────────────────
async def init_client():
    """Initialize the Telethon client."""
    global _client

    if not settings.telethon_api_id or not settings.telethon_api_hash:
        logger.warning("Telethon credentials not set — MTProto features disabled")
        return False

    _client = TelegramClient(
        settings.telethon_session,
        settings.telethon_api_id,
        settings.telethon_api_hash,
    )

    try:
        await _client.start(bot_token=settings.bot_token)
        me = await _client.get_me()
        logger.info(f"Telethon client started as @{me.username}")
        return True
    except Exception as e:
        logger.error(f"Failed to start Telethon client: {e}")
        _client = None
        return False


async def disconnect_client():
    """Disconnect the Telethon client."""
    global _client
    if _client:
        await _client.disconnect()
        _client = None
        logger.info("Telethon client disconnected")
