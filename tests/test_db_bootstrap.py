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
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import bot.database as bdb
from bot.async_bridge import run_sync
from bot.database import db

# ``_ensure_indexes`` / ``_log_snapshot`` became coroutines when the
# Mongo layer went async.  They are internal, so these tests drive them
# through the bridge rather than through the ``db`` facade.
_ensure_indexes = lambda inst: run_sync(inst._ensure_indexes())  # noqa: E731
_log_snapshot = lambda inst: run_sync(inst._log_snapshot())       # noqa: E731


class TestResolveUri(unittest.TestCase):
    def test_discovery_runner_is_a_test_process(self):
        # python -m unittest discover → sys.orig_argv carries the -m form
        # (sys.argv[0] itself is rewritten to "python.exe -m unittest").
        with mock.patch.object(
            sys, "orig_argv", ["python.exe", "-m", "unittest", "discover"]
        ), mock.patch.object(sys, "argv", ["python.exe -m unittest"]):
            self.assertTrue(bdb._is_test_process())

    def test_pytest_module_runner_is_a_test_process(self):
        with mock.patch.object(
            sys, "orig_argv", ["python.exe", "-m", "pytest"]
        ), mock.patch.object(sys, "argv", ["python.exe -m pytest"]):
            self.assertTrue(bdb._is_test_process())

    def test_direct_test_file_run_is_a_test_process(self):
        with mock.patch.object(
            sys, "argv", [r"C:\repo\tests\test_thing.py"]
        ):
            self.assertTrue(bdb._is_test_process())

    def test_explicit_env_flag_wins(self):
        with mock.patch.object(
            sys, "argv", ["main.py"]
        ), mock.patch.dict(os.environ, {"PI_TEST_BACKEND": "1"}):
            self.assertTrue(bdb._is_test_process())

    def test_production_invocation_is_not_a_test_process(self):
        # Regression: aiogram does `from unittest.mock import sentinel`
        # at import time, so unittest IS in sys.modules during a normal
        # `python main.py` boot — yet it must NOT count as a test run
        # (that false positive made Railway run on mongomock and wiped
        # all data on every restart despite MONGO_URI being set).
        # Clear PI_TEST_BACKEND: other test modules may set it at import
        # time (discovery loads every module before the first test runs).
        for argv in (["main.py"], [r"C:\app\main.py"], ["worker"]):
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(sys, "orig_argv", ["python", argv[0]]), \
                    mock.patch.dict(os.environ, {"PI_TEST_BACKEND": ""}):
                self.assertFalse(bdb._is_test_process(), argv)

    def test_this_very_suite_resolves_to_none(self):
        # The suite always runs via a detected runner → mongomock.
        self.assertIsNone(bdb._resolve_uri())
        self.assertIn("mongomock", db.backend)

    def test_missing_uri_outside_tests_fails_fast(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(
                bdb, "_is_test_process", return_value=False
            ), mock.patch.object(bdb, "_LEGACY_DIR", Path(td)), mock.patch.dict(
                os.environ, {"MONGO_URI": ""}
            ):
                with self.assertRaises(SystemExit) as cm:
                    bdb._resolve_uri()
        msg = str(cm.exception)
        self.assertIn("MONGO_URI", msg)
        self.assertIn("in-memory", msg)

    def test_env_uri_wins(self):
        sentinel_uri = "mongodb://example.invalid/phi"
        with mock.patch.object(
            bdb, "_is_test_process", return_value=False
        ), mock.patch.dict(os.environ, {"MONGO_URI": sentinel_uri}):
            self.assertEqual(bdb._resolve_uri(), sentinel_uri)


class TestEnsureIndexes(unittest.TestCase):
    def setUp(self):
        # A fresh mongomock-backed instance: empty collections, so the
        # unique constraints are deterministic regardless of whichever
        # tests ran before this module.
        self.fresh = bdb.Database()

    def test_index_creation_logs_no_warnings(self):
        # regression: dict-form keys → mongomock TypeError on every boot
        with self.assertNoLogs(logger="phi", level=logging.WARNING):
            _ensure_indexes(self.fresh)

    def test_expected_indexes_exist_and_are_unique(self):
        _ensure_indexes(self.fresh)
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
            _log_snapshot(inst)

    def test_snapshot_logs_counts(self):
        inst = self._prod_instance()
        inst._real_mongo = self._FakeMongo(
            {"users": 5, "groups": 2, "daily_messages": 100}
        )
        with self.assertLogs("phi", logging.INFO) as cm:
            _log_snapshot(inst)
        joined = "\n".join(cm.output)
        self.assertIn("users=5", joined)
        self.assertIn("groups=2", joined)
        self.assertIn("daily_messages=100", joined)
        self.assertNotIn("EMPTY", joined)

    def test_empty_database_warns_loudly(self):
        inst = self._prod_instance()
        inst._real_mongo = self._FakeMongo({})
        with self.assertLogs("phi", logging.WARNING) as cm:
            _log_snapshot(inst)
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
            _log_snapshot(inst)  # must not raise
        self.assertIn("snapshot failed", "\n".join(cm.output))


if __name__ == "__main__":
    unittest.main()
