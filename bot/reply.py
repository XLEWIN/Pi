"""PTB-faithful ``Message.reply_*`` helpers for aiogram.

python-telegram-bot's ``msg.reply_text(...)`` sent a reply-to only when
the chat was NOT private (``quote`` default logic in telegram/_message.py).
aiogram's ``msg.reply()`` always sets reply_parameters; ``msg.answer()``
never does.  These helpers reproduce the PTB behavior exactly:

* explicit ``reply_to_message_id=`` wins (sent without reply_parameters);
* explicit ``quote=True/False`` overrides the default;
* otherwise: reply-to iff ``chat.type != "private"``.

Kwargs are forwarded unchanged to the aiogram shortcut.  Usage::

    from bot.reply import reply_text
    await reply_text(message, "hello", parse_mode=ParseMode.HTML)
"""

from __future__ import annotations

from typing import Any


def _wants_quote(message: Any, quote) -> bool:  # noqa: ANN001
    if quote is not None:
        return bool(quote)
    return getattr(getattr(message, "chat", None), "type", None) != "private"


async def _send(message: Any, reply, answer, *args: Any,  # noqa: ANN001
                quote=None, reply_to_message_id=None, **kwargs: Any):
    if reply_to_message_id is not None:
        return await answer(message, *args,
                            reply_to_message_id=reply_to_message_id, **kwargs)
    if _wants_quote(message, quote):
        return await reply(message, *args, **kwargs)
    return await answer(message, *args, **kwargs)


async def reply_text(message: Any, text: str, *args: Any, **kwargs: Any):
    return await _send(message,
                       lambda m, *a, **k: m.reply(text, *a, **k),
                       lambda m, *a, **k: m.answer(text, *a, **k),
                       *args, **kwargs)


async def reply_photo(message: Any, photo: Any, *args: Any, **kwargs: Any):
    return await _send(message,
                       lambda m, *a, **k: m.reply_photo(photo, *a, **k),
                       lambda m, *a, **k: m.answer_photo(photo, *a, **k),
                       *args, **kwargs)


async def reply_document(message: Any, document: Any, *args: Any, **kwargs: Any):
    return await _send(message,
                       lambda m, *a, **k: m.reply_document(document, *a, **k),
                       lambda m, *a, **k: m.answer_document(document, *a, **k),
                       *args, **kwargs)


async def reply_animation(message: Any, animation: Any, *args: Any, **kwargs: Any):
    return await _send(message,
                       lambda m, *a, **k: m.reply_animation(animation, *a, **k),
                       lambda m, *a, **k: m.answer_animation(animation, *a, **k),
                       *args, **kwargs)


async def reply_video(message: Any, video: Any, *args: Any, **kwargs: Any):
    return await _send(message,
                       lambda m, *a, **k: m.reply_video(video, *a, **k),
                       lambda m, *a, **k: m.answer_video(video, *a, **k),
                       *args, **kwargs)


async def reply_sticker(message: Any, sticker: Any, *args: Any, **kwargs: Any):
    return await _send(message,
                       lambda m, *a, **k: m.reply_sticker(sticker, *a, **k),
                       lambda m, *a, **k: m.answer_sticker(sticker, *a, **k),
                       *args, **kwargs)


async def reply_voice(message: Any, voice: Any, *args: Any, **kwargs: Any):
    return await _send(message,
                       lambda m, *a, **k: m.reply_voice(voice, *a, **k),
                       lambda m, *a, **k: m.answer_voice(voice, *a, **k),
                       *args, **kwargs)


async def reply_audio(message: Any, audio: Any, *args: Any, **kwargs: Any):
    return await _send(message,
                       lambda m, *a, **k: m.reply_audio(audio, *a, **k),
                       lambda m, *a, **k: m.answer_audio(audio, *a, **k),
                       *args, **kwargs)


__all__ = [
    "reply_text", "reply_photo", "reply_document", "reply_animation",
    "reply_video", "reply_sticker", "reply_voice", "reply_audio",
]
