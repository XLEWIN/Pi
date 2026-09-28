"""Database module — MongoDB operations for user storage and logging.

Backend selection (checked in order):

1. ``unittest`` / ``pytest`` already imported  → in-memory mongomock.
   Test runs NEVER touch a real server, even when MONGO_URI is set.
2. ``MONGO_URI`` from the environment (``bot.config`` loads ``.env``).
3. The project ``.env`` file, loaded here when this module is imported
   before ``bot.config``.

Without a URI at runtime the module raises ``RuntimeError``.

The public API is identical to the previous SQLite layer: same method
names, arguments and return shapes (``dict(sqlite3.Row)`` column keys,
0/1 ints for booleans, ``None`` for NULL). Callers do not change. The
per-thread connection machinery is gone — PyMongo's MongoClient is
thread-safe and pools connections process-wide.
"""

from __future__ import annotations

import atexit
import os
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Rank ladders + IST windows. Safe here: bot.constants only imports
# bot.emojis (stdlib) and bot.timeutils is stdlib — no cycles.
from bot.constants import CHAT_RANK_MESSAGES, GLOBAL_RANK_MESSAGES
from bot.timeutils import ist_date

# The shared console logger ("phi" handler). getLogger(__name__) here
# went to an unconfigured logger: INFO was filtered by the root level
# and WARNING+ escaped unformatted, so boot diagnostics like the
# database snapshot never reached Railway's logs properly.
from bot.logger import logger

# Prefer a local (non-OneDrive) path for logs + MTProto sessions so
# sync/locking cannot stall handlers. Falls back to the project folder
# if LOCALAPPDATA is unavailable. (Data itself now lives in MongoDB.)
_LEGACY_DIR = Path(__file__).resolve().parent.parent
_LOCAL_DIR = Path(os.environ.get("LOCALAPPDATA", _LEGACY_DIR)) / "PiBot"
try:
    _LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    DB_DIR = _LOCAL_DIR
except OSError:
    DB_DIR = _LEGACY_DIR

# Single-process id generator lock (the bot is one process; tests too).
_SEQ_LOCK = threading.Lock()

# ── Perf layer (Database: read cache / write-behind buffers) ─────────
# Read-cache entries live at most _CACHE_TTL seconds; writes invalidate
# them exactly via the collection proxy, the TTL is only a safety net.
_CACHE_TTL = 60.0
_MEMO_TTL = 60.0            # aggregate memo safety net (day totals, …)
_FLUSH_INTERVAL = 5.0       # background counter flush cadence (seconds)
_SKIP_TTL = 60.0            # identity/activity fast-skip window (seconds)
_BUFFERED = frozenset({     # collections with write-behind counters
    "daily_messages", "chat_daily_stats", "chat_hourly_stats", "groups",
})
_MISSING = object()

# Collection ops that REMOVE rows: these clear the fast-skip maps too
# (tests rebuild collections with delete_many — a cached "already
# registered" would otherwise skip the re-insert and strand the test).
_DELETE_OPS = frozenset({
    "delete_one", "delete_many", "drop", "find_one_and_delete",
})

_TEST_BACKENDS = ("unittest", "pytest")


def _resolve_uri() -> Optional[str]:
    """Mongo URI for this process — tests always get mongomock.

    Outside tests, a missing URI is FATAL: the old silent mongomock
    fallback made production run against an in-memory database, so
    message counts/rankings vanished on every restart while the bot
    kept pretending everything worked.
    """
    if any(m in sys.modules for m in _TEST_BACKENDS):
        return None  # caller switches to mongomock
    uri = os.getenv("MONGO_URI", "").strip()
    if uri:
        return uri
    # Imported before bot.config? Load the project .env ourselves.
    env_file = _LEGACY_DIR / ".env"
    if env_file.exists():
        try:
            from dotenv import load_dotenv

            load_dotenv(env_file)  # never overrides real env vars
        except ImportError:
            pass
        uri = os.getenv("MONGO_URI", "").strip()
    if uri:
        return uri
    raise SystemExit(
        "MONGO_URI is not set and no .env file was found at "
        f"{env_file}. Refusing to run with an in-memory database — "
        "chat rankings and message counts would silently vanish on "
        "restart. Set MONGO_URI in the environment (e.g. Railway → "
        "Variables) or create a .env file."
    )


class _CollProxy:
    """Collection wrapper that keeps the perf layer honest.

    * Reads on a buffered collection flush pending write-behind counters
      first, so every reader (db methods, raw ``db.collection(...)`` in
      tests) sees an exact, fully-written view.
    * Writes invalidate the read cache and aggregate memos for that
      collection — again for both internal writes and raw test writes.

    Flusher writes go to the raw handle, so this never re-enters itself.
    """

    _READS = frozenset({
        "find", "find_one", "count_documents", "distinct", "aggregate",
        "estimated_document_count",
    })
    _WRITES = frozenset({
        "insert_one", "insert_many", "update_one", "update_many",
        "replace_one", "delete_one", "delete_many", "bulk_write",
        "find_one_and_update", "find_one_and_delete",
        "find_one_and_replace", "drop",
    })

    def __init__(self, owner: "Database", name: str, real) -> None:
        self._owner = owner
        self._name = name
        self._real = real

    def __getattr__(self, attr: str):
        target = getattr(self._real, attr)
        if attr in self._READS:
            def _read(*args, **kwargs):
                if self._name in _BUFFERED:
                    self._owner.flush_buffers()
                return target(*args, **kwargs)
            return _read
        if attr in self._WRITES:
            def _write(*args, **kwargs):
                if self._name in _BUFFERED:
                    self._owner.flush_buffers()
                result = target(*args, **kwargs)
                self._owner._invalidate(self._name, op=attr)
                return result
            return _write
        return target


class _MongoProxy:
    """Database object whose ``[name]`` access hands back a _CollProxy."""

    def __init__(self, owner: "Database", real) -> None:
        self._owner = owner
        self._real = real

    def __getitem__(self, name: str) -> _CollProxy:
        return _CollProxy(self._owner, name, self._real[name])

    def __getattr__(self, attr: str):
        return getattr(self._real, attr)


