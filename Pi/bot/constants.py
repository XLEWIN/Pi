"""Bot-wide constants: texts, URLs and callback data."""

BOT_NAME = "Phi π"

BOT_DESCRIPTION = (
    "The ultimate Telegram bot for community management. "
    "Leveling, moderation, giveaways, custom commands, and so much more."
)

START_TEXT = (
    "🔥 {username}\n"
    "{description}\n\n"
    "➡️ /help for the full command list."
)

HELP_TEXT = """📖 Help Menu

Here are all the available commands:

<b>🏠 General</b>
/start — Open the main menu
/help — Show this help menu
/myinfo — See your info
/userinfo @user — See user info (admin)
/userstats — Bot statistics (admin)
/recentactivity — Recent activity (admin)

<b>🔇 Moderation</b>

<b>Mute Commands</b>
/mute @user [period] [reason] — Mute a user
/dmute (reply) [period] [reason] — Mute and delete message
/smute @user [period] [reason] — Silent mute
/tmute @user &lt;period&gt; [reason] — Temporary mute
/unmute @username — Unmute a user

<b>Ban Commands</b>
/ban @user [period] [reason] — Ban a user
/dban (reply) [period] [reason] — Ban and delete message
/sban @user [period] [reason] — Silent ban
/tban @user &lt;period&gt; [reason] — Temporary ban
/unban @user — Unban a user

<b>Kick Commands</b>
/kick @user [reason] — Kick a user
/dkick (reply) [reason] — Kick and delete message
/skick @user [reason] — Silent kick

<b>Warning Commands</b>
/warn @user [reason] — Issue a warning
/dwarn (reply) [reason] — Warn and delete message
/swarn @user [reason] — Silent warn
/warns @user — Show user warnings
/rmwarn @user — Remove latest warning
/resetwarn @user — Clear all user warnings
/resetallwarns — Clear all warnings in chat

<b>Warning Configuration</b>
/warnlimit [number] — Set warning limit
/warnmode [action] [duration] — Set warning action
/warntime [duration|off] — Set warning expiration

<b>Rules Commands</b>
/rules — Show chat rules
/setrules &lt;text&gt; — Set rules
/resetrules — Clear rules
/privaterules &lt;on|off&gt; — Toggle private rules mode

<b>Duration Formats:</b>
30s • 5m • 1h • 2d • 1w

<b>🎯 Filters</b>
/filter &lt;trigger&gt; — Add a filter (reply to message)
/stop &lt;trigger&gt; — Remove a filter
/filters — List all filters in chat

<b>🚫 Blocklist</b>
/blocklist &lt;word1&gt; &lt;word2&gt; — Add blocked words
/unblocklist &lt;word1&gt; — Remove blocked words
/blocklistview — View blocked words
/unblocklistall — Clear all blocked words
/setblocklistaction &lt;delete|warn|mute|kick|ban&gt; — Set action
/blocklistreason &lt;reason&gt; — Set reason

<b>🎉 Fun</b>
/hug [user] — Hug someone
/kiss [user] — Kiss someone
/slap [user] — Slap someone
/poke [user] — Poke someone
/tickle [user] — Tickle someone
/highfive [user] — High five
/wave [user] — Wave hello
/pat [user] — Pat on the head
/punch [user] — Punch someone
/kill [user] — Playfully eliminate
/yeet [user] — YEET!

<b>👑 Admin</b>
/promote @user — Promote to admin
/demote @user — Demote an admin
/pin — Pin a message
/unpin — Unpin messages
/adminlist — List all admins
/admincount — Count admins

<b>🔨 Gban & Sudo</b>
/gban @user [reason] — Globally ban (sudo)
/ungban @user — Globally unban (sudo)
/gbanlist — List gbanned users (sudo)
/massban ID ID — Mass ban (owner)
/addsudo @user — Add sudo user (owner)
/rmsudo @user — Remove sudo user (owner)
/sudolist — List sudo users (owner)

<b>👀 Watch Words</b>
/watch &lt;word&gt; — Add a watched word (admin)
/unwatch &lt;word&gt; — Remove a watched word (admin)
/watchlist — List your watched words (admin)
/watchmode &lt;copy|forward&gt; — Set delivery mode (admin)

<b>👋 Welcome/Goodbye</b>
/welcome [on|off] — Toggle/view welcome messages
/goodbye [on|off] — Toggle/view goodbye messages
/setwelcome &lt;text&gt; — Set custom welcome message
/setgoodbye &lt;text&gt; — Set custom goodbye message
/resetwelcome — Reset welcome to default
/resetgoodbye — Reset goodbye to default
/cleanwelcome [on|off] — Delete old welcome messages
/cleangoodbye [on|off] — Delete old goodbye messages

<b>Variables:</b> {'first'} {'last'} {'fullname'} {'username'} {'mention'} {'chatname'} {'id'}

<b>🏆 Leveling &amp; Leaderboard</b>
/rank [@user] — View rank card
/ranktemplate — Pick rank card template (DM only)
/nextlevel — XP needed for next level
/streak — Your message streaks
/leaderboard /lb — Chat leaderboard
/daily — Top chatters today
/weekly — Top chatters this week
/monthly — Top chatters this month

<b>Leveling Rules:</b>
+1 chat level per 50 messages • +1 global level per 100 messages
"""

# ── URLs ────────────────────────────────────────────────
URL_ADD_TO_GROUP = "http://t.me/Phi_RoBot?startgroup=botstart"
URL_OFFICIAL_CHANNEL = "https://t.me/Phi_Chart"
URL_NETWORK = "https://t.me/ShadowBotsHQ"

# ── Callback data ───────────────────────────────────────
CB_HELP = "start:help"
CB_DASHBOARD = "start:dashboard"