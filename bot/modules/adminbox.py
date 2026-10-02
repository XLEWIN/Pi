"""Adminbox — inline admin panel for groups (/adminbox).

One command opens the panel; every action is an ``abox:`` callback:

    /adminbox → Admin Panel (stats + colored button grid)
        ├── 👥 Admins          list with per-admin [Remove] buttons
        ├── ✏️ Group Name       waiting-input flow (panel shows prompt)
        ├── 📝 Group Bio       waiting-input flow
        ├── 🔗 Invite Link     primary link + Generate New (+ copy)
        ├── 👥 Group Info      name / id / members / admins / owner
        ├── 🔒 Permissions     YOUR current admin rights
        ├── 🤖 Bot Status      the bot's own admin rights
        ├── 📌 Message Tools   pin / unpin / delete / purge / broadcast
        ├── ⚙️ Group Settings   slow mode, join-request link, defaults
        ├── 🔄 Refresh
        └── ❌ Close            deletes the panel

Security model (per spec)
-------------------------
* The command refuses private chats and non-admins.
* EVERY callback re-checks the presser's CURRENT admin status via
  ``get_chat_member`` — an admin demoted mid-session loses the panel
  immediately, including the waiting-input flows.
* Demotion protects the group owner, the bot itself and the presser,
  re-fetches the target's live status (stale list taps), and pre-checks
  the bot's own ``can_promote_members`` right before calling the API.

Slow mode is display-only: Bot API (PTB 21.6) no longer exposes a
slow-mode setter — the note on the settings card says so honestly.

Handler groups: command + callbacks in 0, message hook in 21 (0-20 are
taken — see tests/test_dispatch_groups.py). The hook both records
recent message ids for /purge and consumes waiting-input text.
"""

from __future__ import annotations

import re
import time
from collections import deque
from html import escape

from aiogram import Bot, F
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, CopyTextButton, InlineKeyboardButton, Message

from bot.command_handler import parse_command
from bot.pipeline import cmd, on
from bot.reply import reply_text
from bot.emojis import E, EID
from bot.keyboards.colored import (
    btn_danger,
    btn_default,
    btn_primary,
    btn_success,
    build_keyboard,
)
from bot.responses import action_card, error_card, success_card

# ── Constants ─────────────────────────────────────────────────────
CB = "abox"
MESSAGE_GROUP = 21          # unique — 0..20 are taken by other modules
WAIT_TTL = 180.0            # seconds a name/bio/broadcast prompt stays live
PURGE_MAX = 30              # newest tracked ids deleted per purge
RECENT_CAP = 60             # tracked ids kept per chat
ADMIN_STATUSES = ("creator", "administrator")

#: (label, ChatMember attribute) shown on the permission cards.
_RIGHTS = (
    ("Manage Chat", "can_manage_chat"),
    ("Change Info", "can_change_info"),
    ("Invite Users", "can_invite_users"),
    ("Pin Messages", "can_pin_messages"),
    ("Delete Messages", "can_delete_messages"),
    ("Restrict Members", "can_restrict_members"),
    ("Promote Members", "can_promote_members"),
)

#: Default-permission lines for the Group Settings submenu.
_DEFAULT_RIGHTS = (
    ("Send Messages", "can_send_messages"),
    ("Send Polls", "can_send_polls"),
    ("Stickers & Media", "can_send_other_messages"),
    ("Web Previews", "can_add_web_page_previews"),
    ("Change Info", "can_change_info"),
    ("Invite Users", "can_invite_users"),
    ("Pin Messages", "can_pin_messages"),
    ("Manage Topics", "can_manage_topics"),
)

#: Service-message markers — never recorded for /purge (not deletable).
_SERVICE_MARKERS = (
    "new_chat_members",
    "left_chat_member",
    "pinned_message",
    "group_chat_created",
    "delete_chat_photo",
    "video_chat_started",
    "migrate_from_chat_id",
    "successful_payment",
)

_PROMPTS = {
    "name": (E.FOLDER, "Set Group Name", "Send the new group name now."),
    "bio": (E.BOOKMARK, "Set Group Bio", "Send the new group description now."),
    "bcast": (E.ANNOUNCE, "Broadcast Message", "Send the message to post in this group."),
}

_STATUS_LABELS = {
    "creator": "Owner",
    "administrator": "Administrator",
    "member": "Member",
    "restricted": "Restricted",
    "left": "Left",
    "kicked": "Kicked",
}


