"""Tagging module configuration — handler groups, limits, cycles, env knobs.

Handler group placement (PTB: all groups are evaluated per update, first
match per group wins, one handler per group):

    0   commands + `tag:` callbacks — no name collisions (verified).
    15  message activity observer (write-behind buffer).
    16  joins/leaves (service messages) + chat_member updates.
    17  catch-all callback activity observer.

CRITICAL: `tagging` loads alphabetically BEFORE `users`/`welcome`, so it
must never register NEW_CHAT_MEMBERS / ChatMember handlers in group 0 —
that would shadow users.py. Groups 16/17 exist to avoid this.
"""

# ── Handler groups ────────────────────────────────────────────────
COMMAND_GROUP = 0           # /all /tagabort /allsettings /tagstats + tag: callbacks
ACTIVITY_GROUP = 15         # group-message activity observer
MEMBER_GROUP = 16           # joins/leaves + chat_member updates
CALLBACK_ACTIVITY_GROUP = 17  # catch-all callback activity observer

# ── Presence ──────────────────────────────────────────────────────
# Seconds after last message a user counts as "active now" (rank 1).
# Activity NEVER proves "online" — that rank is MTProto-only (honesty
# rule: never claim online from activity alone).
PRESENCE_TTL = 15.0
# Activity ranks: 1 = active now, 2 = within a day, 3 = older/stale.
ACTIVITY_RANK_DAY = 86400.0

# ── Activity write-behind ─────────────────────────────────────────
FLUSH_INTERVAL = 3.0       # seconds between buffer flushes (lazy task)

# ── Mention batching (UTF-16 code units — Telegram's length unit) ─
TARGET_LEN = 3600          # soft target per batch message
HARD_LIMIT = 4096          # absolute Telegram message limit
MAX_MENTIONS_PER_BATCH = 150  # cap so no single message spams too hard
NAME_MAX = 32              # display-name cap inside mention anchors

# ── Sending ───────────────────────────────────────────────────────
THROTTLE_DELAY = 2.0       # seconds between batches in "throttled" mode
FLOOD_MAX = 60             # FloodWait longer than this ends the session
SEND_RETRIES = 1           # retries for transient TimedOut/NetworkError
PROGRESS_EDIT_MIN = 2.0    # min seconds between progress-card edits

# ── MTProto (optional enrichment — never required) ────────────────
MTPROTO_SYNC_INTERVAL = 600.0  # background member/presence refresh (s)
ENV_API_ID = "TELEGRAM_API_ID"
ENV_API_HASH = "TELEGRAM_API_HASH"
ENV_MTPROTO_ENABLED = "TAG_MTPROTO"  # "1" to enable when creds present

# ── Settings cycles (`/allsettings` + inline buttons) ─────────────
MODES = ("online_first", "recent", "random", "all")
WINDOWS_H = (1, 6, 12, 24, 72, 168, 0)        # 0 = off / no window
MAX_LIMITS = (50, 100, 250, 500, 1000, 0)     # 0 = unlimited
BATCH_TARGETS = (3200, 3600, 3800)
SEND_MODES = ("normal", "throttled")
REGISTRY_MODES = ("hybrid", "registry_only", "sync")

# Cycle keys → (option tuple, DB column).
CYCLES = {
    "mode": (MODES, "mode"),
    "window_hours": (WINDOWS_H, "window_hours"),
    "max_mentions": (MAX_LIMITS, "max_mentions"),
    "batch_size": (BATCH_TARGETS, "batch_size"),
    "send_mode": (SEND_MODES, "send_mode"),
    "registry_mode": (REGISTRY_MODES, "registry_mode"),
}

# Friendly aliases accepted as /allsettings arguments.
KEY_ALIASES = {
    "mode": "mode",
    "window": "window_hours",
    "max": "max_mentions",
    "batch": "batch_size",
    "send": "send_mode",
    "registry": "registry_mode",
}

# ── User-facing strings (tests assert these exactly) ─────────────
MSG_NOT_GROUP = "Groups only."
MSG_NO_REPLY = "Reply to a message to use /all."
MSG_RUNNING = (
    "A tagging process is already running in this chat. "
    "Use /tagabort to stop it."
)
MSG_NO_SESSION = "No active tagging process."
MSG_NOT_ADMIN = "Only admins can do that."
MSG_STOPPED_FMT = "Tagging stopped. Tagged: {tagged} / {total} users"
MSG_NOBODY = "No users matched the current tagging filters."
MSG_ADMIN_FETCH_FAIL = "Could not fetch the admin list — try again."
MSG_SETTINGS_VALUE_BAD = "Unknown value for {key}: {value}"
