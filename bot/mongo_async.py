"""Async MongoDB backend for the bot.

Two interchangeable backends behind one coroutine surface:

* **Production** — ``motor.motor_asyncio.AsyncIOMotorClient``.  Every
  driver call is a real coroutine, so a message handler never burns a
  thread-pool slot to talk to Mongo.  The event loop multiplexes as many
  concurrent operations as the connection pool allows, which is exactly
  what the old ``asyncio.to_thread(db.foo, ...)`` pattern could not do:
  under a burst it queued work behind a bounded executor and the whole
  bot went unresponsive.

* **Tests** — an async shim over ``mongomock``, which is synchronous and
  in-memory.  The shim exposes the same coroutine surface so
  ``bot/database.py`` has exactly one code path in both environments.

Neither backend changes your data.  Same ``MONGO_URI``, same database
name, same collections, same documents, same indexes — this module only
changes *how the calls are dispatched*.

The op surface is deliberately closed: it is exactly the set of methods
that ``bot/database.py::_CollProxy`` already wraps (``_READS`` +
``_WRITES``) plus ``create_index`` and ``index_information``.  Anything
else raises ``AttributeError`` loudly instead of silently returning a
sync object.

Cursors come back as :class:`AsyncCursor`, which is ``async for``-able
*and* iterable: the module databases (``bind``, ``tagging``) still use
plain ``for d in coll.find(...)``, so ``__iter__`` blocks on the inner
cursor (direct iteration for the mongomock shim, ``run_sync`` for Motor).
One cursor type therefore serves both styles without touching those
modules.
"""

from __future__ import annotations

import inspect
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from bot.async_bridge import run_sync

__all__ = ["open_backend", "AsyncCursor", "MONGO_ASYNC_OPS"]


# ── cursor ─────────────────────────────────────────────────────────

class AsyncCursor:
    """``async for``-able cursor over either a Motor or a sync cursor.

    ``sort``/``limit``/``skip`` are chainable and return ``self``, which is
    how both Motor and pymongo behave, so ``_find`` reads the same either
    way::

        cur = await coll.find(flt, projection)
        cur = cur.sort(sort).limit(limit)
        return [clean(d) async for d in cur]
    """

    __slots__ = ("_inner",)

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def sort(self, *args: Any, **kwargs: Any) -> "AsyncCursor":
        self._inner = self._inner.sort(*args, **kwargs)
        return self

    def limit(self, *args: Any, **kwargs: Any) -> "AsyncCursor":
        self._inner = self._inner.limit(*args, **kwargs)
        return self

    def skip(self, *args: Any, **kwargs: Any) -> "AsyncCursor":
        self._inner = self._inner.skip(*args, **kwargs)
        return self

    def __aiter__(self) -> "AsyncCursor":
        return self

    def __iter__(self):
        """Synchronous iteration — what the module databases expect.

        ``bot/modules/{bind,tagging}/database.py`` are deliberately sync
        (they are driven with ``asyncio.to_thread``) and they iterate
        ``collection.find(...)`` with a plain ``for``.  That worked when
        pymongo handed back a sync cursor, so it has to keep working.

        * mongomock / pymongo inner cursor, or a plain list from
          ``aggregate`` — iterate it directly, it is already in memory.
        * Motor inner cursor — drain it on the loop that owns it, via
          the bridge, so the caller's thread just blocks briefly.
        """
        inner = self._inner
        if hasattr(inner, "__anext__"):
            return iter(run_sync(self.to_list()))
        try:
            return iter(inner)
        except TypeError:
            if isinstance(inner, list):
                return iter(inner)
            raise

    async def __anext__(self) -> Any:
        inner = self._inner
        # Motor's command cursor is natively async-iterable.
        if hasattr(inner, "__anext__"):
            return await inner.__anext__()
        # mongomock/pymongo cursors are plain sync iterators.  Blocking
        # here is fine: mongomock is in-memory and returns instantly, and
        # this class is never used for Motor.
        try:
            return next(inner)
        except StopIteration:
            raise StopAsyncIteration from None
        except TypeError:
            # list-like cursors (aggregate() may hand back a list)
            if isinstance(inner, list):
                if inner:
                    return inner.pop(0)
            raise StopAsyncIteration from None

    async def to_list(self, length: Optional[int] = None) -> List[Any]:
        out: List[Any] = []
        async for item in self:
            out.append(item)
            if length is not None and len(out) >= length:
                break
        return out


