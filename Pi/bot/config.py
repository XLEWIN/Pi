"""Environment-based configuration.

All secrets live in the .env file (never commit it).
"""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _int_env(key: str, default: int) -> int:
    """Read an integer from the environment, falling back safely."""
    raw = os.getenv(key, "")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Settings:
    """Typed access to environment variables."""

    bot_token: str = os.getenv("BOT_TOKEN", "")
    owner_id: int = _int_env("OWNER_ID", 0)
    bot_username: str = os.getenv("BOT_USERNAME", "Phi_RoBot")

    # Telethon MTProto
    telethon_api_id: int = _int_env("TELETHON_API_ID", 0)
    telethon_api_hash: str = os.getenv("TELETHON_API_HASH", "")
    telethon_session: str = os.getenv("TELETHON_SESSION", "Phi_bot")


settings = Settings()

if not settings.bot_token:
    raise SystemExit(
        "BOT_TOKEN is missing. Copy .env.example to .env and fill in your token."
    )