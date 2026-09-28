# Pi — Telegram Group Management Bot

A modular Telegram bot for community management: moderation, antispam,
welcomes, filters, levels/XP, chat stats, channel binds, Instagram
downloads, mass-tagging sessions, and an admin toolbox.

Built on [aiogram](https://docs.aiogram.dev/)
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
| `TELETHON_SESSION` | Authorized MTProto session (one-time: `python scripts/mtproto_login.py`) |
| `MONGO_URI` | MongoDB connection string (database defaults to `pi_bot`) |

## Run

```bash
python main.py
```

## Deploy on Railway

The repo ships a `Procfile` (`worker: python main.py`) — the bot long-
polls, so use a **worker** service with no port/healthcheck.

1. Import the GitHub repo into Railway; it builds with Nixpacks from
   `requirements.txt` (Python auto-detected).
2. Add these **Service Variables** (same keys as `.env.example`; no
   `.env` file is needed in the cloud):

   | Required | Optional |
   |----------|----------|
   | `BOT_TOKEN` | `TELEGRAM_API_ID` + `TELEGRAM_API_HASH` + `TAG_MTPROTO=1` + `TELETHON_SESSION` (one-time session via `python scripts/mtproto_login.py` — survives redeploys; without it MTProto gracefully disables) |
   | `OWNER_ID` | `FFMPEG_PATH`, `LOCAL_BOT_API_URL`, `IG_TEMP_DIR` |
   | `MONGO_URI` | |
   | `BOT_USERNAME` | |

3. Deploy. Startup posts a confirmation card to the log channel;
   `/restart` (owner) flushes write-behind buffers and re-executes the
   process in place via `os.execvp`.

### Restarting vs. deploying (updates)

* **Railway → Restart does NOT pick up new commits.** It re-runs the
  image of the *last deployment*. After `git push`, get the new code
  either by turning on **Auto Deploy** (project → GitHub repo → auto
  deploy on push) or by clicking **Redeploy** on the latest deployment.
* **Never delete the service to update code** — that also deletes its
  Service Variables (`MONGO_URI`, `BOT_TOKEN`, …) and any managed
  datastores attached to it. Push + Redeploy instead.
* **Restarts are data-safe**: everything lives in MongoDB (Atlas),
  which no restart/redeploy touches. The process flushes write-behind
  buffers on SIGTERM, so at most the sub-second tail of counters is at
  risk on a hard kill.
* **Verify each deploy in the logs** — three lines prove both code and
  database are the ones you expect:

  ```
  Loaded 33 module(s) — Phi π is ready (190 handlers — …)
  Database backend: mongodb (pi_bot)
  Database snapshot at boot: users=…, groups=…, daily_messages=…
  ```

  Wrong/missing handler count → old code still running (deploy, don't
  restart). `mongomock` or `MONGO_URI is not set` → the boot fails
  fast on purpose (an in-memory database would silently wipe itself on
  every restart). Empty snapshot on a bot that had data → `MONGO_URI`
  points at the wrong cluster/database.

One-time MTProto login (online presence + sticker tools): run
`python scripts/mtproto_login.py`, then put the printed
`TELETHON_SESSION` value in `.env` locally and in Railway Service
Variables.

Notes: video/GIF kangs use the bundled `imageio-ffmpeg` binary — no
system ffmpeg needed. Runtime state writes to `LOCALAPPDATA` when set,
otherwise to the project folder (ephemeral on Railway — sessions and
logs reset on redeploy). `/setprivacy → Disable` in BotFather is
required for group message features.

## Tests

```bash
python -m unittest discover -s tests
```

666 tests, no network access, no real MongoDB required (tests run
against an in-memory mongomock backend).

## Layout

```
main.py            entry point (polling loop)
Procfile           Railway worker start command
bot/
  config.py        .env loading
  database.py      MongoDB layer (indexes, ID counters)
  loader.py        auto-loads bot/modules/*.py
  command_handler.py  prefix-aware command wrappers
  modules/         feature modules (one file or package each)
  keyboards/       inline keyboard builders
tests/             unittest suite
```

## Notes

* Logs and runtime state live under `%LOCALAPPDATA%/PiBot/` on Windows
  (falls back to the project folder elsewhere).
* The bot must be admin in a group with delete-message, restrict-member
  and ban permissions for moderation commands to work.
