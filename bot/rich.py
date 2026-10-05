"""Telegram **Rich Messages** (Bot API 10.1+, June 2026) for this bot.

Why this module exists
----------------------
aiogram 3.21 — the version this bot runs on — predates Rich Messages: it
ships neither ``sendRichMessage`` nor any ``Rich*`` / ``InputRich*``
type.  Rather than upgrade the whole bot (aiogram releases lag the Bot
API by months), the two methods we need are declared here as raw
:class:`TelegramMethod` subclasses.

That is safe because of how aiogram serialises a request:

* ``AiohttpSession.build_form_data`` walks ``method.model_dump()`` and
  runs each value through ``BaseSession.prepare_value``;
* ``prepare_value`` recurses into plain ``dict``/``list`` and JSON-encodes
  them at the top level;
* the reply is deserialised with ``Response[method.__returning__]``, and
  ``TelegramObject`` is ``extra="allow"``, so fields aiogram has never
  heard of (``Message.rich_message``) round-trip fine.

So an untyped ``rich_message`` payload rides through untouched.

Failure policy
--------------
Every helper here **raises**.  Callers are expected to catch and fall
back to the HTML path, which is why rich support is a strict upgrade and
never a single point of failure: an old API server, an unknown block
type or a too-new client feature degrades to the menu we shipped before.

Reference: https://core.telegram.org/bots/api#rich-messages
"""

from __future__ import annotations

import html
import re
from typing import Any, Dict, List, Optional, Sequence, Union

from aiogram.methods.base import TelegramMethod
from aiogram.types import InlineKeyboardMarkup, Message

__all__ = [
    "SendRichMessage",
    "EditRichMessage",
    "send_rich",
    "edit_rich",
    # RichText builders
    "bold", "italic", "underline", "strike", "code", "link", "custom_emoji",
    "html_to_rich", "strip_html",
    # RichBlock builders
    "heading", "paragraph", "divider", "footer", "table", "cell",
    "list_block", "details", "blockquote", "buttons_block", "button",
    "icon_rich",
    "unstyle", "has_styled_buttons",
    "validate",
]

#: Hard limits from the Bot API (sending more is rejected outright).
MAX_BLOCKS = 500
MAX_NESTING = 16
MAX_TABLE_COLUMNS = 20
MAX_BUTTONS_PER_BLOCK = 8


# ── Raw methods (absent from aiogram 3.21) ─────────────────────────

class SendRichMessage(TelegramMethod[Message]):
    """``sendRichMessage`` — send a structured (block-based) message.

    Returns the sent :class:`~aiogram.types.message.Message`, exactly
    like ``sendMessage``.
    """

    __returning__ = Message
    __api_method__ = "sendRichMessage"

    chat_id: Union[int, str]
    rich_message: Dict[str, Any]
    reply_markup: Optional[InlineKeyboardMarkup] = None
    disable_notification: Optional[bool] = None
    protect_content: Optional[bool] = None


class EditRichMessage(TelegramMethod[Union[Message, bool]]):
    """``editMessageText`` with ``rich_message`` instead of ``text``.

    ``text`` is optional in the Bot API as soon as ``rich_message`` is
    given, which is why this is a separate class: aiogram's own
    ``EditMessageText`` declares ``text`` as required.
    """

    __returning__ = Union[Message, bool]
    __api_method__ = "editMessageText"

    chat_id: Union[int, str]
    message_id: int
    rich_message: Dict[str, Any]
    reply_markup: Optional[InlineKeyboardMarkup] = None


async def send_rich(
    bot,
    chat_id: Union[int, str],
    blocks: Sequence[Dict[str, Any]],
    *,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    **kwargs: Any,
) -> Message:
    """Send *blocks* as a Rich Message.  Raises on any Telegram error."""
    payload = {
        "blocks": list(blocks),
        # Deterministic rendering: the HTML path never auto-links, so the
        # rich path must not either or the two would disagree.
        "skip_entity_detection": True,
    }
    return await bot(
        SendRichMessage(
            chat_id=chat_id,
            rich_message=payload,
            reply_markup=reply_markup,
            **kwargs,
        )
    )