# ── Small helpers ────────────────────────────────────────────────

def _yn(value) -> str:
    return f"{E.CHECK} Allowed" if value else f"{E.CROSS} Denied"


def _status_label(status: str | None) -> str:
    return _STATUS_LABELS.get(status or "", (status or "Unknown").title())


def _plain_name(user) -> str:
    """Button-safe plain name (no HTML, single line)."""
    if user is None:
        return "?"
    name = getattr(user, "full_name", None)
    if not name:
        first = getattr(user, "first_name", None) or ""
        last = getattr(user, "last_name", None) or ""
        name = " ".join(p for p in (first, last) if p).strip()
    name = name or str(getattr(user, "id", "?"))
    return name.replace("\n", " ")[:64]


def _rights_fields(member) -> list:
    return [
        (E.ADMIN, label, _yn(getattr(member, attr, None)))
        for label, attr in _RIGHTS
    ]


def _member_label(member) -> str:
    status = getattr(member, "status", None)
    label = _status_label(status)
    if status == "administrator":
        return f"{E.CHECK} {label}"
    if status == "creator":
        return f"{E.CROWN} {label}"
    return f"{E.CROSS} {label}"


async def _member(bot, chat_id: int, user_id: int):
    try:
        return await bot.get_chat_member(chat_id, user_id)
    except TelegramAPIError:
        return None


async def _is_admin(bot, chat_id: int, user_id: int) -> bool:
    member = await _member(bot, chat_id, user_id)
    return bool(member and member.status in ADMIN_STATUSES)


async def _answer(query, text: str | None = None, *, alert: bool = False) -> None:
    try:
        await query.answer(text or "", show_alert=alert)
    except TelegramAPIError:
        pass


async def _edit(query, text: str, markup=None) -> None:
    try:
        await query.message.edit_text(
            text, parse_mode=ParseMode.HTML, reply_markup=markup
        )
    except TelegramAPIError:
        pass  # "message is not modified" and stale-tap races are fine


async def _gate(query, bot, chat_id: int, user_id: int) -> bool:
    """Admin re-verification — called on EVERY callback press."""
    if await _is_admin(bot, chat_id, user_id):
        return True
    await _answer(query)
    return False


def _flat(markup):
    return [b for row in markup.inline_keyboard for b in row]


def _data(button) -> str:
    return getattr(button, "callback_data", None) or ""


# ── Waiting-input state (chat_data, per pressing admin) ──────────

def _wait_map(chat_data: dict) -> dict:
    return chat_data.setdefault("abox_wait", {})


def _set_waiting(chat_data: dict, user_id: int, action: str, mid: int,
                 panel_mid: int) -> None:
    _wait_map(chat_data)[user_id] = {
        "action": action,
        "mid": mid,
        "panel_mid": panel_mid,
        "expires": time.time() + WAIT_TTL,
    }


def _pop_waiting(chat_data: dict, user_id: int) -> dict | None:
    return _wait_map(chat_data).pop(user_id, None)


# ── Main panel ───────────────────────────────────────────────────

def _main_kb(mid: int):
    rows = [
        [
            btn_primary("Admins", f"{CB}:admins:{mid}", EID.ADMIN),
            btn_danger("Remove Admin", f"{CB}:admins:{mid}", EID.CROSS),
        ],
        [
            btn_success("Group Name", f"{CB}:name:{mid}", EID.SETTINGS),
            btn_success("Group Bio", f"{CB}:bio:{mid}", EID.BOOKMARK),
        ],
        [
            btn_success("Invite Link", f"{CB}:invite:{mid}", EID.GLOBE),
            btn_default("Group Info", f"{CB}:info:{mid}", EID.INFO),
        ],
        [
            btn_success("Permissions", f"{CB}:perms:{mid}", EID.CHECK),
            btn_success("Bot Status", f"{CB}:bot:{mid}", EID.ADMIN),
        ],
        [
            btn_primary("Message Tools", f"{CB}:tools:{mid}", EID.PIN),
            btn_primary("Group Settings", f"{CB}:settings:{mid}", EID.FOLDER),
        ],
        [
            btn_primary("Refresh", f"{CB}:main:{mid}", EID.CLOCK),
            btn_danger("Close", f"{CB}:close", EID.DISAPPROVE),
        ],
    ]
    return build_keyboard(rows)


