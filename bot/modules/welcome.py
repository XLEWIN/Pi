"""Welcome module — Welcome/Goodbye messages for new and leaving members.

Adapted from boa2 for Pi bot. Enabled by default.
"""

import logging
import re
from html import escape
from typing import Any, Dict, List, Optional, Tuple

from aiogram import Bot, F
from aiogram.enums import ParseMode
from aiogram.filters.logic import and_f
from aiogram.types import Message

from bot.database import db
from bot.emojis import E, plain
from bot.pipeline import on, GROUPS, cmd
from bot.reply import reply_text
from bot.async_bridge import adb

logger = logging.getLogger(__name__)

# {name} tokens are substituted ONE AT A TIME.
#
# The old code ran ``str.format()`` over the whole template, which is
# all-or-nothing: a single unknown/stray brace (decorated frames like
# ``{──── SHADOW HUB ────}`` are common in welcome art) raised KeyError and
# the ENTIRE message was shipped verbatim — that is how ``{username}``
# ended up posted literally.  Regex substitution only fills the tokens we
# know and leaves everything else byte-for-byte alone.
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _utf16_len(text: str) -> int:
    """Telegram entity offsets/lengths are counted in UTF-16 code units."""
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in text)


def _values(user, chat, *, html: bool) -> Dict[str, str]:
    """Placeholder values, HTML-escaped (HTML send) or raw (entities send)."""
    esc = escape if html else (lambda s: s)
    first = esc(user.first_name or "User")
    last = esc(user.last_name or user.first_name or "User")
    fullname = esc(user.full_name or user.first_name or "User")
    username = f"@{esc(user.username)}" if user.username else first
    mention = (
        f"<a href='tg://user?id={user.id}'>{first}</a>"
        if html else (user.first_name or "User")
    )
    chatname = esc(chat.title or "") if chat.type != "private" else first
    return {
        "first": first,
        "last": last,
        "fullname": fullname,
        "username": username,
        "mention": mention,
        "chatname": chatname,
        "id": str(user.id),
    }


# ── Helpers ──────────────────────────────────────────────
async def _is_admin(message: Message, bot: Bot) -> bool:
    user_id = message.from_user.id
    chat_id = message.chat.id
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ["administrator", "creator"]
    except Exception:
        return False


def format_welcome(text: str, user, chat) -> str:
    """Fill placeholders with HTML-safe values (one token at a time)."""
    if not text:
        return text
    values = _values(user, chat, html=True)
    return _PLACEHOLDER_RE.sub(lambda m: values.get(m.group(1), m.group(0)), text)


# ── Entity (premium emoji) preserving render ─────────────
#
# A welcome written by an admin carries MessageEntityCustomEmoji entries
# (the premium stickers Telegram renders as emoji).  ``message.text``
# drops them, so re-sending through parse_mode=HTML degraded every one of
# them to its fallback character.  Keeping the ORIGINAL entities and
# re-sending with ``entities=`` reproduces the source message exactly —
# same look as a forwarded copy — while still swapping in the joiner's
# name.


