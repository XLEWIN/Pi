"""PTB-faithful ``Message.reply_*`` helpers for aiogram.

python-telegram-bot's ``msg.reply_text(...)`` sent a reply-to only when
the chat was NOT private (``quote`` default logic in telegram/_message.py).
aiogram's ``msg.reply()`` always sets reply_parameters; ``msg.answer()``
never does.  These helpers reproduce the PTB behavior exactly:

* explicit ``reply_to_message_id=`` wins (sent without reply_parameters);
* explicit ``quote=True/False`` overrides the default;
* otherwise: reply-to iff ``chat.type != "private"``.

Before dispatching, a payload carrying our own custom-emoji markup is
sent as HTML even when the caller forgot ``parse_mode``.  ``E.*`` expands
to ``<tg-emoji emoji-id="…">``; without HTML parsing Telegram shows that
tag literally — the raw markup the user sees instead of an emoji.  See
:func:`_maybe_html`.

Kwargs are forwarded unchanged to the aiogram shortcut.  Usage::

    from bot.reply import reply_text
    await reply_text(message, "hello", parse_mode=ParseMode.HTML)
"""

from __future__ import annotations

from typing import Any

#: Marker for markup only ``bot.emojis`` can generate.  Deliberately NOT
#: a general HTML sniff: user text may legitimately contain a stray ``<``,
#: and parsing that as HTML would make Telegram reject the whole message.
_HTML_MARKER = "<tg-emoji"


def _maybe_html(args: Any, kwargs: dict) -> None:
    """Promote the payload to HTML when it carries our own emoji markup.

    ``reply_text(message, f"{E.INFO} You're already the group creator.")``
    used to reach Telegram unparsed and render as::

        <tg-emoji emoji-id="5904248647972820334">💭</tg-emoji> You're ...

    Rather than rely on every one of the dozens of call sites remembering
    ``parse_mode=ParseMode.HTML``, the send layer notices the marker and
    parses.  Callers that already set ``parse_mode`` are left alone, and
    plain payloads are untouched — so nothing that used to send as plain
    text changes behaviour.
    """
    if kwargs.get("parse_mode") is not None:
        return
    for value in args:
        if isinstance(value, str) and _HTML_MARKER in value:
            kwargs["parse_mode"] = "HTML"
            return
    for key in ("text", "caption"):
        value = kwargs.get(key)
        if isinstance(value, str) and _HTML_MARKER in value:
            kwargs["parse_mode"] = "HTML"
            return


def _wants_quote(message: Any, quote) -> bool:  # noqa: ANN001
    if quote is not None:
        return bool(quote)
    return getattr(getattr(message, "chat", None), "type", None) != "private"


async def _send(message: Any, reply, answer, *args: Any,  # noqa: ANN001
                quote=None, reply_to_message_id=None, **kwargs: Any):
    _maybe_html(args, kwargs)
    if reply_to_message_id is not None:
        return await answer(message, *args,
                            reply_to_message_id=reply_to_message_id, **kwargs)
    if _wants_quote(message, quote):
        return await reply(message, *args, **kwargs)
    return await answer(message, *args, **kwargs)


async def reply_text(message: Any, text: str, *args: Any, **kwargs: Any):
    # ``text`` must travel through _send's args: it is what _maybe_html
    # inspects for the marker.  Capturing it in the lambda instead left
    # the promotion dead code for every reply_text call site.
    return await _send(message,
                       lambda m, *a, **k: m.reply(*a, **k),
                       lambda m, *a, **k: m.answer(*a, **k),
                       text, *args, **kwargs)


async def reply_photo(message: Any, photo: Any, *args: Any, **kwargs: Any):
    return await _send(message,
                       lambda m, *a, **k: m.reply_photo(*a, **k),
                       lambda m, *a, **k: m.answer_photo(*a, **k),
                       photo, *args, **kwargs)


async def reply_document(message: Any, document: Any, *args: Any, **kwargs: Any):
    return await _send(message,
                       lambda m, *a, **k: m.reply_document(*a, **k),
                       lambda m, *a, **k: m.answer_document(*a, **k),
                       document, *args, **kwargs)


async def reply_animation(message: Any, animation: Any, *args: Any, **kwargs: Any):
    return await _send(message,
                       lambda m, *a, **k: m.reply_animation(*a, **k),
                       lambda m, *a, **k: m.answer_animation(*a, **k),
                       animation, *args, **kwargs)


async def reply_video(message: Any, video: Any, *args: Any, **kwargs: Any):
    return await _send(message,
                       lambda m, *a, **k: m.reply_video(*a, **k),
                       lambda m, *a, **k: m.answer_video(*a, **k),
                       video, *args, **kwargs)


async def reply_sticker(message: Any, sticker: Any, *args: Any, **kwargs: Any):
    return await _send(message,
                       lambda m, *a, **k: m.reply_sticker(*a, **k),
                       lambda m, *a, **k: m.answer_sticker(*a, **k),
                       sticker, *args, **kwargs)


async def reply_voice(message: Any, voice: Any, *args: Any, **kwargs: Any):
    return await _send(message,
                       lambda m, *a, **k: m.reply_voice(*a, **k),
                       lambda m, *a, **k: m.answer_voice(*a, **k),
                       voice, *args, **kwargs)


async def reply_audio(message: Any, audio: Any, *args: Any, **kwargs: Any):
    return await _send(message,
                       lambda m, *a, **k: m.reply_audio(*a, **k),
                       lambda m, *a, **k: m.answer_audio(*a, **k),
                       audio, *args, **kwargs)


__all__ = [
    "reply_text", "reply_photo", "reply_document", "reply_animation",
    "reply_video", "reply_sticker", "reply_voice", "reply_audio",
]
