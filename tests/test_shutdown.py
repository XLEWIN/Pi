"""Regression tests: SIGTERM must reach the shutdown flush.

Railway's Restart/Redeploy stops the container with SIGTERM. The
default action kills the process outright — no ``finally``, no
``atexit`` — so the last ~5s of buffered message counters were lost on
every restart. ``_install_signal_handlers`` cancels the main task
instead, letting ``amain()``'s ``finally`` block flush buffers and
close the session before exit.

Environment isolation (BEFORE any bot import):
    * BOT_TOKEN is forced — importing `bot` pulls bot.config, which
      exits without one.
    * LOCALAPPDATA points at a temp dir so any DB touch stays isolated.

No network: signal wiring only; the real loop is never started.
"""

from __future__ import annotations

import atexit
import os
import shutil
import signal
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["BOT_TOKEN"] = "1:TEST-TOKEN-FOR-UNIT-TESTS"
_TEST_DIR = tempfile.mkdtemp(prefix="pi_shutdown_test_")
os.environ["LOCALAPPDATA"] = _TEST_DIR
atexit.register(shutil.rmtree, _TEST_DIR, ignore_errors=True)

import main as main_mod  # noqa: E402 — after env isolation


class _FakeTask:
    def __init__(self, done: bool = False) -> None:
        self._done = done
        self.cancelled = False

    def done(self) -> bool:
        return self._done

    def cancel(self) -> None:
        self.cancelled = True


class _FakeLoop:
    def __init__(self) -> None:
        self.handlers = {}

    def add_signal_handler(self, sig, cb) -> None:  # noqa: ANN001
        self.handlers[sig] = cb


class _UnsupportedLoop:
    def add_signal_handler(self, sig, cb) -> None:  # noqa: ANN001
        raise NotImplementedError("add_signal_handler unsupported")


class TestInstallSignalHandlers(unittest.TestCase):
    def test_registers_sigterm_and_cancels_main_task(self):
        loop, task = _FakeLoop(), _FakeTask()
        main_mod._install_signal_handlers(loop, task)
        self.assertIn(signal.SIGTERM, loop.handlers)
        loop.handlers[signal.SIGTERM]()
        self.assertTrue(task.cancelled)

    def test_on_stop_flag_runs_before_cancel(self):
        order = []
        task = _FakeTask()
        raw_cancel = task.cancel
        task.cancel = lambda: (order.append("cancel"), raw_cancel())[1]
        loop = _FakeLoop()
        main_mod._install_signal_handlers(
            loop, task, on_stop=lambda: order.append("stop")
        )
        loop.handlers[signal.SIGTERM]()
        # amain() must see the flag BEFORE the CancelledError lands.
        self.assertEqual(order, ["stop", "cancel"])

    def test_already_finished_task_is_not_cancelled(self):
        loop, task = _FakeLoop(), _FakeTask(done=True)
        main_mod._install_signal_handlers(loop, task)
        loop.handlers[signal.SIGTERM]()
        self.assertFalse(task.cancelled)

    def test_unsupported_loop_falls_back_to_signal_module(self):
        task = _FakeTask()
        with mock.patch.object(main_mod.signal, "signal") as sig_mock:
            main_mod._install_signal_handlers(_UnsupportedLoop(), task)
        sig_mock.assert_called_once()
        self.assertEqual(sig_mock.call_args[0][0], signal.SIGTERM)
        handler = sig_mock.call_args[0][1]
        handler(signal.SIGTERM, None)  # simulate delivery
        self.assertTrue(task.cancelled)


if __name__ == "__main__":
    unittest.main()
