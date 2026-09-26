"""Join requests module — approval card for pending join requests.

* ``/request on|off`` — toggle the approval card per group (admin only).
* When enabled, every new join request posts a card with the
  requester's info (name, mention, ID, scan, username) and colored
  Accept / Decline buttons.
* Only group admins may press the buttons; pressing one approves or
  declines the request via the Bot API and rewrites the card into a
  plain result message (``X accepted join request of Y``).

Callback data:
    joinreq:accept:<chat_id>:<user_id>
    joinreq:decline:<chat_id>:<user_id>
"""

from __future__ import annotations

from html import escape

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatJoinRequestHandler,
    ContextTypes,
)

from bot.command_handler import CommandHandler
from bot.database import db
from bot.emojis import E, EID
from bot.keyboards.colored import btn_danger, btn_success, build_keyboard
from bot.logger import logger
from bot.responses import action_card, field_extra, reply_card

#: Callback data prefix.
_CB = "joinreq"

#: (chat_id, user_id) → mention HTML for cards still on screen.
#: Lets the result message name the requester even when they were never
#: a member (decline), or the API lookup fails; survives a restart only
#: as a plain-ID fallback.
_PENDING: dict[tuple[int, int], str] = {}


# ── Helpers ──────────────────────────────────────────────────────

async def _is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        member = await context.bot.get_chat_member(
            update.effective_chat.id, update.effective_user.id
        )
        return member.status in ("administrator", "creator")
    except Exception:
        return False


def _mention(user) -> str:
    name = escape(getattr(user, "full_name", None) or str(user.id))
    return f'<a href="tg://user?id={user.id}">{name}</a>'


def _username(user) -> str:
    return f"@{escape(user.username)}" if user.username else "n/a"


def _scan_flag(user_id: int) -> str:
    """Scan: True when the requester is flagged (globally banned)."""
    try:
        return "True" if db.is_gbanned(user_id) else "False"
    except Exception:
        return "False"


def request_card(user) -> str:
    """Approval card body — USER'S INFO block from the reference design."""
    name = escape(getattr(user, "full_name", None) or "Unknown")
    return "\n".join(
        [
            f"{E.INFO} New join request is available",
            "",
            f"{E.USER} USER'S INFO",
            f"├ Name: {name}",
            f"├ Mention: {_mention(user)}",
            f"├ ID: <code>{user.id}</code>",
            f"├ Scan: {_scan_flag(user.id)}",
            f"└ Username: {_username(user)}",
        ]
    )


def request_keyboard(chat_id: int, user_id: int):
    """Colored Accept / Decline row for the approval card."""
    return build_keyboard(
        [
            [
                btn_success(
                    "Accept",
                    f"{_CB}:accept:{chat_id}:{user_id}",
                    icon_emoji_id=EID.CHECK,
                ),
                btn_danger(
                    "Decline",
                    f"{_CB}:decline:{chat_id}:{user_id}",
                    icon_emoji_id=EID.CROSS,
                ),
            ]
        ]
    )


# ── /request on|off ──────────────────────────────────────────────

async def request_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /request — show status, or toggle with on/off."""
    if update.effective_chat.type == "private":
        await update.message.reply_text(
            f"{E.ERROR} This command only works in groups.",
            parse_mode=ParseMode.HTML,
        )
        return

    if not await _is_admin(update, context):
        await update.message.reply_text(
            f"{E.ERROR} You need admin rights to change join request settings.",
            parse_mode=ParseMode.HTML,
        )
        return

    if not context.args or context.args[0].lower() not in ("on", "off"):
        enabled = db.get_join_requests(update.effective_chat.id)
        await reply_card(
            update.message,
            action_card(
                "Join Requests",
                [
                    field_extra(
                        E.INFO, "Status", "Enabled" if enabled else "Disabled"
                    ),
                    field_extra(E.SETTINGS, "Usage", "/request on · /request off"),
                ],
                icon=E.INFO,
            ),
        )
        return

    enable = context.args[0].lower() == "on"
    db.set_join_requests(update.effective_chat.id, enable)
    await reply_card(
        update.message,
        action_card(
            "Join Requests",
            [
                field_extra(E.INFO, "Status", "Enabled" if enable else "Disabled"),
                field_extra(
                    E.USER,
                    "Cards",
                    "New requests now get an approval card in this chat."
                    if enable
                    else "Approval cards are off.",
                ),
            ],
            icon=E.INFO,
        ),
    )


# ── ChatJoinRequestHandler ───────────────────────────────────────

async def on_join_request(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Post the approval card when someone requests to join the group."""
    request = update.chat_join_request
    if request is None:
        return

    chat = request.chat
    if not db.get_join_requests(chat.id):
        return

    _PENDING[(chat.id, request.from_user.id)] = _mention(request.from_user)

    try:
        await context.bot.send_message(
            chat.id,
            request_card(request.from_user),
            parse_mode=ParseMode.HTML,
            reply_markup=request_keyboard(chat.id, request.from_user.id),
        )
    except Exception as e:
        logger.warning(f"Failed to post join-request card in {chat.id}: {e}")


# ── Accept / Decline callbacks ───────────────────────────────────

async def join_request_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Approve/decline a pending request and rewrite the card in place."""
    query = update.callback_query
    if query is None or not (query.data or "").startswith(f"{_CB}:"):
        return

    # joinreq:<action>:<chat_id>:<user_id>
    parts = query.data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    chat_id = (
        int(parts[2])
        if len(parts) > 3 and parts[2].lstrip("-").isdigit()
        else 0
    )
    user_id = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0

    if action not in ("accept", "decline") or not chat_id or not user_id:
        await query.answer("Invalid request.", show_alert=True)
        return

    # Admin gate — only group admins/owner may press the buttons.
    try:
        member = await context.bot.get_chat_member(chat_id, query.from_user.id)
        if member.status not in ("administrator", "creator"):
            await query.answer(
                "Only admins can process join requests.", show_alert=True
            )
            return
    except Exception:
        await query.answer(
            "Could not verify your permissions.", show_alert=True
        )
        return

    await query.answer()

    admin = escape(
        getattr(query.from_user, "full_name", None) or str(query.from_user.id)
    )

    try:
        if action == "accept":
            await context.bot.approve_chat_join_request(chat_id, user_id)
        else:
            await context.bot.decline_chat_join_request(chat_id, user_id)
    except Exception as e:
        logger.warning(f"Join request {action} failed for {user_id}: {e}")
        await _edit_text(
            query, f"{E.ERROR} Could not {action} the request: {escape(str(e))}"
        )
        return

    # Approved users become members → live lookup gives a fresh name;
    # declined users never were members → use the card-time cache.
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        requester_text = _mention(member.user)
    except Exception:
        requester_text = _PENDING.pop(
            (chat_id, user_id), f"<code>{user_id}</code>"
        )

    if action == "accept":
        result = f"{E.CHECK} {admin} accepted join request of {requester_text}"
    else:
        result = f"{E.CROSS} {admin} declined join request of {requester_text}"
    await _edit_text(query, result)


async def _edit_text(query, text: str) -> None:
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.debug(f"join-request result edit failed: {e}")


# ── Module setup ─────────────────────────────────────────────────

def setup(app: Application) -> list[str]:
    """Register this module's handlers. Returns route descriptions for the log."""
    app.add_handler(CommandHandler("request", request_command))
    app.add_handler(ChatJoinRequestHandler(on_join_request))
    app.add_handler(
        CallbackQueryHandler(join_request_callback, pattern=rf"^{_CB}:")
    )
    return [
        "/request on|off",
        "join-request approval cards",
        f"{_CB}:* callbacks",
    ]
