"""Tagging module database — tables + CRUD on the shared PiBot SQLite file.

Schema (see README.md):

    tag_settings   one row per chat — /allsettings values
    tag_members    observed member registry (dedup by chat+user)
    tag_activity   write-behind activity counters (flushed every ~3s)
    tag_sessions   session history for /tagstats + crash recovery
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from bot.database import db as _db
from bot.logger import logger

# Shared connection from bot.database (WAL, busy_timeout already set).
_conn = _db.connection


def now() -> float:
    return time.time()


# ── Schema ────────────────────────────────────────────────────────

def ensure_tables() -> None:
    """Create tagging tables if missing. Safe to call once at setup()."""
    cur = _conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS tag_settings (
            chat_id INTEGER PRIMARY KEY,
            mode TEXT NOT NULL DEFAULT 'online_first',
            window_hours INTEGER NOT NULL DEFAULT 24,
            max_mentions INTEGER NOT NULL DEFAULT 0,
            batch_size INTEGER NOT NULL DEFAULT 3600,
            send_mode TEXT NOT NULL DEFAULT 'normal',
            registry_mode TEXT NOT NULL DEFAULT 'hybrid',
            updated_at REAL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS tag_members (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT,
            display_name TEXT,
            is_bot INTEGER NOT NULL DEFAULT 0,
            first_seen REAL NOT NULL,
            last_seen REAL NOT NULL,
            last_active_at REAL,
            presence_at REAL,
            left_at REAL,
            PRIMARY KEY (chat_id, user_id)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS tag_activity (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            last_message_at REAL NOT NULL,
            message_count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (chat_id, user_id)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS tag_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            started_by INTEGER NOT NULL,
            started_at REAL NOT NULL,
            finished_at REAL,
            status TEXT NOT NULL DEFAULT 'running',
            total INTEGER NOT NULL DEFAULT 0,
            tagged INTEGER NOT NULL DEFAULT 0,
            messages_sent INTEGER NOT NULL DEFAULT 0,
            mode TEXT,
            window_hours INTEGER,
            error TEXT
        )
        """
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_tag_members_chat "
        "ON tag_members(chat_id, left_at)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_tag_sessions_chat "
        "ON tag_sessions(chat_id, started_at)"
    )
    _conn.commit()
    logger.info("Tagging tables created/verified")


def mark_interrupted() -> int:
    """Mark rows left 'running' by a crash/restart as interrupted.

    Never auto-resumes: a restart must not surprise the chat with a
    resumed mass tag.
    """
    cur = _conn.execute(
        "UPDATE tag_sessions SET status='interrupted', finished_at=? "
        "WHERE status='running'",
        (now(),),
    )
    _conn.commit()
    n = cur.rowcount or 0
    if n:
        logger.info(f"Tagging: marked {n} interrupted session(s)")
    return n


# ── Settings ──────────────────────────────────────────────────────

_SETTINGS_COLS = (
    "mode", "window_hours", "max_mentions", "batch_size",
    "send_mode", "registry_mode",
)


def get_settings(chat_id: int) -> Optional[Dict[str, Any]]:
    row = _conn.execute(
        f"SELECT chat_id, {', '.join(_SETTINGS_COLS)} FROM tag_settings "
        "WHERE chat_id=?",
        (chat_id,),
    ).fetchone()
    return dict(row) if row else None


def update_settings(chat_id: int, **fields: Any) -> Dict[str, Any]:
    """Insert-or-update specific settings columns for a chat."""
    valid = {k: v for k, v in fields.items() if k in _SETTINGS_COLS}
    if not valid:
        existing = get_settings(chat_id)
        if existing:
            return existing
        _conn.execute(
            "INSERT INTO tag_settings (chat_id, updated_at) VALUES (?, ?)",
            (chat_id, now()),
        )
        _conn.commit()
        return get_settings(chat_id) or {"chat_id": chat_id}

    cols = ", ".join(valid)
    placeholders = ", ".join("?" for _ in valid)
    updates = ", ".join(f"{k}=excluded.{k}" for k in valid)
    # SQLite forbids table-qualified columns inside DO UPDATE SET.
    _conn.execute(
        f"INSERT INTO tag_settings (chat_id, updated_at, {cols}) "
        f"VALUES (?, ?, {placeholders}) "
        f"ON CONFLICT(chat_id) DO UPDATE SET {updates}, "
        f"updated_at=excluded.updated_at",
        (chat_id, now(), *(valid[k] for k in valid)),
    )
    _conn.commit()
    return get_settings(chat_id) or {"chat_id": chat_id, **valid}


