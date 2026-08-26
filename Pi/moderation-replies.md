# Moderation Command Replies

## 🔖 Moderation Commands

---

### Mute Commands

#### /mute
**Description:** Mute a user publicly with reason.

**Reply Templates:**

**Success (with duration):**
```
🔇 Muted {user} for {duration}.
Reason: {reason}
```

**Success (permanent):**
```
🔇 Muted {user} permanently.
Reason: {reason}
```

**Error - No user specified:**
```
❌ Please specify a user to mute.
Usage: /mute @user [period] [reason]
```

**Error - Invalid duration:**
```
❌ Invalid duration format. Use: 30s, 5m, 1h, 2d, or 1w
```

**Error - Cannot mute self:**
```
❌ You cannot mute yourself.
```

**Error - Cannot mute bot:**
```
❌ I cannot mute myself.
```

**Error - Insufficient permissions:**
```
❌ You don't have permission to mute this user.
```

**Error - User already muted:**
```
⚠️ {user} is already muted.
```

---

#### /dmute
**Description:** Mute a user and delete the replied-to message.

**Reply Templates:**

**Success (with duration):**
```
🔇 Muted {user} for {duration}.
Reason: {reason}
🗑️ Deleted the offending message.
```

**Success (permanent):**
```
🔇 Muted {user} permanently.
Reason: {reason}
🗑️ Deleted the offending message.
```

**Error - No reply or user specified:**
```
❌ Please reply to a message or specify a user.
Usage: /dmute (reply) [period] [reason]
```

**Error - Invalid duration:**
```
❌ Invalid duration format. Use: 30s, 5m, 1h, 2d, or 1w
```

---

#### /smute
**Description:** Silent mute - deletes your command, no chat reply.

**Reply Templates:**

**Success:**
```
[No reply - command deleted silently]
```

**Error (DM to user):**
```
🔇 You have been muted.
Reason: {reason}
```

---

#### /tmute
**Description:** Temporary mute with required duration.

**Reply Templates:**

**Success:**
```
🔇 Temporarily muted {user} for {duration}.
Reason: {reason}
Auto-unmute: {unmute_time}
```

**Error - No duration specified:**
```
❌ Duration is required for temporary mute.
Usage: /tmute @user <period> [reason]
```

**Error - Invalid duration:**
```
❌ Invalid duration format. Use: 30s, 5m, 1h, 2d, or 1w
```

---

#### /unmute
**Description:** Unmute a user.

**Reply Templates:**

**Success:**
```
🔊 Unmuted {user}.
```

**Error - User not muted:**
```
⚠️ {user} is not currently muted.
```

**Error - No user specified:**
```
❌ Please specify a user to unmute.
Usage: /unmute @username
```

---

### Ban Commands

#### /ban
**Description:** Ban a user publicly with reason.

**Reply Templates:**

**Success (with duration):**
```
🔨 Banned {user} for {duration}.
Reason: {reason}
```

**Success (permanent):**
```
🔨 Banned {user} permanently.
Reason: {reason}
```

**Error - No user specified:**
```
❌ Please specify a user to ban.
Usage: /ban @user [period] [reason]
```

**Error - Invalid duration:**
```
❌ Invalid duration format. Use: 30s, 5m, 1h, 2d, or 1w
```

**Error - Cannot ban self:**
```
❌ You cannot ban yourself.
```

**Error - Cannot ban bot:**
```
❌ I cannot ban myself.
```

**Error - Insufficient permissions:**
```
❌ You don't have permission to ban this user.
```

**Error - User already banned:**
```
⚠️ {user} is already banned.
```

**Error - Cannot ban higher role:**
```
❌ You cannot ban a user with equal or higher permissions.
```

---

#### /dban
**Description:** Ban a user and delete the replied-to message.

**Reply Templates:**

**Success (with duration):**
```
🔨 Banned {user} for {duration}.
Reason: {reason}
🗑️ Deleted the offending message.
```

**Success (permanent):**
```
🔨 Banned {user} permanently.
Reason: {reason}
🗑️ Deleted the offending message.
```

**Error - No reply or user specified:**
```
❌ Please reply to a message or specify a user.
Usage: /dban (reply) [period] [reason]
```

---

#### /sban
**Description:** Silent ban - deletes your command, no chat reply.

**Reply Templates:**

**Success:**
```
[No reply - command deleted silently]
```

**Error (DM to user):**
```
🔨 You have been banned.
Reason: {reason}
```

---

#### /tban
**Description:** Temporary ban with required duration.

**Reply Templates:**

**Success:**
```
🔨 Temporarily banned {user} for {duration}.
Reason: {reason}
Auto-unban: {unban_time}
```

**Error - No duration specified:**
```
❌ Duration is required for temporary ban.
Usage: /tban @user <period> [reason]
```

**Error - Invalid duration:**
```
❌ Invalid duration format. Use: 30s, 5m, 1h, 2d, or 1w
```

---

#### /unban
**Description:** Unban a user.

**Reply Templates:**

**Success:**
```
✅ Unbanned {user}.
```