def _format_entities(
    template: str,
    values: Dict[str, str],
    entities: List[Dict[str, Any]],
    *,
    mention_user=None,
) -> Tuple[str, List[Dict[str, Any]]]:
    """Substitute placeholders and remap entity offsets (UTF-16 safe)."""
    parts: List[str] = []
    # (orig_start, orig_end, new_start, new_end, is_replacement) — UTF-16 units
    segments: List[Tuple[int, int, int, int, bool]] = []
    injected: List[Dict[str, Any]] = []

    orig_u16 = new_u16 = cursor = 0

    for m in _PLACEHOLDER_RE.finditer(template):
        start, end = m.span()
        if start > cursor:
            lit = template[cursor:start]
            n = _utf16_len(lit)
            parts.append(lit)
            segments.append((orig_u16, orig_u16 + n, new_u16, new_u16 + n, False))
            orig_u16 += n
            new_u16 += n

        raw = template[start:end]  # ASCII → Python len == UTF-16 len
        repl = values.get(m.group(1))
        if repl is None:
            parts.append(raw)
            segments.append((orig_u16, orig_u16 + len(raw), new_u16, new_u16 + len(raw), False))
            orig_u16 += len(raw)
            new_u16 += len(raw)
        else:
            r = _utf16_len(repl)
            segments.append((orig_u16, orig_u16 + len(raw), new_u16, new_u16 + r, True))
            parts.append(repl)
            if m.group(1) == "mention" and mention_user is not None and r:
                injected.append({
                    "type": "text_mention",
                    "offset": new_u16,
                    "length": r,
                    "user": {
                        "id": mention_user.id,
                        "is_bot": bool(getattr(mention_user, "is_bot", False)),
                        "first_name": mention_user.first_name or "User",
                        **({"username": mention_user.username} if mention_user.username else {}),
                    },
                })
            orig_u16 += len(raw)
            new_u16 += r
        cursor = end

    if cursor < len(template):
        lit = template[cursor:]
        n = _utf16_len(lit)
        parts.append(lit)
        segments.append((orig_u16, orig_u16 + n, new_u16, new_u16 + n, False))

    new_text = "".join(parts)

    def _map(pos: int, is_end: bool) -> int:
        if pos <= 0:
            return 0
        for a, b, c, d, repl in segments:
            if a <= pos <= b:
                if not repl:
                    return c + (pos - a)
                if pos == a:
                    return c
                if pos == b:
                    return d
                # Position sits inside a substituted token: expand the
                # entity over the whole replacement instead of clipping it.
                return d if is_end else c
        return new_u16

    out: List[Dict[str, Any]] = []
    for ent in entities or []:
        if not isinstance(ent, dict):
            ent = ent.model_dump(mode="json", exclude_none=True)
        offset = int(ent.get("offset", 0))
        length = int(ent.get("length", 0))
        if length <= 0:
            continue
        new_offset = _map(offset, False)
        new_end = _map(offset + length, True)
        if new_end <= new_offset:
            continue
        shifted = dict(ent)
        shifted["offset"] = new_offset
        shifted["length"] = new_end - new_offset
        out.append(shifted)

    out.extend(injected)
    out.sort(key=lambda e: (int(e["offset"]), int(e["length"])))
    return new_text, out


def _sanitize_entities(text: str, entities) -> List[Dict[str, Any]]:
    """Drop entities Telegram would reject against THIS text.

    Stored entities are only valid against the text they were saved
    with.  A placeholder swap, a later ``/setwelcome``, or an entity that
    merely points past the end of the stored template makes Telegram
    answer ``ENTITY_TEXT_INVALID`` — and because the send is
    all-or-nothing, one bad span took the whole welcome with it.

    Apply it twice: once to ``(template, stored_entities)`` to discard
    anything that no longer fits the stored text, then again to the
    rendered result.  Out-of-bounds spans and entities missing the
    payload they need are dropped; of two spans that only *partly*
    overlap, the later one goes (nesting stays legal).  Losing a bold
    run beats losing the welcome.
    """
    limit = _utf16_len(text)
    kept: List[Dict[str, Any]] = []
    for ent in entities or []:
        if not isinstance(ent, dict):
            try:
                ent = ent.model_dump(mode="json", exclude_none=True)
            except Exception:
                continue
        try:
            offset = int(ent.get("offset", -1))
            length = int(ent.get("length", 0))
        except (TypeError, ValueError):
            continue
        if length <= 0 or offset < 0 or offset + length > limit:
            continue
        kind = ent.get("type")
        if kind == "custom_emoji" and not ent.get("custom_emoji_id"):
            continue
        if kind == "text_mention" and not ent.get("user"):
            continue
        if kind == "expandable_blockquote" and (
            offset != 0 or offset + length != limit
        ):
            # Telegram only accepts an expandable quote over the WHOLE
            # message; any placeholder swap breaks that span.
            continue
        kept.append({**ent, "offset": offset, "length": length})

    kept.sort(key=lambda e: (e["offset"], -e["length"]))
    out: List[Dict[str, Any]] = []
    for ent in kept:
        start, end = ent["offset"], ent["offset"] + ent["length"]
        clash = False
        for other in out:
            o_start, o_end = other["offset"], other["offset"] + other["length"]
            if start < o_end and o_start < end:            # they overlap
                inside = o_start <= start and end <= o_end
                covers = start <= o_start and o_end <= end
                if not (inside or covers):                 # partial only
                    clash = True
                    break
        if not clash:
            out.append(ent)
    return out