async def _render_main(bot, chat_id: int, user_id: int, mid: int):
    title = "—"
    try:
        full = await bot.get_chat(chat_id)
        title = getattr(full, "title", None) or title
    except TelegramAPIError:
        pass

    try:
        members = f"{await bot.get_chat_member_count(chat_id):,}"
    except TelegramAPIError:
        members = "—"

    try:
        admins = await bot.get_chat_administrators(chat_id)
        admin_n = len(admins)
        for entry in admins:
            if getattr(entry, "status", None) == "creator":
                break
    except TelegramAPIError:
        admin_n = 0

    you = await _member(bot, chat_id, user_id)
    bot_member = await _member(bot, chat_id, getattr(bot, "id", 0))

    if bot_member is None:
        bot_line = f"{E.CROSS} Not in this group"
    elif bot_member.status in ADMIN_STATUSES:
        bot_line = f"{E.CHECK} Administrator"
    else:
        bot_line = f"{E.WARNING} Not an admin — promote me"

    text = action_card(
        "Admin Panel",
        [
            (E.USER, "Group", escape(title)),
            (E.ADMIN, "Members", members),
            (E.CROWN, "Admins", str(admin_n)),
            (E.INFO, "You", _member_label(you) if you else "Unknown"),
            (E.GLOBE, "Bot", bot_line),
        ],
        icon=E.CROWN,
    )
    return text, _main_kb(mid)


# ── Sub-view: admins + demote ────────────────────────────────────

async def _view_admins(query, bot, chat_id: int, mid: int) -> None:
    try:
        admins = await bot.get_chat_administrators(chat_id)
    except TelegramAPIError:
        admins = []

    rows: list[list[InlineKeyboardButton]] = []
    owner_name = "—"
    admin_count = 0
    for entry in admins:
        user = getattr(entry, "user", None)
        name = _plain_name(user)
        if getattr(entry, "status", None) == "creator":
            owner_name = name
            rows.append([btn_default(f"{name} · Owner", f"{CB}:noop", EID.CROWN)])
            continue
        admin_count += 1
        uid = getattr(user, "id", 0)
        presser_id = getattr(getattr(query, "from_user", None), "id", None)
        if getattr(user, "is_bot", False) and uid == getattr(bot, "id", 0):
            # This bot — protected from demotion, no Remove button.
            rows.append([btn_default(f"{name} · Bot", f"{CB}:noop", EID.ADMIN)])
            continue
        if uid == presser_id:
            # Yourself — protected (would lock the presser out).
            rows.append([btn_default(f"{name} · You", f"{CB}:noop", EID.ADMIN)])
            continue
        rows.append([
            btn_default(name, f"{CB}:noop", EID.ADMIN),
            btn_danger("Remove", f"{CB}:demote:{uid}:{mid}", EID.CROSS),
        ])

    rows.append([
        btn_primary("Refresh", f"{CB}:admins:{mid}", EID.CLOCK),
        btn_default("Back", f"{CB}:main:{mid}"),
    ])

    text = action_card(
        "Admin List",
        [
            (E.CROWN, "Owner", escape(owner_name)),
            (E.ADMIN, "Admins", str(admin_count)),
            (E.INFO, "Protected", "Owner, this bot and yourself"),
        ],
        icon=E.ADMIN,
    )
    await _edit(query, text, build_keyboard(rows))


async def _do_demote(query, bot, chat_id: int, presser_id: int,
                     target_id: int, mid: int) -> None:
    if target_id <= 0:
        await _answer(query, "Invalid admin.", alert=True)
        return
    if target_id == presser_id:
        await _answer(query, "You can't demote yourself.", alert=True)
        return
    if target_id == getattr(bot, "id", 0):
        await _answer(query, "I can't demote myself.", alert=True)
        return

    target = await _member(bot, chat_id, target_id)
    if target is None:
        await _answer(query, "Could not find that member.", alert=True)
        return
    if getattr(target, "status", None) == "creator":
        await _answer(query, "The group owner can't be demoted.", alert=True)
        return
    if getattr(target, "status", None) not in ADMIN_STATUSES:
        await _answer(query, "They are not an admin anymore.", alert=True)
        return

    bot_member = await _member(bot, chat_id, getattr(bot, "id", 0))
    if not (
        bot_member
        and bot_member.status in ADMIN_STATUSES
        and getattr(bot_member, "can_promote_members", False)
    ):
        await _answer(
            query, "I need the Promote members permission first.", alert=True
        )
        return

    try:
        await bot.promote_chat_member(
            chat_id,
            target_id,
            can_change_info=False,
            can_post_messages=False,
            can_edit_messages=False,
            can_delete_messages=False,
            can_invite_users=False,
            can_restrict_members=False,
            can_pin_messages=False,
            can_promote_members=False,
            is_anonymous=False,
            can_manage_chat=False,
            can_manage_video_chats=False,
            can_manage_topics=False,
        )
    except TelegramAPIError as exc:
        await _answer(
            query, f"Could not demote: {str(exc)[:120]}", alert=True
        )
        return

    await _answer(query, f"Demoted {_plain_name(getattr(target, 'user', None))}.")
    await _view_admins(query, bot, chat_id, mid)