**Error - User not banned:**
```
⚠️ {user} is not currently banned.
```

**Error - No user specified:**
```
❌ Please specify a user to unban.
Usage: /unban @user
```

---

### Kick Commands

#### /kick
**Description:** Kick a user with reason.

**Reply Templates:**

**Success:**
```
👢 Kicked {user}.
Reason: {reason}
```

**Error - No user specified:**
```
❌ Please specify a user to kick.
Usage: /kick @user [reason]
```

**Error - Cannot kick self:**
```
❌ You cannot kick yourself.
```

**Error - Cannot kick bot:**
```
❌ I cannot kick myself.
```

**Error - Insufficient permissions:**
```
❌ You don't have permission to kick this user.
```

**Error - Cannot kick higher role:**
```
❌ You cannot kick a user with equal or higher permissions.
```

---

#### /dkick
**Description:** Kick a user and delete the replied-to message.

**Reply Templates:**

**Success:**
```
👢 Kicked {user}.
Reason: {reason}
🗑️ Deleted the offending message.
```

**Error - No reply or user specified:**
```
❌ Please reply to a message or specify a user.
Usage: /dkick (reply) [reason]
```

---

#### /skick
**Description:** Silent kick - deletes your command, no chat reply.

**Reply Templates:**

**Success:**
```
[No reply - command deleted silently]
```

**Error (DM to user):**
```
👢 You have been kicked.
Reason: {reason}
```

---

## ⚠️ Warnings System

### Issue Warnings

#### /warn
**Description:** Issue a warning to a user.

**Reply Templates:**

**Success (warning issued):**
```
⚠️ Warning issued to {user} ({warning_count}/{warn_limit}).
Reason: {reason}
```

**Success (action triggered):**
```
⚠️ Warning issued to {user} ({warning_count}/{warn_limit}).
Reason: {reason}

🚨 Action triggered: {action}
```

**Error - No user specified:**
```
❌ Please specify a user to warn.
Usage: /warn @user [reason]
```

**Error - Cannot warn self:**
```
❌ You cannot warn yourself.
```

**Error - Cannot warn bot:**
```
❌ I cannot warn myself.
```

**Error - Insufficient permissions:**
```
❌ You don't have permission to warn this user.
```

---

#### /dwarn
**Description:** Warn a user and delete the offending message.

**Reply Templates:**

**Success (warning issued):**
```
⚠️ Warning issued to {user} ({warning_count}/{warn_limit}).
Reason: {reason}
🗑️ Deleted the offending message.
```

**Success (action triggered):**
```
⚠️ Warning issued to {user} ({warning_count}/{warn_limit}).
Reason: {reason}
🗑️ Deleted the offending message.

🚨 Action triggered: {action}
```

**Error - No reply or user specified:**
```
❌ Please reply to a message or specify a user.
Usage: /dwarn (reply) [reason]
```

---

#### /swarn
**Description:** Silent warning - deletes your command, no chat reply.

**Reply Templates:**

**Success:**
```
[No reply - command deleted silently]
```

**Error (DM to user):**
```
⚠️ You have been warned.
Reason: {reason}
Warnings: {warning_count}/{warn_limit}
```

---

### View Warnings

#### /warns
**Description:** Show a user's active warnings.

**Reply Templates:**

**Success (with warnings):**
```
⚠️ Active warnings for {user}:
{warning_list}

Total: {count}/{warn_limit}
```

**Success (no warnings):**
```
✅ {user} has no active warnings.
```

**Success (own warnings):**
```
⚠️ Your active warnings:
{warning_list}

Total: {count}/{warn_limit}
```

**Error - No user specified:**
```
❌ Please specify a user.
Usage: /warns @user
```

---

### Remove / Reset Warnings

#### /rmwarn
**Description:** Remove the latest warning for a user.

**Reply Templates:**

**Success:**
```
✅ Removed the latest warning for {user}.
Warnings remaining: {count}/{warn_limit}
```

**Error - No warnings to remove:**
```
⚠️ {user} has no active warnings to remove.
```

**Error - No user specified:**
```
❌ Please specify a user.
Usage: /rmwarn @user
```

---

#### /resetwarn
**Description:** Clear all warnings for a user.

**Reply Templates:**

**Success:**
```
✅ Cleared all warnings for {user}.
Warnings reset to 0/{warn_limit}.
```

**Error - No warnings to clear:**
```
⚠️ {user} has no active warnings to clear.
```

**Error - No user specified:**
```
❌ Please specify a user.
Usage: /resetwarn @user
```

---

#### /resetallwarns
**Description:** Clear every active warning in this chat.

**Reply Templates:**

**Success:**
```
✅ Cleared all {count} active warnings in this chat.
```

**Success (no warnings):**
```
✅ No active warnings found in this chat.
```

**Error - Insufficient permissions:**
```
❌ You don't have permission to reset all warnings.
```

---

### Warning Configuration

#### /warnlimit
**Description:** Set warning limit before action triggers.

**Reply Templates:**

**Success:**
```
⚙️ Warning limit set to {limit}.
Action will trigger on the {limit}{ordinal} warning.
```

