"""Bot-wide constants: texts, URLs and callback data."""

from bot.emojis import E

BOT_NAME = "Phi π"

# ── Rank progression ─────────────────────────────────────────────
# The ONE definition of the message ladder. Every rank surface
# (/rank, /rankings, /mytop, /nextlevel, /leaderboard, /profile,
# /info, /template) derives its numbers from these two values and
# from daily_messages (bot/database.py) — never from the legacy
# user_chat_level / user_level.global_messages columns.
CHAT_RANK_MESSAGES = 100    # messages per chat rank (per group)
GLOBAL_RANK_MESSAGES = 250  # messages per global rank (all groups)

BOT_DESCRIPTION = (
    "The ultimate Telegram bot for community management. "
    "Leveling, moderation, giveaways, custom commands, and so much more."
)

START_TEXT = (
    "{fire} {username}\n"
    "{description}\n\n"
    "{arrow} /help for the full command list."
)

# ── Help menu data ───────────────────────────────────────────────
# Rendered by bot/modules/help.py into the paginated inline menu.
# Each entry:
#   key      unique slug (callback data: help:open:<key>:<page>)
#   icon     E.* custom-emoji HTML for the header + button label
#   title    display title (HTML-escaped at render time)
#   sections list of (header | None, [command lines]) — HTML-safe text,
#            escaping done here exactly like the old flat HELP_TEXT
#   notes    extra HTML lines shown after the command list
# Grid order defines the button order; PAGE_SIZE = 9 (3 rows × 3).