def _serialize_entities(entities) -> Optional[List[Dict[str, Any]]]:
    """JSON-safe copy of a message's entities for storage (Mongo-safe)."""
    if not entities:
        return None
    out: List[Dict[str, Any]] = []
    for ent in entities:
        try:
            data = ent.model_dump(mode="json", exclude_none=True)
        except Exception:
            continue
        if data.get("type") == "bot_command":
            continue
        out.append(data)
    return out or None


def _command_body(message: Message) -> Tuple[str, int]:
    """(text after the command token, its UTF-16 offset in message.text)."""
    full = message.text or ""
    m = re.match(r"^\S+\s*", full)
    if not m:
        return "", 0
    start = m.end()
    body = full[start:]
    lead = len(body) - len(body.lstrip())
    body = body.strip()
    return body, _utf16_len(full[: start + lead])


def _slice_entities(entities, base: int, length: int) -> Optional[List[Dict[str, Any]]]:
    """Serialized entities of the command message inside its argument body.

    Offsets are re-based onto the body (they arrive relative to the full
    ``/setwelcome …`` text) so they line up with the stored template.
    """
    if not entities or length <= 0:
        return None
    out: List[Dict[str, Any]] = []
    for ent in entities:
        offset = int(getattr(ent, "offset", -1))
        ent_len = int(getattr(ent, "length", 0))
        if offset < base or offset + ent_len > base + length:
            continue
        data = _serialize_entities([ent])
        if not data:
            continue
        data[0]["offset"] = offset - base
        out.append(data[0])
    return out or None


