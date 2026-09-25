# Mass Tagging Module (`bot/modules/tagging`)

Admin-only mass mention ("mention everyone") for groups, built on the
Pure Bot API stack — no MTProto required.

## Commands

| Command | Who | What |
|---|---|---|
| `/all` (as a reply) | admin | Copy the replied-to message, then post batches of mentions |
| `/tagall [text]` | admin | Boa-style: mention everyone by name; text = first line (reply-only also works) |
| `/etagall [text]` | admin | Same, but each member linked by a random emoji from the owner's set |
| `@all` / `@eall [text]` | admin | Same triggers without a slash |
| `/tagabort` / `/cancel` | admin | Stop the running session for this chat |
| `/allsettings [key] [value]` | admin | Show card / change one setting |
| `/tagstats` | admin | Session totals, member counts, presence source |

`/tagall` is a port of boabot's Yumeko `tagall` (5 mentions per message,
3 s apart, text+reply rejected) on top of this module's registry,
sessions and cancel tokens; `/etagall` draws its random emoji labels from
`bot/emojis.py` (`EMOJI_POOL`).

Inline buttons: **settings cycles** (`tag:set:<key>`), **Stop** on the
progress card (`tag:abort`), **Close** (`card:close`).

## How it works

1. **Member registry** — Bot API cannot enumerate group members, so the
   registry is built from what the bot actually observes: every group
   message (write-behind, flushed every ~3 s), joins/leaves (service
   messages + `chat_member` updates), and optionally a Telethon sync.
   Each `/all` first **seeds from shared history** (`user_activity`,
   `daily_messages`, `group_members` → `seed_from_history()`), so a
   freshly restarted bot can still tag members who chatted before.
2. **Candidates** — for each `/all`: flush activity → seed history →
   fetch non-left members → exclude **admins** (fresh
   `getChatAdministrators` every run) and **bots** → apply the activity
   window → presence enrich → sort → apply the max cap.
3. **Ordering** — `online_first`/`all`: presence rank → last activity →
   user id; `recent`: activity only; `random`: shuffled.
   Ranks: `0` online (MTProto-confirmed only), `1` active ≤ 15 s,
   `2` active ≤ 24 h, `3` older. Activity **never** claims "online".
4. **Sending** — `copy_message` of the replied source (no re-upload),
   then mention batches: UTF-16 target 3600, hard limit 4096, ≤ 150
   mentions per message. Pacing: 0.35 s (normal) or 2 s (throttled)
   between batches. Flood waits ≤ 60 s are slept through with the cancel
   token raced against the timer; longer waits end the session. Terminal
   cards (done/stopped/failed) are edited into place with a **fresh-reply
   fallback**, so a failed edit can never leave a frozen card.
5. **Cancellation** — one session per chat (sync check-then-create, no
   awaits between); `/tagabort` sets a `CancelToken` that is checked
   between sends *and* during flood sleeps, so it always lands promptly.
6. **Crash recovery** — at startup any `running` session rows are marked
   `interrupted`. Sessions are **never auto-resumed**.

Exact user-facing strings live in `config.py` (`MSG_*`) and are asserted
by tests.

## Handler groups

| Group | Handlers |
|---|---|
| 0 | `/all` `/tagabort` `/allsettings` `/tagstats` `/tagall` `/etagall` `/cancel`, `@all`/`@eall` trigger, `tag:` callbacks |
| 15 | group message activity observer |
| 16 | join/leave service messages + `ChatMemberHandler` |
| 17 | catch-all callback activity observer |

> **Why not group 0 for joins?** `tagging` loads alphabetically before
> `users`/`welcome`; a second `NEW_CHAT_MEMBERS` handler in group 0
> would shadow them (PTB runs at most one handler per group).

## Environment variables (all optional)

| Variable | Purpose |
|---|---|
| `TAG_MTPROTO=1` | Enable MTProto enrichment (requires the two below + `pip install telethon`) |
| `TELEGRAM_API_ID` | api_id from my.telegram.org — must belong to a **user** account (bots cannot list members) |
| `TELEGRAM_API_HASH` | api_hash for the same account |

MTProto notes:
* Session file: `%LOCALAPPDATA%/PiBot/pi_tag.session`.
* The session must be authorized once interactively (run a small Telethon
  login script with the same session path); otherwise the module logs a
  warning and stays activity-only.
