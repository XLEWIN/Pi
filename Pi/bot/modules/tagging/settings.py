"""Per-chat settings — load, cycle, and parse `/allsettings` values."""

from __future__ import annotations

from typing import Any, Tuple

from . import config, database as tdb
from .models import TagSettings

MODE_LABELS = {
    "online_first": "Online First",
    "recent": "Recent",
    "random": "Random",
    "all": "All Members",
}
SEND_LABELS = {"normal": "Normal", "throttled": "Throttled"}
REGISTRY_LABELS = {
    "hybrid": "Hybrid",
    "registry_only": "Registry Only",
    "sync": "Sync",
}


def window_label(hours: int) -> str:
    return "Off" if hours <= 0 else f"{hours}h"


def max_label(n: int) -> str:
    return "All" if n <= 0 else str(n)


def get(chat_id: int) -> TagSettings:
    """Settings for a chat (defaults when no row exists)."""
    row = tdb.get_settings(chat_id)
    if not row:
        return TagSettings(chat_id=chat_id)
    return TagSettings(
        chat_id=chat_id,
        mode=row.get("mode", "online_first"),
        window_hours=int(row.get("window_hours", 24)),
        max_mentions=int(row.get("max_mentions", 0)),
        batch_size=int(row.get("batch_size", 3600)),
        send_mode=row.get("send_mode", "normal"),
        registry_mode=row.get("registry_mode", "hybrid"),
    )


def save(settings: TagSettings) -> TagSettings:
    tdb.update_settings(
        settings.chat_id,
        mode=settings.mode,
        window_hours=settings.window_hours,
        max_mentions=settings.max_mentions,
        batch_size=settings.batch_size,
        send_mode=settings.send_mode,
        registry_mode=settings.registry_mode,
    )
    return settings


def cycle(chat_id: int, key: str) -> TagSettings:
    """Advance one cycle key to its next value and persist."""
    if key not in config.CYCLES:
        raise ValueError(f"Unknown setting: {key}")
    options, _col = config.CYCLES[key]
    current = get(chat_id)
    value = getattr(current, key)
    try:
        idx = options.index(value)
    except ValueError:
        idx = -1
    nxt = options[(idx + 1) % len(options)]
    updated = current.replace_(**{key: nxt})
    return save(updated)


def apply_arg(chat_id: int, key: str, raw: str) -> TagSettings:
    """Parse and persist one `/allsettings <key> <value>` pair."""
    key = config.KEY_ALIASES.get(key, key)
    if key not in config.CYCLES:
        raise ValueError(f"Unknown setting: {key}")
    parsed = _parse_value(key, raw)
    current = get(chat_id)
    updated = current.replace_(**{key: parsed})
    return save(updated)


def _parse_value(key: str, raw: str) -> Any:
    raw = (raw or "").strip()
    low = raw.lower()

    if key == "mode":
        aliases = {
            "online": "online_first", "online_first": "online_first",
            "recent": "recent", "recent_active": "recent",
            "random": "random", "shuffle": "random",
            "all": "all", "everyone": "all",
        }
        if low not in aliases:
            raise ValueError(
                f"Unknown mode: {raw} (use "
                f"{', '.join(config.MODES)})"
            )
        return aliases[low]

    if key == "send_mode":
        if low not in config.SEND_MODES:
            raise ValueError(
                f"Unknown send mode: {raw} (use {', '.join(config.SEND_MODES)})"
            )
        return low

    if key == "registry_mode":
        if low not in config.REGISTRY_MODES:
            raise ValueError(
                f"Unknown registry mode: {raw} (use "
                f"{', '.join(config.REGISTRY_MODES)})"
            )
        return low

    # Numeric keys: window_hours, max_mentions, batch_size.
    text = low[:-1] if low.endswith("h") and key == "window_hours" else low
    if text in ("off", "none", "unlimited", "all"):
        if key == "window_hours":
            return 0
        if key == "max_mentions":
            return 0
        raise ValueError(f"{key} has no 'all' value")
    try:
        n = int(text)
    except ValueError:
        raise ValueError(f"Unknown value for {key}: {raw}") from None

    if key == "window_hours":
        if n < 0 or n > 8760:
            raise ValueError(f"Window out of range: {raw} (0–8760 hours)")
        # Any sane hour count is accepted; cycles still show presets.
        return n
    if key == "max_mentions":
        if n < 0:
            raise ValueError(f"max cannot be negative: {raw}")
        return n
    if key == "batch_size":
        if n < 500 or n > config.HARD_LIMIT:
            raise ValueError(
                f"Batch size out of range: {raw} (500–{config.HARD_LIMIT})"
            )
        return n
    raise ValueError(f"Unknown setting: {key}")


def describe(settings: TagSettings) -> list[Tuple[str, str, str]]:
    """(icon-emoji, label, value) rows for cards — icons added by caller."""
    return [
        ("mode", MODE_LABELS.get(settings.mode, settings.mode),
         settings.mode),
        ("window", window_label(settings.window_hours),
         str(settings.window_hours)),
        ("max", max_label(settings.max_mentions),
         str(settings.max_mentions)),
        ("batch", str(settings.batch_size), str(settings.batch_size)),
        ("send", SEND_LABELS.get(settings.send_mode, settings.send_mode),
         settings.send_mode),
        ("registry",
         REGISTRY_LABELS.get(settings.registry_mode, settings.registry_mode),
         settings.registry_mode),
    ]