# ── Member registry ───────────────────────────────────────────────

def upsert_member(
    chat_id: int,
    user_id: int,
    *,
    username: Optional[str] = None,
    display_name: Optional[str] = None,
    is_bot: bool = False,
    seen_at: Optional[float] = None,
    active_at: Optional[float] = None,
    join: bool = False,
) -> None:
    """Insert or refresh one registry row (dedup by chat+user)."""
    ts = seen_at if seen_at is not None else now()
    row = _conn.execute(
        "SELECT first_seen, left_at FROM tag_members "
        "WHERE chat_id=? AND user_id=?",
        (chat_id, user_id),
    ).fetchone()
    if row is None:
        _conn.execute(
            "INSERT INTO tag_members "
            "(chat_id, user_id, username, display_name, is_bot, "
            " first_seen, last_seen, last_active_at, left_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            (chat_id, user_id, username, display_name, int(is_bot),
             ts, ts, active_at),
        )
    else:
        sets = ["last_seen=?"]
        args: List[Any] = [ts]
        if username is not None:
            sets.append("username=?")
            args.append(username)
        if display_name is not None:
            sets.append("display_name=?")
            args.append(display_name)
        if active_at is not None:
            sets.append("last_active_at=COALESCE(?, last_active_at)")
            args.append(active_at)
        if join or row["left_at"] is not None:
            # Re-join clears the leave marker and re-anchors first_seen.
            sets.append("left_at=NULL")
        args.extend([chat_id, user_id])
        _conn.execute(
            f"UPDATE tag_members SET {', '.join(sets)} "
            "WHERE chat_id=? AND user_id=?",
            args,
        )
    _conn.commit()


def mark_join(
    chat_id: int,
    user_id: int,
    *,
    username: Optional[str] = None,
    display_name: Optional[str] = None,
    is_bot: bool = False,
) -> None:
    """Record a join: clear left_at, refresh identity."""
    upsert_member(
        chat_id, user_id, username=username, display_name=display_name,
        is_bot=is_bot, join=True,
    )


def mark_leave(chat_id: int, user_id: int) -> None:
    """Record a leave — row stays for dedup, excluded from candidates."""
    _conn.execute(
        "UPDATE tag_members SET left_at=? WHERE chat_id=? AND user_id=?",
        (now(), chat_id, user_id),
    )
    _conn.commit()


def set_presence(chat_id: int, user_id: int, ts: float) -> None:
    """Store best-known presence timestamp (MTProto only, never activity)."""
    _conn.execute(
        "UPDATE tag_members SET presence_at=? "
        "WHERE chat_id=? AND user_id=?",
        (ts, chat_id, user_id),
    )
    _conn.commit()


