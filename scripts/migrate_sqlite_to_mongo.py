#!/usr/bin/env python3
"""One-shot migration: legacy SQLite bot_database.db -> MongoDB.

Usage:
  python scripts/migrate_sqlite_to_mongo.py [--sqlite PATH] [--uri URI]
                                            [--db NAME] [--overwrite] [--dry-run]

What it does:
  1. Copies the source db + -wal + -shm into a temp dir first, so the
     live (un-checkpointed) WAL is included and the original files are
     never modified.
  2. Reads every table read-only and inserts each row as a Mongo
     document, preserving column names AND sqlite ``id`` values.
  3. Points the Mongo ``counters`` collection at MAX(id) per table
     (using $max, so re-runs can only raise it) so the bot's
     _next_id() continues where SQLite left off.
  4. Creates the production indexes (via bot.database) before insert,
     so unique constraints are enforced during the copy.

Safety:
  * Refuses to write into a non-empty target unless --overwrite.
  * --dry-run prints what would happen and writes nothing.

MONGO_URI comes from --uri, the environment, or the project .env
(in that order). The target database defaults to the one named in
the URI, else ``pi_bot``.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _default_sqlite() -> Path:
    local = Path(os.environ.get("LOCALAPPDATA", str(PROJECT_ROOT))) / "PiBot" / "bot_database.db"
    if local.exists():
        return local
    return PROJECT_ROOT / "bot_database.db"


def _load_uri(cli_uri: Optional[str], required: bool = True) -> Optional[str]:
    uri = (cli_uri or os.getenv("MONGO_URI", "")).strip()
    if uri:
        os.environ["MONGO_URI"] = uri  # bot.database reads the env
        return uri
    env_file = PROJECT_ROOT / ".env"
    if env_file.exists():
        try:
            from dotenv import load_dotenv

            load_dotenv(env_file)
        except ImportError:
            pass
    uri = os.getenv("MONGO_URI", "").strip()
    if not uri:
        if required:
            sys.exit("MONGO_URI not set. Pass --uri, export MONGO_URI, or add it to .env.")
        return None
    os.environ["MONGO_URI"] = uri
    return uri


def _copy_source(src: Path) -> Path:
    """Copy db(+wal,+shm) to a temp dir; return the copied db path."""
    if not src.exists():
        sys.exit(f"Source SQLite file not found: {src}")
    tmp = Path(tempfile.mkdtemp(prefix="pi_migrate_"))
    dst = tmp / src.name
    for suffix in ("", "-wal", "-shm"):
        part = Path(str(src) + suffix)
        if part.exists():
            shutil.copy2(part, dst.with_name(dst.name + suffix))
    return dst


def _tables(conn: sqlite3.Connection) -> List[Dict[str, object]]:
    """Table names, column info, and whether `id` is INTEGER PRIMARY KEY."""
    cur = conn.execute(
        "SELECT name, sql FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
    out = []
    for name, sql in cur.fetchall():
        cols = conn.execute(f'PRAGMA table_info("{name}")').fetchall()
        # (cid, name, type, notnull, dflt_value, pk)
        int_pk_id = any(
            c[1] == "id" and c[2].upper().startswith("INTEGER") and c[5] == 1
            for c in cols
        )
        out.append(
            {
                "name": name,
                "columns": [c[1] for c in cols],
                "auto_id": int_pk_id,
            }
        )
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sqlite", type=Path, default=_default_sqlite(),
                    help="source SQLite db (default: live PiBot data dir)")
    ap.add_argument("--uri", default=None, help="MongoDB URI (default: env/.env MONGO_URI)")
    ap.add_argument("--db", default=None, help="target database name (default: from URI, else pi_bot)")
    ap.add_argument("--overwrite", action="store_true",
                    help="drop existing target collections before inserting")
    ap.add_argument("--dry-run", action="store_true",
                    help="print counts only; touch nothing")
    args = ap.parse_args()

    copied = _copy_source(args.sqlite)
    conn = sqlite3.connect(str(copied))
    conn.row_factory = sqlite3.Row

    try:
        tables = _tables(conn)
        for t in tables:
            n = conn.execute(
                f'SELECT COUNT(*) AS n FROM "{t["name"]}"'
            ).fetchone()["n"]
            t["rows"] = n

        uri = _load_uri(args.uri, required=not args.dry_run)
        if uri is None:
            # Dry run without credentials: validate the source only.
            total = sum(int(t["rows"]) for t in tables)
            print(f"Source : {args.sqlite}")
            print(f"{len(tables)} tables, {total} rows (source only — "
                  "no MONGO_URI, target not checked)")
            for t in tables:
                flag = " id" if t["auto_id"] else ""
                print(f'  {t["name"]}: {t["rows"]} rows{flag}')
            return

        # Import AFTER MONGO_URI is set so Database() connects to the real target.
        sys.path.insert(0, str(PROJECT_ROOT))
        from bot.database import db  # noqa: E402  (connects + creates indexes)

        if "mongomock" in db.backend:
            sys.exit(f"Refusing to migrate into the test backend ({db.backend}).")
        if args.db and args.db != db._mongo.name:
            sys.exit(f"--db {args.db} != URI database {db._mongo.name}; "
                     "fix the URI's /dbname instead.")

        print(f"Target : {db.backend}")
        print(f"Source : {args.sqlite}")

        plan: List[Dict[str, object]] = []
        blocked: List[str] = []

        for t in tables:
            name = str(t["name"])
            coll = db.collection(name)
            existing = coll.estimated_document_count()
            if existing and not args.overwrite:
                blocked.append(f"  {name}: {existing} docs already present")
                continue
            plan.append({**t, "existing": existing})

        if blocked:
            print("Target not empty (re-run with --overwrite to replace):")
            print("\n".join(blocked))
            sys.exit(1)

        total = sum(int(p["rows"]) for p in plan)
        print(f"{len(plan)} tables, {total} rows -> "
              f"{'DRY RUN (no writes)' if args.dry_run else 'inserting'}")
        for p in plan:
            flag = " id" if p["auto_id"] else ""
            pre = f" (replace {p['existing']})" if p["existing"] else ""
            print(f"  {p['name']}: {p['rows']} rows{flag}{pre}")

        if args.dry_run:
            return

        for p in plan:
            name = str(p["name"])
            cols: List[str] = list(p["columns"])  # type: ignore[arg-type]
            coll = db.collection(name)
            if p["existing"]:
                coll.delete_many({})

            cur = conn.execute(f'SELECT * FROM "{name}"')
            batch = []
            for row in cur:
                doc = {k: row[k] for k in cols}  # keep sqlite column names
                batch.append(doc)
                if len(batch) >= 1000:
                    coll.insert_many(batch, ordered=False)
                    batch = []
            if batch:
                coll.insert_many(batch, ordered=False)

            if p["auto_id"]:
                mx = conn.execute(
                    f'SELECT MAX(id) AS m FROM "{name}"'
                ).fetchone()["m"]
                if mx is not None:
                    # $max: re-runs can only move the counter forward.
                    db.collection("counters").update_one(
                        {"_id": name}, {"$max": {"seq": int(mx)}}, upsert=True
                    )
            print(f"  migrated {name}: {p['rows']} rows")

        print("Done. Verify with the bot's read paths before retiring SQLite.")
    finally:
        conn.close()
        shutil.rmtree(copied.parent, ignore_errors=True)


if __name__ == "__main__":
    main()
