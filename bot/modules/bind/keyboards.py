"""Bind module keyboards — inline menus with owner brand emoji only.

Button labels are plain text, so they use only the fallback characters from
the owner's custom set (bot/emojis.py). The visual brand icon comes from
icon_emoji_id (EID.*). Message HTML must use E.* (<tg-emoji>), never stock
Unicode outside that set.
"""

from typing import Any, Dict, List, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from bot.emojis import EID
from bot.keyboards.colored import (
    btn_danger,
    btn_primary,
    btn_success,
    btn_url,
    build_keyboard,
)

from .config import (
    AUTO_DELETE_OPTIONS,
    CB_PREFIX,
    GATE_LABELS,
    GATES,
    GRACE_OPTIONS,
)
from .utils import format_autodel, format_grace, indicator


def _back_close(extra_rows: Optional[List[List[InlineKeyboardButton]]] = None) -> List[List[InlineKeyboardButton]]:
    rows = list(extra_rows or [])
    rows.append(
        [
            btn_primary("Back", f"{CB_PREFIX}:menu", icon_emoji_id=EID.INFO),
            btn_danger("❌ Close", f"{CB_PREFIX}:close", icon_emoji_id=EID.CROSS),
        ]
    )
    return rows


def _refresh_row() -> List[InlineKeyboardButton]:
    return [
        btn_success("✔ Refresh", f"{CB_PREFIX}:refresh", icon_emoji_id=EID.CHECK),
    ]


def bind_main_menu(settings: Optional[Dict[str, Any]], *, group_title: str = "") -> InlineKeyboardMarkup:
    """Main /bindmenu panel."""
    if not settings:
        rows = [
            [
                btn_primary(
                    "Bind Channel",
                    f"{CB_PREFIX}:help_bind",
                    icon_emoji_id=EID.ADD,
                )
            ],
            [
                btn_primary("How To Bind", f"{CB_PREFIX}:help_bind", icon_emoji_id=EID.INFO),
            ],
            _refresh_row(),
            [
                btn_danger("❌ Close", f"{CB_PREFIX}:close", icon_emoji_id=EID.CROSS),
            ],
        ]
        return build_keyboard(rows)

    force = indicator(settings.get("force_join"))
    bypass = indicator(settings.get("admin_bypass"))

    rows: List[List[InlineKeyboardButton]] = [
        [
            btn_primary(
                f"Force Join — {force}",
                f"{CB_PREFIX}:toggle:force_join",
                icon_emoji_id=EID.ANNOUNCE,
            )
        ],
        [
            btn_primary(
                f"Admin Bypass — {bypass}",
                f"{CB_PREFIX}:toggle:admin_bypass",
                icon_emoji_id=EID.CROWN,
            )
        ],
        [
            btn_success("Gates", f"{CB_PREFIX}:gates", icon_emoji_id=EID.SETTINGS),
            btn_primary(
                f"Grace — {format_grace(int(settings.get('grace_minutes') or 0))}",
                f"{CB_PREFIX}:grace",
                icon_emoji_id=EID.CLOCK,
            ),
        ],
        [
            btn_primary(
                f"Auto-Delete — {format_autodel(int(settings.get('auto_delete_seconds') or 0))}",
                f"{CB_PREFIX}:autodel",
                icon_emoji_id=EID.TIME,
            ),
            btn_primary("Custom Message", f"{CB_PREFIX}:custom", icon_emoji_id=EID.INFO),
        ],
        [
            btn_primary("Change Channel", f"{CB_PREFIX}:change", icon_emoji_id=EID.WEB),
            btn_primary("Status", f"{CB_PREFIX}:status", icon_emoji_id=EID.WATCH),
        ],
        _refresh_row(),
        [
            btn_danger("‼️ Unbind", f"{CB_PREFIX}:unbind", icon_emoji_id=EID.WARNING),
            btn_danger("❌ Close", f"{CB_PREFIX}:close", icon_emoji_id=EID.CROSS),
        ],
    ]
    return build_keyboard(rows)


def gates_menu(settings: Dict[str, Any]) -> InlineKeyboardMarkup:
    """Per-type message restriction toggles."""
    rows: List[List[InlineKeyboardButton]] = []
    keys = list(GATES.keys())
    for i in range(0, len(keys), 2):
        pair = keys[i : i + 2]
        row = []
        for key in pair:
            on = indicator(settings.get(GATES[key]))
            row.append(
                btn_primary(
                    f"{GATE_LABELS[key]} — {on}",
                    f"{CB_PREFIX}:gate:{key}",
                    icon_emoji_id=EID.SETTINGS,
                )
            )
        rows.append(row)

    rows.append(_refresh_row())
    rows.extend(_back_close())
    return build_keyboard(rows)


