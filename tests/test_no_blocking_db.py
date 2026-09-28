"""Guard: no synchronous MongoDB call may run on the event loop.

aiogram runs SYNC handlers/filters in a thread (dispatcher/event/
handler.py:40-44), but ASYNC handlers run on the loop itself. Any
pymongo call made from an async handler body blocks EVERY other chat
until Atlas answers. The established fix pattern in this codebase is
``await asyncio.to_thread(db.method, ...)`` (or a helper submitted via
``run_in_executor``), which this test enforces repo-wide:

  1. Phase A - a ``db.*`` call lexically inside an async function must
     sit under a to_thread/run_in_executor ancestor, or live inside a
     nested sync helper whose name is submitted to an executor
     somewhere in the same file.
  2. Phase B - a sync helper that touches the DB (directly or through
     other helpers) must not be called directly from an async function
     without an executor boundary.

bot/database.py itself is the sync layer (thread-confined by design).
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "bot"
DB_ROOTS = {"db", "database"}
THREAD_FUNCS = {"to_thread", "run_in_executor"}


def _call_name(node: ast.Call):
    f = node.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return None


def _is_db_call(node: ast.Call) -> bool:
    f = node.func
    return (
        isinstance(f, ast.Attribute)
        and isinstance(f.value, ast.Name)
        and f.value.id in DB_ROOTS
    )


def _under_thread(node, parent_map, stop) -> bool:
    cur = parent_map.get(node)
    while cur is not None and cur is not stop:
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


def scan() -> list[str]:
    problems: set[str] = set()
    for path in sorted(ROOT.rglob("*.py")):
        if path.name == "database.py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            problems.add(f"{path}: syntax error: {exc}")
            continue
        rel = path.relative_to(ROOT.parent)

        parent_map: dict = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parent_map[child] = node

        # Names handed to an executor in this file (multiline-safe via AST).
        wrapped: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_name(node) in THREAD_FUNCS:
                for arg in node.args:
                    if isinstance(arg, ast.Name):
                        wrapped.add(arg.id)

        # Sync functions that touch the DB, plus transitive callers.
        touches: set[tuple[str, str]] = set()
        fns: list[ast.FunctionDef] = [
            n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
        ]
        for fn in fns:
            if any(isinstance(c, ast.Call) and _is_db_call(c) for c in ast.walk(fn)):
                touches.add((rel.as_posix(), fn.name))
        changed = True
        while changed:
            changed = False
            for fn in fns:
                key = (rel.as_posix(), fn.name)
                if key in touches:
                    continue
                for c in ast.walk(fn):
                    if (
                        isinstance(c, ast.Call)
                        and isinstance(c.func, ast.Name)
                        and (rel.as_posix(), c.func.id) in touches
                    ):
                        touches.add(key)
                        changed = True
                        break

        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            for sub in ast.walk(node):
                if not isinstance(sub, ast.Call):
                    continue
                if _under_thread(sub, parent_map, node):
                    continue
                if _is_db_call(sub):
                    fn = _nearest_fn(sub, parent_map, node)
                    if fn is None:
                        problems.add(
                            f"{rel}:{sub.lineno} [{node.name}] "
                            f"direct db call on the loop: {ast.unparse(sub)[:80]}"
                        )
                    elif isinstance(fn, ast.FunctionDef) and fn.name in wrapped:
                        continue  # helper submitted to an executor elsewhere
                    else:
                        problems.add(
                            f"{rel}:{sub.lineno} [{node.name} <- {getattr(fn, 'name', 'lambda')}] "
                            f"unwrapped db call: {ast.unparse(sub)[:80]}"
                        )
                elif (
                    isinstance(sub.func, ast.Name)
                    and (rel.as_posix(), sub.func.id) in touches
                ):
                    fn = _nearest_fn(sub, parent_map, node)
                    if fn is None or fn is node:
                        problems.add(
                            f"{rel}:{sub.lineno} [{node.name} -> {sub.func.id}] "
                            f"sync db helper called on the loop"
                        )
        # nothing
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