* Never required — every feature degrades gracefully without it.

## Database schema (shared `bot_database.db`)

| Table | Purpose |
|---|---|
| `tag_settings` | one row per chat: mode, window_hours, max_mentions, batch_size, send_mode, registry_mode |
| `tag_members` | observed registry: identity, first/last seen, last activity, presence, `left_at` marker |
| `tag_activity` | write-behind message counters per user/chat |
| `tag_sessions` | history: status (`running/completed/aborted/failed/interrupted`), totals, errors |

Settings values:

* **mode**: `online_first` \| `recent` \| `random` \| `all`
* **window**: 1/6/12/24/72/168 h, or 0 = off (window applies to
  `online_first` and `recent` only)
* **max**: 50/100/250/500/1000, or 0 = unlimited
* **batch**: 3200/3600/3800 UTF-16 units (default 3600)
* **send**: `normal` (0.35 s gap) \| `throttled` (2 s gap)
* **registry**: `hybrid` (activity + MTProto) \| `registry_only` (no
  MTProto calls) \| `sync` (full MTProto participant refresh before run)

## Integration points

* **Loader** — auto-discovered package; `setup(app)` lives in
  `__init__.py` (same pattern as `bind`/`instagram`).
* **Help** — interactive menu in `bot/modules/help.py`; module data lives
  in `HELP_MENU` (`bot/constants.py`) with the `tagging` entry rendered
  like every other module.
* **Command prefixes** — every command runs with `/` `!` `.` `#` `$` `%`
  `&` `?` (see `bot/command_handler.py`); the activity observer excludes
  prefix-commands via `~COMMAND` exactly like `~filters.COMMAND`.
* **Cards/buttons** — `bot/responses.py` (`action_card`,
  `plain_error/plain_ok`) and `bot/keyboards/colored.py`.
* **DB** — shared connection from `bot.database` (`from bot.database import db`),
  tables created by `tagging.database.ensure_tables()`.
* **Callbacks** — `tag:` prefix, group 0; `card:close` handled by
  `bot/modules/cards.py`.
* **Allowed updates** — `main.py` already passes `Update.ALL_TYPES`, so
  `chat_member` updates flow.

## Tests

```powershell
# from Pi/Pi
python tests/test_tagging.py
python tests/test_command_handler.py
```

`tests/test_tagging.py` isolates itself: `BOT_TOKEN` forced, and
`LOCALAPPDATA` redirected to a temp dir so a fresh SQLite file is created
(a placeholder file prevents the legacy-DB migration copy). No network —
handlers run against fake bots/messages, presence is stubbed. Covers
escaping, batching limits, sorting, settings cycles/validation, registry
dedup/leave/window, admin exclusion, cancel races, flood parsing,
session-per-chat, and the exact spec strings.

## File map

```
tagging/
├── __init__.py        setup(): tables, handlers, groups
├── handler.py         commands, tag: callbacks, observers
├── tagall.py          Boa-style /tagall /etagall @all @eall (Yumeko port)
├── sender.py          copy → batches → progress → final card
├── session.py         one-session-per-chat manager
├── cancellation.py    CancelToken (cancel-aware sleeps)
├── batcher.py         UTF-16 mention batching
├── mention_builder.py 👤 <a href="tg://user?id=…"> anchors
├── sorter.py          mode-specific ordering
├── member_registry.py candidate pipeline
├── activity_tracker.py write-behind buffer (~3 s flush)
├── permissions.py     fresh admin checks / admin exclusion
├── settings.py        load/cycle/parse /allsettings
├── keyboards.py       settings + Stop/Close buttons
├── database.py        tables + CRUD
├── config.py          groups, limits, cycles, MSG_* strings
├── models.py          TagSettings / Candidate dataclasses
├── exceptions.py      TaggingError hierarchy
├── cancellation.py    CancelToken
├── metrics.py         per-session counters
├── utils.py           fmt / progress bar / UTF-16 math
└── presence/
    ├── base.py        provider interface + ranks
    ├── activity.py    honest activity tiers
    ├── mtproto.py     optional Telethon enrichment
    └── manager.py     composite + source_label
```