HELP_MENU: list[dict] = [
    {
        "key": "general",
        "icon": E.INFO,
        "title": "General",
        "sections": [
            (None, [
                "/start — Open the main menu",
                "/help — Show this help menu",
                "/testcolors — Preview colored buttons (groups)",
                "/restart — Restart the bot (owner)",
                "/free @user — Clear spam warnings &amp; block (sudo)",
            ]),
        ],
        "notes": [
            "<b>Anti-flood:</b> 5 messages in 3 seconds → blocked "
            "5 min, then 10 min, then 20 min (resets at IST midnight)"
        ],
    },
    {
        "key": "moderation",
        "icon": E.MUTE,
        "title": "Moderation",
        "sections": [
            ("Mute Commands", [
                "/mute @user [period] [reason] — Mute a user",
                "/dmute (reply) [period] [reason] — Mute and delete message",
                "/smute @user [period] [reason] — Silent mute",
                "/tmute @user &lt;period&gt; [reason] — Temporary mute",
                "/unmute @username — Unmute a user",
            ]),
            ("Ban Commands", [
                "/ban @user [period] [reason] — Ban a user",
                "/dban (reply) [period] [reason] — Ban and delete message",
                "/sban @user [period] [reason] — Silent ban",
                "/tban @user &lt;period&gt; [reason] — Temporary ban",
                "/unban @user — Unban a user",
            ]),
            ("Kick Commands", [
                "/kick @user [reason] — Kick a user",
                "/dkick (reply) [reason] — Kick and delete message",
                "/skick @user [reason] — Silent kick",
            ]),
            ("Warning Commands", [
                "/warn @user [reason] — Issue a warning",
                "/dwarn (reply) [reason] — Warn and delete message",
                "/swarn @user [reason] — Silent warn",
                "/warns @user — Show user warnings",
                "/rmwarn @user — Remove latest warning",
                "/resetwarn @user — Clear all user warnings",
                "/resetallwarns — Clear all warnings in chat",
            ]),
            ("Warning Configuration", [
                "/warnlimit [number] — Set warning limit",
                "/warnmode [action] [duration] — Set warning action",
                "/warntime [duration|off] — Set warning expiration",
            ]),
            ("Rules Commands", [
                "/rules — Show chat rules",
                "/setrules &lt;text&gt; — Set rules",
                "/resetrules — Clear rules",
                "/privaterules &lt;on|off&gt; — Toggle private rules mode",
            ]),
        ],
        "notes": ["<b>Duration Formats:</b> 30s • 5m • 1h • 2d • 1w"],
    },
    {
        "key": "admin",
        "icon": E.CROWN,
        "title": "Admin",
        "sections": [
            (None, [
                "/promote @user — Promote to admin",
                "/demote @user — Demote an admin",
                "/pin — Pin a message",
                "/unpin — Unpin messages",
                "/adminlist — List all admins",
                "/admincount — Count admins",
                "/setchatphoto — Set chat photo (reply to a photo)",
                "/setchatname — Set the chat title",
                "/setchatdescription — Set the chat description",
                "/fullpromote — Self-promote to full admin (owner only)",
            ]),
        ],
        "notes": [],
    },
    {
        "key": "gban",
        "icon": E.KICK,
        "title": "Gban & Sudo",
        "sections": [
            (None, [
                "/gban @user [reason] — Globally ban (sudo)",
                "/ungban @user — Globally unban (sudo)",
                "/gbanlist — List gbanned users (sudo)",
                "/massban ID ID — Mass ban (owner)",
                "/addsudo @user — Add sudo user (owner)",
                "/rmsudo @user — Remove sudo user (owner)",
                "/sudolist — List sudo users (owner)",
            ]),
        ],
        "notes": [],
    },
    {
        "key": "security",
        "icon": E.ALERT,
        "title": "Security",
        "sections": [
            (None, [
                "/shield [on|off] — View / toggle Group Shield (admin)",
                "/shieldcfg joins &lt;N&gt; &lt;sec&gt; — Join-burst threshold (admin)",
                "/shieldcfg msgs &lt;N&gt; &lt;sec&gt; — Message-burst threshold (admin)",
                "/shieldcfg action &lt;alert|mute|kick|ban&gt; — Raid response (admin)",
                "/lockdown [on|off] — Freeze non-admin messaging (admin)",
                "/raidlog [n] — Recent raid events (admin)",
            ]),
        ],
        "notes": [],
    },
    {
        "key": "analytics",
        "icon": E.SETTINGS,
        "title": "Analytics",
        "sections": [
            (None, [
                "/stats [day|week|month] — Analytics dashboard (admin)",
                "/analytics — Alias of /stats (admin)",
                "/topactive [day|week|month] — Most active users (admin)",
                "/peakhours [days] — Busiest hours (admin)",
            ]),
        ],
        "notes": [],
    },
    {
        "key": "leveling",
        "icon": E.FIRE,
        "title": "Leveling",
        "sections": [
            (None, [
                "/rank [@user] — View rank card",
                "/template — Pick rank card template with preview (DM only)",
                "/ranktemplate — Pick rank card template (DM only)",
                "/nextlevel — Messages needed for next rank",
                "/streak — Your message streaks",
                "/leaderboard /lb — Chat ranks by messages",
                "/daily — Top chatters today",
                "/weekly — Top chatters this week",
                "/monthly — Top chatters this month",
            ]),
        ],
        "notes": [
            "<b>Ranking Rules:</b> +1 chat rank per 100 messages • "
            "+1 global rank per 250 messages"
        ],
    },
    {
        "key": "stats",
        "icon": E.CHART,
        "title": "Chat Stats",
        "sections": [
            (None, [
                "/rankings — Top chatters in this group",
                "/mytop — Your groups ranked by your messages",
            ]),
        ],
        "notes": [
            "<b>Rankings:</b> counts text messages only • "
            "switch Overall / Today / Weekly from the buttons • "
            "weeks start Monday, Today refreshes at midnight IST • "
            "milestones at 100 and every 500 messages"
        ],
    },
    {
        "key": "filters",
        "icon": E.BOOKMARK,
        "title": "Filters",
        "sections": [
            (None, [
                "/filter &lt;trigger&gt; — Add a filter (reply to message)",
                "/stop &lt;trigger&gt; — Remove a filter",
                "/filters — List all filters in chat",
            ]),
        ],
        "notes": [],
    },
    {
        "key": "blocklist",
        "icon": E.CROSS,
        "title": "Blocklist",
        "sections": [
            (None, [
                "/blocklist &lt;word1&gt; &lt;word2&gt; — Add blocked words",
                "/unblocklist &lt;word1&gt; — Remove blocked words",
                "/blocklistview — View blocked words",
                "/unblocklistall — Clear all blocked words",
                "/setblocklistaction &lt;delete|warn|mute|kick|ban&gt; — Set action",
                "/blocklistreason &lt;reason&gt; — Set reason",
            ]),
        ],
        "notes": [],
    },
    {
        "key": "watchwords",
        "icon": E.EYES,
        "title": "Watch Words",
        "sections": [
            (None, [
                "/watch &lt;word&gt; — Add a watched word (admin)",
                "/unwatch &lt;word&gt; — Remove a watched word (admin)",
                "/watchlist — List your watched words (admin)",
                "/watchmode &lt;copy|forward&gt; — Set delivery mode (admin)",
            ]),
        ],
        "notes": [],
    },
    {
        "key": "welcome",
        "icon": E.WAVE,
        "title": "Welcome/Goodbye",
        "sections": [
            (None, [
                "/welcome [on|off] — Toggle/view welcome messages",
                "/goodbye [on|off] — Toggle/view goodbye messages",
                "/setwelcome &lt;text&gt; — Set custom welcome message",
                "/setgoodbye &lt;text&gt; — Set custom goodbye message",
                "/resetwelcome — Reset welcome to default",
                "/resetgoodbye — Reset goodbye to default",
                "/cleanwelcome [on|off] — Delete old welcome messages",
                "/cleangoodbye [on|off] — Delete old goodbye messages",
                "/request [on|off] - Join-request approval card",
            ]),
        ],
        "notes": [
            "<b>Variables:</b> {{first}} {{last}} {{fullname}} {{username}} "
            "{{mention}} {{chatname}} {{id}}"
        ],
    },
    {
        "key": "fun",
        "icon": E.GUITAR,
        "title": "Fun",
        "sections": [
            (None, [
                "/hug [user] — Hug someone",
                "/kiss [user] — Kiss someone",
                "/slap [user] — Slap someone",
                "/poke [user] — Poke someone",
                "/tickle [user] — Tickle someone",
                "/highfive [user] — High five",
                "/wave [user] — Wave hello",
                "/pat [user] — Pat on the head",
                "/punch [user] — Punch someone",
                "/kill [user] — Playfully eliminate",
                "/yeet [user] — YEET!",
            ]),
        ],
        "notes": [],
    },
    {
        "key": "stickers",
        "icon": E.SPARKLE,
        "title": "Stickers",
        "sections": [
            (None, [
                "/kang [pack#] [emoji] — Kang a replied sticker/photo/animation into your pack",
                "/unkang — Remove the replied sticker from your pack",
                "/stickerinfo — Sticker details + pack link (also /stinfo)",
                "/stickerid — Show a sticker's file ID",
                "/getsticker — Download a static sticker as an image",
                "/getvidsticker — Download a video sticker as MP4",
                "/getvideo — Download a replied GIF as MP4 video",
                "/mmf &lt;text&gt; — Memify replied image/video (; splits top/bottom)",
            ]),
        ],
        "notes": [
            "Kangs need MTProto (TAG_MTPROTO=1); GIF/video conversion needs "
            "ffmpeg. Packs are named a&lt;#&gt;_yourid_by_&lt;bot&gt;.",
        ],
    },
    {
        "key": "users",
        "icon": E.USER,
        "title": "Users & Stats",
        "sections": [
            (None, [
                "/info [user] - Full user info card",
                "/myinfo - See your info",
                "/userinfo @user - See another user's info",
                "/id - Chat ID + Your ID",
                "/userstats - Bot statistics (admin)",
                "/recentactivity - Recent activity (admin)",
            ]),
        ],
        "notes": [],
    },
    {
        "key": "profile",
        "icon": E.HEART,
        "title": "Profile",
        "sections": [
            (None, [
                "/profile [@user] — Reputation profile card",
                "/rep — Alias of /profile",
            ]),
        ],
        "notes": [],
    },
    {
        "key": "tagging",
        "icon": E.ANNOUNCE,
        "title": "Mass Tagging",
        "sections": [
            (None, [
                "/all — Tag members (reply to a message) (admin)",
                "/tagabort — Stop a running tag (admin)",
                "/allsettings [mode|window|max|batch|send|registry] [value] — Tag settings (admin)",
                "/tagstats — Tagging stats (admin)",
                "/tagall [text] — Mention everyone by name; also @all (admin)",
                "/etagall [text] — Mention everyone with random emojis; also @eall (admin)",
                "/cancel — Stop a running tag (alias of /tagabort)",
            ]),
        ],
        "notes": [
            "Tags order by presence &amp; recent activity; admins &amp; bots "
            "are never tagged."
        ],
    },
    {
        "key": "instagram",
        "icon": E.WEB,
        "title": "Instagram",
        "sections": [
            (None, [
                "/igdl &lt;url&gt; — Download a post/reel (or reply to a link)",
                "/igsettings [auto on|off] [max N] — Auto-download settings (admin)",
                "/igstats — Downloader metrics (admin)",
                "/igcache [clear] — file_id cache (owner)",
                "/igbenchmark &lt;url&gt; — Time a resolve (owner)",
            ]),
        ],
        "notes": [
            "Auto: Instagram links in groups/DMs download automatically when enabled."
        ],
    },
    {
        "key": "bind",
        "icon": E.PIN,
        "title": "Bind",
        "sections": [
            (None, [
                "/bind [channel] — Bind this group to a channel (admin)",
                "/bindmenu — Open the bind settings menu (admin)",
            ]),
        ],
        "notes": [],
    },
]

# ── URLs ────────────────────────────────────────────────
URL_ADD_TO_GROUP = "http://t.me/PiModulerBot?startgroup=botstart"
URL_OFFICIAL_CHANNEL = "https://t.me/ThePiUpdates"
URL_NETWORK = "https://t.me/ShadowBotsHQ"

# ── Callback data ───────────────────────────────────────
CB_HELP = "start:help"
CB_DASHBOARD = "start:dashboard"
