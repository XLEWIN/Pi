"""Bind module database — tables + CRUD on the shared PiBot SQLite file."""

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from bot.database import db as _db, ThreadLocalConn

logger = logging.getLogger(__name__)

# Thread-safe view of the shared connection (never bind `_db.connection`
# directly — that pins one thread's connection and corrupts under
# executor use).
_conn = ThreadLocalConn(_db)


def ensure_tables() -> None:
    """Create bind tables if missing. Safe to call once at setup()."""
    try:
        cur = _conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS bind_settings (
                chat_id INTEGER PRIMARY KEY,
                channel_id INTEGER,
                channel_username TEXT,
                channel_title TEXT,
                channel_link TEXT,
                force_join INTEGER DEFAULT 1,
                gate_text INTEGER DEFAULT 0,
                gate_media INTEGER DEFAULT 0,
                gate_link INTEGER DEFAULT 0,
                gate_document INTEGER DEFAULT 0,
                gate_gif INTEGER DEFAULT 0,
                gate_audio INTEGER DEFAULT 0,
                gate_sticker INTEGER DEFAULT 0,
                admin_bypass INTEGER DEFAULT 1,
                grace_minutes INTEGER DEFAULT 0,
                auto_delete_seconds INTEGER DEFAULT 0,
                custom_message TEXT,
                bound_at TIMESTAMP,
                bound_by INTEGER
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS bind_user_joins (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                joined_at REAL NOT NULL,
                PRIMARY KEY (chat_id, user_id)
            )
            """
        )
        # Track pending warning messages for auto-delete cleanup.
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS bind_warnings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                user_id INTEGER,
                created_at REAL NOT NULL
            )
            """
        )
        # One channel ↔ one group: partial unique index on channel_id.
        # (App-level checks catch this first with a friendly message.)
        try:
            cur.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_bind_settings_channel
                ON bind_settings(channel_id)
                WHERE channel_id IS NOT NULL
                """
            )
        except sqlite3.Error as e:
            logger.warning(f"Bind channel unique index skipped (duplicate rows?): {e}")
        _conn.commit()
        logger.info("Bind tables created/verified")
    except sqlite3.Error as e:
        logger.error(f"Bind table creation failed: {e}")


def _row_to_settings(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    # Normalize ints → bool-like 0/1 as ints for handlers.
    for key in (
        "force_join",
        "gate_text",
        "gate_media",
        "gate_link",
        "gate_document",
        "gate_gif",
        "gate_audio",
        "gate_sticker",
        "admin_bypass",
    ):
        d[key] = int(d.get(key) or 0)
    d["grace_minutes"] = int(d.get("grace_minutes") or 0)
    d["auto_delete_seconds"] = int(d.get("auto_delete_seconds") or 0)
    return d


def get_settings(chat_id: int) -> Optional[Dict[str, Any]]:
    """Return bind settings for a group, or None if unbound."""
    try:
        cur = _conn.cursor()
        cur.execute("SELECT * FROM bind_settings WHERE chat_id = ?", (chat_id,))
        row = cur.fetchone()
        if row is None:
            return None
        d = _row_to_settings(row)
        if not d.get("channel_id"):
            return None
        return d
    except sqlite3.Error as e:
        logger.error(f"bind get_settings({chat_id}): {e}")
        return None


def find_other_binding(channel_id: int, exclude_chat_id: int) -> Optional[Dict[str, Any]]:
    """Settings row for a *different* group already bound to this channel."""
    try:
        cur = _conn.cursor()
        cur.execute(
            "SELECT * FROM bind_settings WHERE channel_id = ? AND chat_id != ? LIMIT 1",
            (channel_id, exclude_chat_id),
        )
        row = cur.fetchone()
        return _row_to_settings(row) if row else None
    except sqlite3.Error as e:
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
        cur = _conn.cursor()
        cur.execute(
            """
            INSERT INTO bind_settings (
                chat_id, channel_id, channel_username, channel_title, channel_link,
                force_join, gate_text, gate_media, gate_link, gate_document,
                gate_gif, gate_audio, gate_sticker, admin_bypass,
                grace_minutes, auto_delete_seconds, custom_message,
                bound_at, bound_by
            )
            VALUES (?, ?, ?, ?, ?, 1, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, NULL, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                channel_id = excluded.channel_id,
                channel_username = excluded.channel_username,
                channel_title = excluded.channel_title,
                channel_link = excluded.channel_link,
                bound_at = excluded.bound_at,
                bound_by = excluded.bound_by
            """,
            (chat_id, channel_id, channel_username, channel_title, channel_link, now, bound_by),
        )
        _conn.commit()
        return get_settings(chat_id) or {}
    except sqlite3.IntegrityError:
        # Unique channel_id index — another group claimed it between check & write.
        logger.warning(f"bind upsert: channel {channel_id} already taken (chat {chat_id})")
        raise ValueError("CHANNEL_TAKEN") from None
    except sqlite3.Error as e:
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
        cur = _conn.cursor()
        if isinstance(value, bool):
            value = 1 if value else 0
        cur.execute(
            f"UPDATE bind_settings SET {field} = ? WHERE chat_id = ?",
            (value, chat_id),
        )
        _conn.commit()
        return get_settings(chat_id)
    except sqlite3.Error as e:
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
        cur = _conn.cursor()
        cur.execute("DELETE FROM bind_settings WHERE chat_id = ?", (chat_id,))
        removed = cur.rowcount > 0
        _conn.commit()
        cur.execute("DELETE FROM bind_user_joins WHERE chat_id = ?", (chat_id,))
        _conn.commit()
        cur.execute("DELETE FROM bind_warnings WHERE chat_id = ?", (chat_id,))
        _conn.commit()
        return removed
    except sqlite3.Error as e:
        logger.error(f"bind remove_binding({chat_id}): {e}")
        return False


def record_join(chat_id: int, user_id: int, joined_at: Optional[float] = None) -> None:
    """Store first-seen join time for a user in this group (INSERT OR IGNORE)."""
    ts = joined_at if joined_at is not None else datetime.now(timezone.utc).timestamp()
    try:
        cur = _conn.cursor()
        cur.execute(
            "INSERT OR IGNORE INTO bind_user_joins (chat_id, user_id, joined_at) VALUES (?, ?, ?)",
            (chat_id, user_id, ts),
        )
        _conn.commit()
    except sqlite3.Error as e:
        logger.error(f"bind record_join({chat_id},{user_id}): {e}")


def get_join_time(chat_id: int, user_id: int) -> Optional[float]:
    """Return stored join timestamp, or None if unknown."""
    try:
        cur = _conn.cursor()
        cur.execute(
            "SELECT joined_at FROM bind_user_joins WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        )
        row = cur.fetchone()
        return float(row[0]) if row else None
    except sqlite3.Error as e:
        logger.error(f"bind get_join_time({chat_id},{user_id}): {e}")
        return None


def clear_join(chat_id: int, user_id: int) -> None:
    """Drop join time (e.g. after left-kick so rejoin starts fresh)."""
    try:
        cur = _conn.cursor()
        cur.execute(
            "DELETE FROM bind_user_joins WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        )
        _conn.commit()
    except sqlite3.Error as e:
        logger.error(f"bind clear_join: {e}")


def add_warning(chat_id: int, message_id: int, user_id: int) -> None:
    """Track a force-join warning message for auto-delete."""
    ts = datetime.now(timezone.utc).timestamp()
    try:
        cur = _conn.cursor()
        cur.execute(
            "INSERT INTO bind_warnings (chat_id, message_id, user_id, created_at) VALUES (?, ?, ?, ?)",
            (chat_id, message_id, user_id, ts),
        )
        _conn.commit()
    except sqlite3.Error as e:
        logger.error(f"bind add_warning: {e}")


def remove_warning(chat_id: int, message_id: int) -> None:
    try:
        cur = _conn.cursor()
        cur.execute(
            "DELETE FROM bind_warnings WHERE chat_id = ? AND message_id = ?",
            (chat_id, message_id),
        )
        _conn.commit()
    except sqlite3.Error:
        pass


def list_bindings() -> List[Dict[str, Any]]:
    """All active bindings (for status/debug)."""
    try:
        cur = _conn.cursor()
        cur.execute("SELECT * FROM bind_settings WHERE channel_id IS NOT NULL")
        return [_row_to_settings(r) for r in cur.fetchall()]
    except sqlite3.Error:
        return []
