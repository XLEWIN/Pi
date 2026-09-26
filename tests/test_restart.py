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
    def __init__(self, text: str = "/restart") -> None:
        self.text = text
        self.replies: list = []
        self.prog: _Prog | None = None

    async def reply_text(self, text, **kw):
        self.replies.append({"text": text, **kw})
        self.prog = _Prog()
        return self.prog

    @property
    def last(self):
        return self.replies[-1] if self.replies else None


def _update(msg: _Msg, user_id=OWNER_ID):
    user = None if user_id is None else SimpleNamespace(
        id=user_id, username=None, first_name="Lewin"
    )
    return SimpleNamespace(effective_message=msg, effective_user=user, message=msg)


def _ctx():
    return SimpleNamespace(bot=None, args=[])


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
        msg = _Msg()
        with _deny_case() as ex:
            await rm.restart_command(_update(msg, user_id=99), _ctx())
        self.assertIn("Only the bot owner", msg.last["text"])
        self.assertIn("restart me", msg.last["text"])
        ex.assert_not_called()

    async def test_missing_user_denied(self):
        msg = _Msg()
        with _deny_case() as ex:
            await rm.restart_command(_update(msg, user_id=None), _ctx())
        self.assertIn("Only the bot owner", msg.last["text"])
        ex.assert_not_called()

    async def test_unconfigured_owner_id_denies_everyone(self):
        """OWNER_ID unset (0) → nobody may restart, not even id 0."""
        msg = _Msg()
        with mock.patch.object(rm, "settings", SimpleNamespace(owner_id=0)), \
                mock.patch.object(
                    rm.os, "execvp",
                    side_effect=AssertionError("exec must not run")) as ex:
            await rm.restart_command(_update(msg, user_id=0), _ctx())
        self.assertIn("Only the bot owner", msg.last["text"])
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
            await rm.restart_command(_update(msg, user_id=OWNER_ID), _ctx())

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
            await rm.restart_command(_update(msg, user_id=OWNER_ID), _ctx())
        self.assertIn("Restart failed", msg.prog.last["text"])
        self.assertIn("still running", msg.prog.last["text"])


# ═════════════════════════════════════════════════════════════════
# Wiring + help
# ═════════════════════════════════════════════════════════════════

class TestWiring(unittest.TestCase):
    def test_setup_registers_restart_and_reboot(self):
        from telegram.ext import CommandHandler as PTBCommandHandler

        class _App:
            def __init__(self):
                self.handlers = []

            def add_handler(self, handler, group=0):
                self.handlers.append(handler)

        app = _App()
        routes = rm.setup(app)
        self.assertEqual(routes, ["/restart", "/reboot"])
        cmds = [h for h in app.handlers
                if isinstance(h, PTBCommandHandler)]
        self.assertEqual(len(cmds), 1)
        self.assertEqual(sorted(cmds[0].commands), ["reboot", "restart"])

    def test_help_documents_restart(self):
        general = next(m for m in HELP_MENU if m["key"] == "general")
        lines = [line for _, cmds in general["sections"] for line in cmds]
        self.assertTrue(
            any(line.startswith("/restart") for line in lines),
            "General help is missing the /restart entry",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
