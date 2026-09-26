"""Bind module configuration — constants, gate labels, defaults."""

# MessageHandler group: filters=1, blocklist=2, watchwords=3 → bind=4
HANDLER_GROUP = 4
# Waiting-text input. Was HANDLER_GROUP + 1 = 5, which collided with
# leveling's XP tracker (group 5) — PTB runs ONE handler per group and
# bind loads before leveling, so XP never ran in groups. 20 is free
# (tagging tops out at 17); created right after group 4, so it still
# runs after the gates.
WAITING_TEXT_GROUP = 20
# Separate group so join tracking never collides with welcome (group=10).
JOIN_TRACKER_GROUP = 11

# Membership cache TTL (spec: 30–60s; "I've Joined" bypasses cache).
MEMBERSHIP_CACHE_TTL = 45

# Grace period options in minutes (0 = OFF).
GRACE_OPTIONS = (0, 1, 5, 10, 15, 30, 60)

# Auto-delete warning delay in seconds (0 = OFF / keep forever).
AUTO_DELETE_OPTIONS = (0, 5, 10, 15, 30)

# Toggleable message gates → column name on bind_settings.
GATES = {
    "text": "gate_text",
    "media": "gate_media",
    "link": "gate_link",
    "document": "gate_document",
    "gif": "gate_gif",
    "audio": "gate_audio",
    "sticker": "gate_sticker",
}

GATE_LABELS = {
    "text": "Text",
    "media": "Media (photo/video)",
    "link": "Links",
    "document": "Documents",
    "gif": "GIF / Animation",
    "audio": "Audio / Voice",
    "sticker": "Stickers",
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
# Button labels are plain text — use only the owner's custom-emoji fallbacks
# (✅ / ❌ from bot/emojis.py). Message HTML should use E.CHECK / E.ERROR instead.
ON = "✅ ON"
OFF = "❌ OFF"
