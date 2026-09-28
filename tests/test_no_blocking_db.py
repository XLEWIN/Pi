"""Guard: no *unawaited* MongoDB call may run on the event loop.

aiogram runs SYNC handlers/filters in a thread (dispatcher/event/
handler.py:40-44), but ASYNC handlers run on the loop itself.  A
synchronous pymongo call made from an async handler body blocks every
other chat until Atlas answers.

Two eras of the fix are accepted:

* **Thread hop** (pymongo era) — ``await asyncio.to_thread(db.method,
  ...)``.  Correct but costs a thread-pool slot per call, which is what
  made bursts queue up behind the bounded executor.
* **Await** (async driver) — ``await db.method(...)`` or
  ``await adb(db.method(...))``.  The call returns a lazy box and the
  driver multiplexes it on the loop; no thread is touched.

What is still forbidden is a db call that is neither awaited nor handed
to an executor: a bare ``db.method(...)`` inside an ``async def`` would
either block the loop (pymongo) or drop its own side effect (a lazy box
nobody drives).

Checks
------
1. Phase A - a database call lexically inside an async function must sit
   under an ``await`` / ``to_thread`` / ``run_in_executor`` ancestor, or
   live inside a nested sync helper whose name is submitted to an
   executor somewhere in the file.
2. Phase B - a sync helper that touches the DB (directly, transitively
   within its own file, or through another module) must not be called
   from an async function without that boundary.

History: the first version only recognised the literal roots ``db`` and
``database``, so aliased bindings (``bdb``, ``tdb``, ``_pdb``, ``_db``)
walked straight past it — which is how bind's per-message gate handler
shipped with raw Mongo round-trips on the loop. Roots are now derived
per file from the actual imports, and module attributes (``mod.helper``)
are resolved across files.

bot/database.py and bot/modules/*/database.py are the sync layer by
design and are not scanned; their callers are.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "bot"
# The sync DB layers themselves — they ARE blocking, by design.
SYNC_LAYER_NAMES = {"database.py"}
THREAD_FUNCS = {"to_thread", "run_in_executor"}
# Deliberately in-process `db` methods: they read a dict under the perf
# lock and never touch Mongo or the network, so calling them straight on
# the loop is the whole point (a to_thread hop would cost more than the
# read). Everything else on `db` blocks.
IN_MEMORY_METHODS = {"peek_cached", "peek_spam_blocked"}
# Canonical dotted path of bot.database's `db` object.
_DB_OBJECT_PREFIX = "bot.database.db"


# ── names & paths ─────────────────────────────────────────────────

def _module_of(path: Path) -> str:
    """bot/modules/bind/callbacks.py -> bot.modules.bind.callbacks"""
    return path.relative_to(ROOT.parent).with_suffix("").as_posix().replace("/", ".")


def _call_name(node: ast.Call):
    f = node.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return None


def _resolve_source(rel_module: str, node: ast.ImportFrom) -> str:
    """Dotted module that `from … import x` pulls from (handles level=)."""
    pkg = rel_module.rsplit(".", 1)[0] if "." in rel_module else ""
    if node.level:
        parts = pkg.split(".") if pkg else []
        keep = len(parts) - (node.level - 1)
        if keep < 0:
            return ""
        base = ".".join(parts[:keep])
        mod = node.module or ""
        if base and mod:
            return f"{base}.{mod}"
        return base or mod
    return node.module or ""


# ── per-file import analysis ──────────────────────────────────────

def _import_maps(tree: ast.AST, rel_module: str):
    """(db_roots, attr_modules, name_targets) from this file's imports.

    db_roots       local names bound to bot.database's `db` object or to a
                   database module  (``db``, ``bdb``, ``tdb``, ``_pdb``, …)
    attr_modules   alias -> dotted module it stands for, for `alias.f(...)`
    name_targets   alias -> dotted object it stands for, for `alias(...)`
    """
    roots: set[str] = set()
    attr_modules: dict[str, str] = {}
    name_targets: dict[str, str] = {}

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            source = _resolve_source(rel_module, node)
            for a in node.names:
                alias = a.asname or a.name
                if a.name == "db" and source.rstrip(".").endswith("database"):
                    roots.add(alias)              # from bot.database import db as X
                elif a.name == "database":
                    roots.add(alias)              # from pkg import database as X
                full = f"{source}.{a.name}" if source else a.name
                attr_modules[alias] = full
                name_targets[alias] = full
        elif isinstance(node, ast.Import):
            for a in node.names:
                alias = a.asname or a.name.split(".")[0]
                if a.name.endswith("database"):
                    roots.add(alias)
                attr_modules[alias] = a.name
                name_targets[alias] = a.name
    return roots, attr_modules, name_targets


# ── database-call detection ───────────────────────────────────────

def _is_db_ref(node, roots: set[str]) -> bool:
    """True when this value expression is the bot.database sync layer."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Call):          # db.collection("x").find(...)
        return _is_db_call(node, roots)
    if not isinstance(node, ast.Name):
        return False
    parts.append(node.id)
    dotted = ".".join(reversed(parts))
    return parts[-1] in roots or dotted.startswith(_DB_OBJECT_PREFIX)