class Database:
    """MongoDB manager for the bot.

    Thread model: ONE pooled MongoClient for the whole process — it is
    thread-safe, so the old per-thread SQLite connections (and the
    "cannot commit - no transaction is active" corruption class) are
    gone entirely.
    """

    def __init__(self):
        uri = _resolve_uri()
        if uri is None:
            import mongomock

            self._client = mongomock.MongoClient()
            self._mongo = self._client["pi_bot_test"]
            self.backend = "mongomock (tests)"
        else:
            from pymongo import MongoClient
            from pymongo.errors import ConfigurationError

            self._client = MongoClient(
                uri,
                serverSelectionTimeoutMS=8000,
                appname="pi-bot",
            )
            try:
                self._mongo = self._client.get_default_database()
            except ConfigurationError:
                # SRV URI without a /dbname — pymongo 4.x raises instead
                # of returning None.
                self._mongo = None
            if self._mongo is None:
                self._mongo = self._client["pi_bot"]
            # Fail fast on a bad URI / unreachable cluster.
            self._client.admin.command("ping")
            self.backend = f"mongodb ({self._mongo.name})"

        # ── Perf layer: read cache + aggregate memos + write-behind ──
        # ONE re-entrant lock guards all of it (flush-on-read re-enters
        # from inside memoized readers). _real_mongo is the raw handle
        # used only by the flusher so its writes never re-enter the proxy.
        self._lock = threading.RLock()
        self._read_cache: Dict[Tuple[str, tuple], Tuple[float, Any]] = {}
        self._cache_gen = 0
        self._memos: Dict[str, Tuple[float, Any]] = {}
        self._buf_daily: Dict[Tuple[int, str, str], int] = {}
        self._buf_hourly: Dict[Tuple[int, str, int], int] = {}
        self._buf_msg: Dict[Tuple[int, int, str], int] = {}
        self._buf_groups: Dict[int, Dict[str, Any]] = {}
        # Fast-skip maps: per-message upserts that re-write identical
        # identity/activity rows (and no-op membership touches) on every
        # message are skipped for _SKIP_TTL seconds. Keyed by the exact
        # write arguments; deletes clear them (see _invalidate).
        self._skip_reg: Dict[Any, float] = {}
        self._skip_act: Dict[Any, float] = {}
        self._skip_member: Dict[Any, float] = {}
        self._flusher: Optional[threading.Thread] = None
        self._flusher_stop = threading.Event()
        self._real_mongo = self._mongo
        self._mongo = _MongoProxy(self, self._mongo)
        atexit.register(self._atexit_flush)

        self._ensure_indexes()
        logger.info(f"Connected to database: {self.backend}")
        self._log_snapshot()

    # ── internals ────────────────────────────────────────

    def _log_snapshot(self) -> None:
        """Boot-time row counts — make the real store unmistakable.

        A wrong-but-reachable ``MONGO_URI`` (valid cluster, empty or
        other database) makes 'everything vanished' look like data
        loss: the bot boots and serves an empty database. These counts
        in the deploy log prove which store actually came up, and an
        empty one is called out loudly (a brand-new bot's first boot is
        the only acceptable empty).
        """
        if self.backend.startswith("mongomock"):
            return  # tests: counts are meaningless noise
        try:
            counts = {
                name: self._real_mongo[name].estimated_document_count()
                for name in ("users", "groups", "daily_messages")
            }
            line = ", ".join(f"{name}={n}" for name, n in counts.items())
            logger.info(f"Database snapshot at boot: {line}")
            if not any(counts.values()):
                logger.warning(
                    "Database is EMPTY at boot — if this bot had data "
                    "before, MONGO_URI points at the wrong cluster or "
                    "database name. Check Railway → Variables → "
                    "MONGO_URI (default database: pi_bot)."
                )
        except Exception as e:  # noqa: BLE001 — diagnostics must never crash boot
            logger.warning(f"database boot snapshot failed: {e}")

    def collection(self, name: str):
        """The raw Collection for `name` (tests / module databases)."""
        return self._mongo[name]

    @staticmethod
    def _clean(doc: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Drop Mongo's ``_id`` so dicts keep the old sqlite Row keys."""
        if doc is None:
            return None
        doc.pop("_id", None)
        return doc

    def _find(self, name: str, flt: Optional[Dict[str, Any]] = None,
              projection: Optional[Dict[str, int]] = None,
              sort: Optional[List[Tuple[str, int]]] = None,
              limit: int = 0) -> List[Dict[str, Any]]:
        cur = self._mongo[name].find(flt or {}, projection or {})
        if sort:
            cur = cur.sort(sort)
        if limit:
            cur = cur.limit(limit)
        return [self._clean(d) for d in cur]

    def _find_one(self, name: str, flt: Optional[Dict[str, Any]] = None,
                  projection: Optional[Dict[str, int]] = None,
                  sort: Optional[List[Tuple[str, int]]] = None) -> Optional[Dict[str, Any]]:
        kwargs: Dict[str, Any] = {}
        if projection is not None:
            kwargs["projection"] = projection
        if sort:
            kwargs["sort"] = sort
        return self._clean(self._mongo[name].find_one(flt or {}, **kwargs))

    def _next_id(self, coll_name: str) -> int:
        """AUTOINCREMENT replacement — monotonic per collection, in-process."""
        with _SEQ_LOCK:
            self._mongo["counters"].update_one(
                {"_id": coll_name}, {"$inc": {"seq": 1}}, upsert=True
            )
            doc = self._mongo["counters"].find_one({"_id": coll_name})
            return int(doc["seq"]) if doc else 1

    @staticmethod
    def _now() -> str:
        """Python-written timestamps (local wall clock, isoformat)."""
        return datetime.now().isoformat()

    @staticmethod
    def _ts() -> str:
        """SQLite CURRENT_TIMESTAMP equivalent (UTC, 'YYYY-MM-DD HH:MM:SS')."""
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    # ── Perf layer: read cache ─────────────────────────────

    # Cached method → collections it reads. A proxy write to any of
    # those collections drops the method's cache entries.
    _CACHE_SOURCES: Dict[str, Tuple[str, ...]] = {
        "get_filters": ("filters",),
        "get_blocklist": ("blocklist",),
        "is_blocklist_exempt": ("blocklist_exemptions",),
        "get_watch_words": ("watch_words",),
        "get_all_watch_words": ("watch_words",),
        "get_watch_mode": ("watch_words",),
        "is_spam_blocked": ("spam_protection",),
        "get_shield_settings": ("shield_settings",),
        "get_sudo_users": ("sudo_users",),
        "get_welcome_settings": ("welcome_settings",),
        "get_welcome_message": ("welcome_messages",),
    }

    @staticmethod
    def _copy_cached(value: Any) -> Any:
        """Hand callers a shallow copy so shared cache entries stay clean."""
        if isinstance(value, dict):
            return dict(value)
        if isinstance(value, list):
            return list(value)
        return value

    def _cached_read(self, method: str, key: tuple, loader,
                     ttl: float = _CACHE_TTL) -> Any:
        """Read-through cache: loader runs at most once per TTL window.

        The cache generation counter closes the load-vs-invalidate race:
        if any write lands while the loader runs, the entry is not stored.
        """
        now = time.monotonic()
        with self._lock:
            hit = self._read_cache.get((method, key))
            if hit is not None and hit[0] > now:
                return self._copy_cached(hit[1])
            gen = self._cache_gen
        value = loader()
        with self._lock:
            if self._cache_gen == gen:
                self._read_cache[(method, key)] = (now + ttl, value)
        return self._copy_cached(value)

    def _invalidate(self, coll: str, op: str = "") -> None:
        """Drop cache entries + aggregate memos fed by `coll`.

        ``op`` is the collection operation name: deletes (and drops)
        also clear the identity fast-skip maps for that collection —
        tests rebuild rows with ``delete_many``, and a cached "already
        registered" would otherwise skip the re-insert. Plain
        updates/inserts keep their skip window (that is its purpose).
        """
        with self._lock:
            if op in _DELETE_OPS:
                if coll == "users":
                    self._skip_reg.clear()
                if coll in ("users", "user_activity"):
                    self._skip_act.clear()
                if coll == "group_members":
                    self._skip_member.clear()
            self._cache_gen += 1
            stale = {
                m for m, srcs in self._CACHE_SOURCES.items() if coll in srcs
            }
            if stale:
                for k in [k for k in self._read_cache if k[0] in stale]:
                    del self._read_cache[k]
            if coll == "daily_messages":
                self._memos.clear()

    def _memo_get(self, key: str) -> Tuple[bool, Any]:
        with self._lock:
            hit = self._memos.get(key, _MISSING)
            if hit is not _MISSING and hit[0] > time.monotonic():
                return True, hit[1]
        return False, None

    def _memo_set(self, key: str, value: Any) -> None:
        with self._lock:
            self._memos[key] = (time.monotonic() + _MEMO_TTL, value)

    # ── Perf layer: write-behind buffers ───────────────────

    def _ensure_flusher(self) -> None:
        with self._lock:
            if self._flusher_stop.is_set():
                return
            if self._flusher is not None and self._flusher.is_alive():
                return
            self._flusher = threading.Thread(
                target=self._flush_loop, name="db-buffer-flush", daemon=True
            )
            self._flusher.start()

    def _flush_loop(self) -> None:
        while not self._flusher_stop.wait(_FLUSH_INTERVAL):
            try:
                self.flush_buffers()
                self._prune_skips()
            except Exception as e:  # pragma: no cover — defensive
                logger.warning(f"background flush failed: {e}")

    def _prune_skips(self) -> None:
        """Drop expired fast-skip keys (runs on the flush cadence)."""
        now = time.monotonic()
        with self._lock:
            for bucket in (self._skip_reg, self._skip_act, self._skip_member):
                for k in [k for k, exp in bucket.items() if exp <= now]:
                    del bucket[k]

    def _atexit_flush(self) -> None:
        self._flusher_stop.set()
        try:
            self.flush_buffers()
        except Exception:  # pragma: no cover — process is exiting
            pass

    def _enqueue_daily(self, chat_id: int, date_str: str,
                       cols: Dict[str, int]) -> None:
        with self._lock:
            for field, n in cols.items():
                k = (chat_id, date_str, field)
                self._buf_daily[k] = self._buf_daily.get(k, 0) + int(n)
        self._ensure_flusher()

    def _enqueue_hourly(self, chat_id: int, date_str: str,
                        hour: int, n: int) -> None:
        with self._lock:
            k = (chat_id, date_str, int(hour))
            self._buf_hourly[k] = self._buf_hourly.get(k, 0) + int(n)
        self._ensure_flusher()

    def _enqueue_msg(self, chat_id: int, user_id: int,
                     date_str: str, n: int) -> None:
        with self._lock:
            k = (chat_id, user_id, date_str)
            self._buf_msg[k] = self._buf_msg.get(k, 0) + int(n)
        self._ensure_flusher()

    def _pending_msg_sum(self, chat_id: Optional[int] = None,
                         user_id: Optional[int] = None,
                         date_str: Optional[str] = None) -> int:
        """Unflushed message counts (call with self._lock held)."""
        total = 0
        for (c, u, d), n in self._buf_msg.items():
            if chat_id is not None and c != chat_id:
                continue
            if user_id is not None and u != user_id:
                continue
            if date_str is not None and d != date_str:
                continue
            total += n
        return total

    @staticmethod
    def _apply_ops(coll, pairs: List[Tuple[Dict[str, Any], Dict[str, Any]]]) -> None:
        """$inc upserts: one bulk round-trip when possible."""
        try:
            from pymongo import UpdateOne
            coll.bulk_write(
                [UpdateOne(f, u, upsert=True) for f, u in pairs], ordered=False
            )
        except Exception:
            for f, u in pairs:
                coll.update_one(f, u, upsert=True)

    def flush_buffers(self) -> None:
        """Write pending counters/groups to Mongo.

        Holds the perf lock for the whole write: any reader that gets
        past this call sees an exact, fully-flushed view (this is how
        tests and /stats-style commands keep read-your-writes).
        """
        with self._lock:
            daily = dict(self._buf_daily)
            self._buf_daily.clear()
            hourly = dict(self._buf_hourly)
            self._buf_hourly.clear()
            msgs = dict(self._buf_msg)
            self._buf_msg.clear()
            groups = dict(self._buf_groups)
            self._buf_groups.clear()
            if not (daily or hourly or msgs or groups):
                return
            if msgs:
                # daily_messages aggregates must refetch their bases.
                self._memos.clear()
            real = self._real_mongo
            try:
                if msgs:
                    self._apply_ops(real["daily_messages"], [
                        ({"chat_id": c, "user_id": u, "date": d},
                         {"$inc": {"messages": n}})
                        for (c, u, d), n in msgs.items()
                    ])
                if daily:
                    merged: Dict[Tuple[int, str], Dict[str, int]] = {}
                    for (c, d, field), n in daily.items():
                        inc = merged.setdefault((c, d), {})
                        inc[field] = inc.get(field, 0) + n
                    for (c, d), inc in merged.items():
                        real["chat_daily_stats"].update_one(
                            {"chat_id": c, "date": d},
                            {"$inc": inc}, upsert=True,
                        )
                if hourly:
                    self._apply_ops(real["chat_hourly_stats"], [
                        ({"chat_id": c, "date": d, "hour": h},
                         {"$inc": {"messages": n}})
                        for (c, d, h), n in hourly.items()
                    ])
                for cid, doc in groups.items():
                    res = real["groups"].update_one(
                        {"chat_id": cid}, {"$set": doc}
                    )
                    if res.matched_count == 0:
                        now = doc.get("last_active")
                        real["groups"].insert_one({
                            "chat_id": cid,
                            "chat_title": doc.get("chat_title"),
                            "member_count": 0,
                            "first_seen": now,
                            "last_active": now,
                            "is_active": 1,
                        })
            except Exception as e:
                # Nothing is lost — re-queue and retry on the next flush.
                for k, v in daily.items():
                    self._buf_daily[k] = self._buf_daily.get(k, 0) + v
                for k, v in hourly.items():
                    self._buf_hourly[k] = self._buf_hourly.get(k, 0) + v
                for k, v in msgs.items():
                    self._buf_msg[k] = self._buf_msg.get(k, 0) + v
                for k, v in groups.items():
                    self._buf_groups[k] = v
                logger.warning(f"flush_buffers failed: {e}")

    def _ensure_indexes(self) -> None:
        """Create the indexes backing the old PRIMARY KEYs/UNIQUEs."""
        indexes: Dict[str, List[Tuple[Dict[str, int], Dict[str, Any]]]] = {
            "users": [
                ({"user_id": 1}, {"unique": True}),
                ({"username": 1}, {}),
            ],
            "user_activity": [
                ({"timestamp": -1}, {}),
                ({"user_id": 1}, {}),
            ],
            "moderation_log": [
                ({"timestamp": -1}, {}),
            ],
            "groups": [
                ({"chat_id": 1}, {"unique": True}),
            ],
            "group_members": [
                ({"chat_id": 1, "user_id": 1}, {"unique": True}),
                ({"user_id": 1}, {}),
                ({"chat_id": 1, "joined_at": 1}, {}),
            ],
            "filters": [
                ({"chat_id": 1, "trigger_word": 1}, {"unique": True}),
            ],
            "blocklist": [
                ({"chat_id": 1, "word": 1}, {"unique": True}),
            ],
            "blocklist_exemptions": [
                ({"chat_id": 1, "user_id": 1}, {"unique": True}),
            ],
            "sudo_users": [
                ({"user_id": 1}, {"unique": True}),
            ],
            "spam_protection": [
                ({"user_id": 1}, {"unique": True}),
            ],
            "gbanned_users": [
                ({"user_id": 1}, {"unique": True}),
            ],
            "watch_words": [
                ({"chat_id": 1, "admin_id": 1, "word": 1}, {"unique": True}),
                ({"chat_id": 1}, {}),
            ],
            "welcome_settings": [
                ({"chat_id": 1}, {"unique": True}),
            ],
            "join_request_settings": [
                ({"chat_id": 1}, {"unique": True}),
            ],
            "welcome_messages": [
                ({"chat_id": 1}, {"unique": True}),
            ],
            "user_level": [
                ({"user_id": 1}, {"unique": True}),
            ],
            "afk": [
                ({"user_id": 1}, {"unique": True}),
                ({"username": 1}, {}),
            ],
            "user_chat_level": [
                ({"chat_id": 1, "user_id": 1}, {"unique": True}),
            ],
            "daily_messages": [
                ({"chat_id": 1, "user_id": 1, "date": 1}, {"unique": True}),
                ({"chat_id": 1, "date": 1}, {}),
                ({"user_id": 1, "date": 1}, {}),
            ],
            "chat_daily_stats": [
                ({"chat_id": 1, "date": 1}, {"unique": True}),
            ],
            "chat_hourly_stats": [
                ({"chat_id": 1, "date": 1, "hour": 1}, {"unique": True}),
            ],
            "user_reputation": [
                ({"user_id": 1}, {"unique": True}),
            ],
            "shield_settings": [
                ({"chat_id": 1}, {"unique": True}),
            ],
            "raid_events": [
                ({"chat_id": 1, "id": -1}, {}),
            ],
            "ig_file_cache": [
                ({"cache_key": 1}, {"unique": True}),
            ],
            "ig_settings": [
                ({"chat_id": 1}, {"unique": True}),
            ],
            "ig_download_log": [
                ({"chat_id": 1}, {}),
            ],
        }
        for name, entries in indexes.items():
            for keys, opts in entries:
                try:
                    # mongomock's gen_index_name iterates dict KEYS when
                    # given dict-form, then does '%s_%s' % 'user_id' →
                    # TypeError. Both backends accept a proper pair list.
                    key_list = (
                        list(keys.items()) if isinstance(keys, dict) else list(keys)
                    )
                    self._mongo[name].create_index(key_list, **opts)
                except Exception as e:  # pragma: no cover — index quirk
                    logger.warning(f"Index {name} {keys} skipped: {e}")
        logger.info("Database indexes created/verified")

    # ── Instagram cache / settings / log ─────────────────
    def ig_get_file_ids(self, keys: List[str]) -> Dict[str, Tuple[str, str]]:
        """Return {key: (file_id, media_kind)} for the requested keys."""
        if not keys:
            return {}
        try:
            out: Dict[str, Tuple[str, str]] = {}
            for d in self._mongo["ig_file_cache"].find(
                {"cache_key": {"$in": list(keys)}},
                {"cache_key": 1, "file_id": 1, "media_kind": 1, "_id": 0},
            ):
                out[d["cache_key"]] = (d.get("file_id"), d.get("media_kind"))
            return out
        except Exception as e:
            logger.error(f"ig_get_file_ids: {e}")
            return {}

    def ig_put_file_id(
        self, cache_key: str, file_id: str, media_kind: str, source_url: str = ""
    ) -> None:
        try:
            self._mongo["ig_file_cache"].update_one(
                {"cache_key": cache_key},
                {
                    "$set": {
                        "file_id": file_id,
                        "media_kind": media_kind,
                        "source_url": source_url,
                        "last_used": self._ts(),
                    },
                    "$inc": {"hits": 1},
                },
                upsert=True,
            )
        except Exception as e:
            logger.error(f"ig_put_file_id: {e}")

    def ig_touch_file_id(self, cache_key: str) -> None:
        try:
            self._mongo["ig_file_cache"].update_one(
                {"cache_key": cache_key},
                {"$inc": {"hits": 1}, "$set": {"last_used": self._ts()}},
            )
        except Exception as e:
            logger.error(f"ig_touch_file_id: {e}")

    def ig_clear_file_cache(self) -> int:
        try:
            n = self._mongo["ig_file_cache"].count_documents({})
            self._mongo["ig_file_cache"].delete_many({})
            return int(n)
        except Exception as e:
            logger.error(f"ig_clear_file_cache: {e}")
            return 0

    def ig_cache_stats(self) -> Dict[str, int]:
        try:
            rows = self._mongo["ig_file_cache"].count_documents({})
            hits = sum(
                int(d.get("hits") or 0)
                for d in self._mongo["ig_file_cache"].find(
                    {}, {"hits": 1, "_id": 0}
                )
            )
            return {"rows": int(rows), "hits": int(hits)}
        except Exception as e:
            logger.error(f"ig_cache_stats: {e}")
            return {"rows": 0, "hits": 0}

    def ig_get_settings(self, chat_id: int) -> Optional[Dict[str, Any]]:
        try:
            doc = self._find_one("ig_settings", {"chat_id": chat_id})
            if doc is None:
                return None
            # sqlite SELECT * materialized schema defaults for partial rows.
            doc.setdefault("auto_download", 1)
            doc.setdefault("max_items", 10)
            doc.setdefault("send_spoiler", 0)
            doc.setdefault("updated_at", None)
            return doc
        except Exception as e:
            logger.error(f"ig_get_settings: {e}")
            return None

    def ig_set_settings(self, chat_id: int, **fields: int) -> None:
        allowed = {"auto_download", "max_items", "send_spoiler"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        try:
            self._mongo["ig_settings"].update_one(
                {"chat_id": chat_id},
                {"$set": {**updates, "updated_at": self._ts()}},
                upsert=True,
            )
        except Exception as e:
            logger.error(f"ig_set_settings: {e}")

    def ig_log_download(
        self,
        chat_id: int,
        user_id: int,
        status: str,
        media_kind: str,
        items: int,
        duration_ms: int,
        error: Optional[str],
    ) -> None:
        try:
            self._mongo["ig_download_log"].insert_one(
                {
                    "id": self._next_id("ig_download_log"),
                    "chat_id": chat_id,
                    "user_id": user_id,
                    "status": status,
                    "media_kind": media_kind,
                    "items": int(items),
                    "duration_ms": int(duration_ms),
                    "error": error,
                    "created_at": self._ts(),
                }
            )
        except Exception as e:
            logger.error(f"ig_log_download: {e}")

    # ── Analytics counters ───────────────────────────────
    def _bump_daily(self, chat_id: int, **cols: int) -> None:
        """Increment per-chat daily counters (write-behind, flushed in background)."""
        inc = {k: int(v) for k, v in cols.items()}
        if inc:
            self._enqueue_daily(chat_id, date.today().isoformat(), inc)

    def bump_messages(self, chat_id: int, n: int = 1) -> None:
        try:
            self._bump_daily(chat_id, messages=n)
        except Exception as e:
            logger.error(f"bump_messages: {e}")

    def bump_new_members(self, chat_id: int, n: int = 1) -> None:
        try:
            self._bump_daily(chat_id, new_members=n)
        except Exception as e:
            logger.error(f"bump_new_members: {e}")

    def bump_left_members(self, chat_id: int, n: int = 1) -> None:
        try:
            self._bump_daily(chat_id, left_members=n)
        except Exception as e:
            logger.error(f"bump_left_members: {e}")

    def bump_spam_attempts(self, chat_id: int, n: int = 1) -> None:
        try:
            self._bump_daily(chat_id, spam_attempts=n)
        except Exception as e:
            logger.error(f"bump_spam_attempts: {e}")

    def bump_mod_actions(self, chat_id: int, n: int = 1) -> None:
        try:
            self._bump_daily(chat_id, mod_actions=n)
        except Exception as e:
            logger.error(f"bump_mod_actions: {e}")

    def bump_bind_fails(self, chat_id: int, n: int = 1) -> None:
        try:
            self._bump_daily(chat_id, bind_fails=n)
        except Exception as e:
            logger.error(f"bump_bind_fails: {e}")

    def bump_hourly(self, chat_id: int, hour: int, n: int = 1) -> None:
        today = date.today().isoformat()
        try:
            self._enqueue_hourly(chat_id, today, hour, n)
        except Exception as e:
            logger.error(f"bump_hourly: {e}")

    def get_daily_stats(self, chat_id: int, days: int = 1) -> Dict[str, int]:
        """Sum chat_daily_stats over the last N days (inclusive of today)."""
        start = (date.today() - timedelta(days=max(days - 1, 0))).isoformat()
        keys = (
            "messages", "new_members", "left_members",
            "spam_attempts", "mod_actions", "bind_fails",
        )
        sums: Dict[str, int] = {k: 0 for k in keys}
        try:
            for d in self._mongo["chat_daily_stats"].find(
                {"chat_id": chat_id, "date": {"$gte": start}},
                {"_id": 0},
            ):
                for k in keys:
                    sums[k] += int(d.get(k) or 0)
            return sums
        except Exception as e:
            logger.error(f"get_daily_stats: {e}")
            return sums

    def get_active_member_count(self, chat_id: int, days: int = 1) -> int:
        start = (date.today() - timedelta(days=max(days - 1, 0))).isoformat()
        try:
            ids = self._mongo["daily_messages"].distinct(
                "user_id", {"chat_id": chat_id, "date": {"$gte": start}}
            )
            return len(ids)
        except Exception:
            return 0

    def sum_daily_messages(self, chat_id: int, start: str,
                           end: Optional[str] = None) -> int:
        """Sum daily_messages.messages: ``start <= date < end`` (end optional)."""
        flt: Dict[str, Any] = {"chat_id": chat_id, "date": {"$gte": start}}
        if end is not None:
            flt["date"]["$lt"] = end
        try:
            return sum(
                int(d.get("messages") or 0)
                for d in self._mongo["daily_messages"].find(
                    flt, {"messages": 1, "_id": 0}
                )
            )
        except Exception as e:
            logger.error(f"sum_daily_messages: {e}")
            return 0

    def get_peak_hours(self, chat_id: int, days: int = 7, limit: int = 5) -> List[Dict[str, Any]]:
        start = (date.today() - timedelta(days=max(days - 1, 0))).isoformat()
        try:
            totals: Dict[int, int] = {}
            for d in self._mongo["chat_hourly_stats"].find(
                {"chat_id": chat_id, "date": {"$gte": start}},
                {"hour": 1, "messages": 1, "_id": 0},
            ):
                h = int(d.get("hour") or 0)
                totals[h] = totals.get(h, 0) + int(d.get("messages") or 0)
            rows = sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
            return [{"hour": h, "messages": m} for h, m in rows]
        except Exception:
            return []

    # ── Reputation ───────────────────────────────────────
    def _ensure_reputation(self, user_id: int) -> Dict[str, Any]:
        now = self._now()
        self._mongo["user_reputation"].update_one(
            {"user_id": user_id},
            {
                "$setOnInsert": {
                    "positive_actions": 0,
                    "warnings_total": 0,
                    "restrictions_total": 0,
                    "first_seen": now,
                    "updated_at": now,
                }
            },
            upsert=True,
        )
        doc = self._find_one("user_reputation", {"user_id": user_id})
        if doc:
            return doc
        return {
            "user_id": user_id,
            "positive_actions": 0,
            "warnings_total": 0,
            "restrictions_total": 0,
            "first_seen": now,
            "updated_at": now,
        }

    def record_reputation_event(self, user_id: int, kind: str, delta: int = 1) -> None:
        """kind: positive | warning | restriction"""
        try:
            self._ensure_reputation(user_id)
            col = {
                "positive": "positive_actions",
                "warning": "warnings_total",
                "restriction": "restrictions_total",
            }.get(kind)
            if not col:
                return
            cur = self._find_one("user_reputation", {"user_id": user_id}) or {}
            new = max(0, int(cur.get(col) or 0) + int(delta))
            self._mongo["user_reputation"].update_one(
                {"user_id": user_id},
                {"$set": {col: new, "updated_at": self._now()}},
            )
        except Exception as e:
            logger.error(f"record_reputation_event: {e}")

    def get_reputation(self, user_id: int) -> Dict[str, Any]:
        """Computed reputation score + component counters."""
        self._ensure_reputation(user_id)
        try:
            row = self._find_one("user_reputation", {"user_id": user_id}) or {}
            messages = self.get_user_messages(user_id)
            pos = int(row.get("positive_actions") or 0)
            warns = int(row.get("warnings_total") or 0)
            restr = int(row.get("restrictions_total") or 0)
            # Activity-based base + boosts/penalties (never negative).
            score = max(0, (messages // 5) + (pos * 10) - (warns * 20) - (restr * 40))
            return {
                "user_id": user_id,
                "reputation": score,
                "messages": messages,
                "positive_actions": pos,
                "warnings": warns,
                "restrictions": restr,
                "first_seen": row.get("first_seen"),
            }
        except Exception as e:
            logger.error(f"get_reputation: {e}")
            return {
                "user_id": user_id, "reputation": 0, "messages": 0,
                "positive_actions": 0, "warnings": 0, "restrictions": 0,
                "first_seen": None,
            }

    def get_active_days(self, user_id: int) -> int:
        """Days since first_seen (users collection)."""
        try:
            doc = self._find_one("users", {"user_id": user_id},
                                 projection={"first_seen": 1, "_id": 0})
            if not doc or not doc.get("first_seen"):
                return 0
            first = datetime.fromisoformat(str(doc["first_seen"]).replace("Z", ""))
            return max(0, (datetime.now() - first).days)
        except (ValueError, TypeError):
            return 0

    # ── Shield / anti-raid ───────────────────────────────
    def get_shield_settings(self, chat_id: int) -> Dict[str, Any]:
        try:
            def _load() -> Dict[str, Any]:
                doc = self._find_one("shield_settings", {"chat_id": chat_id})
                if doc:
                    return doc
                return {
                    "chat_id": chat_id,
                    "shield_enabled": 1,
                    "join_limit": 8,
                    "join_window": 15,
                    "msg_limit": 10,
                    "msg_window": 5,
                    "action": "alert",
                    "lockdown": 0,
                    "updated_at": None,
                }

            return self._cached_read(
                "get_shield_settings", (chat_id,), _load
            )
        except Exception as e:
            logger.error(f"get_shield_settings: {e}")
            return {}

    def set_shield_settings(self, chat_id: int, **fields: Any) -> Dict[str, Any]:
        current = self.get_shield_settings(chat_id)
        current.update(fields)
        current["chat_id"] = chat_id
        current["updated_at"] = self._now()
        try:
            self._mongo["shield_settings"].replace_one(
                {"chat_id": chat_id},
                {
                    "chat_id": chat_id,
                    "shield_enabled": int(current.get("shield_enabled") or 0),
                    "join_limit": int(current.get("join_limit") or 8),
                    "join_window": int(current.get("join_window") or 15),
                    "msg_limit": int(current.get("msg_limit") or 10),
                    "msg_window": int(current.get("msg_window") or 5),
                    "action": str(current.get("action") or "alert"),
                    "lockdown": int(current.get("lockdown") or 0),
                    "updated_at": current.get("updated_at"),
                },
                upsert=True,
            )
            return current
        except Exception as e:
            logger.error(f"set_shield_settings: {e}")
            return current

    def log_raid_event(self, chat_id: int, kind: str, detail: str, count: int = 1) -> None:
        try:
            coll = self._mongo["raid_events"]
            coll.insert_one(
                {
                    "id": self._next_id("raid_events"),
                    "chat_id": chat_id,
                    "kind": kind,
                    "detail": detail,
                    "count": count,
                    "created_at": self._ts(),
                }
            )
            # Keep log bounded (same global id LIMIT 500 as before).
            ids = sorted(
                d["id"] for d in coll.find({}, {"id": 1, "_id": 0}) if "id" in d
            )
            if len(ids) > 500:
                coll.delete_many({"id": {"$in": ids[: len(ids) - 500]}})
        except Exception as e:
            logger.error(f"log_raid_event: {e}")

    def get_raid_events(self, chat_id: int, limit: int = 10) -> List[Dict[str, Any]]:
        try:
            return self._find("raid_events", {"chat_id": chat_id},
                              sort=[("id", -1)], limit=limit)
        except Exception:
            return []

    # ── Users / groups / activity ────────────────────────
    @staticmethod
    def _identity_key(user_id: int, username: Optional[str],
                      first_name: Optional[str], last_name: Optional[str],
                      is_bot: bool) -> Tuple[Any, ...]:
        return (user_id, username, first_name, last_name, bool(is_bot))

    def _skip_hit(self, bucket: Dict[Any, float], key: Tuple[Any, ...]) -> bool:
        with self._lock:
            return bucket.get(key, 0.0) > time.monotonic()

    def _skip_mark(self, bucket: Dict[Any, float], key: Tuple[Any, ...]) -> None:
        with self._lock:
            bucket[key] = time.monotonic() + _SKIP_TTL

    def _upsert_user_row(self, user_id: int, username: Optional[str],
                         first_name: Optional[str], last_name: Optional[str],
                         is_bot: bool, now: str) -> bool:
        """One update+maybe-insert pass. Returns True when a row was created."""
        sets: Dict[str, Any] = {"last_seen": now}
        if username is not None:
            sets["username"] = username
        if first_name is not None:
            sets["first_name"] = first_name
        if last_name is not None:
            sets["last_name"] = last_name
        res = self._mongo["users"].update_one(
            {"user_id": user_id}, {"$set": sets}
        )
        if res.matched_count == 0:
            self._mongo["users"].insert_one(
                {
                    "user_id": user_id,
                    "username": username,
                    "first_name": first_name,
                    "last_name": last_name,
                    "is_bot": 1 if is_bot else 0,
                    "first_seen": now,
                    "last_seen": now,
                    "total_messages": 0,
                    "warnings": 0,
                    "is_banned": 0,
                    "is_muted": 0,
                }
            )
            return True
        return False

    def add_user(self, user_id: int, username: str = None, first_name: str = None,
                 last_name: str = None, is_bot: bool = False) -> bool:
        """Add or update a user in the database.

        Same-identity writes within _SKIP_TTL seconds are skipped —
        every group message used to re-$set identical fields (2 round
        trips) for a row that already exists.
        """
        key = self._identity_key(user_id, username, first_name, last_name, is_bot)
        if self._skip_hit(self._skip_reg, key):
            return True
        try:
            self._upsert_user_row(
                user_id, username, first_name, last_name, is_bot, self._now()
            )
            self._skip_mark(self._skip_reg, key)
            return True
        except Exception as e:
            logger.error(f"Error adding user {user_id}: {e}")
            return False

    def register_user(self, user_id: int, username: str = None,
                      first_name: str = None, last_name: str = None,
                      is_bot: bool = False) -> bool:
        """Add-or-update a user in ONE round trip (chatstats first contact).

        Replaces the ``get_user``-then-``add_user`` pair: returns True
        only when the users row was actually created — the same value
        ``get_user(uid) is None`` produced, without the extra read.
        """
        key = self._identity_key(user_id, username, first_name, last_name, is_bot)
        if self._skip_hit(self._skip_reg, key):
            return False  # registered moments ago — row exists, not new
        try:
            created = self._upsert_user_row(
                user_id, username, first_name, last_name, is_bot, self._now()
            )
            self._skip_mark(self._skip_reg, key)
            return created
        except Exception as e:
            logger.error(f"Error registering user {user_id}: {e}")
            return False

    def get_user(self, user_id: int) -> Optional[Dict[str, Any]]:
        """Get user data by ID."""
        try:
            return self._find_one("users", {"user_id": user_id})
        except Exception as e:
            logger.error(f"Error getting user {user_id}: {e}")
            return None

    def get_user_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        """Get user data by username."""
        try:
            return self._find_one("users", {"username": username})
        except Exception as e:
            logger.error(f"Error getting user by username {username}: {e}")
            return None

    def update_user_activity(self, user_id: int, action: str, chat_id: int = None,
                             chat_title: str = None, details: str = None,
                             dedupe: bool = False):
        """Log user activity.

        ``dedupe=True`` (the per-message path) skips an identical
        user/action/chat log within _SKIP_TTL seconds — one row per
        minute instead of one per message, with last_seen fresh at the
        same cadence. Event logs (start/join) keep writing every row.
        """
        if dedupe:
            key = (user_id, action, chat_id)
            if self._skip_hit(self._skip_act, key):
                return
        try:
            now = self._now()
            # Update last_seen (no upsert — mirrors SQL UPDATE).
            self._mongo["users"].update_one(
                {"user_id": user_id}, {"$set": {"last_seen": now}}
            )
            self._mongo["user_activity"].insert_one(
                {
                    "id": self._next_id("user_activity"),
                    "user_id": user_id,
                    "action": action,
                    "chat_id": chat_id,
                    "chat_title": chat_title,
                    "timestamp": self._ts(),
                    "details": details,
                }
            )
            if dedupe:
                self._skip_mark(self._skip_act, key)
        except Exception as e:
            logger.error(f"Error updating user activity: {e}")

    def add_group(self, chat_id: int, chat_title: str) -> bool:
        """Add or update a group."""
        try:
            now = self._now()
            res = self._mongo["groups"].update_one(
                {"chat_id": chat_id},
                {"$set": {"chat_title": chat_title, "last_active": now}},
            )
            if res.matched_count == 0:
                self._mongo["groups"].insert_one(
                    {
                        "chat_id": chat_id,
                        "chat_title": chat_title,
                        "member_count": 0,
                        "first_seen": now,
                        "last_active": now,
                        "is_active": 1,
                    }
                )
            return True
        except Exception as e:
            logger.error(f"Error adding group {chat_id}: {e}")
            return False

    def add_group_member(self, chat_id: int, user_id: int, role: str = "member"):
        """Add a user to a group's member list (replace — fresh joined_at)."""
        try:
            self._mongo["group_members"].replace_one(
                {"chat_id": chat_id, "user_id": user_id},
                {
                    "chat_id": chat_id,
                    "user_id": user_id,
                    "role": role,
                    "joined_at": self._ts(),
                },
                upsert=True,
            )
        except Exception as e:
            logger.error(f"Error adding group member: {e}")

    def cache_group_member(self, chat_id: int, user_id: int,
                           role: str = "member") -> bool:
        """Group-members cache touch (chatstats first-contact path).

        INSERT OR IGNORE — keeps the original joined_at; safe to call
        on every message. Returns True when the row is new. Repeated
        touches within _SKIP_TTL seconds are skipped (a skip returns
        False — the row exists, so the real call would not insert).
        """
        key = (chat_id, user_id, role)
        if self._skip_hit(self._skip_member, key):
            return False
        try:
            res = self._mongo["group_members"].update_one(
                {"chat_id": chat_id, "user_id": user_id},
                {"$setOnInsert": {"role": role, "joined_at": self._ts()}},
                upsert=True,
            )
            self._skip_mark(self._skip_member, key)
            return res.upserted_id is not None
        except Exception as e:
            logger.error(f"Error caching group member: {e}")
            return False

    def log_moderation(self, moderator_id: int, target_id: int, action: str,
                       reason: str, chat_id: int, chat_title: str, duration: str = None):
        """Log a moderation action."""
        try:
            self._mongo["moderation_log"].insert_one(
                {
                    "id": self._next_id("moderation_log"),
                    "moderator_id": moderator_id,
                    "target_id": target_id,
                    "action": action,
                    "reason": reason,
                    "chat_id": chat_id,
                    "chat_title": chat_title,
                    "timestamp": self._ts(),
                    "duration": duration,
                }
            )
        except Exception as e:
            logger.error(f"Error logging moderation action: {e}")

    def get_user_count(self) -> int:
        """Get total number of registered users."""
        try:
            return self._mongo["users"].count_documents({"is_bot": 0})
        except Exception as e:
            logger.error(f"Error getting user count: {e}")
            return 0

    def get_group_count(self) -> int:
        """Get total number of groups."""
        try:
            return self._mongo["groups"].count_documents({})
        except Exception as e:
            logger.error(f"Error getting group count: {e}")
            return 0

    def get_all_groups(self) -> List[Dict[str, Any]]:
        """All tracked groups — ``chat_id`` + ``chat_title`` (``/mychats``)."""
        try:
            return list(
                self._mongo["groups"]
                .find({}, {"_id": 0, "chat_id": 1, "chat_title": 1})
                .sort("chat_title", 1)
            )
        except Exception as e:
            logger.error(f"Error listing groups: {e}")
            return []

    # ── /bstats counters ──────────────────────────────────────────

    def count_filters(self) -> int:
        """Total filter triggers across all chats (``/bstats``)."""
        try:
            return self._mongo["filters"].count_documents({})
        except Exception as e:
            logger.error(f"Error counting filters: {e}")
            return 0

    def count_gmuted(self) -> int:
        """Globally muted users (``/bstats``) — 0 until gmute ships."""
        try:
            return self._mongo["gmuted_users"].count_documents({})
        except Exception as e:
            logger.error(f"Error counting gmuted users: {e}")
            return 0

    def get_lock_stats(self) -> Tuple[int, int]:
        """``(chats_with_locks, total_locks)`` for ``/bstats``.

        Reads the ``locks`` collection (``{chat_id, lock_type}``);
        returns ``(0, 0)`` until a locks feature creates it.
        """
        try:
            total = self._mongo["locks"].count_documents({})
            chats = len(self._mongo["locks"].distinct("chat_id"))
            return chats, total
        except Exception as e:
            logger.error(f"Error getting lock stats: {e}")
            return 0, 0

    # ── /broadcast targets ────────────────────────────────────────

    def get_all_chat_ids(self) -> List[int]:
        """Every tracked chat id — broadcast group targets."""
        try:
            return list(self._mongo["groups"].distinct("chat_id"))
        except Exception as e:
            logger.error(f"Error listing chat ids: {e}")
            return []

    def get_all_user_ids(self) -> List[int]:
        """Every non-bot user id — broadcast user targets."""
        try:
            return list(self._mongo["users"].distinct("user_id", {"is_bot": 0}))
        except Exception as e:
            logger.error(f"Error listing user ids: {e}")
            return []

    def count_user_groups(self, user_id: int) -> int:
        """Number of tracked groups a user is a member of."""
        try:
            return len(
                self._mongo["group_members"].distinct("chat_id", {"user_id": user_id})
            )
        except Exception as e:
            logger.error(f"Error counting user groups: {e}")
            return 0

    def get_recent_activity(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get recent user activity (LEFT JOIN users for identity)."""
        try:
            rows = self._find(
                "user_activity",
                sort=[("timestamp", -1), ("_id", 1)],
                limit=limit,
            )
            uids = [r["user_id"] for r in rows if r.get("user_id") is not None]
            users: Dict[int, Dict[str, Any]] = {}
            if uids:
                users = {
                    u["user_id"]: u
                    for u in self._find(
                        "users",
                        {"user_id": {"$in": uids}},
                        projection={"user_id": 1, "username": 1, "first_name": 1},
                    )
                }
            for r in rows:
                u = users.get(r.get("user_id"), {})
                r["username"] = u.get("username")
                r["first_name"] = u.get("first_name")
            return rows
        except Exception as e:
            logger.error(f"Error getting recent activity: {e}")
            return []

    def get_moderation_log(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get recent moderation actions."""
        try:
            return self._find(
                "moderation_log",
                sort=[("timestamp", -1), ("_id", 1)],
                limit=limit,
            )
        except Exception as e:
            logger.error(f"Error getting moderation log: {e}")
            return []

    # ── Filters ───────────────────────────────────────────
    def add_filter(self, chat_id: int, trigger: str, text: str = None,
                   buttons_json: str = None, media_type: str = None, media_id: str = None) -> bool:
        try:
            self._mongo["filters"].replace_one(
                {"chat_id": chat_id, "trigger_word": trigger.lower()},
                {
                    "id": self._next_id("filters"),
                    "chat_id": chat_id,
                    "trigger_word": trigger.lower(),
                    "reply_text": text,
                    "buttons_json": buttons_json,
                    "media_type": media_type,
                    "media_id": media_id,
                },
                upsert=True,
            )
            return True
        except Exception as e:
            logger.error(f"Error adding filter: {e}")
            return False

    def get_filters(self, chat_id: int) -> List[Dict[str, Any]]:
        try:
            return self._cached_read(
                "get_filters", (chat_id,),
                lambda: self._find("filters", {"chat_id": chat_id},
                                   sort=[("id", 1)]),
            )
        except Exception as e:
            logger.error(f"Error getting filters: {e}")
            return []

    def get_filter(self, chat_id: int, trigger: str) -> Optional[Dict[str, Any]]:
        try:
            return self._find_one(
                "filters", {"chat_id": chat_id, "trigger_word": trigger.lower()}
            )
        except Exception as e:
            logger.error(f"Error getting filter: {e}")
            return None

    def remove_filter(self, chat_id: int, trigger: str) -> bool:
        try:
            res = self._mongo["filters"].delete_one(
                {"chat_id": chat_id, "trigger_word": trigger.lower()}
            )
            return res.deleted_count > 0
        except Exception as e:
            logger.error(f"Error removing filter: {e}")
            return False

    # ── Blocklist ─────────────────────────────────────────
    def add_blocklist_word(self, chat_id: int, word: str, action: str = "delete", reason: str = "Blocked word") -> bool:
        try:
            self._mongo["blocklist"].replace_one(
                {"chat_id": chat_id, "word": word.lower()},
                {
                    "id": self._next_id("blocklist"),
                    "chat_id": chat_id,
                    "word": word.lower(),
                    "action": action,
                    "reason": reason,
                },
                upsert=True,
            )
            return True
        except Exception as e:
            logger.error(f"Error adding blocklist word: {e}")
            return False

    def get_blocklist(self, chat_id: int) -> List[Dict[str, Any]]:
        try:
            return self._cached_read(
                "get_blocklist", (chat_id,),
                lambda: self._find("blocklist", {"chat_id": chat_id},
                                   sort=[("id", 1)]),
            )
        except Exception as e:
            logger.error(f"Error getting blocklist: {e}")
            return []

    def remove_blocklist_word(self, chat_id: int, word: str) -> bool:
        try:
            res = self._mongo["blocklist"].delete_one(
                {"chat_id": chat_id, "word": word.lower()}
            )
            return res.deleted_count > 0
        except Exception as e:
            logger.error(f"Error removing blocklist word: {e}")
            return False

    def clear_blocklist(self, chat_id: int) -> int:
        try:
            res = self._mongo["blocklist"].delete_many({"chat_id": chat_id})
            return res.deleted_count
        except Exception as e:
            logger.error(f"Error clearing blocklist: {e}")
            return 0

    def set_blocklist_action(self, chat_id: int, action: str):
        try:
            self._mongo["blocklist"].update_many(
                {"chat_id": chat_id}, {"$set": {"action": action}}
            )
        except Exception as e:
            logger.error(f"Error setting blocklist action: {e}")

    def set_blocklist_reason(self, chat_id: int, reason: str):
        try:
            self._mongo["blocklist"].update_many(
                {"chat_id": chat_id}, {"$set": {"reason": reason}}
            )
        except Exception as e:
            logger.error(f"Error setting blocklist reason: {e}")

    def exempt_blocklist_user(self, chat_id: int, user_id: int):
        try:
            self._mongo["blocklist_exemptions"].update_one(
                {"chat_id": chat_id, "user_id": user_id},
                {"$setOnInsert": {"chat_id": chat_id, "user_id": user_id}},
                upsert=True,
            )
        except Exception as e:
            logger.error(f"Error exempting user: {e}")

    def is_blocklist_exempt(self, chat_id: int, user_id: int) -> bool:
        try:
            return bool(self._cached_read(
                "is_blocklist_exempt", (chat_id, user_id),
                lambda: self._mongo["blocklist_exemptions"].count_documents(
                    {"chat_id": chat_id, "user_id": user_id}, limit=1
                ) > 0,
            ))
        except Exception:
            return False

    # ── Sudo users ────────────────────────────────────────
    def add_sudo_user(self, user_id: int, added_by: int = None) -> bool:
        try:
            self._mongo["sudo_users"].replace_one(
                {"user_id": user_id},
                {"user_id": user_id, "added_by": added_by, "added_at": self._ts()},
                upsert=True,
            )
            return True
        except Exception as e:
            logger.error(f"Error adding sudo user: {e}")
            return False

    def remove_sudo_user(self, user_id: int) -> bool:
        try:
            res = self._mongo["sudo_users"].delete_one({"user_id": user_id})
            return res.deleted_count > 0
        except Exception as e:
            logger.error(f"Error removing sudo user: {e}")
            return False

    def get_sudo_users(self) -> List[int]:
        try:
            return self._cached_read(
                "get_sudo_users", (),
                lambda: [
                    d["user_id"]
                    for d in self._mongo["sudo_users"].find(
                        {}, {"user_id": 1, "_id": 0}
                    )
                ],
            )
        except Exception as e:
            logger.error(f"Error getting sudo users: {e}")
            return []

    def is_sudo_user(self, user_id: int) -> bool:
        try:
            # Same result as the old count_documents — backed by the cache.
            return user_id in self.get_sudo_users()
        except Exception:
            return False

    # ── Anti-flood state (bot/modules/antispam.py) ────────
    def spam_bump_offence(self, user_id: int, offence_day: str) -> int:
        """Count one offence for this IST day; resets on a new day.

        Returns the offence number: 1st, 2nd, 3rd… (24h refresh = IST date).
        """
        try:
            row = self._find_one(
                "spam_protection",
                {"user_id": user_id},
                projection={"offence_day": 1, "offences": 1, "_id": 0},
            )
            if row is None or row.get("offence_day") != offence_day:
                offences = 1
            else:
                offences = int(row.get("offences") or 0) + 1
            self._mongo["spam_protection"].update_one(
                {"user_id": user_id},
                {
                    "$set": {
                        "user_id": user_id,
                        "offence_day": offence_day,
                        "offences": offences,
                    }
                },
                upsert=True,
            )
            return offences
        except Exception as e:
            logger.error(f"Error recording spam offence: {e}")
            return 1

    def spam_set_block(self, user_id: int, blocked_until_iso: str) -> bool:
        """Block the user until the given aware-UTC ISO timestamp."""
        try:
            res = self._mongo["spam_protection"].update_one(
                {"user_id": user_id}, {"$set": {"blocked_until": blocked_until_iso}}
            )
            if res.matched_count == 0:
                self._mongo["spam_protection"].update_one(
                    {"user_id": user_id},
                    {
                        "$set": {
                            "user_id": user_id,
                            "blocked_until": blocked_until_iso,
                        }
                    },
                    upsert=True,
                )
            return True
        except Exception as e:
            logger.error(f"Error setting spam block: {e}")
            return False

    def is_spam_blocked(self, user_id: int) -> bool:
        """True while the user's block is still active (UTC compare).

        The cached row is the raw ``blocked_until`` value — the expiry
        comparison always uses a fresh clock, and ``spam_set_block`` /
        ``spam_clear`` invalidate the entry immediately.
        """
        try:
            row = self._cached_read(
                "is_spam_blocked", (user_id,),
                lambda: self._find_one(
                    "spam_protection",
                    {"user_id": user_id},
                    projection={"blocked_until": 1, "_id": 0},
                ),
            )
            if row is None or not row.get("blocked_until"):
                return False
            until = datetime.fromisoformat(row["blocked_until"])
            return until > datetime.now(timezone.utc)
        except Exception as e:
            logger.error(f"Error checking spam block: {e}")
            return False

    def spam_clear(self, user_id: int) -> bool:
        """Wipe warnings + block (/free). Returns True if anything existed."""
        try:
            res = self._mongo["spam_protection"].delete_one({"user_id": user_id})
            return res.deleted_count > 0
        except Exception as e:
            logger.error(f"Error clearing spam state: {e}")
            return False

    def spam_get(self, user_id: int) -> Optional[Dict[str, Any]]:
        try:
            doc = self._find_one(
                "spam_protection",
                {"user_id": user_id},
                projection={
                    "user_id": 1, "offence_day": 1,
                    "offences": 1, "blocked_until": 1, "_id": 0,
                },
            )
            if doc is None:
                return None
            # sqlite materialized schema defaults for partial rows.
            return {
                "user_id": doc.get("user_id"),
                "offence_day": doc.get("offence_day"),
                "offences": int(doc.get("offences") or 0),
                "blocked_until": doc.get("blocked_until"),
            }
        except Exception as e:
            logger.error(f"Error reading spam state: {e}")
            return None

    # ── Gbanned users ─────────────────────────────────────
    def add_gban(self, user_id: int, reason: str = "No reason provided", banned_by: int = None) -> bool:
        try:
            self._mongo["gbanned_users"].replace_one(
                {"user_id": user_id},
                {
                    "user_id": user_id,
                    "reason": reason,
                    "banned_by": banned_by,
                    "banned_at": self._ts(),
                },
                upsert=True,
            )
            return True
        except Exception as e:
            logger.error(f"Error adding gban: {e}")
            return False

    def remove_gban(self, user_id: int) -> bool:
        try:
            res = self._mongo["gbanned_users"].delete_one({"user_id": user_id})
            return res.deleted_count > 0
        except Exception as e:
            logger.error(f"Error removing gban: {e}")
            return False

    def get_gbanned_users(self) -> List[Dict[str, Any]]:
        try:
            return self._find("gbanned_users", sort=[("banned_at", 1)])
        except Exception as e:
            logger.error(f"Error getting gbanned users: {e}")
            return []

    def is_gbanned(self, user_id: int) -> bool:
        try:
            return (
                self._mongo["gbanned_users"].count_documents(
                    {"user_id": user_id}, limit=1
                )
                > 0
            )
        except Exception:
            return False

    # ── Watch words ───────────────────────────────────────
    def add_watch_word(self, chat_id: int, admin_id: int, word: str, mode: str = "copy") -> bool:
        try:
            self._mongo["watch_words"].replace_one(
                {"chat_id": chat_id, "admin_id": admin_id, "word": word.lower()},
                {
                    "id": self._next_id("watch_words"),
                    "chat_id": chat_id,
                    "admin_id": admin_id,
                    "word": word.lower(),
                    "mode": mode,
                },
                upsert=True,
            )
            return True
        except Exception as e:
            logger.error(f"Error adding watch word: {e}")
            return False

    def remove_watch_word(self, chat_id: int, admin_id: int, word: str) -> bool:
        try:
            res = self._mongo["watch_words"].delete_one(
                {"chat_id": chat_id, "admin_id": admin_id, "word": word.lower()}
            )
            return res.deleted_count > 0
        except Exception as e:
            logger.error(f"Error removing watch word: {e}")
            return False

    def get_watch_words(self, chat_id: int, admin_id: int) -> List[str]:
        try:
            return self._cached_read(
                "get_watch_words", (chat_id, admin_id),
                lambda: [
                    d["word"]
                    for d in self._mongo["watch_words"].find(
                        {"chat_id": chat_id, "admin_id": admin_id},
                        {"word": 1, "_id": 0},
                    )
                ],
            )
        except Exception as e:
            logger.error(f"Error getting watch words: {e}")
            return []

    def get_all_watch_words(self, chat_id: int) -> Dict[int, List[str]]:
        """Get all watch words for a chat, grouped by admin_id."""
        try:
            def _load() -> Dict[int, List[str]]:
                result: Dict[int, List[str]] = {}
                for d in self._mongo["watch_words"].find(
                    {"chat_id": chat_id},
                    {"admin_id": 1, "word": 1, "id": 1, "_id": 0},
                ):
                    result.setdefault(d["admin_id"], []).append(d["word"])
                for words in result.values():
                    words.sort()
                return result

            return self._cached_read(
                "get_all_watch_words", (chat_id,), _load
            )
        except Exception as e:
            logger.error(f"Error getting all watch words: {e}")
            return {}

    def get_watch_mode(self, chat_id: int, admin_id: int) -> str:
        try:
            def _load() -> str:
                doc = self._find_one(
                    "watch_words",
                    {"chat_id": chat_id, "admin_id": admin_id},
                    projection={"mode": 1, "_id": 0},
                    sort=[("id", 1)],
                )
                return doc["mode"] if doc else "copy"

            return self._cached_read(
                "get_watch_mode", (chat_id, admin_id), _load
            )
        except Exception:
            return "copy"

    def set_watch_mode(self, chat_id: int, admin_id: int, mode: str):
        try:
            self._mongo["watch_words"].update_many(
                {"chat_id": chat_id, "admin_id": admin_id},
                {"$set": {"mode": mode}},
            )
        except Exception as e:
            logger.error(f"Error setting watch mode: {e}")

    # ── Welcome/Goodbye ──────────────────────────────────
    def get_welcome_settings(self, chat_id: int) -> Dict[str, Any]:
        try:
            def _load() -> Dict[str, Any]:
                doc = self._find_one("welcome_settings", {"chat_id": chat_id})
                if doc:
                    return doc
                return {"chat_id": chat_id, "welcome_enabled": 1,
                        "goodbye_enabled": 1, "clean_welcome": 0,
                        "clean_goodbye": 0, "clean_service": 0,
                        "last_welcome_msg_id": None,
                        "last_goodbye_msg_id": None}

            return self._cached_read(
                "get_welcome_settings", (chat_id,), _load
            )
        except Exception as e:
            logger.error(f"Error getting welcome settings: {e}")
            return {}

    def _replace_welcome_settings(self, chat_id: int, enabled_col: str,
                                  enabled: int, keep_last: bool = True) -> None:
        """Full-row replace — replicates INSERT OR REPLACE semantics."""
        settings = self.get_welcome_settings(chat_id)
        doc: Dict[str, Any] = {
            "chat_id": chat_id,
            "welcome_enabled": int(settings.get("welcome_enabled", 1)),
            "goodbye_enabled": int(settings.get("goodbye_enabled", 1)),
            "clean_welcome": int(settings.get("clean_welcome", 0)),
            "clean_goodbye": int(settings.get("clean_goodbye", 0)),
            "clean_service": int(settings.get("clean_service", 0)),
        }
        doc[enabled_col] = enabled
        if keep_last:
            # Preserve tracked msg ids (update_last_* callers set both).
            doc["last_welcome_msg_id"] = settings.get("last_welcome_msg_id")
            doc["last_goodbye_msg_id"] = settings.get("last_goodbye_msg_id")
        self._mongo["welcome_settings"].replace_one(
            {"chat_id": chat_id}, doc, upsert=True
        )

    def set_welcome_enabled(self, chat_id: int, enabled: bool):
        try:
            # Original INSERT OR REPLACE omitted the last_* columns → NULL reset.
            self._replace_welcome_settings(
                chat_id, "welcome_enabled", 1 if enabled else 0, keep_last=False
            )
        except Exception as e:
            logger.error(f"Error setting welcome enabled: {e}")

    def set_goodbye_enabled(self, chat_id: int, enabled: bool):
        try:
            self._replace_welcome_settings(
                chat_id, "goodbye_enabled", 1 if enabled else 0, keep_last=False
            )
        except Exception as e:
            logger.error(f"Error setting goodbye enabled: {e}")

    def set_clean_welcome(self, chat_id: int, enabled: bool):
        try:
            self._replace_welcome_settings(
                chat_id, "clean_welcome", 1 if enabled else 0, keep_last=False
            )
        except Exception as e:
            logger.error(f"Error setting clean welcome: {e}")

    def set_clean_goodbye(self, chat_id: int, enabled: bool):
        try:
            self._replace_welcome_settings(
                chat_id, "clean_goodbye", 1 if enabled else 0, keep_last=False
            )
        except Exception as e:
            logger.error(f"Error setting clean goodbye: {e}")

    def update_last_welcome_msg(self, chat_id: int, msg_id: int):
        try:
            settings = self.get_welcome_settings(chat_id)
            self._mongo["welcome_settings"].replace_one(
                {"chat_id": chat_id},
                {
                    "chat_id": chat_id,
                    "welcome_enabled": int(settings.get("welcome_enabled", 1)),
                    "goodbye_enabled": int(settings.get("goodbye_enabled", 1)),
                    "clean_welcome": int(settings.get("clean_welcome", 0)),
                    "clean_goodbye": int(settings.get("clean_goodbye", 0)),
                    "clean_service": int(settings.get("clean_service", 0)),
                    "last_welcome_msg_id": msg_id,
                    "last_goodbye_msg_id": settings.get("last_goodbye_msg_id"),
                },
                upsert=True,
            )
        except Exception as e:
            logger.error(f"Error updating last welcome msg: {e}")

    def update_last_goodbye_msg(self, chat_id: int, msg_id: int):
        try:
            settings = self.get_welcome_settings(chat_id)
            self._mongo["welcome_settings"].replace_one(
                {"chat_id": chat_id},
                {
                    "chat_id": chat_id,
                    "welcome_enabled": int(settings.get("welcome_enabled", 1)),
                    "goodbye_enabled": int(settings.get("goodbye_enabled", 1)),
                    "clean_welcome": int(settings.get("clean_welcome", 0)),
                    "clean_goodbye": int(settings.get("clean_goodbye", 0)),
                    "clean_service": int(settings.get("clean_service", 0)),
                    "last_welcome_msg_id": settings.get("last_welcome_msg_id"),
                    "last_goodbye_msg_id": msg_id,
                },
                upsert=True,
            )
        except Exception as e:
            logger.error(f"Error updating last goodbye msg: {e}")

    def get_welcome_message(self, chat_id: int) -> Dict[str, Any]:
        try:
            def _load() -> Dict[str, Any]:
                doc = self._find_one("welcome_messages", {"chat_id": chat_id})
                if doc:
                    return doc
                return {"chat_id": chat_id,
                        "welcome_text": "Hey {first}, welcome to {chatname}! 👋",
                        "welcome_buttons": None, "welcome_media": None,
                        "welcome_media_type": None,
                        "goodbye_text": "Sad to see you leaving {first}. Take Care! 👋",
                        "goodbye_buttons": None, "goodbye_media": None,
                        "goodbye_media_type": None}

            return self._cached_read(
                "get_welcome_message", (chat_id,), _load
            )
        except Exception as e:
            logger.error(f"Error getting welcome message: {e}")
            return {}

    def set_welcome_text(self, chat_id: int, text: str):
        try:
            msg = self.get_welcome_message(chat_id)
            self._mongo["welcome_messages"].replace_one(
                {"chat_id": chat_id},
                {
                    "chat_id": chat_id,
                    "welcome_text": text,
                    "welcome_buttons": msg.get("welcome_buttons"),
                    "welcome_media": msg.get("welcome_media"),
                    "welcome_media_type": msg.get("welcome_media_type"),
                    "goodbye_text": msg.get("goodbye_text"),
                    "goodbye_buttons": msg.get("goodbye_buttons"),
                    "goodbye_media": msg.get("goodbye_media"),
                    "goodbye_media_type": msg.get("goodbye_media_type"),
                },
                upsert=True,
            )
        except Exception as e:
            logger.error(f"Error setting welcome text: {e}")

    def set_goodbye_text(self, chat_id: int, text: str):
        try:
            msg = self.get_welcome_message(chat_id)
            self._mongo["welcome_messages"].replace_one(
                {"chat_id": chat_id},
                {
                    "chat_id": chat_id,
                    "welcome_text": msg.get("welcome_text"),
                    "welcome_buttons": msg.get("welcome_buttons"),
                    "welcome_media": msg.get("welcome_media"),
                    "welcome_media_type": msg.get("welcome_media_type"),
                    "goodbye_text": text,
                    "goodbye_buttons": msg.get("goodbye_buttons"),
                    "goodbye_media": msg.get("goodbye_media"),
                    "goodbye_media_type": msg.get("goodbye_media_type"),
                },
                upsert=True,
            )
        except Exception as e:
            logger.error(f"Error setting goodbye text: {e}")

    def reset_welcome(self, chat_id: int):
        self.set_welcome_text(chat_id, "Hey {first}, welcome to {chatname}! 👋")

    def reset_goodbye(self, chat_id: int):
        self.set_goodbye_text(chat_id, "Sad to see you leaving {first}. Take Care! 👋")

    # ── Join requests ────────────────────────────────────
    def set_join_requests(self, chat_id: int, enabled: bool):
        """Enable/disable the join-request approval card for a chat."""
        try:
            self._mongo["join_request_settings"].replace_one(
                {"chat_id": chat_id},
                {
                    "chat_id": chat_id,
                    "enabled": 1 if enabled else 0,
                    "updated_at": self._ts(),
                },
                upsert=True,
            )
        except Exception as e:
            logger.error(f"Error setting join requests: {e}")

    def get_join_requests(self, chat_id: int) -> bool:
        """Whether join-request approval is enabled for a chat."""
        try:
            doc = self._find_one(
                "join_request_settings",
                {"chat_id": chat_id},
                projection={"enabled": 1, "_id": 0},
            )
            return bool(doc and doc.get("enabled"))
        except Exception as e:
            logger.error(f"Error getting join requests: {e}")
            return False

    # ── Leveling system ──────────────────────────────────
    def get_user_level(self, user_id: int) -> Dict[str, Any]:
        try:
            doc = self._find_one("user_level", {"user_id": user_id})
            if doc:
                return doc
            return {"user_id": user_id, "global_level": 1, "global_xp": 0, "global_messages": 0,
                    "template": 1, "streak_current": 0, "streak_best": 0,
                    "last_message_date": None, "last_streak_date": None}
        except Exception as e:
            logger.error(f"Error getting user level: {e}")
            return {}

    def update_user_level(self, user_id: int, **kwargs):
        try:
            existing = self.get_user_level(user_id)
            self._mongo["user_level"].replace_one(
                {"user_id": user_id},
                {
                    "user_id": user_id,
                    "global_level": kwargs.get("global_level", existing.get("global_level", 1)),
                    "global_xp": kwargs.get("global_xp", existing.get("global_xp", 0)),
                    "global_messages": kwargs.get("global_messages", existing.get("global_messages", 0)),
                    "template": kwargs.get("template", existing.get("template", 1)),
                    "streak_current": kwargs.get("streak_current", existing.get("streak_current", 0)),
                    "streak_best": kwargs.get("streak_best", existing.get("streak_best", 0)),
                    "last_message_date": kwargs.get("last_message_date", existing.get("last_message_date")),
                    "last_streak_date": kwargs.get("last_streak_date", existing.get("last_streak_date")),
                },
                upsert=True,
            )
        except Exception as e:
            logger.error(f"Error updating user level: {e}")

    def add_message_xp(self, user_id: int, chat_id: int) -> Tuple[int, int, bool]:
        """Award XP + advance the daily streak for one message.

        ``chat_id`` stays in the signature for callers but is unused:
        message COUNTS live in daily_messages (bot/modules/chatstats.py
        counts every message, no XP cooldown), and ranks are derived from
        those counts — this path must not write counters of any kind.
        Rank-up announcements are owned by the counting path.

        Returns the legacy tuple ``(0, 0, False)`` — no level-ups here.
        """
        today = ist_date()

        user = self.get_user_level(user_id)
        global_xp = user.get("global_xp", 0)

        # Add XP per message (10-20 XP)
        import random
        xp_gain = random.randint(10, 20)
        global_xp += xp_gain

        # Update streak (IST days — same window as the rankings boards)
        streak_current = user.get("streak_current", 0)
        streak_best = user.get("streak_best", 0)
        last_msg_date = user.get("last_message_date")

        if last_msg_date != today:
            from datetime import date as _date, timedelta as _td
            yesterday = (_date.today() - _td(days=1)).isoformat()
            if last_msg_date == yesterday:
                streak_current += 1
            else:
                streak_current = 1
            if streak_current > streak_best:
                streak_best = streak_current

        # global_level / global_messages are intentionally NOT written:
        # they are legacy columns (frozen values) — every reader uses
        # get_user_messages()/get_user_rank_info() over daily_messages.
        self.update_user_level(user_id,
            global_xp=global_xp,
            streak_current=streak_current,
            streak_best=streak_best,
            last_message_date=today,
        )

        return (0, 0, False)

    # ── Rank math — derived from daily_messages (single source) ──
    #
    # daily_messages (the rows /rankings shows) is the ONE counter.
    # user_chat_level / user_level.global_messages are legacy columns:
    # frozen history, never read for ranks anymore.

    @staticmethod
    def chat_rank_for(messages: int) -> int:
        """Chat rank ladder: 100 messages → +1 rank (1 at 0 msgs)."""
        return int(messages) // CHAT_RANK_MESSAGES + 1

    @staticmethod
    def global_rank_for(messages: int) -> int:
        """Global rank ladder: 250 messages → +1 rank (1 at 0 msgs)."""
        return int(messages) // GLOBAL_RANK_MESSAGES + 1

    @staticmethod
    def _position_in(totals: List[Tuple[int, int]], user_id: int,
                     mine: int) -> int:
        """1-based position: higher totals first, ties → lower user_id."""
        pos = 1
        for uid, msgs in totals:
            if uid == user_id:
                continue
            if msgs > mine or (msgs == mine and uid < user_id):
                pos += 1
        return pos

    def _totals_by_user(self, extra: Optional[Dict[str, Any]] = None) -> List[Tuple[int, int]]:
        """Every user's total (global, or one chat when ``extra`` says so).

        Server-side ``$group`` on daily_messages — 1 round trip instead
        of streaming every row into Python (the /rank position calc
        calls this twice per card; all-time global = every row ever).
        """
        return self._totals_rows(dict(extra or {}))

    def _totals_rows(self, match: Dict[str, Any],
                     by: str = "user_id") -> List[Tuple[int, int]]:
        """Server-side ``$group`` on daily_messages — 1 round trip.

        ``by`` names the grouping key (``user_id``/``chat_id``).
        """
        rows = list(self._mongo["daily_messages"].aggregate([
            {"$match": match},
            {"$group": {"_id": f"${by}", "total": {"$sum": "$messages"}}},
        ]))
        return [(int(r["_id"]), int(r.get("total") or 0)) for r in rows]

    def _totals_sorted(self, match: Dict[str, Any], limit: int,
                       by: str = "user_id") -> List[Tuple[int, int]]:
        """``_totals_rows`` + server-side sort/limit.

        Order matches the old Python ``sorted(-total, key)`` exactly:
        totals descending, ties by the grouped key ascending.
        """
        rows = list(self._mongo["daily_messages"].aggregate([
            {"$match": match},
            {"$group": {"_id": f"${by}", "total": {"$sum": "$messages"}}},
            {"$sort": {"total": -1, "_id": 1}},
            {"$limit": int(limit)},
        ]))
        return [(int(r["_id"]), int(r.get("total") or 0)) for r in rows]

    def get_user_messages(self, user_id: int) -> int:
        """A user's message total across all groups — daily_messages sum.

        Memoized: the first call per window flushes + scans once, later
        calls are base + unflushed pending (exact — no extra round-trip).
        """
        try:
            key = f"gu:{user_id}"
            with self._lock:
                fresh, base = self._memo_get(key)
                if fresh:
                    return base + self._pending_msg_sum(user_id=user_id)
                self.flush_buffers()
                base = sum(
                    int(d.get("messages") or 0)
                    for d in self._mongo["daily_messages"].find(
                        {"user_id": user_id}, {"messages": 1, "_id": 0}
                    )
                )
                self._memo_set(key, base)
                return base + self._pending_msg_sum(user_id=user_id)
        except Exception as e:
            logger.error(f"Error totalling user messages: {e}")
            return 0

    def get_user_message_totals(self, chat_id: int,
                                user_id: int) -> Tuple[int, int]:
        """(chat, global) message totals — same rows /rankings counts."""
        try:
            ckey = f"ct:{chat_id}:{user_id}"
            with self._lock:
                fresh, chat_msgs = self._memo_get(ckey)
                if not fresh:
                    self.flush_buffers()
                    chat_msgs = sum(
                        int(d.get("messages") or 0)
                        for d in self._mongo["daily_messages"].find(
                            {"chat_id": chat_id, "user_id": user_id},
                            {"messages": 1, "_id": 0},
                        )
                    )
                    self._memo_set(ckey, chat_msgs)
                return (
                    chat_msgs + self._pending_msg_sum(chat_id, user_id, None),
                    self.get_user_messages(user_id),
                )
        except Exception as e:
            logger.error(f"Error totalling user messages: {e}")
            return 0, 0

    def get_user_rank_info(self, user_id: int,
                           chat_id: Optional[int] = None) -> Dict[str, Any]:
        """Unified rank info — what every rank surface displays.

        Counts come from daily_messages (identical to /rankings); ranks
        derive from CHAT_RANK_MESSAGES / GLOBAL_RANK_MESSAGES. A ``None``
        position means the user has no messages in that scope yet.
        Also carries template/streak/xp from user_level (never messages).
        """
        info: Dict[str, Any] = {
            "user_id": user_id,
            "chat_messages": 0, "chat_rank": 1, "chat_position": None,
            "chat_members": 0,
            "global_messages": 0, "global_rank": 1, "global_position": None,
            "global_members": 0,
            "template": 1, "global_xp": 0,
            "streak_current": 0, "streak_best": 0,
        }
        try:
            # Global: every chatter across all groups, all time.
            rows = self._totals_by_user()
            mine = dict(rows).get(user_id, 0)
            info["global_messages"] = mine
            info["global_members"] = len(rows)
            if mine > 0:
                info["global_rank"] = self.global_rank_for(mine)
                info["global_position"] = self._position_in(rows, user_id, mine)

            # Chat scope (groups only — skip for DM callers).
            if chat_id is not None:
                crows = self._totals_by_user({"chat_id": chat_id})
                cmine = dict(crows).get(user_id, 0)
                info["chat_messages"] = cmine
                info["chat_members"] = len(crows)
                if cmine > 0:
                    info["chat_rank"] = self.chat_rank_for(cmine)
                    info["chat_position"] = self._position_in(
                        crows, user_id, cmine
                    )

            # Presentation extras (template / xp / streaks — no counters).
            lvl = self.get_user_level(user_id)
            info["template"] = int(lvl.get("template") or 1)
            info["global_xp"] = int(lvl.get("global_xp") or 0)
            info["streak_current"] = int(lvl.get("streak_current") or 0)
            info["streak_best"] = int(lvl.get("streak_best") or 0)
        except Exception as e:
            logger.error(f"Error building rank info: {e}")
        return info

    def get_leaderboard(self, chat_id: int, limit: int = 10) -> List[Dict[str, Any]]:
        """Chat leaderboard — derives from get_chat_top (same counts,
        same order as /rankings) and adds the derived chat rank."""
        rows = self.get_chat_top(chat_id, limit=limit)
        for row in rows:
            msgs = int(row.get("total_messages") or 0)
            row["messages"] = msgs
            row["rank"] = self.chat_rank_for(msgs)
        return rows

    def get_daily_top(self, chat_id: int, limit: int = 10) -> List[Dict[str, Any]]:
        today = ist_date()  # IST day — same window as /rankings Today
        try:
            rows = self._find(
                "daily_messages",
                {"chat_id": chat_id, "date": today},
                sort=[("messages", -1), ("_id", 1)],
                limit=limit,
            )
            uids = [r["user_id"] for r in rows]
            users: Dict[int, Dict[str, Any]] = {}
            if uids:
                users = {
                    u["user_id"]: u
                    for u in self._find(
                        "users",
                        {"user_id": {"$in": uids}},
                        projection={"user_id": 1, "username": 1, "first_name": 1},
                    )
                }
            for r in rows:
                u = users.get(r.get("user_id"), {})
                r["username"] = u.get("username")
                r["first_name"] = u.get("first_name")
            return rows
        except Exception:
            return []

    def get_period_top(self, chat_id: int, since: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Top senders since an inclusive date (IST windows: week/month)."""
        try:
            top = self._totals_sorted(
                {"chat_id": chat_id, "date": {"$gte": since}}, limit
            )
            uids = [u for u, _ in top]
            users: Dict[int, Dict[str, Any]] = {}
            if uids:
                users = {
                    u["user_id"]: u
                    for u in self._find(
                        "users",
                        {"user_id": {"$in": uids}},
                        projection={"user_id": 1, "username": 1, "first_name": 1},
                    )
                }
            out = []
            for uid, total in top:
                u = users.get(uid, {})
                out.append({
                    "user_id": uid,
                    "total_messages": total,
                    "username": u.get("username"),
                    "first_name": u.get("first_name"),
                })
            return out
        except Exception:
            return []

    # ── Chat message rankings (bot/modules/chatstats.py) ──────

    def count_message(self, chat_id: int, user_id: int, date: str,
                      chat_title: Optional[str] = None) -> bool:
        """Count one text message into daily_messages (+ refresh group title).

        Both writes are write-behind: the $inc and the title/last_active
        $set reach Mongo on the next read or within the flush cadence.
        """
        try:
            self._enqueue_msg(chat_id, user_id, date, 1)
            if chat_title:
                now = self._now()
                with self._lock:
                    prev = self._buf_groups.get(chat_id)
                    if prev is None:
                        self._buf_groups[chat_id] = {
                            "chat_title": chat_title, "last_active": now,
                        }
                    else:
                        prev["chat_title"] = chat_title
                        prev["last_active"] = now
                self._ensure_flusher()
            return True
        except Exception as e:
            logger.error(f"Error counting message: {e}")
            return False

    def get_chat_top(self, chat_id: int, since: Optional[str] = None,
                     limit: int = 10) -> List[Dict[str, Any]]:
        """Top senders in one chat (since = inclusive ISO lower bound)."""
        try:
            flt: Dict[str, Any] = {"chat_id": chat_id}
            if since is not None:
                flt["date"] = {"$gte": since}
            # Totals grouped/sorted/limited on the server (1 round trip).
            top = self._totals_sorted(flt, limit)
            uids = [u for u, _ in top]
            users: Dict[int, Dict[str, Any]] = {}
            if uids:
                users = {
                    u["user_id"]: u
                    for u in self._find(
                        "users",
                        {"user_id": {"$in": uids}},
                        projection={
                            "user_id": 1, "username": 1,
                            "first_name": 1, "last_name": 1,
                        },
                    )
                }
            out = []
            for uid, total in top:
                u = users.get(uid, {})
                out.append({
                    "user_id": uid,
                    "total_messages": total,
                    "username": u.get("username"),
                    "first_name": u.get("first_name"),
                    "last_name": u.get("last_name"),
                })
            return out
        except Exception as e:
            logger.error(f"Error ranking chat: {e}")
            return []

    def get_chat_message_total(self, chat_id: int,
                               since: Optional[str] = None) -> int:
        """Total messages in a chat (all senders, same window as get_chat_top)."""
        try:
            flt: Dict[str, Any] = {"chat_id": chat_id}
            if since is not None:
                flt["date"] = {"$gte": since}
            rows = list(self._mongo["daily_messages"].aggregate([
                {"$match": flt},
                {"$group": {"_id": None, "total": {"$sum": "$messages"}}},
            ]))
            return int(rows[0].get("total") or 0) if rows else 0
        except Exception as e:
            logger.error(f"Error totalling chat: {e}")
            return 0

    def get_chat_day_total(self, chat_id: int, date: str) -> int:
        """One chat's total on one date — milestone threshold checks.

        Memoized with pending-merge (same exactness as get_user_messages).
        """
        try:
            key = f"day:{chat_id}:{date}"
            with self._lock:
                fresh, base = self._memo_get(key)
                if fresh:
                    return base + self._pending_msg_sum(
                        chat_id=chat_id, date_str=date
                    )
                self.flush_buffers()
                base = sum(
                    int(d.get("messages") or 0)
                    for d in self._mongo["daily_messages"].find(
                        {"chat_id": chat_id, "date": date},
                        {"messages": 1, "_id": 0},
                    )
                )
                self._memo_set(key, base)
                return base + self._pending_msg_sum(
                    chat_id=chat_id, date_str=date
                )
        except Exception as e:
            logger.error(f"Error totalling chat day: {e}")
            return 0

    def get_user_top_groups(self, user_id: int, since: Optional[str] = None,
                            limit: int = 10) -> List[Dict[str, Any]]:
        """A user's groups ranked by how much they chatted in each."""
        try:
            flt: Dict[str, Any] = {"user_id": user_id}
            if since is not None:
                flt["date"] = {"$gte": since}
            # Group by chat_id server-side (1 round trip), same tie order.
            top = self._totals_sorted(flt, limit, by="chat_id")
            cids = [c for c, _ in top]
            groups: Dict[int, Dict[str, Any]] = {}
            if cids:
                groups = {
                    g["chat_id"]: g
                    for g in self._find(
                        "groups",
                        {"chat_id": {"$in": cids}},
                        projection={"chat_id": 1, "chat_title": 1},
                    )
                }
            out = []
            for cid, total in top:
                g = groups.get(cid, {})
                out.append({
                    "chat_id": cid,
                    "total_messages": total,
                    "chat_title": g.get("chat_title"),
                })
            return out
        except Exception as e:
            logger.error(f"Error ranking user groups: {e}")
            return []

    # ── AFK (bot/modules/afk.py) ────────────────────────────────────

    def set_afk(self, user_id: int, first_name: Optional[str],
                username: Optional[str], reason: Optional[str],
                start_time: str, media_id: Optional[str] = None,
                media_type: Optional[str] = None) -> None:
        """Mark ``user_id`` AFK (upsert on their own row).

        ``username`` is stored lowercased so ``get_afk_by_username``
        matches Telegram's case-insensitive @mentions.
        """
        try:
            self._mongo["afk"].update_one(
                {"user_id": user_id},
                {"$set": {
                    "user_id": user_id,
                    "user_first_name": first_name or "",
                    "username": (username or "").lower() or None,
                    "afk_reason": reason,
                    "afk_start_time": start_time,
                    "media_id": media_id,
                    "media_type": media_type,
                }},
                upsert=True,
            )
        except Exception as e:
            logger.error(f"Error setting AFK for {user_id}: {e}")

    def get_afk(self, user_id: int) -> Optional[Dict[str, Any]]:
        """AFK row for ``user_id`` or None."""
        try:
            return self._find_one("afk", {"user_id": user_id})
        except Exception as e:
            logger.error(f"Error reading AFK for {user_id}: {e}")
            return None

    def get_afk_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        """AFK row by @username (case-insensitive - stored lowercase)."""
        try:
            return self._find_one("afk", {"username": (username or "").lower()})
        except Exception as e:
            logger.error(f"Error reading AFK by username {username!r}: {e}")
            return None

    def clear_afk(self, user_id: int) -> None:
        """Remove this user's single AFK row (their own document only)."""
        try:
            self._mongo["afk"].delete_one({"user_id": user_id})
        except Exception as e:
            logger.error(f"Error clearing AFK for {user_id}: {e}")

    def set_template(self, user_id: int, template: int):
        self.update_user_level(user_id, template=template)


# Global database instance
db = Database()
