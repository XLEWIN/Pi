"""Tagging module database — collections + CRUD on the shared MongoDB.

Schema (see README.md):

    tag_settings   one row per chat — /allsettings values
    tag_members    observed member registry (dedup by chat+user)
    tag_activity   write-behind activity counters (flushed every ~3s)
    tag_sessions   session history for /tagstats + crash recovery
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from bot.async_bridge import run_sync
from bot.database import db as _db
from bot.logger import logger


def now() -> float:
    return time.time()


# ── Schema ────────────────────────────────────────────────

def ensure_tables() -> None:
    """Queue tagging index creation for ``db.startup()`` (the bot loop).

    ``setup()`` runs at import time, before any event loop exists —
    calling ``create_index`` there produced un-awaited coroutines (the
    indexes were never actually built).  See ``Database.defer``.
    """
    _db.defer(_ensure_tables)


async def _ensure_tables() -> None:
    """Create tagging indexes if missing (runs during ``db.startup()``)."""
    try:
        await _db.collection("tag_settings").create_index("chat_id", unique=True)
        await _db.collection("tag_members").create_index(
            [("chat_id", 1), ("user_id", 1)], unique=True
        )
        await _db.collection("tag_members").create_index(
            [("chat_id", 1), ("left_at", 1)]
        )
        await _db.collection("tag_activity").create_index(
            [("chat_id", 1), ("user_id", 1)], unique=True
        )
        await _db.collection("tag_sessions").create_index(
            [("chat_id", 1), ("started_at", -1)]
        )
        logger.info("Tagging indexes created/verified")
    except Exception as e:
        logger.error(f"Tagging index creation failed: {e}")


def mark_interrupted() -> int:
    """Mark rows left 'running' by a crash/restart as interrupted.

    Never auto-resumes: a restart must not surprise the chat with a
    resumed mass tag.
    """
    return run_sync(_mark_interrupted())


def defer_interrupted() -> None:
    """Queue :func:`mark_interrupted` for ``db.startup()`` (the bot loop).

    ``setup()`` runs at import time: performing the write there binds
    the async client to a throw-away inline loop, and startup()'s ping
    on the bot loop then dies with ``Cannot use AsyncMongoClient in
    different event loop``.
    """
    _db.defer(_mark_interrupted)


async def _mark_interrupted() -> int:
    res = await _db.collection("tag_sessions").update_many(
        {"status": "running"},
        {"$set": {"status": "interrupted", "finished_at": now()}},
    )
    n = res.modified_count or 0
    if n:
        logger.info(f"Tagging: marked {n} interrupted session(s)")
    return n


# ── Settings ──────────────────────────────────────────────

_SETTINGS_COLS = (
    "mode", "window_hours", "max_mentions", "batch_size",
    "send_mode", "registry_mode",
)

_SETTINGS_DEFAULTS: Dict[str, Any] = {
    "mode": "online_first",
    "window_hours": 24,
    "max_mentions": 0,
    "batch_size": 3600,
    "send_mode": "normal",
    "registry_mode": "hybrid",
}


def get_settings(chat_id: int) -> Optional[Dict[str, Any]]:
    raw = _db._find_one(
        "tag_settings",
        {"chat_id": chat_id},
        projection={"_id": 0, "chat_id": 1, **{c: 1 for c in _SETTINGS_COLS}},
    )
    if raw is None:
        return None
    # sqlite SELECT materialised schema defaults for partial rows.
    return {"chat_id": chat_id, **_SETTINGS_DEFAULTS, **raw}


def update_settings(chat_id: int, **fields: Any) -> Dict[str, Any]:
    """Insert-or-update specific settings columns for a chat."""
    valid = {k: v for k, v in fields.items() if k in _SETTINGS_COLS}
    if not valid:
        existing = get_settings(chat_id)
        if existing:
            return existing
        _db.collection("tag_settings").update_one(
            {"chat_id": chat_id},
            {"$set": {"chat_id": chat_id, "updated_at": now()}},
            upsert=True,
        )
        return get_settings(chat_id) or {"chat_id": chat_id}

    _db.collection("tag_settings").update_one(
        {"chat_id": chat_id},
        {"$set": {**valid, "updated_at": now()}},
        upsert=True,
    )
    return get_settings(chat_id) or {"chat_id": chat_id, **valid}


# ── Member registry ───────────────────────────────────────

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
    coll = _db.collection("tag_members")
    row = coll.find_one(
        {"chat_id": chat_id, "user_id": user_id},
        {"first_seen": 1, "left_at": 1, "_id": 0},
    )
    if row is None:
        try:
            coll.insert_one(
                {
                    "chat_id": chat_id,
                    "user_id": user_id,
                    "username": username,
                    "display_name": display_name,
                    "is_bot": int(is_bot),
                    "first_seen": ts,
                    "last_seen": ts,
                    "last_active_at": active_at,
                    "left_at": None,
                }
            )
            return
        except Exception as e:
            if "E11000" not in str(e) and "duplicate" not in str(e).lower():
                raise
            # A racing writer won the find_one → insert_one window (two
            # joins in one service message, or the activity batch).  The
            # unique index did its job; fall through to the update path.
            row = {"left_at": None}
    sets: Dict[str, Any] = {"last_seen": ts}
    if username is not None:
        sets["username"] = username
    if display_name is not None:
        sets["display_name"] = display_name
    if active_at is not None:
        # COALESCE(?, last_active_at) — replace only when provided.
        sets["last_active_at"] = active_at
    if join or row.get("left_at") is not None:
        # Re-join clears the leave marker.
        sets["left_at"] = None
    coll.update_one({"chat_id": chat_id, "user_id": user_id}, {"$set": sets})


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
    _db.collection("tag_members").update_one(
        {"chat_id": chat_id, "user_id": user_id},
        {"$set": {"left_at": now()}},
    )


def mark_left_except(chat_id: int, keep_ids: set) -> None:
    """Full-sync reconcile: mark registry rows NOT in `keep_ids` as left.

    Only called after a COMPLETE participant enumeration — guards against
    an empty/partial list mass-expelling everyone.
    """
    if not keep_ids:
        return
    rows = _db.collection("tag_members").find(
        {"chat_id": chat_id, "left_at": None},
        {"user_id": 1, "_id": 0},
    )
    stale = [int(r["user_id"]) for r in rows if int(r["user_id"]) not in keep_ids]
    if not stale:
        return
    _db.collection("tag_members").update_many(
        {"chat_id": chat_id, "user_id": {"$in": stale}},
        {"$set": {"left_at": now()}},
    )


def set_presence(chat_id: int, user_id: int, ts: float) -> None:
    """Store best-known presence timestamp (MTProto only, never activity)."""
    _db.collection("tag_members").update_one(
        {"chat_id": chat_id, "user_id": user_id},
        {"$set": {"presence_at": ts}},
    )


def fetch_members(chat_id: int) -> List[Dict[str, Any]]:
    """All non-left members of a chat (identity + activity + presence)."""
    rows = _db._find(
        "tag_members",
        {"chat_id": chat_id, "left_at": None},
        projection={
            "_id": 0, "user_id": 1, "username": 1, "display_name": 1,
            "is_bot": 1, "first_seen": 1, "last_active_at": 1, "presence_at": 1,
        },
    )
    for r in rows:
        # Guarantee the old SELECT-column keys even on partial docs.
        r.setdefault("username", None)
        r.setdefault("display_name", None)
        r.setdefault("is_bot", 0)
        r.setdefault("last_active_at", None)
        r.setdefault("presence_at", None)
    return rows


def count_members(chat_id: int) -> int:
    return _db.collection("tag_members").count_documents(
        {"chat_id": chat_id, "left_at": None}
    )


def count_active(chat_id: int, since: float) -> int:
    """Members whose activity/presence signal is newer than `since`."""
    # COALESCE(last_active_at, first_seen, 0) >= since
    branches: List[Dict[str, Any]] = [
        {"last_active_at": {"$gte": since}},
        {"last_active_at": None, "first_seen": {"$gte": since}},
    ]
    if since <= 0:
        branches.append({"last_active_at": None, "first_seen": None})
    return _db.collection("tag_members").count_documents(
        {"chat_id": chat_id, "left_at": None, "$or": branches}
    )


# ── History seeding (fresh bots / quiet chats) ────────────────────

def _parse_ts(value: Any) -> float:
    """Shared-table timestamp → epoch seconds (0.0 when unparseable).

    Handles the three formats found in the shared tables:
      * 'YYYY-MM-DD HH:MM:SS'  — CURRENT_TIMESTAMP, **UTC**
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
    """Backfill tag_members from the shared history collections.

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
    try:
        for d in _db.collection("user_activity").find(
            {"chat_id": chat_id}, {"user_id": 1, "timestamp": 1, "_id": 0}
        ):
            uid = int(d["user_id"])
            ts = _parse_ts(d.get("timestamp"))
            if ts > best.get(uid, 0.0):
                best[uid] = ts
        for d in _db.collection("daily_messages").find(
            {"chat_id": chat_id}, {"user_id": 1, "date": 1, "_id": 0}
        ):
            uid = int(d["user_id"])
            ts = _parse_ts(d.get("date"))
            if ts > best.get(uid, 0.0):
                best[uid] = ts
        for d in _db.collection("group_members").find(
            {"chat_id": chat_id},
            {"user_id": 1, "joined_at": 1, "role": 1, "_id": 0},
        ):
            uid = int(d["user_id"])
            ts = _parse_ts(d.get("joined_at"))
            if ts > best.get(uid, 0.0):
                best[uid] = ts
            if d.get("role") == "bot":
                bot_ids.add(uid)
    except Exception as e:
        logger.warning(f"Tagging history seed query failed: {e}")
        return 0
    if not best:
        return 0

    # 2. Identity from the shared users collection (chunked $in lists).
    ids = sorted(best)
    identity: Dict[int, Tuple[Optional[str], str]] = {}
    try:
        for i in range(0, len(ids), 400):
            chunk = ids[i:i + 400]
            for row in _db.collection("users").find(
                {"user_id": {"$in": chunk}},
                {
                    "_id": 0, "user_id": 1, "username": 1,
                    "first_name": 1, "last_name": 1, "is_bot": 1,
                },
            ):
                uid = int(row["user_id"])
                name = " ".join(
                    p for p in (row.get("first_name") or "",
                                row.get("last_name") or "") if p
                ) or str(uid)
                identity[uid] = (row.get("username"), name)
                if row.get("is_bot"):
                    bot_ids.add(uid)
    except Exception as e:
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


# ── Activity write-behind ─────────────────────────────────

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
        coll = _db.collection("tag_members")
        # Prefetch existing rows so MAX/COALESCE semantics stay portable.
        existing: Dict[Tuple[int, int], Dict[str, Any]] = {}
        for d in coll.find(
            {"$or": [{"chat_id": c, "user_id": u} for (c, u, *_rest) in rows]},
            {
                "_id": 0, "chat_id": 1, "user_id": 1, "username": 1,
                "display_name": 1, "last_seen": 1, "last_active_at": 1,
                "left_at": 1,
            },
        ):
            existing[(int(d["chat_id"]), int(d["user_id"]))] = d

        from pymongo import UpdateOne

        ops = []
        for (c, u, un, dn, bot, ts, _ls, _la) in rows:
            flt = {"chat_id": c, "user_id": u}
            cur = existing.get((c, u))
            if cur is None:
                ops.append(UpdateOne(
                    flt,
                    {
                        "$set": {
                            "username": un,
                            "display_name": dn,
                            "last_seen": ts,
                            "last_active_at": ts,
                            "left_at": None,
                        },
                        "$setOnInsert": {"first_seen": ts, "is_bot": bot},
                    },
                    upsert=True,
                ))
                continue
            set_doc: Dict[str, Any] = {
                "last_seen": max(float(cur.get("last_seen") or 0), ts),
                "last_active_at": max(float(cur.get("last_active_at") or 0), ts),
            }
            if un is not None:
                set_doc["username"] = un
            if dn is not None:
                set_doc["display_name"] = dn
            left = cur.get("left_at")
            if left is None or ts >= float(left):
                set_doc["left_at"] = None
            ops.append(UpdateOne(flt, {"$set": set_doc}))
        coll.bulk_write(ops, ordered=False)
    except Exception as e:
        logger.warning(f"Tagging bulk observe failed: {e}")


def flush_activity(items: Iterable[Tuple[int, int, int, float]]) -> None:
    """Persist buffered counters: (chat_id, user_id, count, last_ts).

    Only the tag_activity collection — identity/activity columns are
    written by bulk_observe() so one flush = one bulk op each.
    """
    batch = list(items)
    if not batch:
        return
    try:
        from pymongo import UpdateOne

        ops = [
            UpdateOne(
                {"chat_id": c, "user_id": u},
                {"$inc": {"message_count": n}, "$set": {"last_message_at": ts}},
                upsert=True,
            )
            for (c, u, n, ts) in batch
        ]
        _db.collection("tag_activity").bulk_write(ops, ordered=False)
    except Exception as e:
        logger.warning(f"Tagging activity flush failed: {e}")


# ── Sessions ──────────────────────────────────────────────

def create_session(
    chat_id: int,
    started_by: int,
    *,
    mode: str,
    window_hours: int,
) -> int:
    doc = {
        "id": _db._next_id("tag_sessions"),
        "chat_id": chat_id,
        "started_by": started_by,
        "started_at": now(),
        "finished_at": None,
        "status": "running",
        "total": 0,
        "tagged": 0,
        "messages_sent": 0,
        "mode": mode,
        "window_hours": window_hours,
        "error": None,
    }
    _db.collection("tag_sessions").insert_one(doc)
    return int(doc["id"])


def finish_session(
    session_id: int,
    status: str,
    *,
    total: int = 0,
    tagged: int = 0,
    messages_sent: int = 0,
    error: Optional[str] = None,
) -> None:
    _db.collection("tag_sessions").update_one(
        {"id": session_id},
        {
            "$set": {
                "status": status,
                "finished_at": now(),
                "total": total,
                "tagged": tagged,
                "messages_sent": messages_sent,
                "error": error,
            }
        },
    )


def session_stats(chat_id: int) -> Dict[str, int]:
    """Aggregates for /tagstats (counts by status + sums)."""
    out = {"sessions": 0, "completed": 0, "stopped": 0, "failed": 0,
           "tagged": 0, "messages": 0}
    for d in _db.collection("tag_sessions").find(
        {"chat_id": chat_id},
        {"status": 1, "tagged": 1, "messages_sent": 1, "_id": 0},
    ):
        out["sessions"] += 1
        status = d.get("status")
        if status == "completed":
            out["completed"] += 1
        elif status in ("aborted", "interrupted"):
            out["stopped"] += 1
        elif status == "failed":
            out["failed"] += 1
        out["tagged"] += int(d.get("tagged") or 0)
        out["messages"] += int(d.get("messages_sent") or 0)
    return out


def last_session(chat_id: int) -> Optional[Dict[str, Any]]:
    doc = _db._find_one(
        "tag_sessions",
        {"chat_id": chat_id},
        projection={
            "_id": 0, "status": 1, "started_at": 1, "finished_at": 1,
            "total": 1, "tagged": 1, "messages_sent": 1, "mode": 1, "error": 1,
        },
        sort=[("id", -1)],
    )
    if doc is None:
        return None
    # Guarantee old SELECT-column keys on partial docs.
    doc.setdefault("status", None)
    doc.setdefault("started_at", None)
    doc.setdefault("finished_at", None)
    doc.setdefault("total", 0)
    doc.setdefault("tagged", 0)
    doc.setdefault("messages_sent", 0)
    doc.setdefault("mode", None)
    doc.setdefault("error", None)
    return doc