def _is_db_call(call: ast.Call, roots: set[str]) -> bool:
    return _is_db_ref(call.func, roots)


def _db_call_ancestor(node, parent_map, stop, roots) -> bool:
    """True when an enclosing call is itself a db call (dedupe outermost)."""
    cur = parent_map.get(node)
    while cur is not None and cur is not stop:
        if isinstance(cur, ast.Call) and _is_db_call(cur, roots):
            return True
        cur = parent_map.get(cur)
    return False


# ── ancestor / position helpers ───────────────────────────────────

def _under_thread(node, parent_map, stop, *, allow_await: bool = False) -> bool:
    """True when `node` sits under a boundary that keeps it off the loop.

    ``to_thread`` / ``run_in_executor`` always count — they are the
    pymongo-era fix.  ``await`` counts only when ``allow_await``: awaiting
    a db coroutine drives it on the loop through the async client
    without blocking, but awaiting a *sync* helper does not help,
    because the helper already ran the moment it was called.
    """
    cur = parent_map.get(node)
    while cur is not None and cur is not stop:
        if allow_await and isinstance(cur, ast.Await):
            return True
        if isinstance(cur, ast.Call) and _call_name(cur) in THREAD_FUNCS:
            return True
        cur = parent_map.get(cur)
    return False


def _nearest_fn(node, parent_map, stop):
    """Innermost function between node and stop; None => body of stop."""
    cur = parent_map.get(node)
    while cur is not None and cur is not stop:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            return cur
        cur = parent_map.get(cur)
    return None


# ── repo-wide sync-helper map ─────────────────────────────────────

def _target_of_name_call(name_targets: dict[str, str], name: str):
    """(module, function) for a plain `name(...)` call, or None."""
    full = name_targets.get(name)
    if not full or "." not in full:
        return None
    module, _, fn = full.rpartition(".")
    return module, fn


def _sync_db_functions(parsed):
    """module dotted name -> sync function names in it that hit MongoDB.

    Seeded per file from db calls, then propagated within the file
    through name and attribute calls (A calls B, B touches the DB
    => A touches the DB).
    """
    touches: dict[str, set[str]] = {}
    per_file: dict[str, list[ast.FunctionDef]] = {}

    for mod, (_r, _am, _nt, tree, _path) in parsed.items():
        fns = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
        per_file[mod] = fns
        touches[mod] = {
            fn.name for fn in fns
            if any(
                isinstance(c, ast.Call) and _is_db_call(c, _r)
                for c in ast.walk(fn)
            )
        }

    changed = True
    while changed:
        changed = False
        for mod, (_roots, attr_modules, name_targets, _tree, _path) in parsed.items():
            hit = touches[mod]
            for fn in per_file[mod]:
                if fn.name in hit:
                    continue
                for c in ast.walk(fn):
                    if not isinstance(c, ast.Call):
                        continue
                    if isinstance(c.func, ast.Name):
                        if c.func.id in hit:
                            break
                        tgt = _target_of_name_call(name_targets, c.func.id)
                        if tgt and tgt[1] in touches.get(tgt[0], ()):
                            break
                        continue
                    if isinstance(c.func, ast.Attribute):
                        if not isinstance(c.func.value, ast.Name):
                            continue
                        owner = attr_modules.get(c.func.value.id)
                        if owner and c.func.attr in touches.get(owner, ()):
                            break
                        continue
                else:
                    continue
                hit.add(fn.name)
                changed = True
    return touches