def fetch_members(chat_id: int) -> List[Dict[str, Any]]:
    """All non-left members of a chat (identity + activity + presence)."""
    rows = _conn.execute(
        "SELECT user_id, username, display_name, is_bot, first_seen, "
        "last_active_at, presence_at "
        "FROM tag_members WHERE chat_id=? AND left_at IS NULL",
        (chat_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def count_members(chat_id: int) -> int:
    row = _conn.execute(
        "SELECT COUNT(*) AS n FROM tag_members "
        "WHERE chat_id=? AND left_at IS NULL",
        (chat_id,),
    ).fetchone()
    return int(row["n"]) if row else 0


def count_active(chat_id: int, since: float) -> int:
    """Members whose activity/presence signal is newer than `since`."""
    row = _conn.execute(
        "SELECT COUNT(*) AS n FROM tag_members "
        "WHERE chat_id=? AND left_at IS NULL AND "
        "COALESCE(last_active_at, first_seen, 0) >= ?",
        (chat_id, since),
    ).fetchone()
    return int(row["n"]) if row else 0


# ── History seeding (fresh bots / quiet chats) ────────────────────

def _parse_ts(value: Any) -> float:
    """Shared-table timestamp → epoch seconds (0.0 when unparseable).

    Handles the three formats found in the shared tables:
      * 'YYYY-MM-DD HH:MM:SS'  — SQLite CURRENT_TIMESTAMP, **UTC**
      * isoformat with 'T'     — written by Python, local wall-clock
      * 'YYYY-MM-DD'           — daily_messages.date, local midnight
    """
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return 0.0
    if len(text) == 10:  # bare date → local midnight
        try:
            return datetime.strptime(text, "%Y-%m-%d").timestamp()
        except ValueError:
            return 0.0
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if dt.tzinfo is None:
        if "T" in text:
            dt = dt.astimezone()          # naive local (Python isoformat)
        else:
            dt = dt.replace(tzinfo=timezone.utc)  # CURRENT_TIMESTAMP = UTC
    return dt.timestamp()


def seed_from_history(chat_id: int) -> int:
    """Backfill tag_members from the shared history tables.

    The activity observer only learns members from messages seen since
    the bot started — a /all right after a restart would otherwise find
    nobody. Merges user_activity (UTC), daily_messages (local date) and
    group_members (joined_at) into one best-known timestamp per user,
    enriches identity from `users`, then reuses bulk_observe() whose MAX
    semantics never regress fresher live rows or re-open a leave.

    Returns the number of members upserted (0 when there is no history).
    """
    # 1. Best-known activity timestamp per user, across every source.
    best: Dict[int, float] = {}
    bot_ids: set = set()
    sources = (
        "SELECT user_id, MAX(timestamp) AS ts FROM user_activity "
        "WHERE chat_id=? GROUP BY user_id",
        "SELECT user_id, MAX(date) AS ts FROM daily_messages "
        "WHERE chat_id=? GROUP BY user_id",
        "SELECT user_id, MAX(joined_at) AS ts, "
        "MAX(CASE WHEN role='bot' THEN 1 ELSE 0 END) AS isbot "
        "FROM group_members WHERE chat_id=? GROUP BY user_id",
    )
    try:
        for sql in sources:
            for row in _conn.execute(sql, (chat_id,)).fetchall():
                uid = int(row["user_id"])
                ts = _parse_ts(row["ts"])
                if ts > best.get(uid, 0.0):
                    best[uid] = ts
                keys = row.keys()
                if "isbot" in keys and row["isbot"]:
                    bot_ids.add(uid)
    except sqlite3.Error as e:
        logger.warning(f"Tagging history seed query failed: {e}")
        return 0
    if not best:
        return 0

    # 2. Identity from the shared users table (chunked IN lists).
    ids = sorted(best)
    identity: Dict[int, Tuple[Optional[str], str]] = {}
    try:
        for i in range(0, len(ids), 400):
            chunk = ids[i:i + 400]
            marks = ",".join("?" for _ in chunk)
            for row in _conn.execute(
                "SELECT user_id, username, first_name, last_name, is_bot "
                f"FROM users WHERE user_id IN ({marks})",
                chunk,
            ).fetchall():
                uid = int(row["user_id"])
                name = " ".join(
                    p for p in (row["first_name"] or "",
                                row["last_name"] or "") if p
                ) or str(uid)
                identity[uid] = (row["username"], name)
                if row["is_bot"]:
                    bot_ids.add(uid)
    except sqlite3.Error as e:
        logger.warning(f"Tagging history identity lookup failed: {e}")

    # 3. One upsert pass — same code path as the live write-behind.
    items = [
        (
            chat_id,
            uid,
            *identity.get(uid, (None, str(uid))),
            1 if uid in bot_ids else 0,
            best[uid],
        )
        for uid in ids
    ]
    bulk_observe(items)
    return len(items)


# ── Activity write-behind ─────────────────────────────────────────

def bulk_observe(
    items: Sequence[Tuple[int, int, Optional[str], Optional[str], int, float]]
) -> None:
    """One-shot member identity/activity upserts (single commit).

    items: (chat_id, user_id, username, display_name, is_bot, seen_ts)
    Used by the write-behind flush — never call per message.
    """
    rows = [
        (c, u, un, dn, bot, ts, ts, ts)   # first_seen = last_seen = active
        for (c, u, un, dn, bot, ts) in items
        if ts > 0
    ]
    if not rows:
        return
    try:
        _conn.executemany(
            """
            INSERT INTO tag_members
                (chat_id, user_id, username, display_name, is_bot,
                 first_seen, last_seen, last_active_at, left_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET
                username=COALESCE(excluded.username, username),
                display_name=COALESCE(excluded.display_name, display_name),
                last_seen=MAX(last_seen, excluded.last_seen),
                last_active_at=MAX(COALESCE(last_active_at, 0),
                                   excluded.last_active_at),
                left_at=CASE
                    WHEN excluded.last_active_at >= COALESCE(left_at, 0)
                    THEN NULL ELSE left_at END
            """,
            rows,
        )
        _conn.commit()
    except sqlite3.Error as e:
        logger.warning(f"Tagging bulk observe failed: {e}")
        _conn.rollback()


def flush_activity(items: Iterable[Tuple[int, int, int, float]]) -> None:
    """Persist buffered counters: (chat_id, user_id, count, last_ts).

    Only the tag_activity table — identity/activity columns are written
    by bulk_observe() so one flush = two commits total.
    """
    batch = list(items)
    if not batch:
        return
    try:
        _conn.executemany(
            """
            INSERT INTO tag_activity (chat_id, user_id, last_message_at, message_count)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET
                last_message_at=excluded.last_message_at,
                message_count=message_count + excluded.message_count
            """,
            [(c, u, ts, n) for (c, u, n, ts) in batch],
        )
        _conn.commit()
    except sqlite3.Error as e:
        logger.warning(f"Tagging activity flush failed: {e}")
        _conn.rollback()


# ── Sessions ──────────────────────────────────────────────────────

def create_session(
    chat_id: int,
    started_by: int,
    *,
    mode: str,
    window_hours: int,
) -> int:
    cur = _conn.execute(
        "INSERT INTO tag_sessions "
        "(chat_id, started_by, started_at, status, mode, window_hours) "
        "VALUES (?, ?, ?, 'running', ?, ?)",
        (chat_id, started_by, now(), mode, window_hours),
    )
    _conn.commit()
    return int(cur.lastrowid)


def finish_session(
    session_id: int,
    status: str,
    *,
    total: int = 0,
    tagged: int = 0,
    messages_sent: int = 0,
    error: Optional[str] = None,
) -> None:
    _conn.execute(
        "UPDATE tag_sessions SET status=?, finished_at=?, total=?, "
        "tagged=?, messages_sent=?, error=? WHERE id=?",
        (status, now(), total, tagged, messages_sent, error, session_id),
    )
    _conn.commit()


def session_stats(chat_id: int) -> Dict[str, int]:
    """Aggregates for /tagstats (counts by status + sums)."""
    row = _conn.execute(
        "SELECT COUNT(*) AS sessions, "
        "SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed, "
        "SUM(CASE WHEN status IN ('aborted','interrupted') THEN 1 ELSE 0 END) AS stopped, "
        "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed, "
        "COALESCE(SUM(tagged), 0) AS tagged, "
        "COALESCE(SUM(messages_sent), 0) AS messages "
        "FROM tag_sessions WHERE chat_id=?",
        (chat_id,),
    ).fetchone()
    return {
        "sessions": int(row["sessions"] or 0),
        "completed": int(row["completed"] or 0),
        "stopped": int(row["stopped"] or 0),
        "failed": int(row["failed"] or 0),
        "tagged": int(row["tagged"] or 0),
        "messages": int(row["messages"] or 0),
    }


def last_session(chat_id: int) -> Optional[Dict[str, Any]]:
    row = _conn.execute(
        "SELECT status, started_at, finished_at, total, tagged, "
        "messages_sent, mode, error "
        "FROM tag_sessions WHERE chat_id=? ORDER BY id DESC LIMIT 1",
        (chat_id,),
    ).fetchone()
    return dict(row) if row else None
