"""Bind module callbacks — every action re-verifies group admin; edit in place."""

import logging
from html import escape
from typing import Any, Dict, Optional

from telegram import InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from bot.emojis import E

from . import database as bdb
from .checks import invalidate_cache, is_channel_member, is_group_admin
from .config import (
    AUTO_DELETE_OPTIONS,
    GATE_LABELS,
    GATES,
    GRACE_OPTIONS,
    PLACEHOLDERS_HELP,
)
from .keyboards import (
    autodel_menu,
    bind_main_menu,
    custom_menu,
    gates_menu,
    grace_menu,
    help_bind_text,
    status_menu,
    unbind_confirm_menu,
)
from .utils import channel_display, format_autodel, format_grace, indicator


def _channel_taken_text() -> str:
    return (
        f"{E.ERROR} <b>Channel already bound.</b>\n\n"
        "One channel can only be bound to one group at a time.\n"
        "Unbind it from the other group first, or pick a different channel."
    )

logger = logging.getLogger(__name__)


async def _safe_answer(query, text: str = "", alert: bool = False) -> None:
    try:
        await query.answer(text, show_alert=alert)
    except Exception:
        pass


async def _edit(query, text: str, reply_markup: Optional[InlineKeyboardMarkup]) -> None:
    try:
        await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
    except Exception as e:
        # "message is not modified" is fine; log others.
        if "not modified" not in str(e).lower():
            logger.debug(f"bind callback edit failed: {e}")


async def _deny_non_admin(query, context, chat_id: int, user_id: int) -> bool:
    """Return True (and answer) if the user is no longer a group admin."""
    if await is_group_admin(context.bot, chat_id, user_id):
        return False
    await _safe_answer(query, "Admin rights required.", alert=True)
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    return True


def _menu_text(settings: Optional[Dict[str, Any]], group_title: str = "") -> str:
    if not settings:
        return (
            f"{E.INFO} <b>Bind Module</b>\n\n"
            "This group is not bound to a channel yet.\n\n"
            f"{help_bind_text()}"
        )
    gates_on = [GATE_LABELS[k] for k, col in GATES.items() if int(settings.get(col) or 0)]
    gates_line = ", ".join(gates_on) if gates_on else "None (type gates)"
    force = f"{E.CHECK} ON" if settings.get("force_join") else f"{E.ERROR} OFF"
    bypass = f"{E.CHECK} ON" if settings.get("admin_bypass") else f"{E.ERROR} OFF"
    return (
        f"{E.SETTINGS} <b>Bind Menu</b>"
        + (f"\n{E.USER} {escape(group_title)}" if group_title else "")
        + f"\n{E.ANNOUNCE} Channel: {channel_display(settings)}"
        + f"\n{E.CHECK} Force Join: <b>{force}</b>"
        + f"\n{E.CROWN} Admin Bypass: <b>{bypass}</b>"
        + f"\n{E.SETTINGS} Gates: <b>{gates_line}</b>"
        + f"\n{E.CLOCK} Grace: <b>{format_grace(int(settings.get('grace_minutes') or 0))}</b>"
        + f"\n{E.TIME} Auto-delete: <b>{format_autodel(int(settings.get('auto_delete_seconds') or 0))}</b>"
        + f"\n{E.INFO} Custom message: <b>{'set' if settings.get('custom_message') else 'default'}</b>"
        + f"\n\n<i>Placeholders:</i> <code>{PLACEHOLDERS_HELP}</code>"
    )