# ── scan ──────────────────────────────────────────────────────────

def scan() -> list[str]:
    problems: set[str] = set()

    parsed = {}
    paths = sorted(p for p in ROOT.rglob("*.py") if p.name not in SYNC_LAYER_NAMES)
    for path in paths:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            problems.add(f"{path}: syntax error: {exc}")
            continue
        mod = _module_of(path)
        roots, attr_modules, name_targets = _import_maps(tree, mod)
        parsed[mod] = (roots, attr_modules, name_targets, tree, path)

    touches = _sync_db_functions(parsed)

    for mod, (roots, attr_modules, name_targets, tree, path) in parsed.items():
        rel = path.relative_to(ROOT.parent).as_posix()

        parent_map: dict = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parent_map[child] = node

        file_hits = touches.get(mod, ())

        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue

            # Names THIS async function hands to an executor. Scoped per
            # function on purpose: a wrapper elsewhere in the file must not
            # excuse an unwrapped call of the same name over here.
            wrapped: set[str] = set()
            for cn in ast.walk(node):
                if isinstance(cn, ast.Call) and _call_name(cn) in THREAD_FUNCS:
                    for arg in cn.args:
                        if isinstance(arg, (ast.Name, ast.Attribute)):
                            wrapped.add(ast.unparse(arg))

            for sub in ast.walk(node):
                if not isinstance(sub, ast.Call):
                    continue
                if _under_thread(sub, parent_map, node):
                    continue
                if ast.unparse(sub.func) in wrapped:
                    continue
                fn = _nearest_fn(sub, parent_map, node)
                if isinstance(fn, ast.FunctionDef) and fn.name in wrapped:
                    continue
                # Only report the outermost db expression — `db.collection()`
                # and the `.find_one()` hanging off it are one finding.
                if _is_db_call(sub, roots) and _db_call_ancestor(sub, parent_map, node, roots):
                    continue
                # In-memory cache probes are non-blocking by design.
                if _is_db_call(sub, roots) and _call_name(sub) in IN_MEMORY_METHODS:
                    continue

                where = f"{rel}:{sub.lineno} [{node.name}]"
                helper = f" <- {fn.name}" if isinstance(fn, ast.FunctionDef) else ""

                # Phase A: a direct call into the database layer.  Awaiting
                # it is enough now — `await db.foo(...)` / `await adb(...)`
                # drives the coroutine instead of blocking the loop.
                if _is_db_call(sub, roots):
                    if not _under_thread(sub, parent_map, node, allow_await=True):
                        problems.add(
                            f"{where}{helper} direct db call on the loop: "
                            f"{ast.unparse(sub)[:90]}"
                        )
                    continue

                # Phase B: a sync helper that transitively hits MongoDB.
                # `await` does not excuse this — the helper body has
                # already executed by the time there is anything to await.
                if isinstance(sub.func, ast.Name):
                    hit_here = sub.func.id in file_hits
                    if not hit_here:
                        # Cross-module helper imported by name:
                        # `from .manager import known_chat_ids`.
                        tgt = _target_of_name_call(name_targets, sub.func.id)
                        hit_here = bool(
                            tgt and tgt[1] in touches.get(tgt[0], ())
                        )
                    if hit_here:
                        problems.add(
                            f"{where}{helper} sync db helper on the loop: "
                            f"{ast.unparse(sub)[:90]}"
                        )
                        continue
                if isinstance(sub.func, ast.Attribute) and isinstance(
                    sub.func.value, ast.Name
                ):
                    owner = attr_modules.get(sub.func.value.id)
                    if owner and sub.func.attr in touches.get(owner, ()):
                        problems.add(
                            f"{where}{helper} sync db helper on the loop: "
                            f"{owner}.{sub.func.attr} ({ast.unparse(sub)[:60]})"
                        )

    return sorted(problems)


class TestNoBlockingDbCalls(unittest.TestCase):
    def test_no_sync_db_on_event_loop(self):
        problems = scan()
        self.assertEqual(
            problems,
            [],
            "\nBlocking MongoDB call(s) on the event loop "
            f"({len(problems)}):\n" + "\n".join(problems),
        )


if __name__ == "__main__":
    unittest.main()
