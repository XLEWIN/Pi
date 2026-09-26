# Pi — Telegram Group Management Bot

A modular Telegram bot for community management: moderation, antispam,
welcomes, filters, levels/XP, chat stats, channel binds, Instagram
downloads, mass-tagging sessions, and an admin toolbox.

Built on [python-telegram-bot](https://docs.python-telegram-bot.org/)
(long polling) with **MongoDB** for storage.

## Features

| Area | Module(s) |
|------|-----------|
| Moderation — mute/ban/kick/warn, rules | `moderation`, `bans`, `admin` |
| Admin toolbox (adminbox) | `adminbox` |
| Antispam, raids, shield, security | `antispam`, `security` |
| Blocklist & watch words | `blocklist`, `watchwords` |
| Auto-filters (keyword replies) | `filters` |
| Welcomes & join requests | `welcome` |
| Levels / XP, reputation, profiles | `leveling`, `profile`, `cards` |
| Chat stats & analytics | `chatstats`, `analytics` |
| Mass tagging (`/all`, `/tagall`) + MTProto presence | `tagging` |
| Channel binds (auto-post forwarding) | `bind` |
| Instagram downloads | `instagram` |
| Stickers, fun, misc tools | `sticker`, `fun`, `users`, `requests`, `template` |

Commands also respond to prefix aliases (`!`, `.`, `#`, `$`, `%`, `&`,
`?`) — see `bot/command_handler.py`.

## Requirements

* Python 3.11+
* A Telegram bot token ([@BotFather](https://t.me/BotFather))
* A MongoDB database (Atlas SRV or local)
* Optional: MTProto API credentials ([my.telegram.org](https://my.telegram.org))
  for tagging presence features

## Setup

```bash
pip install -r requirements.txt
```

Configure the bot — copy the template and fill it in (`.env` is
gitignored, never commit it):

```bash
cp .env.example .env
```

| Key | Purpose |
|-----|---------|
| `BOT_TOKEN` | Bot token from @BotFather |
| `OWNER_ID` | Numeric owner user ID |
| `BOT_USERNAME` | Bot username (no @) |
| `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` | MTProto credentials |
| `TAG_MTPROTO` | Tagging module MTProto session |
| `MONGO_URI` | MongoDB connection string (database defaults to `pi_bot`) |

### Migrating from the old SQLite database

If you have legacy data in `bot_database.db`:

```bash
python scripts/migrate_sqlite_to_mongo.py --dry-run   # inspect first
python scripts/migrate_sqlite_to_mongo.py             # then migrate
```

The script never modifies the source files (it copies db + WAL to a
temp dir), preserves all IDs, and rewinds the ID counters. It refuses
to write into a non-empty database unless you pass `--overwrite`.

## Run

```bash
python main.py
```

## Tests

```bash
python -m unittest discover -s tests
```

514 tests, no network access, no real MongoDB required (tests run
against an in-memory mongomock backend).

## Layout

```
main.py            entry point (polling loop)
bot/
  config.py        .env loading
  database.py      MongoDB layer (indexes, ID counters)
  loader.py        auto-loads bot/modules/*.py
  command_handler.py  prefix-aware command wrappers
  modules/         feature modules (one file or package each)
  keyboards/       inline keyboard builders
scripts/           one-shot utilities (SQLite → Mongo migration)
tests/             unittest suite
```

## Notes

* Logs and runtime state live under `%LOCALAPPDATA%/PiBot/` on Windows
  (falls back to the project folder elsewhere).
* The bot must be admin in a group with delete-message, restrict-member
  and ban permissions for moderation commands to work.
