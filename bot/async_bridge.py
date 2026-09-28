"""Bridging coroutine database calls to synchronous call sites.

Why this exists
---------------
``bot/database.py`` is being converted to ``async def`` so production can
use Motor (no thread-pool hop per Mongo round trip, real concurrency under
bursts).  But ~440 call sites across the test-suite — plus ``setUp``/
``setUpModule`` hooks and a handful of genuine ``threading.Thread``
workers — call ``db.some_method(...)`` with no ``await`` and cannot be
made to await without restructuring them.

This module makes one call site work in all three worlds:

========================  ================================================
context                   behaviour
========================  ================================================
``await db.foo()``        on a loop this module *drives* — the bot's main
                          loop (bound by ``bind_loop``), the private
                          executor loop, or an inline thread-local loop —
                          ``box`` returns a lazy :class:`Hybrid` instead of
                          a value, so the coroutine runs on *that* loop and
                          Motor stays bound where it was opened.  Callers
                          written for production read ``await adb(db.foo())``
                          and get this branch.
``db.foo()`` on some       resolves the coroutine to completion and hands
else's loop (an async      back the plain result.  A sync call inside an
test's own loop)           async test must not return an unawaitable, so
                          ``is None`` / ``==`` assertions still hold.
``db.foo()`` with no       resolves on a thread-local loop on the calling
running loop (sync         thread and hands back the plain result — there
test, worker thread)       is no foreign loop to be on, so this is simply
                          "run it now, blocking".
========================  ================================================

The choice is made per call, at ``box()`` time, from four facts: the loop
running in the *calling* thread (if any), whether that loop is marked as an
executor, the private executor loop, and the bot loop bound by
``bind_loop()``.  Every branch is deadlock-free because a blocking branch
only ever blocks a thread that is *not* the loop it is waiting on.

``bot/database.py`` exports ``db = make_facade(Database())`` in both
production and test processes.  The facade only differs in what it does
with a coroutine it was handed: in production the caller awaits it (lazy
``Hybrid``), in a synchronous test it is resolved to a value.  Methods in
``_Facade._UNBOXED`` (currently just ``startup``) are never boxed —
boxing them would bind Motor to whichever loop first touched them.
"""

from __future__ import annotations

import asyncio
import atexit
import inspect
import threading
from typing import Any, Callable, Optional

__all__ = [
    "Hybrid", "box", "adb", "run_sync", "bind_loop", "unbind_loop",
    "bound_loop", "make_facade", "hybridize",
]

#: How long a synchronous caller waits before giving up.  A Mongo round
#: trip is milliseconds; this only trips when the loop is wedged, and a
#: clear error beats a hung process.
SYNC_TIMEOUT = 60.0

_private_lock = threading.Lock()
_private_loop: Optional[asyncio.AbstractEventLoop] = None
_private_thread: Optional[threading.Thread] = None

_bound_loop: Optional[asyncio.AbstractEventLoop] = None
_bound_thread_id: Optional[int] = None


# ── loop registry ──────────────────────────────────────────────────

def _mark_executor(loop: asyncio.AbstractEventLoop) -> asyncio.AbstractEventLoop:
    """Tag `loop` as one this module drives database coroutines on.

    ``box`` needs to tell "I am running database code" (stay lazy, the
    caller will ``await``) from "somebody else's loop" (an async test,
    where a bare ``db.foo(...)`` must still resolve).  A loop attribute
    beats a set of ids: no staleness when a loop is closed and replaced.
    """
    loop._pi_db_executor = True  # type: ignore[attr-defined]
    return loop


def _is_executor(loop: Optional[asyncio.AbstractEventLoop]) -> bool:
    return bool(loop is not None and getattr(loop, "_pi_db_executor", False))


