"""Tests for the owner-only restart command (bot/modules/restart.py).

Run from the Pi/Pi root:

    python tests/test_restart.py
    python -m unittest discover -s tests

Environment isolation (BEFORE any bot import):
    * BOT_TOKEN is forced — importing `bot` pulls bot.config, which
      exits without one.
    * LOCALAPPDATA points at a temp dir so any DB touch stays isolated.

No process is ever actually re-exec'd: ``os.execvp`` is mocked in
every path that could reach it.
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

# ── Environment isolation — must precede bot imports ──────────────
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["BOT_TOKEN"] = "1:TEST-TOKEN-FOR-UNIT-TESTS"
_TEST_DIR = tempfile.mkdtemp(prefix="pi_restart_test_")
os.environ["LOCALAPPDATA"] = _TEST_DIR
atexit.register(shutil.rmtree, _TEST_DIR, ignore_errors=True)

# ── Imports (after env) ───────────────────────────────────────────
from aiofakes import call, command_filters  # noqa: E402
from bot import pipeline  # noqa: E402
from bot.constants import HELP_MENU  # noqa: E402
from bot.modules import restart as rm  # noqa: E402

OWNER_ID = 42


# ═════════════════════════════════════════════════════════════════
# Fakes
# ═════════════════════════════════════════════════════════════════

class _Prog:
    def __init__(self) -> None:
        self.edits: list = []

    async def edit_text(self, text, **kw):
        self.edits.append({"text": text, **kw})
        return self

    @property
    def last(self):
        return self.edits[-1] if self.edits else None


class _Msg:
    """Group command message — ``reply_text`` lands in .replies and
    returns the progress message the handler edits afterwards."""

    def __init__(self, text: str = "/restart", user_id: int | None = OWNER_ID) -> None:
        self.text = text
        self.chat = SimpleNamespace(id=-100999, type="supergroup", title="T")
        self.from_user = (
            None if user_id is None
            else SimpleNamespace(id=user_id, username=None, first_name="Lewin")
        )
        self.replies: list = []
        self.prog: _Prog | None = None

    async def reply(self, text, **kw):
        self.replies.append({"text": text, **kw})
        self.prog = _Prog()
        return self.prog

    async def answer(self, text, **kw):
        return await self.reply(text, **kw)

    @property
    def last(self):
        return self.replies[-1] if self.replies else None


@contextmanager
def _deny_case():
    """Owner gate active + execvp rigged to fail the test if it runs."""
    with mock.patch.object(rm, "settings", SimpleNamespace(owner_id=OWNER_ID)):
        with mock.patch.object(
            rm.os, "execvp", side_effect=AssertionError("exec must not run")
        ) as ex:
            yield ex


# ═════════════════════════════════════════════════════════════════
# argv construction
# ═════════════════════════════════════════════════════════════════

class TestRestartArgv(unittest.TestCase):
    def test_prefers_orig_argv(self):
        with mock.patch.object(rm.sys, "orig_argv",
                               ["python", "-m", "main"], create=True):
            self.assertEqual(rm._restart_argv(),
                             ["python", "-m", "main"])

    def test_falls_back_to_executable_and_argv(self):
        with mock.patch.object(rm.sys, "orig_argv", None, create=True):
            argv = rm._restart_argv()
        self.assertEqual(argv[0], sys.executable)
        self.assertEqual(argv[1:], sys.argv)


# ═════════════════════════════════════════════════════════════════
# Owner gate
# ═════════════════════════════════════════════════════════════════

class TestOwnerGate(unittest.IsolatedAsyncioTestCase):
    async def test_non_owner_denied_without_exec(self):
        msg = _Msg(user_id=99)
        with _deny_case() as ex:
            await call(rm.restart_command, msg)
        self.assertIsNone(msg.last)
        ex.assert_not_called()

    async def test_missing_user_denied(self):
        msg = _Msg(user_id=None)
        with _deny_case() as ex:
            await call(rm.restart_command, msg)
        self.assertIsNone(msg.last)
        ex.assert_not_called()

    async def test_unconfigured_owner_id_denies_everyone(self):
        """OWNER_ID unset (0) → nobody may restart, not even id 0."""
        msg = _Msg(user_id=0)
        with mock.patch.object(rm, "settings", SimpleNamespace(owner_id=0)), \
                mock.patch.object(
                    rm.os, "execvp",
                    side_effect=AssertionError("exec must not run")) as ex:
            await call(rm.restart_command, msg)
        self.assertIsNone(msg.last)
        ex.assert_not_called()


# ═════════════════════════════════════════════════════════════════
# Owner path
# ═════════════════════════════════════════════════════════════════

class TestOwnerRestart(unittest.IsolatedAsyncioTestCase):
    async def test_owner_reexecs_and_reports_failure_if_exec_dies(self):
        msg = _Msg()
        with mock.patch.object(rm, "settings",
                               SimpleNamespace(owner_id=OWNER_ID)), \
                mock.patch.object(rm, "_NOTICE_WAIT", 0), \
                mock.patch.object(rm, "logger"), \
                mock.patch.object(rm.os, "execvp",
                                  side_effect=RuntimeError("nope")) as ex:
            await call(rm.restart_command, msg)

        # notice sent first, with the branded card
        self.assertEqual(len(msg.replies), 1)
        self.assertIn("Restarting the bot", msg.last["text"])
        self.assertEqual(msg.last.get("parse_mode"), "HTML")

        # re-exec attempted with the current run's argv
        ex.assert_called_once()
        argv = rm._restart_argv()
        self.assertEqual(ex.call_args[0][0], argv[0])
        self.assertEqual(list(ex.call_args[0][1]), argv)

        # exec failed → bot stays up and the owner sees why
        edit = msg.prog.last
        self.assertIsNotNone(edit)
        self.assertIn("Restart failed", edit["text"])
        self.assertIn("RuntimeError", edit["text"])
        self.assertIn("still running", edit["text"])

    async def test_exec_returning_counts_as_failure(self):
        """execvp returning normally means nothing was replaced."""
        msg = _Msg()
        with mock.patch.object(rm, "settings",
                               SimpleNamespace(owner_id=OWNER_ID)), \
                mock.patch.object(rm, "_NOTICE_WAIT", 0), \
                mock.patch.object(rm, "logger"), \
                mock.patch.object(rm.os, "execvp", return_value=None):
            await call(rm.restart_command, msg)
        self.assertIn("Restart failed", msg.prog.last["text"])
        self.assertIn("still running", msg.prog.last["text"])


# ═════════════════════════════════════════════════════════════════
# Write-behind flush before exec
# ═════════════════════════════════════════════════════════════════

class TestFlushBeforeExec(unittest.IsolatedAsyncioTestCase):
    """os.execvp skips post_shutdown/atexit — buffers must flush first."""

    async def test_owner_flushes_both_buffers_then_execs(self):
        import bot.database as bdb
        from bot.modules.tagging import activity_tracker as at

        events: list = []
        msg = _Msg()

        def _exec(cmd, argv):
            events.append("exec")
            raise RuntimeError("stop here")

        fake_db = SimpleNamespace(flush_buffers=lambda: events.append("db"))
        fake_at = SimpleNamespace(flush_now=lambda: events.append("activity"))
        with mock.patch.object(rm, "settings",
                               SimpleNamespace(owner_id=OWNER_ID)), \
                mock.patch.object(rm, "_NOTICE_WAIT", 0), \
                mock.patch.object(rm, "logger"), \
                mock.patch.object(bdb, "db", fake_db), \
                mock.patch.object(at, "flush_now", fake_at.flush_now), \
                mock.patch.object(rm.os, "execvp", side_effect=_exec):
            await call(rm.restart_command, msg)

        self.assertEqual(events, ["db", "activity", "exec"])
        self.assertIn("Restart failed", msg.prog.last["text"])

    async def test_flush_failure_does_not_block_restart(self):
        import bot.database as bdb
        from bot.modules.tagging import activity_tracker as at

        events: list = []
        msg = _Msg()

        def _boom():
            events.append("db")
            raise RuntimeError("flush boom")

        def _exec(cmd, argv):
            events.append("exec")
            raise RuntimeError("nope")

        with mock.patch.object(rm, "settings",
                               SimpleNamespace(owner_id=OWNER_ID)), \
                mock.patch.object(rm, "_NOTICE_WAIT", 0), \
                mock.patch.object(rm, "logger"), \
                mock.patch.object(bdb, "db",
                                  SimpleNamespace(flush_buffers=_boom)), \
                mock.patch.object(at, "flush_now",
                                  lambda: events.append("activity")), \
                mock.patch.object(rm.os, "execvp", side_effect=_exec):
            await call(rm.restart_command, msg)

        # db flush blew up, activity flush + exec still ran
        self.assertEqual(events, ["db", "activity", "exec"])
        self.assertIn("Restart failed", msg.prog.last["text"])
        self.assertIn("still running", msg.prog.last["text"])


# ═════════════════════════════════════════════════════════════════
# Wiring + help
# ═════════════════════════════════════════════════════════════════

class TestWiring(unittest.TestCase):
    def test_setup_registers_restart_and_reboot(self):
        pipeline.clear()
        routes = rm.setup()
        self.assertEqual(routes, ["/restart", "/reboot"])
        entries = pipeline.snapshot()
        cmds = [e for e in entries if command_filters(e.flt)]
        self.assertEqual(len(cmds), 1)
        self.assertEqual(cmds[0].event, "message")
        self.assertEqual(cmds[0].group, 0)
        self.assertEqual(cmds[0].key, "bot.modules.restart.restart_command")
        self.assertEqual(sorted(command_filters(cmds[0].flt)[0].commands),
                         ["reboot", "restart"])

    def test_help_documents_restart(self):
        general = next(m for m in HELP_MENU if m["key"] == "general")
        lines = [line for _, cmds in general["sections"] for line in cmds]
        self.assertTrue(
            any(line.startswith("/restart") for line in lines),
            "General help is missing the /restart entry",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