# ── Sub-view: invite / info / permissions / bot status ───────────

def _copy_btn(link: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text="Copy Link",
        callback_data=f"{CB}:noop",
        copy_text=CopyTextButton(text=link),
    )


async def _view_invite(query, bot, chat_id: int, mid: int) -> None:
    link = None
    try:
        full = await bot.get_chat(chat_id)
        link = getattr(full, "invite_link", None)
    except TelegramAPIError:
        pass

    link_value = f"<code>{escape(link)}</code>" if link else f"{E.INFO} None yet — tap Generate New"
    text = action_card(
        "Invite Link",
        [
            (E.GLOBE, "Link", link_value),
            (E.INFO, "Share", "Send this link to invite new members"),
        ],
        icon=E.GLOBE,
    )

    rows = [[
        btn_primary("Generate New", f"{CB}:invitegen:{mid}", EID.PLUS),
        btn_default("Refresh", f"{CB}:invite:{mid}", EID.CLOCK),
    ]]
    if link:
        rows.insert(0, [_copy_btn(link)])
    rows.append([btn_default("Back", f"{CB}:main:{mid}")])
    await _edit(query, text, build_keyboard(rows))


async def _gen_invite(query, bot, chat_id: int, mid: int, *, request: bool) -> None:
    try:
        if request:
            resp = await bot.create_chat_invite_link(
                chat_id, name="Admin Panel join request",
                creates_join_request=True,
            )
        else:
            resp = await bot.create_chat_invite_link(chat_id, name="Admin Panel")
    except TelegramAPIError as exc:
        await _answer(query, f"Could not create link: {str(exc)[:120]}", alert=True)
        return

    link = getattr(resp, "invite_link", None)
    await _answer(query, "New link created." if not request else "Join-request link created.")
    if not link:
        return

    if request:
        text = action_card(
            "Join-Request Invite",
            [
                (E.GLOBE, "Link", f"<code>{escape(link)}</code>"),
                (E.WAVE, "Flow", "New members wait for your approval"),
                (E.INFO, "Manage", "Approve or decline from the join-request queue"),
            ],
            icon=E.CHECK,
        )
        kb = build_keyboard([
            [_copy_btn(link)],
            [
                btn_primary("Refresh", f"{CB}:joinlink:{mid}", EID.CLOCK),
                btn_default("Back", f"{CB}:settings:{mid}"),
            ],
        ])
    else:
        text = action_card(
            "Invite Link",
            [
                (E.GLOBE, "Link", f"<code>{escape(link)}</code>"),
                (E.INFO, "Share", "Send this link to invite new members"),
            ],
            icon=E.GLOBE,
        )
        kb = build_keyboard([
            [_copy_btn(link)],
            [
                btn_primary("Generate New", f"{CB}:invitegen:{mid}", EID.PLUS),
                btn_default("Back", f"{CB}:main:{mid}"),
            ],
        ])
    await _edit(query, text, kb)