def _maybe_await(value: Any) -> Any:
    """Await `value` when the backend hands back a coroutine."""
    if inspect.isawaitable(value):
        return value
    return _resolved(value)


class _resolved:
    """Awaitable box so ``await x`` works on a value that is already final.

    Lets one call site — ``async def _read`` in ``_CollProxy`` — stay
    uniform across Motor (real coroutine) and the mongomock shim (already
    computed), instead of branching on the backend everywhere.
    """

    __slots__ = ("_value",)

    def __init__(self, value: Any) -> None:
        self._value = value

    def __await__(self):
        async def _self():
            return self._value
        return _self().__await__()


# ── mongomock over async ───────────────────────────────────────────

# Ops that are pure passthrough coroutines: same args, same return.
_ASYNC_PASSTHROUGH = (
    "find_one", "count_documents", "distinct", "estimated_document_count",
    "insert_one", "insert_many", "update_one", "update_many",
    "replace_one", "delete_one", "delete_many", "bulk_write",
    "find_one_and_update", "find_one_and_delete", "find_one_and_replace",
    "drop", "create_index", "index_information",
)

MONGO_ASYNC_OPS = frozenset(_ASYNC_PASSTHROUGH) | {"find", "aggregate"}


class _AsyncColl:
    """Coroutine facade over a synchronous mongomock collection."""

    __slots__ = ("_inner",)

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def find(self, *args: Any, **kwargs: Any) -> AsyncCursor:
        return AsyncCursor(self._inner.find(*args, **kwargs))

    def aggregate(self, *args: Any, **kwargs: Any) -> AsyncCursor:
        return AsyncCursor(self._inner.aggregate(*args, **kwargs))

    def __getattr__(self, attr: str):
        if attr not in _ASYNC_PASSTHROUGH:
            raise AttributeError(
                f"async mongo backend has no op {attr!r} "
                f"(add it to _ASYNC_PASSTHROUGH and bot/mongo_async.py)"
            )
        target = getattr(self._inner, attr)

        async def _call(*args: Any, **kwargs: Any):
            return target(*args, **kwargs)

        _call.__name__ = attr
        return _call


class _AsyncCollDB:
    """Coroutine facade over a synchronous mongomock database."""

    __slots__ = ("_inner",)

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getitem__(self, name: str) -> _AsyncColl:
        return _AsyncColl(self._inner[name])

    def __getattr__(self, attr: str):
        target = getattr(self._inner, attr)
        if not callable(target):
            return target

        async def _call(*args: Any, **kwargs: Any):
            return target(*args, **kwargs)

        _call.__name__ = attr
        return _call


class _AsyncMongoClient:
    """Coroutine facade over a synchronous mongomock client."""

    __slots__ = ("_inner",)

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getitem__(self, name: str) -> _AsyncCollDB:
        return _AsyncCollDB(self._inner[name])

    @property
    def admin(self):
        return self._inner.admin

    def get_default_database(self, default=None):
        db = self._inner.get_default_database(default)
        return None if db is None else _AsyncCollDB(db)

    def close(self) -> None:
        self._inner.close()


def _open_mongomock() -> Tuple[Any, Any, str]:
    import mongomock

    client = _AsyncMongoClient(mongomock.MongoClient())
    return client, client["pi_bot_test"], "mongomock (tests)"


def _open_motor(uri: str) -> Tuple[Any, Any, str]:
    from motor.motor_asyncio import AsyncIOMotorClient

    client = AsyncIOMotorClient(uri, appname="pi-bot")
    try:
        mongo = client.get_default_database()
    except Exception:
        mongo = None
    if mongo is None:
        mongo = client["pi_bot"]
    return client, mongo, f"mongodb (async/{mongo.name})"


def open_backend(uri: Optional[str] = None) -> Tuple[Any, Any, str]:
    """Return ``(client, mongo, backend_label)``.

    ``uri is None`` selects the in-memory test backend.  Construction is
    synchronous and non-blocking for both backends — Motor opens its
    sockets lazily on the first ``await``, so building the client inside
    ``Database.__init__`` is safe before the event loop exists.  The
    liveness ``ping`` is a separate coroutine (``Database.ping``).
    """
    if uri is None:
        return _open_mongomock()
    return _open_motor(uri)