def _status_text(settings: Dict[str, Any], group_title: str = "") -> str:
    force = f"{E.CHECK} ON" if settings.get("force_join") else f"{E.ERROR} OFF"
    bypass = f"{E.CHECK} ON" if settings.get("admin_bypass") else f"{E.ERROR} OFF"
    gates_on = [GATE_LABELS[k] for k, col in GATES.items() if int(settings.get(col) or 0)]
    rows = [
        f"{E.WATCH} <b>Bind Status</b>",
    ]
    if group_title:
        rows.append(f"{E.USER} Group: {escape(group_title)}")
    rows.append(f"{E.ANNOUNCE} Channel: {channel_display(settings)}")
    rows.append(f"Channel ID: <code>{settings.get('channel_id')}</code>")
    rows.append(f"{E.CHECK} Force Join: <b>{force}</b>")
    rows.append(f"{E.CROWN} Admin Bypass: <b>{bypass}</b>")
    rows.append(f"{E.CLOCK} Grace: <b>{format_grace(int(settings.get('grace_minutes') or 0))}</b>")
    rows.append(f"{E.TIME} Auto-delete: <b>{format_autodel(int(settings.get('auto_delete_seconds') or 0))}</b>")
    rows.append(f"{E.WEB} Link: {settings.get('channel_link') or 'private (no public link)'}")
    if settings.get("bound_at"):
        rows.append(f"{E.TIME} Bound at: {settings['bound_at']}")
    rows.append(f"{E.USER} Bound by: <code>{settings.get('bound_by') or '?'}</code>")
    rows.append(f"{E.SETTINGS} Gates ON: {', '.join(gates_on) if gates_on else '—'}")
    rows.append("")
    rows.append(f"<i>Placeholders:</i> <code>{PLACEHOLDERS_HELP}</code>")
    return "\n".join(rows)