async def edit_rich(
    bot,
    chat_id: Union[int, str],
    message_id: int,
    blocks: Sequence[Dict[str, Any]],
    *,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> Union[Message, bool]:
    """Rewrite an existing message as a Rich Message.  Raises on error."""
    payload = {"blocks": list(blocks), "skip_entity_detection": True}
    return await bot(
        EditRichMessage(
            chat_id=chat_id,
            message_id=message_id,
            rich_message=payload,
            reply_markup=reply_markup,
        )
    )


# ── RichText builders ─────────────────────────────────────────────
#
# RichText is a union: a bare String, an Array of RichText, or a typed
# object carrying a ``type`` discriminator.  Everything below returns
# plain JSON-serialisable data on purpose.

def bold(text) -> Dict[str, Any]:
    return {"type": "bold", "text": text}


def italic(text) -> Dict[str, Any]:
    return {"type": "italic", "text": text}


def underline(text) -> Dict[str, Any]:
    return {"type": "underline", "text": text}


def strike(text) -> Dict[str, Any]:
    return {"type": "strikethrough", "text": text}


def code(text) -> Dict[str, Any]:
    """Inline monospace — the command column of every grid."""
    return {"type": "code", "text": text}


def link(text: str, url: str) -> Dict[str, Any]:
    return {"type": "url", "text": text, "url": url}


def custom_emoji(emoji_id: str, alternative: str) -> Dict[str, Any]:
    """A premium emoji.  No ``text`` field — the id is the payload."""
    return {
        "type": "custom_emoji",
        "custom_emoji_id": str(emoji_id),
        "alternative_text": alternative or "",
    }


def icon_rich(icon_html: str, fallback: str = "") -> Optional[Dict[str, Any]]:
    """``<tg-emoji emoji-id="123">🔥</tg-emoji>`` → a RichText emoji.

    Returns ``None`` when *icon_html* carries no custom emoji, so callers
    can drop the node instead of emitting an empty one.
    """
    if not icon_html or "tg-emoji" not in icon_html:
        return None
    try:
        emoji_id = icon_html.split('emoji-id="', 1)[1].split('"', 1)[0]
        alt = icon_html.split(">", 1)[1].rsplit("</", 1)[0]
    except (IndexError, ValueError):
        return None
    return custom_emoji(emoji_id, fallback or alt)


# ── HTML → RichText (HELP_MENU is authored as HTML) ───────────────

_HTML_TAGS = ("b", "strong", "i", "em", "u", "s", "strike", "code", "pre",
              "tg-emoji", "a")


#: HTML tag in HELP_MENU → RichText ``type``.
_WRAP_TAGS = {
    "b": "bold", "strong": "bold",
    "i": "italic", "em": "italic",
    "u": "underline",
    "s": "strikethrough", "strike": "strikethrough", "del": "strikethrough",
    "code": "code", "pre": "code",
}
_TAG_RE = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9-]*)((?:[^>\"']|\"[^\"]*\"|'[^']*')*)>")


def strip_html(text: str) -> str:
    """HTML → plain text (tags removed, entities unescaped)."""
    return html.unescape(re.sub(r"<[^>]+>", "", text or ""))


def html_to_rich(text: str) -> Union[str, List[Any]]:
    """Convert the small HTML subset used by ``HELP_MENU`` to RichText.

    Supported: ``<b> <strong> <i> <em> <u> <s> <strike> <code> <pre>``,
    ``<a href="...">`` and ``<tg-emoji emoji-id="...">``.  Every other
    tag is unwrapped (its inner text survives), and HTML entities are
    unescaped — Rich Messages are not HTML, so ``&amp;`` would otherwise
    print literally.

    Returns a plain ``str`` when nothing needed converting, which is
    exactly the shape Telegram accepts for a bare RichText.
    """
    if not text:
        return ""

    root: List[Any] = []
    stack: List[List[Any]] = [root]
    pos = 0

    for m in _TAG_RE.finditer(text):
        run = text[pos:m.start()]
        if run:
            stack[-1].append(html.unescape(run))
        pos = m.end()

        tag = m.group(2).lower()
        attrs = m.group(3)
        if m.group(1):                                   # closing tag
            if len(stack) > 1:
                stack.pop()
            continue

        if tag == "tg-emoji":
            eid = re.search(r'emoji-id="(\d+)"', attrs)
            end = text.find("</tg-emoji>", pos)
            inner = text[pos:end] if end != -1 else ""
            if end != -1:
                pos = end + len("</tg-emoji>")
            if eid:
                stack[-1].append(custom_emoji(eid.group(1), inner))
            elif inner:
                stack[-1].append(inner)
            continue

        if tag == "a":
            href = re.search(r'href="([^"]+)"', attrs)
            node = {"__url__": href.group(1) if href else ""}
            stack[-1].append(node)
            stack.append(node.setdefault("__kids__", []))
            continue

        kind = _WRAP_TAGS.get(tag)
        if kind is None:                                 # unknown → unwrap
            stack.append(stack[-1])
            continue
        node = {"__kind__": kind}
        stack[-1].append(node)
        stack.append(node.setdefault("__kids__", []))

    tail = text[pos:]
    if tail:
        stack[-1].append(html.unescape(tail))

    rendered = _render_rich(root)
    return rendered


def _render_rich(nodes: List[Any]) -> Union[str, List[Any]]:
    out: List[Any] = []
    for node in nodes:
        if isinstance(node, str):
            out.append(node)
        elif "__url__" in node:                          # <a href>
            inner = _render_rich(node["__kids__"]) or ""
            out.append({"type": "url", "text": inner, "url": node["__url__"]})
        elif "__kind__" in node:                         # <b> <i> <code> ...
            inner = _render_rich(node["__kids__"]) or ""
            out.append({"type": node["__kind__"], "text": inner})
        else:
            out.append(node)                             # already finished
    # Collapse single-child spans: a <b> holding only <code>x</code> should
    # serialise as {"type":"bold","text":{"type":"code",...}} rather than an
    # extra nesting level.  Both are valid RichText; this one is easier to
    # assert against and matches how Telegram documents the type.
    if len(out) == 1:
        return out[0]
    return out


# ── RichBlock builders ─────────────────────────────────────────────

def heading(text, size: int = 2) -> Dict[str, Any]:
    """Section heading.  ``size`` 1 (largest) … 6."""
    return {"type": "heading", "text": text, "size": max(1, min(int(size), 6))}


def paragraph(text) -> Dict[str, Any]:
    return {"type": "paragraph", "text": text}


def divider() -> Dict[str, Any]:
    return {"type": "divider"}


def footer(text) -> Dict[str, Any]:
    return {"type": "footer", "text": text}


def blockquote(blocks: Sequence[Dict[str, Any]], credit=None) -> Dict[str, Any]:
    return {"type": "blockquote", "blocks": list(blocks),
            **({"credit": credit} if credit else {})}


def details(summary, blocks: Sequence[Dict[str, Any]], *, is_open: bool = False) -> Dict[str, Any]:
    node: Dict[str, Any] = {"type": "details", "summary": summary,
                            "blocks": list(blocks)}
    if is_open:
        node["is_open"] = True
    return node


def list_block(items: Sequence[Sequence[Dict[str, Any]]]) -> Dict[str, Any]:
    """``items`` — one entry per bullet, each a list of blocks."""
    return {"type": "list", "items": [{"blocks": list(b)} for b in items]}


def cell(text, *, header: bool = False, align: str = "left",
         colspan: Optional[int] = None) -> Dict[str, Any]:
    """One table cell.

    ``align``/``valign`` are always written: the Bot API documents them
    on ``RichBlockTableCell`` as required, and leaving them out is the
    kind of thing that only fails on the live server.
    """
    node: Dict[str, Any] = {"text": text, "align": align, "valign": "middle"}
    if header:
        node["is_header"] = True
    if colspan:
        node["colspan"] = int(colspan)
    return node


def table(rows: Sequence[Sequence[Dict[str, Any]]], *,
          bordered: bool = True, striped: bool = True,
          compact: bool = True, caption=None) -> Dict[str, Any]:
    """The help grid.  ``is_compact`` (10.3) keeps rows tight."""
    if not rows:
        raise ValueError("table needs at least one row")
    width = max(len(r) for r in rows)
    if width > MAX_TABLE_COLUMNS:
        raise ValueError(f"table has {width} columns (max {MAX_TABLE_COLUMNS})")
    node: Dict[str, Any] = {
        "type": "table",
        "cells": [list(r) for r in rows],
        # True-only flags: aiogram's prepare_value drops falsy values, so
        # a False here would silently disappear rather than be honoured.
        "is_bordered": True,
        "is_striped": True,
        "is_compact": True,
    }
    if caption is not None:
        node["caption"] = caption
    return node


def button(text, *, callback_data: Optional[str] = None,
           url: Optional[str] = None, style: Optional[str] = None) -> Dict[str, Any]:
    """A ``RichMessageButton`` (10.3): exactly one action field."""
    if not callback_data and not url:
        raise ValueError("rich button needs callback_data or url")
    node: Dict[str, Any] = {"text": text}
    if style in {"primary", "success", "danger", "link"}:
        node["style"] = style
    if url:
        node["url"] = url
    else:
        node["callback_data"] = callback_data
    if len(callback_data or "") > 64:
        raise ValueError(f"callback_data too long: {callback_data!r}")
    return node


def buttons_block(buttons: Sequence[Dict[str, Any]], *, align: str = "center") -> Dict[str, Any]:
    """A row of in-message buttons (10.3, max 8)."""
    buttons = list(buttons)
    if not 1 <= len(buttons) <= MAX_BUTTONS_PER_BLOCK:
        raise ValueError(f"buttons block needs 1-{MAX_BUTTONS_PER_BLOCK} buttons")
    return {"type": "buttons", "buttons": buttons, "align": align}


def validate(blocks: Sequence[Dict[str, Any]]) -> None:
    """Cheap pre-flight so a bad page fails here, not on Telegram."""
    if len(blocks) > MAX_BLOCKS:
        raise ValueError(f"{len(blocks)} blocks (max {MAX_BLOCKS})")


def unstyle(blocks: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Strip every RichMessageButton ``style``.

    Button styling is a 10.3 addition.  A server that accepts the rich
    body but rejects the style enum would otherwise force the caller
    straight back to HTML, so this is the one retry worth making: same
    message, plain buttons.
    """
    out: List[Dict[str, Any]] = []
    for blk in blocks:
        if blk.get("type") == "buttons":
            blk = dict(blk)
            blk["buttons"] = [
                {k: v for k, v in btn.items() if k != "style"}
                for btn in blk["buttons"]
            ]
        out.append(blk)
    return out


def has_styled_buttons(blocks: Sequence[Dict[str, Any]]) -> bool:
    """True when a retry without ``style`` would actually change anything."""
    return any(
        blk.get("type") == "buttons"
        and any("style" in btn for btn in blk["buttons"])
        for blk in blocks
    )