async def _send_template(
    bot: Bot,
    chat_id: int,
    template: str,
    entities: Optional[List[Dict[str, Any]]],
    user,
    chat,
    *,
    fallback: str,
) -> Optional[int]:
    """Send a welcome/goodbye, preserving custom-emoji entities when stored.

    Three tiers, each one guaranteed to be able to send:

    1. an exact replay of the saved source (premium emoji intact) —
       its entities are validated first so one stale span cannot take
       the whole message down with ``ENTITY_TEXT_INVALID``;
    2. HTML, which is what the admin saw in ``/welcome``;
    3. plain text, which Telegram accepts whatever the template holds.

    A welcome must never be *lost*: every tier that can fail does fail
    loudly, and the last one cannot.
    """
    if not template:
        template = fallback

    # Pass 1: the stored spans must fit the stored template.  A span that
    # used to be in range and no longer is is stale data — `_format_entities`
    # would silently stretch it over the whole result, which is exactly the
    # kind of span Telegram rejects with ENTITY_TEXT_INVALID.
    if entities:
        entities = _sanitize_entities(template, entities)

    if entities:
        try:
            text, ents = _format_entities(
                template, _values(user, chat, html=False), entities, mention_user=user
            )
            # Pass 2: the remapped spans must fit the rendered message.
            ents = _sanitize_entities(text, ents)
            if ents:
                sent = await bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    entities=ents,
                    disable_web_page_preview=True,
                )
                return sent.message_id
        except Exception as e:
            # Never lose the welcome: fall through to the HTML path.
            logger.warning(f"entity welcome send failed, falling back to HTML: {e}")

    html_text = format_welcome(template, user, chat)
    try:
        sent = await bot.send_message(
            chat_id=chat_id,
            text=html_text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return sent.message_id
    except Exception as e:
        # Unbalanced/unknown tags in an admin-written template are the
        # usual cause; plain text can always be delivered.
        logger.warning(f"HTML welcome send failed, falling back to plain: {e}")

    try:
        sent = await bot.send_message(
            chat_id=chat_id,
            text=plain(html_text),
            disable_web_page_preview=True,
        )
        return sent.message_id
    except Exception as e:
        logger.error(f"welcome send failed entirely: {e}")
    return None


# ── Command handlers ─────────────────────────────────────
async def setwelcome_command(message: Message, bot: Bot, args: list):
    """Handle /setwelcome — set custom welcome message."""
    if message.chat.type == "private":
        await reply_text(message, f"{E.INFO} This command only works in groups.",
            parse_mode=ParseMode.HTML)
        return

    if not await _is_admin(message, bot):
        return

    if not args and not message.reply_to_message:
        await reply_text(
            message,
            f"{E.WAVE} <b>Set Welcome Message</b>\n\n"
            "<b>Usage:</b>\n"
            "  /setwelcome &lt;text&gt; — Set welcome text\n"
            "  Reply to a message with /setwelcome\n\n"
            "<b>Variables:</b>\n"
            "  {'first'} — First name\n"
            "  {'last'} — Last name\n"
            "  {'fullname'} — Full name\n"
            "  {'username'} — Username\n"
            "  {'mention'} — Mention link\n"
            "  {'chatname'} — Chat name\n"
            "  {'id'} — User ID",
            parse_mode=ParseMode.HTML,
        )
        return

    text = " ".join(args) if args else ""
    entities: Optional[List[Dict[str, Any]]] = None

    if message.reply_to_message:
        source = message.reply_to_message
        text = source.text or source.caption or text
        entities = _serialize_entities(
            source.entities if source.text else source.caption_entities
        )
    elif args:
        # Typed/pasted argument — keep any formatting (incl. premium
        # emoji) that travelled with it, shifted past the command token.
        body, base = _command_body(message)
        if body:
            text = body
            entities = _slice_entities(
                message.entities, base, _utf16_len(body)
            )

    if not text:
        await reply_text(message, "Please provide welcome text.")
        return

    chat_id = message.chat.id
    await adb(db.set_welcome_text(chat_id, text, entities=entities))
    saved = "with its original formatting (premium emojis kept)" if entities else "as plain HTML"
    await reply_text(message, f"{E.CHECK} Welcome message saved {saved}!",
            parse_mode=ParseMode.HTML)


async def setgoodbye_command(message: Message, bot: Bot, args: list):
    """Handle /setgoodbye — set custom goodbye message."""
    if message.chat.type == "private":
        await reply_text(message, f"{E.INFO} This command only works in groups.",
            parse_mode=ParseMode.HTML)
        return

    if not await _is_admin(message, bot):
        return

    if not args and not message.reply_to_message:
        await reply_text(
            message,
            f"{E.GOODBYE} <b>Set Goodbye Message</b>\n\n"
            "<b>Usage:</b>\n"
            "  /setgoodbye &lt;text&gt; — Set goodbye text\n"
            "  Reply to a message with /setgoodbye\n\n"
            "<b>Variables:</b>\n"
            "  {'first'} — First name\n"
            "  {'last'} — Last name\n"
            "  {'fullname'} — Full name\n"
            "  {'username'} — Username\n"
            "  {'mention'} — Mention link\n"
            "  {'chatname'} — Chat name\n"
            "  {'id'} — User ID",
            parse_mode=ParseMode.HTML,
        )
        return

    text = " ".join(args) if args else ""
    entities: Optional[List[Dict[str, Any]]] = None

    if message.reply_to_message:
        source = message.reply_to_message
        text = source.text or source.caption or text
        entities = _serialize_entities(
            source.entities if source.text else source.caption_entities
        )
    elif args:
        body, base = _command_body(message)
        if body:
            text = body
            entities = _slice_entities(
                message.entities, base, _utf16_len(body)
            )

    if not text:
        await reply_text(message, f"{E.ERROR} Please provide goodbye text.",
            parse_mode=ParseMode.HTML)
        return

    chat_id = message.chat.id
    await adb(db.set_goodbye_text(chat_id, text, entities=entities))
    saved = "with its original formatting (premium emojis kept)" if entities else "as plain HTML"
    await reply_text(message, f"{E.CHECK} Goodbye message saved {saved}!",
            parse_mode=ParseMode.HTML)


async def resetwelcome_command(message: Message, bot: Bot):
    """Handle /resetwelcome — reset welcome to default."""
    if message.chat.type == "private":
        await reply_text(message, "This command only works in groups.")
        return

    if not await _is_admin(message, bot):
        return

    await adb(db.reset_welcome(message.chat.id))
    await reply_text(message, f"{E.CHECK} Welcome message reset to default!",
            parse_mode=ParseMode.HTML)


async def resetgoodbye_command(message: Message, bot: Bot):
    """Handle /resetgoodbye — reset goodbye to default."""
    if message.chat.type == "private":
        await reply_text(message, "This command only works in groups.")
        return

    if not await _is_admin(message, bot):
        return

    await adb(db.reset_goodbye(message.chat.id))
    await reply_text(message, f"{E.CHECK} Goodbye message reset to default!",
            parse_mode=ParseMode.HTML)


async def welcome_command(message: Message, bot: Bot, args: list):
    """Handle /welcome — toggle or view welcome settings."""
    if message.chat.type == "private":
        await reply_text(message, "This command only works in groups.")
        return

    if not await _is_admin(message, bot):
        return

    chat_id = message.chat.id
    settings = await adb(db.get_welcome_settings(chat_id))
    msg = await adb(db.get_welcome_message(chat_id))

    if args:
        arg = args[0].lower()
        if arg == "on":
            await adb(db.set_welcome_enabled(chat_id, True))
            await reply_text(message, f"{E.CHECK} Welcome messages enabled!",
            parse_mode=ParseMode.HTML)
            return
        elif arg == "off":
            await adb(db.set_welcome_enabled(chat_id, False))
            await reply_text(message, f"{E.CROSS} Welcome messages disabled!",
            parse_mode=ParseMode.HTML)
            return
        elif arg == "noformat":
            await reply_text(
                message,
                f"{E.WAVE} <b>Welcome Settings:</b>\n"
                f"  Welcome: {'ON' if settings.get('welcome_enabled') else 'OFF'}\n"
                f"  Clean Welcome: {'ON' if settings.get('clean_welcome') else 'OFF'}\n\n"
                f"<b>Welcome text (no formatting):</b>\n{msg.get('welcome_text', '')}",
                parse_mode=ParseMode.HTML,
            )
            return

    await reply_text(
        message,
        f"{E.WAVE} <b>Welcome Settings:</b>\n"
        f"  Welcome: {'ON' if settings.get('welcome_enabled') else 'OFF'}\n"
        f"  Goodbye: {'ON' if settings.get('goodbye_enabled') else 'OFF'}\n"
        f"  Clean Welcome: {'ON' if settings.get('clean_welcome') else 'OFF'}\n"
        f"  Clean Goodbye: {'ON' if settings.get('clean_goodbye') else 'OFF'}\n\n"
        f"<b>Current Welcome:</b>\n{msg.get('welcome_text', '')}",
        parse_mode=ParseMode.HTML,
    )


async def goodbye_command(message: Message, bot: Bot, args: list):
    """Handle /goodbye — toggle or view goodbye settings."""
    if message.chat.type == "private":
        await reply_text(message, "This command only works in groups.")
        return

    if not await _is_admin(message, bot):
        return

    chat_id = message.chat.id
    settings = await adb(db.get_welcome_settings(chat_id))
    msg = await adb(db.get_welcome_message(chat_id))

    if args:
        arg = args[0].lower()
        if arg == "on":
            await adb(db.set_goodbye_enabled(chat_id, True))
            await reply_text(message, f"{E.CHECK} Goodbye messages enabled!",
            parse_mode=ParseMode.HTML)
            return
        elif arg == "off":
            await adb(db.set_goodbye_enabled(chat_id, False))
            await reply_text(message, f"{E.CROSS} Goodbye messages disabled!",
            parse_mode=ParseMode.HTML)
            return
        elif arg == "noformat":
            await reply_text(
                message,
                f"{E.GOODBYE} <b>Goodbye Settings:</b>\n"
                f"  Goodbye: {'ON' if settings.get('goodbye_enabled') else 'OFF'}\n\n"
                f"<b>Goodbye text (no formatting):</b>\n{msg.get('goodbye_text', '')}",
                parse_mode=ParseMode.HTML,
            )
            return

    await reply_text(
        message,
        f"{E.GOODBYE} <b>Goodbye Settings:</b>\n"
        f"  Goodbye: {'ON' if settings.get('goodbye_enabled') else 'OFF'}\n"
        f"  Clean Goodbye: {'ON' if settings.get('clean_goodbye') else 'OFF'}\n\n"
        f"<b>Current Goodbye:</b>\n{msg.get('goodbye_text', '')}",
        parse_mode=ParseMode.HTML,
    )


async def cleanwelcome_command(message: Message, bot: Bot, args: list):
    """Handle /cleanwelcome — toggle clean welcome."""
    if message.chat.type == "private":
        await reply_text(message, f"{E.INFO} This command only works in groups.",
            parse_mode=ParseMode.HTML)
        return

    if not await _is_admin(message, bot):
        return

    if not args:
        settings = await adb(db.get_welcome_settings(message.chat.id))
        await reply_text(message, f"{E.SETTINGS} Clean welcome: {'ON' if settings.get('clean_welcome') else 'OFF'}",
            parse_mode=ParseMode.HTML)
        return

    arg = args[0].lower()
    if arg == "on":
        await adb(db.set_clean_welcome(message.chat.id, True))
        await reply_text(message, f"{E.CHECK} Clean welcome enabled! Old welcome messages will be deleted.",
            parse_mode=ParseMode.HTML)
    elif arg == "off":
        await adb(db.set_clean_welcome(message.chat.id, False))
        await reply_text(message, f"{E.CROSS} Clean welcome disabled!",
            parse_mode=ParseMode.HTML)
    else:
        await reply_text(message, f"{E.INFO} Usage: /cleanwelcome on|off",
            parse_mode=ParseMode.HTML)


async def cleangoodbye_command(message: Message, bot: Bot, args: list):
    """Handle /cleangoodbye — toggle clean goodbye."""
    if message.chat.type == "private":
        await reply_text(message, f"{E.INFO} This command only works in groups.",
            parse_mode=ParseMode.HTML)
        return

    if not await _is_admin(message, bot):
        return

    if not args:
        settings = await adb(db.get_welcome_settings(message.chat.id))
        await reply_text(message, f"{E.SETTINGS} Clean goodbye: {'ON' if settings.get('clean_goodbye') else 'OFF'}",
            parse_mode=ParseMode.HTML)
        return

    arg = args[0].lower()
    if arg == "on":
        await adb(db.set_clean_goodbye(message.chat.id, True))
        await reply_text(message, f"{E.CHECK} Clean goodbye enabled! Old goodbye messages will be deleted.",
            parse_mode=ParseMode.HTML)
    elif arg == "off":
        await adb(db.set_clean_goodbye(message.chat.id, False))
        await reply_text(message, f"{E.CROSS} Clean goodbye disabled!",
            parse_mode=ParseMode.HTML)
    else:
        await reply_text(message, f"{E.INFO} Usage: /cleangoodbye on|off",
            parse_mode=ParseMode.HTML)


# ── Welcome/Goodbye handlers ─────────────────────────────
async def new_member_handler(message: Message, bot: Bot):
    """Handle new members joining the chat."""
    if not message or message.chat.type == "private":
        return

    chat_id = message.chat.id
    settings = await adb(db.get_welcome_settings(chat_id))

    if not settings.get("welcome_enabled", True):
        return

    msg_data = await adb(db.get_welcome_message(chat_id))
    welcome_text = msg_data.get("welcome_text") or f"{E.WAVE} Hey {{first}}, welcome to {{chatname}}!"
    welcome_entities = msg_data.get("welcome_entities")

    for user in message.new_chat_members:
        # Skip bots
        if user.is_bot:
            continue

        # Skip if user is the bot itself
        if user.id == bot.id:
            continue

        # Clean old welcome message
        if settings.get("clean_welcome") and settings.get("last_welcome_msg_id"):
            try:
                await bot.delete_message(chat_id, settings["last_welcome_msg_id"])
            except Exception:
                pass

        try:
            # Sends as a faithful copy of the saved source (custom/premium
            # emoji entities intact) with {placeholders} swapped in.
            msg_id = await _send_template(
                bot,
                chat_id,
                welcome_text,
                welcome_entities,
                user,
                message.chat,
                fallback=f"{E.WAVE} Hey {{first}}, welcome to {{chatname}}!",
            )
            if msg_id:
                await adb(db.update_last_welcome_msg(chat_id, msg_id))
        except Exception as e:
            logger.warning(f"Welcome message error: {e}")


async def left_member_handler(message: Message, bot: Bot):
    """Handle members leaving the chat."""
    if not message or message.chat.type == "private":
        return

    chat_id = message.chat.id
    settings = await adb(db.get_welcome_settings(chat_id))

    if not settings.get("goodbye_enabled", True):
        return

    user = message.left_chat_member
    if not user or user.is_bot:
        return

    msg_data = await adb(db.get_welcome_message(chat_id))
    goodbye_text = msg_data.get("goodbye_text") or f"{E.GOODBYE} Sad to see you leaving {{first}}. Take Care!"
    goodbye_entities = msg_data.get("goodbye_entities")

    # Clean old goodbye message
    if settings.get("clean_goodbye") and settings.get("last_goodbye_msg_id"):
        try:
            await bot.delete_message(chat_id, settings["last_goodbye_msg_id"])
        except Exception:
            pass

    try:
        msg_id = await _send_template(
            bot,
            chat_id,
            goodbye_text,
            goodbye_entities,
            user,
            message.chat,
            fallback=f"{E.GOODBYE} Sad to see you leaving {{first}}. Take Care!",
        )
        if msg_id:
            await adb(db.update_last_goodbye_msg(chat_id, msg_id))
    except Exception as e:
        logger.warning(f"Goodbye message error: {e}")


# ── Module setup ─────────────────────────────────────────
def setup() -> list:
    """Register welcome commands and handlers."""
    # Commands
    on("message", setwelcome_command, flt=and_f(cmd("setwelcome"), GROUPS))
    on("message", setgoodbye_command, flt=and_f(cmd("setgoodbye"), GROUPS))
    on("message", resetwelcome_command, flt=and_f(cmd("resetwelcome"), GROUPS))
    on("message", resetgoodbye_command, flt=and_f(cmd("resetgoodbye"), GROUPS))
    on("message", welcome_command, flt=and_f(cmd("welcome"), GROUPS))
    on("message", goodbye_command, flt=and_f(cmd("goodbye"), GROUPS))
    on("message", cleanwelcome_command, flt=and_f(cmd("cleanwelcome"), GROUPS))
    on("message", cleangoodbye_command, flt=and_f(cmd("cleangoodbye"), GROUPS))

    # Welcome/Goodbye handlers
    on("message", new_member_handler, group=10, flt=F.new_chat_members)
    on("message", left_member_handler, group=10, flt=F.left_chat_member)

    return ["/setwelcome", "/setgoodbye", "/resetwelcome", "/resetgoodbye",
            "/welcome", "/goodbye", "/cleanwelcome", "/cleangoodbye"]