async def _view_info(query, bot, chat_id: int, mid: int) -> None:
    title = "—"
    try:
        full = await bot.get_chat(chat_id)
        title = getattr(full, "title", None) or title
    except TelegramAPIError:
        pass
    try:
        members = f"{await bot.get_chat_member_count(chat_id):,}"
    except TelegramAPIError:
        members = "—"

    owner_name = "—"
    admin_n = 0
    try:
        admins = await bot.get_chat_administrators(chat_id)
        admin_n = len(admins)
        for entry in admins:
            if getattr(entry, "status", None) == "creator":
                owner_name = _plain_name(getattr(entry, "user", None))
                break
    except TelegramAPIError:
        pass

    text = action_card(
        "Group Info",
        [
            (E.USER, "Group", escape(title)),
            (E.GLOBE, "ID", f"<code>{chat_id}</code>"),
            (E.ADMIN, "Members", members),
            (E.CROWN, "Admins", str(admin_n)),
            (E.CROWN, "Owner", escape(owner_name)),
        ],
        icon=E.INFO,
    )
    kb = build_keyboard([
        [
            btn_primary("Refresh", f"{CB}:info:{mid}", EID.CLOCK),
            btn_default("Back", f"{CB}:main:{mid}"),
        ],
    ])
    await _edit(query, text, kb)


async def _view_perms(query, bot, chat_id: int, user_id: int, mid: int) -> None:
    member = await _member(bot, chat_id, user_id)
    if member is None:
        await _edit(query, error_card("Permissions", "Could not read your status."))
        return

    if getattr(member, "status", None) == "creator":
        fields = [
            (E.INFO, "Status", f"{E.CROWN} Owner"),
            (E.CROWN, "Rights", f"{E.CHECK} Full — you own this group"),
        ]
    else:
        fields = [(E.INFO, "Status", _member_label(member))] + _rights_fields(member)

    kb = build_keyboard([
        [
            btn_primary("Refresh", f"{CB}:perms:{mid}", EID.CLOCK),
            btn_default("Back", f"{CB}:main:{mid}"),
        ],
    ])
    await _edit(query, action_card("Your Permissions", fields, icon=E.CHECK), kb)


async def _view_bot(query, bot, chat_id: int, mid: int) -> None:
    username = getattr(bot, "username", None) or "?"
    bot_member = await _member(bot, chat_id, getattr(bot, "id", 0))

    fields = [
        (E.USER, "Bot", f"@{escape(username)}"),
        (E.CHECK, "Connection", "Online"),
    ]
    if bot_member is None:
        fields.append((E.WARNING, "Status", "Not in this group — add me first"))
    elif bot_member.status in ADMIN_STATUSES:
        fields.append((E.CROWN, "Administrator", f"{E.CHECK} Yes"))
        fields += _rights_fields(bot_member)
    else:
        fields.append((E.WARNING, "Administrator", f"{E.CROSS} No — promote me"))

    kb = build_keyboard([
        [
            btn_primary("Refresh", f"{CB}:bot:{mid}", EID.CLOCK),
            btn_default("Back", f"{CB}:main:{mid}"),
        ],
    ])
    await _edit(query, action_card("Bot Status", fields, icon=E.GLOBE), kb)


# ── Sub-view: message tools ──────────────────────────────────────

async def _view_tools(query, bot, chat_id: int, mid: int) -> None:
    target_value = (
        f"{E.CHECK} Set — the message you replied to"
        if mid
        else f"{E.CROSS} Reply to a message with /adminbox first"
    )
    text = action_card(
        "Message Tools",
        [
            (E.PIN, "Reply Target", target_value),
            (E.ANNOUNCE, "Broadcast", "Bot posts your text to this group"),
            (E.INFO, "Purge", f"Deletes up to {PURGE_MAX} recent messages seen by the bot"),
        ],
        icon=E.PIN,
    )
    rows = [
        [
            btn_primary("Pin", f"{CB}:pin:{mid}", EID.PIN),
            btn_primary("Unpin", f"{CB}:unpin:{mid}", EID.LOCATION),
        ],
        [
            btn_danger("Delete", f"{CB}:del:{mid}", EID.CROSS),
            btn_danger("Purge", f"{CB}:purge:{mid}", EID.DISAPPROVE),
        ],
        [btn_success("Broadcast", f"{CB}:bcast:{mid}", EID.ANNOUNCE)],
        [
            btn_primary("Refresh", f"{CB}:tools:{mid}", EID.CLOCK),
            btn_default("Back", f"{CB}:main:{mid}"),
        ],
    ]
    await _edit(query, text, build_keyboard(rows))


async def _reply_op(query, bot, chat_id: int, action: str, target: int) -> None:
    if not target:
        await _answer(
            query, "Reply to a message with /adminbox first.", alert=True
        )
        return
    try:
        if action == "pin":
            await bot.pin_chat_message(chat_id, target, disable_notification=True)
            await _answer(query, "Pinned.")
        elif action == "unpin":
            await bot.unpin_chat_message(chat_id, target)
            await _answer(query, "Unpinned.")
        else:  # del
            await bot.delete_message(chat_id, target)
            await _answer(query, "Message deleted.")
    except TelegramAPIError as exc:
        await _answer(query, str(exc)[:140], alert=True)


