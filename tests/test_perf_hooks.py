"""Performance wiring: orjson session hooks + uvloop install guard.

orjson (Rust) is plugged into aiogram's BaseSession json_loads /
json_dumps hooks — the single-argument callables used for every
incoming update parse and outgoing request build. uvloop replaces the
event loop on Railway (Linux) only; Windows dev machines have no
wheels and must keep the default asyncio loop.

Environment isolation (BEFORE any bot import):
    * BOT_TOKEN is forced — importing `bot` pulls bot.config, which
      exits without one.
    * LOCALAPPDATA points at a temp dir so any DB touch stays isolated.
    * PI_TEST_BACKEND keeps database.py on mongomock even though this
      file imports main (which imports aiogram → unittest.mock).

No network: serialization and policy calls only.
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["BOT_TOKEN"] = "1:TEST-TOKEN-FOR-UNIT-TESTS"
os.environ["PI_TEST_BACKEND"] = "1"
_TEST_DIR = tempfile.mkdtemp(prefix="pi_perf_test_")
os.environ["LOCALAPPDATA"] = _TEST_DIR
atexit.register(shutil.rmtree, _TEST_DIR, ignore_errors=True)

import main as main_mod  # noqa: E402 — after env isolation

try:
    import orjson  # noqa: E401 — installed as a production dependency
except ImportError:  # pragma: no cover
    orjson = None


class TestJsonHooks(unittest.TestCase):
    def test_hooks_roundtrip_when_orjson_available(self):
        if orjson is None:
            self.skipTest("orjson not installed")
        hooks = main_mod._json_hooks()
        self.assertIn("json_loads", hooks)
        self.assertIn("json_dumps", hooks)
        sample = {"chat_id": -100123, "text": "héllo ✓", "n": [1, 2.5, None]}
        encoded = hooks["json_dumps"](sample)
        self.assertIsInstance(encoded, str)  # BaseSession contract is str
        self.assertEqual(hooks["json_loads"](encoded), sample)

    def test_hooks_empty_when_orjson_missing(self):
        # sys.modules[name] = None forces ImportError on import.
        with mock.patch.dict(sys.modules, {"orjson": None}):
            self.assertEqual(main_mod._json_hooks(), {})

    def test_aiohttp_session_accepts_hooks(self):
        from aiogram.client.session.aiohttp import AiohttpSession

        hooks = main_mod._json_hooks()
        session = AiohttpSession(timeout=30.0, limit=5, **hooks)
        payload = {"chat_id": 1, "text": "hi"}
        self.assertEqual(
            session.json_loads(session.json_dumps(payload)), payload
        )


class TestUvloopInstall(unittest.TestCase):
    def test_windows_keeps_default_loop(self):
        with mock.patch.object(sys, "platform", "win32"):
            self.assertFalse(main_mod._install_uvloop())

    def test_missing_package_is_nonfatal(self):
        with mock.patch.object(sys, "platform", "linux"), \
                mock.patch.dict(sys.modules, {"uvloop": None}):
            self.assertFalse(main_mod._install_uvloop())

    def test_installs_when_available(self):
        fake = mock.MagicMock()
        with mock.patch.object(sys, "platform", "linux"), \
                mock.patch.dict(sys.modules, {"uvloop": fake}):
            self.assertTrue(main_mod._install_uvloop())
        fake.install.assert_called_once()


if __name__ == "__main__":
    unittest.main()
