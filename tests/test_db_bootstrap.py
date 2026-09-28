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


if __name__ == "__main__":
    unittest.main()