async def _do_purge(query, bot, chat_id: int, chat_data: dict) -> None:
    recent = list(chat_data.get("abox_recent") or [])
    ids = recent[-PURGE_MAX:]
    if not ids:
        await _answer(query, "No recent messages tracked yet.", alert=True)
        return
    try:
        await bot.delete_messages(chat_id, ids)
    except TelegramAPIError as exc:
        await _answer(query, str(exc)[:140], alert=True)
        return
    await _answer(query, f"Deleted {len(ids)} recent message(s).")


# ── Sub-view: group settings ─────────────────────────────────────

def _slow_label(delay) -> str:
    try:
        delay = int(delay or 0)
    except (TypeError, ValueError):
        delay = 0
    return "Off" if delay <= 0 else f"{delay}s"


async def _view_settings(query, bot, chat_id: int, mid: int) -> None:
    full = None
    try:
        full = await bot.get_chat(chat_id)
    except TelegramAPIError:
        pass

    slow = _slow_label(getattr(full, "slow_mode_delay", None))
    perms = getattr(full, "permissions", None)
    if perms is None:
        perm_line = f"{E.INFO} Default (everyone can post)"
    else:
        allowed = sum(
            1 for _, attr in _DEFAULT_RIGHTS if getattr(perms, attr, None)
        )
        perm_line = f"{E.CHECK} {allowed}/{len(_DEFAULT_RIGHTS)} allowed"

    text = action_card(
        "Group Settings",
        [
            (E.CLOCK, "Slow Mode", slow),
            (E.ADMIN, "Default Permissions", perm_line),
            (E.INFO, "Note", "Slow mode is changed in Telegram → Group Info"),
            (E.WAVE, "Join Requests", "Generate a join-request invite below"),
        ],
        icon=E.FOLDER,
    )
    kb = build_keyboard([
        [
            btn_success("Join-Request Link", f"{CB}:joinlink:{mid}", EID.GLOBE),
            btn_success("Default Perms", f"{CB}:dperms:{mid}", EID.CHECK),
        ],
        [
            btn_primary("Refresh", f"{CB}:settings:{mid}", EID.CLOCK),
            btn_default("Back", f"{CB}:main:{mid}"),
        ],
    ])
    await _edit(query, text, kb)


async def _view_dperms(query, bot, chat_id: int, mid: int) -> None:
    perms = None
    try:
        full = await bot.get_chat(chat_id)
        perms = getattr(full, "permissions", None)
    except TelegramAPIError:
        pass

    if perms is None:
        fields = [(E.INFO, "Status", f"{E.CHECK} Default (everyone can post)")]
    else:
        fields = [
            (E.ADMIN, label, _yn(getattr(perms, attr, None)))
            for label, attr in _DEFAULT_RIGHTS
        ]

    kb = build_keyboard([
        [
            btn_primary("Refresh", f"{CB}:dperms:{mid}", EID.CLOCK),
            btn_default("Back", f"{CB}:settings:{mid}"),
        ],
    ])
    await _edit(query, action_card("Default Permissions", fields, icon=E.CHECK), kb)


# ── Waiting-input prompts ────────────────────────────────────────

def _prompt_text(action: str) -> str:
    icon, title, hint = _PROMPTS[action]
    return action_card(
        title,
        [
            (E.INFO, "Action", hint),
            (E.TIME, "Waiting", f"For your message — expires in {int(WAIT_TTL // 60)} minutes"),
            (E.INFO, "Cancel", "Tap Cancel below to go back"),
        ],
        icon=icon,
    )


def _prompt_kb():
    return build_keyboard([
        [btn_danger("Cancel", f"{CB}:cancel", EID.DISAPPROVE)],
    ])


async def _revert_panel(bot, chat_id: int, panel_mid: int,
                        user_id: int, mid: int) -> None:
    if not panel_mid:
        return
    try:
        text, markup = await _render_main(bot, chat_id, user_id, mid)
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=panel_mid,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
        )
    except TelegramAPIError:
        pass


