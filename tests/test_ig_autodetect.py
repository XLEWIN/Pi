"""Tests for the Instagram auto-detect filter (bot/modules/instagram).

Run from the repo root:

    python tests/test_ig_autodetect.py
    python -m unittest discover -s tests

Environment isolation (BEFORE any bot import):
    * BOT_TOKEN is forced — importing `bot` pulls bot.config, which
      exits without one.
    * LOCALAPPDATA points at a temp dir so runtime files are isolated.

No network: only the filter runs — the downloader is never touched.

The old filter (magic-filter ``F.text.regexp``) defaulted to
match-at-start mode, so ``https://instagram.com/…`` links never fired
and auto-download was dead without /igdl.  These cases pin the fixed
behaviour: real pasted links (any position, text or caption) match;
plain text and commands do not.
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

# ── Environment isolation — must precede bot imports ──────────────
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["BOT_TOKEN"] = "1:TEST-TOKEN-FOR-UNIT-TESTS"
_TEST_DIR = tempfile.mkdtemp(prefix="pi_ig_test_")
os.environ["LOCALAPPDATA"] = _TEST_DIR
atexit.register(shutil.rmtree, _TEST_DIR, ignore_errors=True)

# ── Imports (after env) ───────────────────────────────────────────
from bot.modules.instagram import ig_filter  # noqa: E402


def _msg(text=None, caption=None) -> SimpleNamespace:
    return SimpleNamespace(text=text, caption=caption)


class TestAutoDetectFilter(unittest.IsolatedAsyncioTestCase):
    async def _match(self, message) -> bool:
        return bool(await ig_filter(message))

    async def test_bare_https_url_matches(self):
        self.assertTrue(
            await self._match(_msg(text="https://www.instagram.com/reel/ABC123/"))
        )

    async def test_url_inside_sentence_matches(self):
        self.assertTrue(
            await self._match(
                _msg(text="check this https://instagram.com/p/XYZ?igsh=1")
            )
        )

    async def test_no_scheme_www_url_matches(self):
        self.assertTrue(
            await self._match(_msg(text="www.instagram.com/reel/ABC"))
        )

    async def test_share_link_matches(self):
        self.assertTrue(
            await self._match(
                _msg(text="https://www.instagram.com/share/reel/xyz")
            )
        )

    async def test_multiline_message_matches(self):
        self.assertTrue(
            await self._match(
                _msg(text="look\nhttps://instagram.com/reel/ABC")
            )
        )

    async def test_caption_matches(self):
        self.assertTrue(
            await self._match(
                _msg(caption="https://www.instagram.com/reel/ABC123/")
            )
        )

    async def test_plain_text_does_not_match(self):
        self.assertFalse(await self._match(_msg(text="hello world")))

    async def test_command_does_not_match(self):
        self.assertFalse(
            await self._match(
                _msg(text="/igdl https://www.instagram.com/reel/ABC")
            )
        )

    async def test_bare_host_without_path_does_not_match(self):
        self.assertFalse(await self._match(_msg(text="instagram.com")))

    async def test_empty_and_none_do_not_match(self):
        self.assertFalse(await self._match(_msg(text="")))
        self.assertFalse(await self._match(_msg(text=None, caption=None)))
        self.assertFalse(await self._match(None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