**Error - Invalid number:**
```
❌ Please provide a valid number.
Usage: /warnlimit <number>
```

**Error - Number too low:**
```
❌ Warning limit must be at least 1.
```

---

#### /warnmode
**Description:** Set action to take when warning limit is reached.

**Reply Templates:**

**Success:**
```
⚙️ Warning mode set to: {action}
{action_description}
```

**Error - Invalid mode:**
```
❌ Invalid warning mode. Choose from: mute, kick, ban, timeout
Usage: /warnmode <action> [duration]
```

**Error - No duration for timed action:**
```
❌ Duration required for {action}. Use: 30s, 5m, 1h, 2d, or 1w
```

---

#### /warntime
**Description:** Set expiration time for warnings.

**Reply Templates:**

**Success:**
```
⚙️ Warning expiration set to: {duration}
Warnings older than {duration} will stop counting.
```

**Success (disabled):**
```
⚙️ Warning expiration disabled.
Warnings will stay forever until cleared.
```

**Error - Invalid duration:**
```
❌ Invalid duration format. Use: 30s, 5m, 1h, 2d, 1w, or "off"
```

---

## 📜 Rules Commands

### Public Commands

#### /rules
**Description:** Show the chat's rules.

**Reply Templates:**

**Success (with rules):**
```
📜 {chat_name} Rules

{rules_text}

[Media if attached]
[Inline keyboard if set]
```

**Success (private mode):**
```
📜 Rules for {chat_name}

Click the button below to view the rules in a private message.
[Button: View Rules]
```

**Error - No rules set:**
```
📜 No rules have been set for this chat yet.
Admins can use /setrules to configure them.
```

---

### Admin Commands

#### /setrules
**Description:** Set rules for the chat.

**Reply Templates:**

**Success (from text):**
```
✅ Rules updated successfully!

Preview:
{rules_preview}
```

**Success (from reply):**
```
✅ Rules copied from the replied message!

Preview:
{rules_preview}
```

**Success (with media):**
```
✅ Rules updated with media attachment!
```

**Error - No text provided:**
```
❌ Please provide the rules text.
Usage: /setrules <text>

Or reply to a message with /setrules to copy its content.
```

**Error - Insufficient permissions:**
```
❌ You don't have permission to set rules.
```

---

#### /resetrules
**Description:** Clear the rules.

**Reply Templates:**

**Success:**
```
✅ Rules have been cleared for this chat.
```

**Error - No rules to clear:**
```
⚠️ No rules are currently set for this chat.
```

**Error - Insufficient permissions:**
```
❌ You don't have permission to reset rules.
```

---

#### /privaterules
**Description:** Toggle private rules mode.

**Reply Templates:**

**Success (enabled):**
```
⚙️ Private rules enabled.
/rules will now send a button that DMs the rules instead of replying inline.
```

**Success (disabled):**
```
⚙️ Private rules disabled.
/rules will now reply with the rules inline.
```

**Error - Invalid toggle:**
```
❌ Please specify "on" or "off".
Usage: /privaterules <on|off>
```

**Error - Insufficient permissions:**
```
❌ You don't have permission to change this setting.
```

---

## 📝 Duration Tokens Reference

When users provide duration, accept these formats:
- `30s` — 30 seconds
- `5m` — 5 minutes
- `1h` — 1 hour
- `2d` — 2 days
- `1w` — 1 week

**Invalid duration error:**
```
❌ Invalid duration format.

Accepted formats:
• 30s (seconds)
• 5m (minutes)
• 1h (hours)
• 2d (days)
• 1w (weeks)

Example: /mute @user 1h Spamming
```

---

## 🎯 Common Error Messages

### Permission Errors
```
❌ You don't have permission to use this command.
Required permission: {permission}
```

### Bot Permission Errors
```
❌ I don't have permission to perform this action.
Please make sure I have the required role/permissions.
```

### Rate Limit
```
⏳ Slow down! Please wait {time} before using this command again.
```

### Command Not Found
```
❌ Unknown command: /{command}

Type /help to see available commands.
```

### Invalid User
```
❌ Could not find user: {user}

Make sure to mention them (@username) or use their user ID.
```

---

## 📊 Command Usage Examples

```
/mute @spammer 1h Spamming in chat
/dmute (reply) 30m Inappropriate content
/smute @troll 2d Being toxic
/tmute @user 5m冷静一下
/unmute @user

/ban @rulebreaker 7w Repeated violations
/dban (reply) 1w Hate speech
/sban @spambot Permanent
/tban @user 3d Bot abuse
/unban @user

/kick @problemuser Disruptive behavior
/dkick (reply) Harassment
/skick @troll Excessive toxicity

/warn @user Please follow the rules
/dwarn (reply) Stop spamming
/swarn @user Minor infraction
/warns @user
/rmwarn @user
/resetwarn @user
/resetallwarns
/warnlimit 3
/warnmode mute 1d
/warntime 7d
/warntime off

/rules
/setrules 1. Be respectful 2. No spam 3. English only
/resetrules
/privaterules on
/privaterules off
```