def bind_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Declare `loop` the loop Motor is bound to (called from startup)."""
    global _bound_loop, _bound_thread_id
    _bound_loop = _mark_executor(loop)
    _bound_thread_id = threading.get_ident()


def unbind_loop() -> None:
    global _bound_loop, _bound_thread_id
    _bound_loop = None
    _bound_thread_id = None


def bound_loop() -> Optional[asyncio.AbstractEventLoop]:
    return _bound_loop


def _private() -> asyncio.AbstractEventLoop:
    """The executor loop.  Created lazily, lives for the process."""
    global _private_loop, _private_thread
    with _private_lock:
        if _private_loop is None or _private_loop.is_closed():
            _private_loop = _mark_executor(asyncio.new_event_loop())
            _private_thread = threading.Thread(
                target=_private_loop.run_forever,
                name="db-async-bridge", daemon=True,
            )
            _private_thread.start()
        return _private_loop


def _target_loop(cur: Optional[asyncio.AbstractEventLoop]):
    """(loop to execute on, mode) for the current call site.

    ``mode`` is one of:

    * ``False`` — the coroutine is already on the loop that must run it,
      so boxing must stay lazy and the caller has to ``await``.
    * ``True`` with a loop — block this (different) thread on that loop.
    * ``True`` with ``loop is None`` — run it **in this very thread**
      (see :func:`_run_inline`).

    The in-thread case is what keeps ``Database._lock`` usable: it is a
    ``threading.RLock``, so re-entering it from the *same* thread is fine,
    while hopping to another thread while holding it deadlocks.  Tests do
    exactly that (hold the perf lock, then make a db call).
    """
    if _is_executor(cur):
        return None, False            # on one of our loops: be lazy
    if _bound_loop is not None:
        return _bound_loop, True      # off-loop (worker): block on bot loop
    if cur is None:
        # No bot loop bound (tests, import time) and no loop running in
        # this thread — execute in place on a thread-local loop.
        return None, True
    # A loop is running here but it is not ours (an async test): it cannot
    # drive a second loop inline, so hand off to the executor loop.
    return _private(), True


# ── driving a coroutine from sync code ─────────────────────────────

_inline_tls = threading.local()
_inline_loops: "list[asyncio.AbstractEventLoop]" = []
_inline_lock = threading.Lock()


def _inline_loop() -> asyncio.AbstractEventLoop:
    """A private event loop belonging to *this thread*.

    Thread-local rather than shared: two threads must never drive the
    same loop, and the whole point of the inline path is to stay on the
    calling thread.
    """
    loop = getattr(_inline_tls, "loop", None)
    if loop is None or loop.is_closed():
        loop = _mark_executor(asyncio.new_event_loop())
        _inline_tls.loop = loop
        with _inline_lock:
            _inline_loops.append(loop)
    return loop


def _run_inline(coro, timeout: float) -> Any:
    """Drive `coro` on a thread-local loop **in the calling thread**."""
    loop = _inline_loop()
    if loop.is_running():
        coro.close()
        raise RuntimeError(
            "run_sync() re-entered a busy inline loop on this thread")
    try:
        return loop.run_until_complete(asyncio.wait_for(coro, timeout))
    except Exception:
        # wait_for closes the coroutine it owns on failure; a raise from
        # run_until_complete before the task starts may not have.
        raise


def run_sync(coro, timeout: float = SYNC_TIMEOUT) -> Any:
    """Run `coro` to completion from synchronous code."""
    cur = asyncio.get_running_loop() if _loop_available() else None
    loop, may_block = _target_loop(cur)
    if loop is None:
        if not may_block:
            # Caller is already on the target loop — running it here would
            # require run_until_complete on a live loop (RuntimeError) or
            # block the loop waiting on itself (deadlock).
            coro.close()
            raise RuntimeError(
                "run_sync() called from the loop that must execute the "
                "coroutine; await it instead")
        return _run_inline(coro, timeout)
    try:
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        return fut.result(timeout)
    except RuntimeError as exc:
        # Loop already closed (process teardown): nothing left to run.
        coro.close()
        raise RuntimeError(
            f"database loop is not available: {exc}") from exc


def _close_inline_loops() -> None:
    with _inline_lock:
        loops, _inline_loops[:] = list(_inline_loops), []
    for loop in loops:
        try:
            if not loop.is_running():
                loop.close()
        except Exception:
            pass


atexit.register(_close_inline_loops)


def _loop_available() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


# ── the box ────────────────────────────────────────────────────────

class Hybrid:
    """An awaited-or-forced coroutine result.

    * ``await h`` drives the coroutine on the caller's loop and caches
      the value, so the coroutine runs at most once.
    * touching it without ``await`` (``==``, ``bool``, ``len``,
      ``[...]``, ``str``, iteration, attribute access…) forces it the
      same way and caches, so a second look never re-runs anything.

    ``__repr__`` deliberately does *not* force: printing an unforced box
    during debugging must never block a thread.
    """

    __slots__ = ("_coro", "_value", "_state", "__weakref__")
    # _state: 0 = pending, 1 = done, 2 = failed

    def __init__(self, coro) -> None:
        self._coro = coro
        self._value: Any = None
        self._state = 0

    # -- await path ------------------------------------------------
    def __await__(self):
        if self._state == 1:
            return _resolved(self._value).__await__()
        if self._state == 2:
            async def _reraise():
                raise self._value
            return _reraise().__await__()
        coro = self._coro
        self._coro = None

        async def _drive():
            try:
                value = await coro
            except BaseException as exc:      # noqa: BLE001 - re-raised
                self._value = exc
                self._state = 2
                raise
            self._value = value
            self._state = 1
            return value

        return _drive().__await__()

    # -- sync force path -------------------------------------------
    def _force(self) -> Any:
        if self._state == 1:
            return self._value
        if self._state == 2:
            raise self._value
        coro, self._coro = self._coro, None
        try:
            self._value = run_sync(coro)
            self._state = 1
        except BaseException as exc:          # noqa: BLE001 - re-raised
            self._value = exc
            self._state = 2
            raise
        return self._value

    @property
    def done(self) -> bool:
        return self._state != 0

    # -- forwarding ------------------------------------------------
    def __bool__(self) -> bool:
        return bool(self._force())

    def __len__(self) -> int:
        return len(self._force())

    def __iter__(self):
        return iter(self._force())

    def __aiter__(self):
        value = self._force()
        return value.__aiter__() if hasattr(value, "__aiter__") else _empty_aiter()

    def __contains__(self, item) -> bool:
        return item in self._force()

    def __eq__(self, other) -> bool:
        if isinstance(other, Hybrid) and other is self:
            return True
        return self._force() == other

    def __ne__(self, other) -> bool:
        return not self.__eq__(other)

    def __hash__(self):
        return hash(self._force())

    def __int__(self) -> int:
        return int(self._force())

    def __float__(self) -> float:
        return float(self._force())

    def __index__(self) -> int:
        return int(self._force())

    def __str__(self) -> str:
        return str(self._force())

    def __format__(self, spec: str) -> str:
        return format(self._force(), spec)

    def __repr__(self) -> str:
        if self._state == 1:
            return f"Hybrid({self._value!r})"
        return "Hybrid(<pending>)"

    def __getitem__(self, key):
        return self._force()[key]

    def __call__(self, *args, **kwargs):
        return self._force()(*args, **kwargs)

    def __getattr__(self, name: str):
        if name.startswith("_"):
            # Never route dunder/private lookups through the forced value:
            # copy/pickle/inspect machinery probes these constantly.
            raise AttributeError(name)
        return getattr(self._force(), name)

    def __enter__(self):
        return self._force().__enter__()

    def __exit__(self, *exc):
        return self._force().__exit__(*exc)

    def __add__(self, other):
        return self._force() + other

    def __radd__(self, other):
        return other + self._force()

    def __or__(self, other):
        return self._force() | other

    def __ror__(self, other):
        return other | self._force()


async def _empty_aiter():
    return
    yield  # pragma: no cover - makes this an async generator


class _resolved:
    """Awaitable box for a value that is already final."""

    __slots__ = ("_value",)

    def __init__(self, value: Any) -> None:
        self._value = value

    def __await__(self):
        async def _self():
            return self._value
        return _self().__await__()


def box(coro) -> Any:
    """Wrap `coro` so it works with or without ``await``.

    Two cases, decided by where the caller is running:

    * **On the loop that must execute it** (inside a database coroutine,
      or a handler on the bot loop): blocking would stall that very loop,
      so hand back a lazy :class:`Hybrid` the caller has to ``await``.

    * **Anywhere else** (a plain sync test method, an async test, an
      ``asyncio.to_thread`` worker): run it to completion and return the
      *value itself*, so ``is None``, ``isinstance()`` and friends keep
      behaving exactly as they did when this layer was synchronous.
      Handlers that may run in either world should write
      ``await adb(db.foo(...))`` — ``adb`` awaits a box when there is one
      and passes a plain value straight through.
    """
    if not inspect.iscoroutine(coro):
        # Already a value, an awaitable future, or a Hybrid.
        return coro
    cur = asyncio.get_running_loop() if _loop_available() else None
    # On one of *our* loops we are executing database code and must not
    # block that loop — hand back a lazy box the caller has to ``await``.
    # Anywhere else (a sync caller with no loop, a worker thread, or
    # someone else's loop such as an async test) resolve to the plain
    # value so ``is None`` / ``isinstance()`` behave exactly as they did
    # when this layer was synchronous.  Handler code that must work in
    # both worlds writes ``await adb(db.foo(...))``.
    if _is_executor(cur):
        return Hybrid(coro)
    return run_sync(coro)


async def _noop():
    return None


async def adb(value: Any) -> Any:
    """Await a database call result in whichever world the caller is in.

    Under production (raw ``Database`` on the bot loop) ``db.foo(...)``
    hands back a lazy :class:`Hybrid`, so this awaits it.  Under the test
    facade ``box()`` already resolved it to a plain value, and
    ``await None`` would explode — so an already-plain result is passed
    straight through.  Handler code should therefore always write::

        row = await adb(db.get_user(uid))

    which is correct in production, inside async tests, and inside sync
    tests that happen to drive a handler.
    """
    if inspect.isawaitable(value):
        return await value
    return value


# ── facades ────────────────────────────────────────────────────────

def _wrap_callable(fn: Callable) -> Callable:
    @functools_wraps(fn)
    def wrapper(*args, **kwargs):
        return box(fn(*args, **kwargs))
    return wrapper


def functools_wraps(fn):
    import functools
    return functools.wraps(fn)


def hybridize(obj: Any) -> Any:
    """Return `obj` with every coroutine method wrapped by :func:`box`."""
    if inspect.iscoroutinefunction(obj):
        return _wrap_callable(obj)
    return obj


class _Facade:
    """Attribute proxy that boxes coroutine methods.

    Installed over ``bot.database.db`` so the ~440 ``db.foo(...)`` call
    sites that do not ``await`` keep working while the implementation
    underneath is ``async def``.
    """

    __slots__ = ("_target",)

    #: Coroutine methods that must run on the *caller's* loop and are
    #: never boxed.  ``startup`` is the one that calls ``bind_loop`` —
    #: routing it to a private loop would bind Motor to the wrong loop
    #: for the rest of the process' life.
    _UNBOXED = frozenset({"startup"})

    def __init__(self, target: Any) -> None:
        object.__setattr__(self, "_target", target)

    def __getattr__(self, name: str):
        attr = getattr(object.__getattribute__(self, "_target"), name)
        if name in _Facade._UNBOXED:
            return attr
        return hybridize(attr)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(object.__getattribute__(self, "_target"), name, value)

    def __repr__(self) -> str:
        return repr(object.__getattribute__(self, "_target"))


def make_facade(target: Any) -> Any:
    """Wrap `target` so coroutine methods can be called with or await."""
    return _Facade(target)