async def bind_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route all ^bind: callbacks."""
    query = update.callback_query
    if not query or not query.data or not query.message:
        return

    user = update.effective_user
    chat = query.message.chat
    # Prefer the chat the menu lives in (group). Callbacks from DM menus unsupported.
    if chat.type == "private":
        await _safe_answer(query, "Open this in a group.", alert=True)
        return

    chat_id = chat.id
    data = query.data
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else "menu"
    payload = parts[2] if len(parts) > 2 else None

    # Always re-verify admin membership for every callback.
    if await _deny_non_admin(query, context, chat_id, user.id):
        return

    settings = bdb.get_settings(chat_id)
    group_title = chat.title or ""

    # ── Close ─────────────────────────────────────────────
    if action == "close":
        await _safe_answer(query, "Closed")
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    # ── Help / first-time ─────────────────────────────────
    if action == "help_bind":
        await _safe_answer(query)
        await _edit(query, help_bind_text(), bind_main_menu(settings, group_title=group_title))
        return

    # ── Menu / Refresh / Back ─────────────────────────────
    if action in ("menu", "refresh", "back"):
        await _safe_answer(query, "Refreshed")
        if settings:
            await _edit(query, _menu_text(settings, group_title), bind_main_menu(settings, group_title=group_title))
        else:
            await _edit(query, _menu_text(None), bind_main_menu(None, group_title=group_title))
        return

    if not settings:
        await _safe_answer(query, "Not bound.", alert=True)
        await _edit(query, _menu_text(None), bind_main_menu(None, group_title=group_title))
        return

    # ── Status ────────────────────────────────────────────
    if action == "status":
        await _safe_answer(query, "Status")
        fresh = bdb.get_settings(chat_id) or settings
        await _edit(query, _status_text(fresh, group_title), status_menu(fresh))
        return

    # ── Toggle master switches ────────────────────────────
    if action == "toggle" and payload in ("force_join", "admin_bypass"):
        fresh = bdb.toggle_field(chat_id, payload)
        await _safe_answer(query, f"{payload} → {'ON' if fresh and fresh.get(payload) else 'OFF'}")
        await _edit(query, _menu_text(fresh, group_title), bind_main_menu(fresh, group_title=group_title))
        return

    # ── Gates ─────────────────────────────────────────────
    if action == "gates":
        await _safe_answer(query, "Gates")
        await _edit(
            query,
            f"{E.SETTINGS} <b>Message Gates</b>\n\n"
            "Toggle which message types are blocked for non-members.\n"
            "When <b>Force Join</b> is ON, all non-member messages are gated anyway;\n"
            "these switches refine behavior when Force Join is OFF.",
            gates_menu(settings),
        )
        return

    if action == "gate" and payload in GATES:
        col = GATES[payload]
        fresh = bdb.toggle_field(chat_id, col)
        on = indicator(fresh.get(col)) if fresh else "OFF"
        await _safe_answer(query, f"{GATE_LABELS[payload]} → {on}")
        await _edit(
            query,
            f"{E.SETTINGS} <b>Message Gates</b>\n\n"
            "Toggle which message types are blocked for non-members.\n"
            "When <b>Force Join</b> is ON, all non-member messages are gated anyway;\n"
            "these switches refine behavior when Force Join is OFF.",
            gates_menu(fresh or settings),
        )
        return

    # ── Grace ─────────────────────────────────────────────
    if action == "grace":
        await _safe_answer(query, "Grace period")
        await _edit(
            query,
            f"{E.CLOCK} <b>Grace Period</b>\n\n"
            "New members get this long before force-join applies.\n"
            "OFF = enforced immediately.",
            grace_menu(settings),
        )
        return

    if action == "grace_set" and payload is not None:
        try:
            minutes = int(payload)
            if minutes not in GRACE_OPTIONS:
                raise ValueError
        except ValueError:
            await _safe_answer(query, "Invalid option.", alert=True)
            return
        fresh = bdb.update_field(chat_id, "grace_minutes", minutes)
        await _safe_answer(query, f"Grace → {format_grace(minutes)}")
        await _edit(
            query,
            f"{E.CLOCK} <b>Grace Period</b>\n\n"
            "New members get this long before force-join applies.\n"
            "OFF = enforced immediately.",
            grace_menu(fresh or settings),
        )
        return

    # ── Auto-delete ───────────────────────────────────────
    if action == "autodel":
        await _safe_answer(query, "Auto-delete")
        await _edit(
            query,
            f"{E.TIME} <b>Auto-Delete Warning</b>\n\n"
            "How long the force-join warning stays before the bot removes it.",
            autodel_menu(settings),
        )
        return

    if action == "autodel_set" and payload is not None:
        try:
            secs = int(payload)
            if secs not in AUTO_DELETE_OPTIONS:
                raise ValueError
        except ValueError:
            await _safe_answer(query, "Invalid option.", alert=True)
            return
        fresh = bdb.update_field(chat_id, "auto_delete_seconds", secs)
        await _safe_answer(query, f"Auto-delete → {format_autodel(secs)}")
        await _edit(
            query,
            f"{E.TIME} <b>Auto-Delete Warning</b>\n\n"
            "How long the force-join warning stays before the bot removes it.",
            autodel_menu(fresh or settings),
        )
        return

    # ── Custom message ────────────────────────────────────
    if action == "custom":
        await _safe_answer(query, "Custom message")
        current = settings.get("custom_message") or "(default)"
        await _edit(
            query,
            f"{E.INFO} <b>Custom Force-Join Message</b>\n\n"
            f"Current:\n<code>{escape(str(current))}</code>\n\n"
            f"Placeholders: <code>{PLACEHOLDERS_HELP}</code>",
            custom_menu(settings),
        )
        return

    if action == "custom_set":
        context.chat_data["bind_wait"] = "custom"
        await _safe_answer(query, "Send the new message", alert=False)
        await _edit(
            query,
            f"{E.INFO} <b>Send the new force-join message</b>\n\n"
            "Reply in this chat with the text to use. It will replace the current message.\n"
            f"Placeholders: <code>{PLACEHOLDERS_HELP}</code>\n\n"
            "<i>Sending /bindmenu cancels.</i>",
            None,
        )
        return

    if action == "custom_reset":
        fresh = bdb.update_field(chat_id, "custom_message", None)
        await _safe_answer(query, "Reset to default")
        await _edit(
            query,
            f"{E.CHECK} Custom message reset to default.\n\n"
            f"<i>Placeholders:</i> <code>{PLACEHOLDERS_HELP}</code>",
            custom_menu(fresh or settings),
        )
        return

    if action == "custom_preview":
        from .utils import render_custom_message

        preview = render_custom_message(
            settings.get("custom_message"),
            user_mention='<a href="tg://user?id=0">User</a>',
            user_id=0,
            group_title=group_title or "Group",
            channel_title=settings.get("channel_title") or "Channel",
            channel_link=settings.get("channel_link") or "https://t.me/",
        )
        await _safe_answer(query, "Preview")
        await _edit(query, f"{E.EYES} <b>Preview</b>\n\n{preview}", custom_menu(settings))
        return

    # ── Change channel ────────────────────────────────────
    if action == "change":
        context.chat_data["bind_wait"] = "channel"
        await _safe_answer(query, "Send new channel")
        await _edit(
            query,
            f"{E.WEB} <b>Change bound channel</b>\n\n"
            "Send the new channel reference in this chat:\n"
            "• <code>@username</code>\n"
            "• <code>https://t.me/username</code>\n"
            "• <code>-100…</code>\n\n"
            "<i>Sending /bindmenu cancels.</i>",
            None,
        )
        return

    if action == "replace_yes":
        pending = context.chat_data.get("bind_pending_channel")
        if not pending:
            await _safe_answer(query, "Nothing pending.", alert=True)
            await _edit(query, _menu_text(settings, group_title), bind_main_menu(settings, group_title=group_title))
            return
        # Re-check at confirm time — channel may have been claimed meanwhile.
        if bdb.find_other_binding(int(pending["id"]), chat_id):
            context.chat_data.pop("bind_pending_channel", None)
            await _safe_answer(query, "Channel is already bound to another group.", alert=True)
            await _edit(query, _channel_taken_text(), unbind_confirm_menu())
            return
        # Simulate channel object-ish for apply
        class _Ch:
            pass

        ch = _Ch()
        ch.id = pending["id"]
        ch.username = pending.get("username")
        ch.title = pending.get("title")
        try:
            bdb.upsert_binding(
                chat_id,
                ch.id,
                channel_username=ch.username,
                channel_title=ch.title,
                channel_link=pending.get("link"),
                bound_by=user.id,
            )
        except ValueError as e:
            context.chat_data.pop("bind_pending_channel", None)
            if str(e) == "CHANNEL_TAKEN":
                await _safe_answer(query, "Channel is already bound to another group.", alert=True)
                await _edit(query, _channel_taken_text(), unbind_confirm_menu())
                return
            raise
        context.chat_data.pop("bind_pending_channel", None)
        fresh = bdb.get_settings(chat_id)
        await _safe_answer(query, "Channel replaced")
        await _edit(query, _menu_text(fresh, group_title), bind_main_menu(fresh, group_title=group_title))
        return

    # ── Unbind ────────────────────────────────────────────
    if action == "unbind":
        await _safe_answer(query, "Confirm unbind")
        await _edit(
            query,
            f"{E.WARNING} <b>Unbind this group?</b>\n\n"
            "Force-join and all gates will stop immediately.\n"
            "This cannot be undone (you can /bind again later).",
            unbind_confirm_menu(),
        )
        return

    if action == "unbind_yes":
        bdb.remove_binding(chat_id)
        context.chat_data.pop("bind_wait", None)
        context.chat_data.pop("bind_pending_channel", None)
        await _safe_answer(query, "Unbound")
        await _edit(query, f"{E.CHECK} Group unbound. All gates disabled.", bind_main_menu(None, group_title=group_title))
        return

    # ── I've Joined (fresh membership check) ──────────────
    if action == "join":
        settings = bdb.get_settings(chat_id)
        if not settings or not settings.get("channel_id"):
            await _safe_answer(query, "Not bound.", alert=True)
            return

        channel_id = settings["channel_id"]
        # Spec: force fresh check, never trust cache on this button.
        invalidate_cache(channel_id, user.id)
        ok = await is_channel_member(context.bot, channel_id, user.id, fresh=True)

        if ok:
            # Persist join for grace bookkeeping going forward.
            bdb.record_join(chat_id, user.id)
            await _safe_answer(query, "Membership verified", alert=False)
            try:
                await query.edit_message_text(
                    f"{E.CHECK} You're a member — you can chat now.",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
        else:
            await _safe_answer(query, "Still not a member of the channel.", alert=True)
        return

    # Unknown action
    await _safe_answer(query)
