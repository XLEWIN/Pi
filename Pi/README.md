# Telegram Moderation Bot

A comprehensive Telegram moderation bot with mute, ban, kick, warnings, and rules management.

## Features

### 🔇 Mute Commands
- `/mute @user [period] [reason]` — Mute a user publicly
- `/dmute (reply) [period] [reason]` — Mute and delete message
- `/smute @user [period] [reason]` — Silent mute (no reply)
- `/tmute @user <period> [reason]` — Temporary mute
- `/unmute @username` — Unmute a user

### 🔨 Ban Commands
- `/ban @user [period] [reason]` — Ban a user publicly
- `/dban (reply) [period] [reason]` — Ban and delete message
- `/sban @user [period] [reason]` — Silent ban (no reply)
- `/tban @user <period> [reason]` — Temporary ban
- `/unban @user` — Unban a user

### 👢 Kick Commands
- `/kick @user [reason]` — Kick a user
- `/dkick (reply) [reason]` — Kick and delete message
- `/skick @user [reason]` — Silent kick (no reply)

### ⚠️ Warning Commands
- `/warn @user [reason]` — Issue a warning
- `/dwarn (reply) [reason]` — Warn and delete message
- `/swarn @user [reason]` — Silent warn (no reply)
- `/warns @user` — Show user warnings
- `/rmwarn @user` — Remove latest warning
- `/resetwarn @user` — Clear all user warnings
- `/resetallwarns` — Clear all warnings in chat

### ⚙️ Warning Configuration
- `/warnlimit [number]` — Set warning limit before action triggers
- `/warnmode [action] [duration]` — Set action (mute/kick/ban/timeout)
- `/warntime [duration|off]` — Set warning expiration time

### 📜 Rules Commands
- `/rules` — Show chat rules
- `/setrules <text>` — Set rules
- `/resetrules` — Clear rules
- `/privaterules <on|off>` — Toggle private rules mode

## Duration Formats
- `30s` — 30 seconds
- `5m` — 5 minutes
- `1h` — 1 hour
- `2d` — 2 days
- `1w` — 1 week

## Setup

### 1. Create a Telegram Bot

1. Open Telegram and search for @BotFather
2. Send `/newbot` and follow the instructions
3. Copy the bot token you receive

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure the Bot

1. Open `moderation_bot.py`
2. Replace `YOUR_BOT_TOKEN_HERE` with your actual bot token:

```python
BOT_TOKEN = "123456789:ABCdefGHIjklMNOpqrsTUVwxyz"
```

### 4. Run the Bot

```bash
python moderation_bot.py
```

### 5. Add Bot to Your Group

1. Add the bot to your Telegram group
2. Make the bot an admin with these permissions:
   - ✅ Delete messages
   - ✅ Restrict members
   - ✅ Ban users
   - ✅ Pin messages

## Permissions Required

| Permission | Commands |
|------------|----------|
| Can Restrict Members | /mute, /dmute, /smute, /tmute, /unmute |
| Can Ban Members | /ban, /dban, /sban, /tban, /unban, /kick, /dkick, /skick |
| Can Delete Messages | /dmute, /dban, /dkick, /dwarn |

## Default Warning Settings

- **Warning Limit:** 3 warnings before action triggers
- **Warning Mode:** Mute (configurable to kick/ban/timeout)
- **Warning Expiration:** None (warnings stay forever until cleared)

## Example Usage

```
/mute @spammer 1h Spamming in chat
/ban @rulebreaker 7d Repeated violations
/warn @user Please follow the rules
/warnlimit 3
/warnmode mute 1d
/warntime 7d
/setrules 1. Be respectful 2. No spam 3. English only
```

## Notes

- The bot uses in-memory storage (warnings/settings reset on restart)
- For production use, integrate with a database (SQLite, PostgreSQL, etc.)
- The bot requires admin privileges in the group to perform moderation actions
- Users cannot moderate users with equal or higher permissions
