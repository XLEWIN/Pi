"""Bind module database — collections + CRUD on the shared MongoDB."""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from bot.database import db as _db

logger = logging.getLogger(__name__)

# Column defaults materialised by the old sqlite SELECT * on partial rows.
_DEFAULTS: Dict[str, Any] = {
    "channel_id": None,
    "channel_username": None,
    "channel_title": None,
    "channel_link": None,
    "force_join": 1,
    "gate_text": 0,
    "gate_media": 0,
    "gate_link": 0,
    "gate_document": 0,
    "gate_gif": 0,
    "gate_audio": 0,
    "gate_sticker": 0,
    "admin_bypass": 1,
    "grace_minutes": 0,
    "auto_delete_seconds": 0,
    "custom_message": None,
    "bound_at": None,
    "bound_by": None,
}

_INT_COLS = (
    "force_join",
    "gate_text",
    "gate_media",
    "gate_link",
    "gate_document",
    "gate_gif",
    "gate_audio",
    "gate_sticker",
    "admin_bypass",
)


def ensure_tables() -> None:
    """Create bind indexes if missing. Safe to call once at setup()."""
    try:
        _db.collection("bind_settings").create_index("chat_id", unique=True)
        # One channel ↔ one group (sparse: rows without a channel skip it).
        _db.collection("bind_settings").create_index(
            "channel_id", unique=True, sparse=True
        )
        _db.collection("bind_user_joins").create_index(
            [("chat_id", 1), ("user_id", 1)], unique=True
        )
        _db.collection("bind_warnings").create_index("message_id")
        _db.collection("bind_warnings").create_index("chat_id")
        logger.info("Bind indexes created/verified")
    except Exception as e:
        logger.error(f"Bind index creation failed: {e}")


def _row_to_settings(raw: Dict[str, Any]) -> Dict[str, Any]:
    d = {**_DEFAULTS, **raw}
    # Normalize ints → bool-like 0/1 as ints for handlers.
    for key in _INT_COLS:
        d[key] = int(d.get(key) or 0)
    d["grace_minutes"] = int(d.get("grace_minutes") or 0)
    d["auto_delete_seconds"] = int(d.get("auto_delete_seconds") or 0)
    return d


def get_settings(chat_id: int) -> Optional[Dict[str, Any]]:
    """Return bind settings for a group, or None if unbound.

    Cached through ``_db._cached_read``: this runs on EVERY group
    message (``gate_message_handler``) and used to be a raw
    ``_find_one`` — one Mongo round-trip per message even though
    bindings change rarely. Writes to ``bind_settings`` invalidate
    it via ``_CACHE_SOURCES``.
    """
    try:
        raw = _db._cached_read(
            "get_bind_settings",
            (chat_id,),
            lambda: _db._find_one("bind_settings", {"chat_id": chat_id}),
        )
        if raw is None:
            return None
        d = _row_to_settings(raw)
        if not d.get("channel_id"):
            return None
        return d
    except Exception as e:
        logger.error(f"bind get_settings({chat_id}): {e}")
        return None


def find_other_binding(channel_id: int, exclude_chat_id: int) -> Optional[Dict[str, Any]]:
    """Settings row for a *different* group already bound to this channel."""
    try:
        raw = _db._cached_read(
            "find_other_binding",
            (channel_id, exclude_chat_id),
            lambda: _db._find_one(
                "bind_settings",
                {"channel_id": channel_id, "chat_id": {"$ne": exclude_chat_id}},
            ),
        )
        return _row_to_settings(raw) if raw else None
    except Exception as e:
        logger.error(f"bind find_other_binding({channel_id},{exclude_chat_id}): {e}")
        return None


def upsert_binding(
    chat_id: int,
    channel_id: int,
    *,
    channel_username: Optional[str] = None,
    channel_title: Optional[str] = None,
    channel_link: Optional[str] = None,
    bound_by: Optional[int] = None,
) -> Dict[str, Any]:
    """Bind (or rebind) a group to a channel. Returns fresh settings row.

    Raises ValueError if the channel is already bound to another group
    (one channel ↔ one group at a time).
    """
    if channel_id is None:
        raise ValueError("channel_id is required")
    other = find_other_binding(channel_id, chat_id)
    if other:
        raise ValueError("CHANNEL_TAKEN")

    now = datetime.now(timezone.utc).isoformat()
    try:
        # Existing rows keep their gate settings ($set only touches the
        # bind columns — same as the old ON CONFLICT DO UPDATE SET);
        # new rows get the schema defaults ($setOnInsert).
        _db.collection("bind_settings").update_one(
            {"chat_id": chat_id},
            {
                "$set": {
                    "channel_id": channel_id,
                    "channel_username": channel_username,
                    "channel_title": channel_title,
                    "channel_link": channel_link,
                    "bound_at": now,
                    "bound_by": bound_by,
                },
                "$setOnInsert": {
                    "force_join": 1,
                    "gate_text": 0,
                    "gate_media": 0,
                    "gate_link": 0,
                    "gate_document": 0,
                    "gate_gif": 0,
                    "gate_audio": 0,
                    "gate_sticker": 0,
                    "admin_bypass": 1,
                    "grace_minutes": 0,
                    "auto_delete_seconds": 0,
                    "custom_message": None,
                },
            },
            upsert=True,
        )
        return get_settings(chat_id) or {}
    except Exception as e:
        # Unique channel index — another group claimed it between check & write.
        if "duplicate" in str(e).lower() or "E11000" in str(e):
            logger.warning(
                f"bind upsert: channel {channel_id} already taken (chat {chat_id})"
            )
            raise ValueError("CHANNEL_TAKEN") from None
        logger.error(f"bind upsert_binding({chat_id}): {e}")
        raise


