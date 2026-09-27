"""MTProto session handling: TELETHON_SESSION env string vs session file.

Run from the Pi/Pi root:

    python tests/test_mtproto_session.py
    python -m unittest discover -s tests

Environment isolation (BEFORE any bot import):
    * BOT_TOKEN is forced — importing `bot` pulls bot.config, which
      exits without one.
    * LOCALAPPDATA points at a temp dir so session files stay isolated.

Covers:
    * configure() prefers the TELETHON_SESSION env string (StringSession)
    * falls back to the sqlite session file when the env string is empty
    * a garbage env string fails safe (configure → False, no crash)
    * TAG_MTPROTO off / creds missing → not configured
    * the unauthorized warning points at scripts/mtproto_login.py
    * scripts/mtproto_login.py env-file helpers
"""

from __future__ import annotations

import atexit
import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

# ── Environment isolation — must precede bot imports ──────────────
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["BOT_TOKEN"] = "1:TEST-TOKEN-FOR-UNIT-TESTS"
_TEST_DIR = tempfile.mkdtemp(prefix="pi_mtproto_test_")
os.environ["LOCALAPPDATA"] = _TEST_DIR
atexit.register(shutil.rmtree, _TEST_DIR, ignore_errors=True)

# ── Imports (after env) ───────────────────────────────────────────
from bot.modules.tagging import config as tconf  # noqa: E402
from bot.modules.tagging.presence import mtproto as mtp  # noqa: E402

_CREDS = {
    "TAG_MTPROTO": "1",
    "TELEGRAM_API_ID": "12345",
    "TELEGRAM_API_HASH": "0123456789abcdef0123456789abcdef",
}


# ═════════════════════════════════════════════════════════════════
# configure(): session source selection
# ═════════════════════════════════════════════════════════════════

class TestConfigureSessionSource(unittest.TestCase):
    def test_env_constant_is_telethon_session(self):
        self.assertEqual(tconf.ENV_SESSION_STRING, "TELETHON_SESSION")

    def test_env_string_takes_priority_over_file(self):
        raw = "1" + "A" * 64  # handed to StringSession untouched
        captured: dict = {}

        def fake_client(session, api_id, api_hash):
            captured["session"] = session
            return SimpleNamespace(session=session)

        with mock.patch.dict(os.environ, {**_CREDS,
                                          "TELETHON_SESSION": raw}), \
                mock.patch.object(mtp, "StringSession",
                                  lambda s: ("StringSession", s)), \
                mock.patch.object(mtp, "TelegramClient", fake_client):
            prov = mtp.MtprotoPresence()
            ok = prov.configure()

        self.assertTrue(ok)
        self.assertEqual(captured["session"], ("StringSession", raw))

    def test_garbage_env_string_fails_safe(self):
        """Invalid StringSession → configure returns False, no crash."""
        with mock.patch.dict(os.environ, {**_CREDS,
                                          "TELETHON_SESSION": "garbage"}):
            prov = mtp.MtprotoPresence()
            ok = prov.configure()

        self.assertFalse(ok)
        self.assertIsNone(prov._client)

    def test_disabled_without_tag_mtproto(self):
        with mock.patch.dict(os.environ, {**_CREDS, "TAG_MTPROTO": ""}):
            prov = mtp.MtprotoPresence()
            self.assertFalse(prov.configure())

    def test_disabled_without_credentials(self):
        with mock.patch.dict(os.environ, _CREDS):
            os.environ.pop("TELEGRAM_API_ID", None)  # restored on exit
            prov = mtp.MtprotoPresence()
            self.assertFalse(prov.configure())


# ═════════════════════════════════════════════════════════════════
# sqlite session-file fallback (async: real TelegramClient init needs
# a running event loop — same as production, where configure() runs
# inside async manager.start())
# ═════════════════════════════════════════════════════════════════

class TestFileSessionFallback(unittest.IsolatedAsyncioTestCase):
    async def test_falls_back_to_sqlite_session_file(self):
        with mock.patch.dict(os.environ, {**_CREDS,
                                          "TELETHON_SESSION": ""}):
            prov = mtp.MtprotoPresence()
            ok = prov.configure()

        self.assertTrue(ok)
        filename = getattr(prov._client.session, "filename", "") or ""
        self.assertIn("pi_tag", filename)
        # DB_DIR is frozen at first bot.database import (some earlier
        # test module's temp dir during full discovery) — assert ours.
        self.assertTrue(filename.startswith(str(mtp.DB_DIR)))


# ═════════════════════════════════════════════════════════════════
# start(): unauthorized warning is actionable
# ═════════════════════════════════════════════════════════════════

class TestUnauthorizedWarning(unittest.IsolatedAsyncioTestCase):
    async def test_warning_points_at_login_script_and_env_key(self):
        class FakeClient:
            async def connect(self):
                pass

            async def is_user_authorized(self):
                return False

            async def disconnect(self):
                pass

        prov = mtp.MtprotoPresence()
        prov._client = FakeClient()
        with self.assertLogs(mtp.logger, level="WARNING") as cm:
            await prov.start()

        self.assertFalse(prov.available)
        self.assertIsNone(prov._client)  # client closed on failure
        text = " ".join(cm.output)
        self.assertIn("scripts/mtproto_login.py", text)
        self.assertIn("TELETHON_SESSION", text)
        self.assertIn("falling back to activity presence", text)


# ═════════════════════════════════════════════════════════════════
# scripts/mtproto_login.py env-file helpers
# ═════════════════════════════════════════════════════════════════

def _load_script():
    spec = importlib.util.spec_from_file_location(
        "pi_mtproto_login_script", ROOT / "scripts" / "mtproto_login.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestLoginScriptEnvHelpers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = _load_script()

    def test_append_env_writes_when_key_missing(self):
        path = Path(tempfile.mkdtemp(prefix="pi_env_")) / ".env"
        path.write_text("BOT_TOKEN=x\n", encoding="utf-8")
        self.assertTrue(self.script.append_env(path, "TELETHON_SESSION", "1abc"))
        self.assertIn("BOT_TOKEN=x\nTELETHON_SESSION=1abc\n",
                      path.read_text(encoding="utf-8"))

    def test_append_env_refuses_when_key_present(self):
        path = Path(tempfile.mkdtemp(prefix="pi_env_")) / ".env"
        path.write_text("TELETHON_SESSION=old\n", encoding="utf-8")
        self.assertFalse(self.script.append_env(path, "TELETHON_SESSION", "new"))
        self.assertEqual(path.read_text(encoding="utf-8"),
                         "TELETHON_SESSION=old\n")

    def test_dotenv_values_parses_file(self):
        path = Path(tempfile.mkdtemp(prefix="pi_env_")) / ".env"
        path.write_text("A=1\n# comment\nB=\n", encoding="utf-8")
        vals = self.script.dotenv_values(path)
        self.assertEqual(vals.get("A"), "1")
        self.assertEqual(vals.get("B"), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