def grace_menu(settings: Dict[str, Any]) -> InlineKeyboardMarkup:
    current = int(settings.get("grace_minutes") or 0)
    rows: List[List[InlineKeyboardButton]] = []
    for minutes in GRACE_OPTIONS:
        label = "OFF" if minutes == 0 else f"{minutes} min"
        mark = " ✅" if minutes == current else ""
        icon = EID.CROSS if minutes == 0 else EID.CLOCK
        rows.append(
            [
                btn_primary(
                    f"{label}{mark}",
                    f"{CB_PREFIX}:grace_set:{minutes}",
                    icon_emoji_id=icon,
                )
            ]
        )
    rows.extend(_back_close())
    return build_keyboard(rows)


def autodel_menu(settings: Dict[str, Any]) -> InlineKeyboardMarkup:
    current = int(settings.get("auto_delete_seconds") or 0)
    rows: List[List[InlineKeyboardButton]] = []
    for secs in AUTO_DELETE_OPTIONS:
        label = "OFF (keep)" if secs == 0 else f"{secs} sec"
        mark = " ✅" if secs == current else ""
        icon = EID.CROSS if secs == 0 else EID.TIME
        rows.append(
            [
                btn_primary(
                    f"{label}{mark}",
                    f"{CB_PREFIX}:autodel_set:{secs}",
                    icon_emoji_id=icon,
                )
            ]
        )
    rows.extend(_back_close())
    return build_keyboard(rows)


def custom_menu(settings: Dict[str, Any]) -> InlineKeyboardMarkup:
    rows = [
        [
            btn_primary("Set Message", f"{CB_PREFIX}:custom_set", icon_emoji_id=EID.INFO),
            btn_danger("Reset", f"{CB_PREFIX}:custom_reset", icon_emoji_id=EID.CROSS),
        ],
        [
            btn_success("Preview", f"{CB_PREFIX}:custom_preview", icon_emoji_id=EID.EYES),
        ],
    ]
    rows.extend(_back_close())
    return build_keyboard(rows)


def status_menu(settings: Dict[str, Any]) -> InlineKeyboardMarkup:
    rows = [_refresh_row()]
    rows.extend(_back_close())
    return build_keyboard(rows)


def unbind_confirm_menu() -> InlineKeyboardMarkup:
    return build_keyboard(
        [
            [
                btn_success("✅ Yes, Unbind", f"{CB_PREFIX}:unbind_yes", icon_emoji_id=EID.CHECK),
                btn_primary("Keep Binding", f"{CB_PREFIX}:menu", icon_emoji_id=EID.INFO),
            ],
            [
                btn_danger("❌ Cancel", f"{CB_PREFIX}:close", icon_emoji_id=EID.CROSS),
            ],
        ]
    )


def replace_confirm_menu() -> InlineKeyboardMarkup:
    return build_keyboard(
        [
            [
                btn_success("✅ Replace", f"{CB_PREFIX}:replace_yes", icon_emoji_id=EID.CHECK),
                btn_primary("Cancel", f"{CB_PREFIX}:menu", icon_emoji_id=EID.INFO),
            ]
        ]
    )


def force_join_keyboard(channel_link: str, channel_title: str) -> InlineKeyboardMarkup:
    """Member-facing gate prompt: join URL + I've Joined."""
    rows: List[List[InlineKeyboardButton]] = []
    if channel_link:
        rows.append(
            [
                btn_url(
                    "📣 Join Channel",
                    channel_link,
                    icon_emoji_id=EID.ANNOUNCE,
                )
            ]
        )
    rows.append(
        [
            btn_success(
                "✅ I've Joined",
                f"{CB_PREFIX}:join",
                icon_emoji_id=EID.CHECK,
            )
        ]
    )
    return build_keyboard(rows)


def help_bind_text() -> str:
    from bot.emojis import E

    return (
        f"{E.INFO} <b>How to bind</b>\n\n"
        "Run in your group:\n"
        "<code>/bind @ChannelName</code>\n"
        "or\n"
        "<code>/bind https://t.me/ChannelName</code>\n"
        "or a numeric id:\n"
        "<code>/bind -1001234567890</code>\n\n"
        f"{E.SETTINGS} Then open <code>/bindmenu</code> to configure gates, "
        "grace period, auto-delete, and the custom message.\n\n"
        f"{E.WARNING} One channel can be bound to only one group at a time.\n\n"
        "The bot must be able to see the channel (add it as admin) to verify membership."
    )
