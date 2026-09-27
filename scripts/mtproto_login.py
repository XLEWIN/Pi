"""One-time interactive Telethon login → TELETHON_SESSION string.

Usage:
    python scripts/mtproto_login.py [--update-env]

Prompts for your phone number, the login code Telegram sends you (and
your 2FA password if the account has one), then prints a session
string for the TELETHON_SESSION key:

    local   → .env             (--update-env appends it for you)
    Railway → Service Variables → TELETHON_SESSION (survives redeploys)

The bot prefers TELETHON_SESSION over the sqlite session file
(LOCALAPPDATA/PiBot/pi_tag.session), which is wiped on every Railway
redeploy. Credentials come from the environment or .env
(TELEGRAM_API_ID / TELEGRAM_API_HASH from https://my.telegram.org).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Dict

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"
KEY = "TELETHON_SESSION"


def dotenv_values(path: Path) -> Dict[str, str]:
    """Parse KEY=VALUE lines from an env file (missing file → {})."""
    from dotenv import dotenv_values

    return {k: v for k, v in dotenv_values(path).items() if v is not None}


def append_env(path: Path, key: str, value: str) -> bool:
    """Append `key=value` to an env file. False when key already exists."""
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    for line in existing.splitlines():
        if line.lstrip().startswith(key + "="):
            return False
    sep = "" if existing.endswith("\n") or not existing else "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{sep}{key}={value}\n")
    return True


def credentials() -> tuple:
    """api_id/api_hash: process env → .env → interactive prompt."""
    file_vals = dotenv_values(ENV_FILE)
    api_id = (
        os.getenv("TELEGRAM_API_ID")
        or file_vals.get("TELEGRAM_API_ID")
        or input("TELEGRAM_API_ID: ").strip()
    )
    api_hash = (
        os.getenv("TELEGRAM_API_HASH")
        or file_vals.get("TELEGRAM_API_HASH")
        or input("TELEGRAM_API_HASH: ").strip()
    )
    if not api_id or not api_hash:
        sys.exit(
            "Missing TELEGRAM_API_ID / TELEGRAM_API_HASH — set them in "
            ".env (get both from https://my.telegram.org)."
        )
    return api_id, api_hash


async def login(api_id: str, api_hash: str) -> str:
    """Interactive phone → code → (2FA password) login; returns the string."""
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    client = TelegramClient(StringSession(), int(api_id), api_hash)
    await client.start()  # prompts on the terminal; never logs the code
    try:
        return client.session.save()
    finally:
        await client.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="One-time Telethon login producing TELETHON_SESSION"
    )
    parser.add_argument(
        "--update-env",
        action="store_true",
        help=f"append {KEY} to .env when the key is missing",
    )
    args = parser.parse_args()

    api_id, api_hash = credentials()
    try:
        session = asyncio.run(login(api_id, api_hash))
    except KeyboardInterrupt:
        sys.exit("\nLogin cancelled.")
    except Exception as e:  # noqa: BLE001 — friendly CLI failure
        sys.exit(f"Login failed: {type(e).__name__}: {e}")
    if not session:
        sys.exit(
            "Login finished but produced an empty session — is this a "
            "user account (not a bot)?"
        )

    print(f"\n{KEY}={session}\n")
    if args.update_env:
        if append_env(ENV_FILE, KEY, session):
            print(f"Appended {KEY} to {ENV_FILE}")
        else:
            print(
                f"{ENV_FILE} already contains {KEY} — replace its value "
                "manually with the string above."
            )
    else:
        print(f"Add to .env (or replace): {KEY}={session}")
    print(
        "Railway: Service Variables → add/update TELETHON_SESSION with "
        "the same value.\n"
        "Then restart (Telegram /restart or a redeploy) — the "
        "unauthorized-session warning is gone."
    )


if __name__ == "__main__":
    main()