def update_field(chat_id: int, field: str, value: Any) -> Optional[Dict[str, Any]]:
    """Update a single bind_settings column and return refreshed settings."""
    allowed = {
        "channel_id",
        "channel_username",
        "channel_title",
        "channel_link",
        "force_join",
        "gate_text",
        "gate_media",
        "gate_link",
        "gate_document",
        "gate_gif",
        "gate_audio",
        "gate_sticker",
        "admin_bypass",
        "grace_minutes",
        "auto_delete_seconds",
        "custom_message",
    }
    if field not in allowed:
        logger.warning(f"bind update_field: blocked unknown field '{field}'")
        return get_settings(chat_id)
    try:
        if isinstance(value, bool):
            value = 1 if value else 0
        if field == "channel_id" and value is None:
            # Keep the sparse unique index clean — absent beats explicit null.
            _db.collection("bind_settings").update_one(
                {"chat_id": chat_id}, {"$unset": {field: ""}}
            )
        else:
            _db.collection("bind_settings").update_one(
                {"chat_id": chat_id}, {"$set": {field: value}}
            )
        return get_settings(chat_id)
    except Exception as e:
        logger.error(f"bind update_field({chat_id}, {field}): {e}")
        return get_settings(chat_id)


def toggle_field(chat_id: int, field: str) -> Optional[Dict[str, Any]]:
    """Flip a 0/1 column and return refreshed settings."""
    settings = get_settings(chat_id)
    if not settings:
        return None
    return update_field(chat_id, field, 0 if settings.get(field) else 1)


def remove_binding(chat_id: int) -> bool:
    """Delete the group's bind row (unbind)."""
    try:
        res = _db.collection("bind_settings").delete_one({"chat_id": chat_id})
        _db.collection("bind_user_joins").delete_many({"chat_id": chat_id})
        _db.collection("bind_warnings").delete_many({"chat_id": chat_id})
        return res.deleted_count > 0
    except Exception as e:
        logger.error(f"bind remove_binding({chat_id}): {e}")
        return False


def record_join(chat_id: int, user_id: int, joined_at: Optional[float] = None) -> None:
    """Store first-seen join time for a user in this group (INSERT OR IGNORE)."""
    ts = joined_at if joined_at is not None else datetime.now(timezone.utc).timestamp()
    try:
        _db.collection("bind_user_joins").update_one(
            {"chat_id": chat_id, "user_id": user_id},
            {"$setOnInsert": {"chat_id": chat_id, "user_id": user_id, "joined_at": ts}},
            upsert=True,
        )
    except Exception as e:
        logger.error(f"bind record_join({chat_id},{user_id}): {e}")


def get_join_time(chat_id: int, user_id: int) -> Optional[float]:
    """Return stored join timestamp, or None if unknown."""
    try:
        raw = _db._find_one(
            "bind_user_joins",
            {"chat_id": chat_id, "user_id": user_id},
            projection={"joined_at": 1, "_id": 0},
        )
        return float(raw["joined_at"]) if raw else None
    except Exception as e:
        logger.error(f"bind get_join_time({chat_id},{user_id}): {e}")
        return None


def clear_join(chat_id: int, user_id: int) -> None:
    """Drop join time (e.g. after left-kick so rejoin starts fresh)."""
    try:
        _db.collection("bind_user_joins").delete_one(
            {"chat_id": chat_id, "user_id": user_id}
        )
    except Exception as e:
        logger.error(f"bind clear_join({chat_id},{user_id}): {e}")


def add_warning(chat_id: int, message_id: int, user_id: int) -> None:
    """Track a force-join warning message for auto-delete."""
    ts = datetime.now(timezone.utc).timestamp()
    try:
        _db.collection("bind_warnings").insert_one(
            {
                "id": _db._next_id("bind_warnings"),
                "chat_id": chat_id,
                "message_id": message_id,
                "user_id": user_id,
                "created_at": ts,
            }
        )
    except Exception as e:
        logger.error(f"bind add_warning({chat_id},{message_id}): {e}")


def remove_warning(chat_id: int, message_id: int) -> None:
    try:
        _db.collection("bind_warnings").delete_one(
            {"chat_id": chat_id, "message_id": message_id}
        )
    except Exception:
        pass


def list_bindings() -> List[Dict[str, Any]]:
    """All active bindings (for status/debug)."""
    try:
        rows = _db._find(
            "bind_settings", {"channel_id": {"$exists": True, "$ne": None}}
        )
        return [_row_to_settings(r) for r in rows]
    except Exception:
        return []