async def _run_waiting(message: Message, bot: Bot, chat_data: dict,
                       entry: dict, text: str) -> None:
    """Consume waiting input: set name / bio, or broadcast."""
    chat_id = message.chat.id
    action = entry["action"]
    uid = message.from_user.id

    if action == "name":
        title = text.strip()
        if not (1 <= len(title) <= 128):
            await reply_text(message,
                error_card("Group Name", "Name must be 1–128 characters."),
                parse_mode=ParseMode.HTML,
            )
            return  # keep waiting — let them retry
        try:
            await bot.set_chat_title(chat_id, title)
        except TelegramAPIError as exc:
            await reply_text(message,
                error_card("Group Name", str(exc)[:150]),
                parse_mode=ParseMode.HTML,
            )
            return
        success = success_card(
            "Group Name Updated",
            [
                (E.INFO, "New", f"「{escape(title)}」"),
                (E.SUCCESS, "Status", "Applied to this group"),
            ],
        )

    elif action == "bio":
        description = text.strip()
        if len(description) > 255:
            await reply_text(message,
                error_card("Group Bio", "Description must be ≤ 255 characters."),
                parse_mode=ParseMode.HTML,
            )
            return
        try:
            await bot.set_chat_description(chat_id, description)
        except TelegramAPIError as exc:
            await reply_text(message,
                error_card("Group Bio", str(exc)[:150]),
                parse_mode=ParseMode.HTML,
            )
            return
        success = success_card(
            "Group Bio Updated",
            [
                (E.INFO, "Status", "Group description updated"),
                (E.SUCCESS, "Applied", "Visible in Group Info"),
            ],
        )

    else:  # bcast
        body = text.strip()
        if not body:
            await reply_text(message,
                error_card("Broadcast", "Send non-empty text."),
                parse_mode=ParseMode.HTML,
            )
            return
        if len(body) > 4096:
            await reply_text(message,
                error_card("Broadcast", "Message must be ≤ 4096 characters."),
                parse_mode=ParseMode.HTML,
            )
            return
        try:
            await bot.send_message(chat_id, body)
        except TelegramAPIError as exc:
            await reply_text(message,
                error_card("Broadcast", str(exc)[:150]),
                parse_mode=ParseMode.HTML,
            )
            return
        success = success_card(
            "Broadcast Sent",
            [
                (E.ANNOUNCE, "Delivered", "Posted to this group"),
                (E.SUCCESS, "Status", "Sent successfully"),
            ],
        )

    # Success — retire the prompt and restore the panel.
    _pop_waiting(chat_data, uid)
    await _revert_panel(
        bot, chat_id, entry.get("panel_mid", 0), uid, entry.get("mid", 0)
    )
    await reply_text(message,
        success,
        parse_mode=ParseMode.HTML,
        reply_markup=build_keyboard([
            [btn_default("Back to Panel", f"{CB}:backdel")],
        ]),
    )


# ── Handlers ─────────────────────────────────────────────────────

async def adminbox_command(message: Message, bot: Bot, chat_data: dict) -> None:
    """/adminbox — open the inline admin panel (groups, admins only)."""
    chat = message.chat
    user = message.from_user
    if chat.type == "private":
        await reply_text(message,
            f"{E.INFO} Open the panel in a group.", parse_mode=ParseMode.HTML
        )
        return
    if not await _is_admin(bot, chat.id, user.id):
        return

    # A fresh panel retires any stale prompt this admin had open.
    _pop_waiting(chat_data, user.id)

    reply = message.reply_to_message
    mid = reply.message_id if reply else 0
    text, markup = await _render_main(bot, chat.id, user.id, mid)
    await reply_text(message,
        text, reply_markup=markup, parse_mode=ParseMode.HTML
    )


