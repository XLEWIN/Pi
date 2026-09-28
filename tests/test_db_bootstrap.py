"""Regression tests: database boot behavior (URI resolution + index creation).

Guards two production incidents:

1. Index creation failed silently on the mongomock backend — dict-form
   keys hit mongomock's ``gen_index_name`` TypeError, so every boot
   logged "Index ... skipped: not enough arguments for format string"
   and no index was ever created.
2. A missing ``MONGO_URI`` silently fell back to mongomock in
   production: message counts and rankings vanished on every restart
   while the bot pretended everything worked.  Outside tests the
   missing URI must now be fatal.
"""

import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import bot.database as bdb
from bot.database import db


class TestResolveUri(unittest.TestCase):
    def test_tests_process_resolves_to_mongomock(self):
        # unittest is loaded in this process → None → mongomock backend
        self.assertIsNone(bdb._resolve_uri())
        self.assertIn("mongomock", db.backend)

    def test_missing_uri_outside_tests_fails_fast(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(
                bdb, "_TEST_BACKENDS", ("no_such_module_xyz",)
            ), mock.patch.object(bdb, "_LEGACY_DIR", Path(td)), mock.patch.dict(
                os.environ, {"MONGO_URI": ""}
            ):
                with self.assertRaises(SystemExit) as cm:
                    bdb._resolve_uri()
        msg = str(cm.exception)
        self.assertIn("MONGO_URI", msg)
        self.assertIn("in-memory", msg)

    def test_env_uri_wins(self):
        sentinel = "mongodb://example.invalid/phi"
        with mock.patch.object(
            bdb, "_TEST_BACKENDS", ("no_such_module_xyz",)
        ), mock.patch.dict(os.environ, {"MONGO_URI": sentinel}):
            self.assertEqual(bdb._resolve_uri(), sentinel)


class TestEnsureIndexes(unittest.TestCase):
    def setUp(self):
        # A fresh mongomock-backed instance: empty collections, so the
        # unique constraints are deterministic regardless of whichever
        # tests ran before this module.
        self.fresh = bdb.Database()

    def test_index_creation_logs_no_warnings(self):
        # regression: dict-form keys → mongomock TypeError on every boot
        with self.assertNoLogs(logger="phi", level=logging.WARNING):
            self.fresh._ensure_indexes()

    def test_expected_indexes_exist_and_are_unique(self):
        self.fresh._ensure_indexes()
        users = self.fresh._mongo["users"].index_information()
        self.assertIn("user_id_1", users)
        self.assertTrue(
            users["user_id_1"].get("unique"), f"user_id_1 not unique: {users}"
        )
        members = self.fresh._mongo["group_members"].index_information()
        self.assertIn("chat_id_1_user_id_1", members)
        self.assertTrue(
            members["chat_id_1_user_id_1"].get("unique"),
            f"chat_id_1_user_id_1 not unique: {members}",
        )


class TestBootSnapshot(unittest.TestCase):
    """_log_snapshot: the deploy log must prove which store booted.

    A wrong-but-reachable MONGO_URI (valid cluster, empty/other
    database) made "everything vanished" look like data loss — the bot
    happily booted and served an empty database with no visible clue.
    """

    class _FakeColl:
        def __init__(self, n: int) -> None:
            self._n = n

        def estimated_document_count(self) -> int:
            return self._n

    class _FakeMongo:
        def __init__(self, counts) -> None:  # noqa: ANN001
            self._counts = counts

        def __getitem__(self, name):
            return TestBootSnapshot._FakeColl(self._counts.get(name, 0))

    def _prod_instance(self):
        inst = bdb.Database()          # mongomock under tests …
        inst.backend = "mongodb (pi_bot)"  # … pretend the production path
        return inst

    def test_mongomock_backend_skips_snapshot(self):
        inst = bdb.Database()
        self.assertIn("mongomock", inst.backend)
        with self.assertNoLogs(logger="phi", level=logging.INFO):
            inst._log_snapshot()

    def test_snapshot_logs_counts(self):
        inst = self._prod_instance()
        inst._real_mongo = self._FakeMongo(
            {"users": 5, "groups": 2, "daily_messages": 100}
        )
        with self.assertLogs("phi", logging.INFO) as cm:
            inst._log_snapshot()
        joined = "\n".join(cm.output)
        self.assertIn("users=5", joined)
        self.assertIn("groups=2", joined)
        self.assertIn("daily_messages=100", joined)
        self.assertNotIn("EMPTY", joined)

    def test_empty_database_warns_loudly(self):
        inst = self._prod_instance()
        inst._real_mongo = self._FakeMongo({})
        with self.assertLogs("phi", logging.WARNING) as cm:
            inst._log_snapshot()
        joined = "\n".join(cm.output)
        self.assertIn("EMPTY", joined)
        self.assertIn("MONGO_URI", joined)

    def test_snapshot_failure_never_crashes_boot(self):
        inst = self._prod_instance()

        class _Boom:
            def __getitem__(self, name):
                raise RuntimeError("nope")

        inst._real_mongo = _Boom()
        with self.assertLogs("phi", logging.WARNING) as cm:
            inst._log_snapshot()  # must not raise
        self.assertIn("snapshot failed", "\n".join(cm.output))


if __name__ == "__main__":
    unittest.main()
