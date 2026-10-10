"""Bind module configuration — constants, gate labels, defaults."""

# Force-join / message-gate enforcement.
#
# NEGATIVE, so it dispatches before group 0 and therefore before every
# command handler, every counter and every XP tracker.  The gate does two
# things when it blocks: it deletes the message, and it raises
# bot.pipeline.StopChain so nothing later in the chain (help menus,
# leaderboards, flood state, message counts) ever sees the update.  It
# still sits clear of filters(1)/blocklist(2)/watchwords(3), which run
# after it and simply never fire for a gated message.
HANDLER_GROUP = -1
# Waiting-text input. Was HANDLER_GROUP + 1 = 5, which collided with
# leveling's XP tracker (group 5).  20 is free (tagging tops out at 17);
# comfortably after the gate, so non-member text never reaches it.
WAITING_TEXT_GROUP = 20
# Separate group so join tracking never collides with welcome (group=10).
JOIN_TRACKER_GROUP = 11

# Membership cache TTL, in seconds.
#
# This is also the leave-detection window: Telegram never pushes a "user
# left the channel" event, so the gate only finds out when it re-checks on
# the next message.  Any message sent inside this window after leaving can
# therefore still slip through — keep it short.  15s bounds the damage to
# a single stray message while still collapsing bursts (two messages from
# the same user in quick succession cost one getChatMember call).
#
# The "I've Joined" button always bypasses the cache, so rejoining is
# honoured immediately regardless of this value.
MEMBERSHIP_CACHE_TTL = 15

# Grace period options in minutes (0 = OFF).
GRACE_OPTIONS = (0, 1, 5, 10, 15, 30, 60)

# Auto-delete warning delay in seconds (0 = OFF / keep forever).
AUTO_DELETE_OPTIONS = (0, 5, 10, 15, 30)

# Toggleable message gates → column name on bind_settings.
# ``other`` is the catch-all: polls, contacts, venues, locations, dice,
# games, invoices, giveaways, stories and anything Telegram adds next.
# Without it ``message_gates()`` returned an empty set for those, so a
# gate-only chat (force_join OFF) let them straight through while text
# was blocked.  Every key here must also exist in ``_DEFAULTS`` /
# ``_INT_COLS`` / ``_update_field``'s allow-list in database.py and in
# the ``gate_enabled`` map in checks.py — the menu and the status line
# read GATES/GATE_LABELS directly, so they pick new keys up by themselves.
GATES = {
    "text": "gate_text",
    "media": "gate_media",
    "link": "gate_link",
    "document": "gate_document",
    "gif": "gate_gif",
    "audio": "gate_audio",
    "sticker": "gate_sticker",
    "other": "gate_other",
}

GATE_LABELS = {
    "text": "Text",
    "media": "Media (photo/video)",
    "link": "Links",
    "document": "Documents",
    "gif": "GIF / Animation",
    "audio": "Audio / Voice",
    "sticker": "Stickers",
    "other": "Other (polls, contacts, dice)",
}

# Statuses that count as a valid channel member.
PASS_STATUSES = frozenset({"creator", "administrator", "member"})

# Fail statuses (spec: restricted/left/kicked fail).
FAIL_STATUSES = frozenset({"restricted", "left", "kicked", "banned"})

DEFAULT_CUSTOM_MESSAGE = (
    "{user}, you must join {channel} before chatting in {group}."
)

# Placeholders available in the custom force-join message.
PLACEHOLDERS = ("{user}", "{user_id}", "{group}", "{channel}", "{channel_link}")

PLACEHOLDERS_HELP = " • ".join(PLACEHOLDERS)

# chat_data keys for waiting-on-input flows.
WAIT_CUSTOM = "bind_wait_custom"
WAIT_CHANNEL = "bind_wait_channel"

# Callback prefix (CallbackQueryHandler pattern ^bind:)
CB_PREFIX = "bind"

# Indicator strings for menu rows.
# Button labels are PLAIN TEXT: no emoji at all. The only emoji a bind
# button may show is the owner's custom emoji, which Telegram renders
# from the button's icon_custom_emoji_id — a Unicode emoji in the label
# would be drawn from Telegram's stock set, not from the owner's pack.
# Message HTML is different: use E.CHECK / E.ERROR / E.ANNOUNCE (<tg-emoji>).
ON = "ON"
OFF = "OFF"