async def adminbox_callback(callback_query: CallbackQuery, bot: Bot, chat_data: dict) -> None:
    """All ``abox:`` presses — admin status re-verified on every call."""
    query = callback_query
    data = query.data or ""
    if not data.startswith(f"{CB}:"):
        return
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    chat = query.message.chat if query.message else None
    user = query.from_user
    if chat is None or user is None:
        return
    chat_id, uid = chat.id, user.id

    if action == "noop":
        await _answer(query)
        return

    # Spec: sensitive actions verify current admin status again here.
    if not await _gate(query, bot, chat_id, uid):
        return

    def _int_at(index: int, default: int = 0) -> int:
        try:
            return int(parts[index])
        except (IndexError, ValueError):
            return default

    mid = _int_at(2, 0)

    if action == "main":
        text, markup = await _render_main(bot, chat_id, uid, mid)
        await _edit(query, text, markup)
    elif action == "admins":
        await _view_admins(query, bot, chat_id, mid)
    elif action == "demote":
        await _do_demote(query, bot, chat_id, uid, _int_at(2, 0), _int_at(3, 0))
    elif action in ("name", "bio", "bcast"):
        panel_mid = query.message.message_id if query.message else 0
        _set_waiting(chat_data, uid, action, mid, panel_mid)
        await _edit(query, _prompt_text(action), _prompt_kb())
    elif action == "cancel":
        entry = _pop_waiting(chat_data, uid)
        back_mid = (entry or {}).get("mid", 0)
        text, markup = await _render_main(bot, chat_id, uid, back_mid)
        await _edit(query, text, markup)
    elif action == "close":
        _pop_waiting(chat_data, uid)
        if query.message:
            try:
                await query.message.delete()
            except TelegramAPIError:
                pass
        await _answer(query, "Panel closed.")
    elif action == "backdel":
        if query.message:
            try:
                await query.message.delete()
            except TelegramAPIError:
                pass
        await _answer(query)
    elif action == "invite":
        await _view_invite(query, bot, chat_id, mid)
    elif action == "invitegen":
        await _gen_invite(query, bot, chat_id, mid, request=False)
    elif action == "joinlink":
        await _gen_invite(query, bot, chat_id, mid, request=True)
    elif action == "info":
        await _view_info(query, bot, chat_id, mid)
    elif action == "perms":
        await _view_perms(query, bot, chat_id, uid, mid)
    elif action == "bot":
        await _view_bot(query, bot, chat_id, mid)
    elif action == "tools":
        await _view_tools(query, bot, chat_id, mid)
    elif action in ("pin", "unpin", "del"):
        await _reply_op(query, bot, chat_id, action, mid)
    elif action == "purge":
        await _do_purge(query, bot, chat_id, chat_data)
    elif action == "settings":
        await _view_settings(query, bot, chat_id, mid)
    elif action == "dperms":
        await _view_dperms(query, bot, chat_id, mid)
    # unknown actions stay silent (stale taps)


async def adminbox_message(message: Message, bot: Bot, chat_data: dict) -> None:
    """Group message hook (group 21): purge tracking + waiting input."""
    chat = message.chat
    if not message or not chat or chat.type == "private":
        return

    # 1) Track deletable recent ids for the purge action.
    sender = message.from_user
    if sender and not sender.is_bot and not any(
        getattr(message, marker, None) for marker in _SERVICE_MARKERS
    ):
        recent = chat_data.get("abox_recent")
        if recent is None:
            recent = deque(maxlen=RECENT_CAP)
            chat_data["abox_recent"] = recent
        recent.append(message.message_id)

    # 2) Waiting-input consumption.
    wait_map = chat_data.get("abox_wait")
    if not wait_map:
        return
    user = message.from_user
    if not user:
        return
    entry = wait_map.get(user.id)
    if not entry:
        return

    text = message.text or ""

    # A new command abandons the prompt (the command re-opens cleanly).
    if parse_command(text):
        _pop_waiting(chat_data, user.id)
        await _revert_panel(
            bot, chat.id, entry.get("panel_mid", 0), user.id,
            entry.get("mid", 0),
        )
        return

    if time.time() > entry["expires"]:
        _pop_waiting(chat_data, user.id)
        await _revert_panel(
            bot, chat.id, entry.get("panel_mid", 0), user.id,
            entry.get("mid", 0),
        )
        return

    # Demoted while typing → prompt dies with the rights.
    if not await _is_admin(bot, chat.id, user.id):
        _pop_waiting(chat_data, user.id)
        return

    if not text:
        return
    await _run_waiting(message, bot, chat_data, entry, text)


# ── Registration ─────────────────────────────────────────────────

def setup() -> list[str]:
    """Register /adminbox, its callback grid and the message hook."""
    on("message", adminbox_command, flt=cmd("adminbox"))
    on("callback_query", adminbox_callback, flt=F.data.regexp(re.compile(r"^abox:")))
    on("message", adminbox_message, group=MESSAGE_GROUP)
    return ["/adminbox", "abox:* callbacks", f"message hook (group {MESSAGE_GROUP})"]
