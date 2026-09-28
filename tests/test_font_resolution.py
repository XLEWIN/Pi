"""Portable font resolution — Railway/Linux must never crash at import.

Run from the Pi root:

    python tests/test_font_resolution.py
    python -m unittest discover -s tests

Regression guard for the Railway deploy crash: profile_templates used
hardcoded ``C:\\Windows\\Fonts\\arialbd.ttf`` at import time and died
with ``OSError: cannot open resource`` on Linux. Fonts now resolve to
the bundled ``bot/assets/NotoSans-Bold.ttf`` and every load has a
never-raising floor.

Environment isolation (BEFORE any bot import):
    * BOT_TOKEN is forced — importing `bot` pulls bot.config, which
      exits without one.
    * LOCALAPPDATA points at a temp dir so any DB touch stays isolated.

No network: PIL loads real local files; failure paths are patched.
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

# ── Environment isolation — must precede bot imports ──────────────
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["BOT_TOKEN"] = "1:TEST-TOKEN-FOR-UNIT-TESTS"
_TEST_DIR = tempfile.mkdtemp(prefix="pi_fonts_test_")
os.environ["LOCALAPPDATA"] = _TEST_DIR
atexit.register(shutil.rmtree, _TEST_DIR, ignore_errors=True)

# ── Imports (after env) ───────────────────────────────────────────
import bot.profile_templates as pt  # noqa: E402
from bot.modules import sticker as sm  # noqa: E402

BUNDLED = ROOT / "bot" / "assets" / "NotoSans-Bold.ttf"


class TestProfileFontResolution(unittest.TestCase):
    def test_bundled_font_ships_with_repo(self):
        self.assertTrue(BUNDLED.is_file(), "bot/assets/NotoSans-Bold.ttf missing")
        self.assertEqual(pt._find_font(), str(BUNDLED))

    def test_import_time_fonts_are_usable(self):
        # These crashed the Railway boot at import (OSError: cannot open resource)
        for f in (pt.FONT_NAME, pt.FONT_USERNAME, pt.FONT_LEVEL_LABEL,
                  pt.FONT_LEVEL_VALUE, pt.FONT_STAT_LABEL, pt.FONT_STAT_VALUE):
            self.assertTrue(hasattr(f, "getbbox"), type(f))
            # drawable: measures text without raising
            f.getbbox("Pi")

    def test_font_bold_path_resolves_to_bundled(self):
        self.assertEqual(pt.FONT_BOLD, str(BUNDLED))
        self.assertEqual(pt.FONT_REG, pt.FONT_BOLD)

    def test_find_font_survives_when_no_candidate_exists(self):
        # Simulates a stripped container: no bundled file, no system fonts.
        with mock.patch.object(pt.os.path, "isfile", return_value=False):
            result = pt._find_font()
        # returns the bundled path anyway; _font() then falls to load_default
        self.assertTrue(result.endswith("NotoSans-Bold.ttf"))


class TestFontNeverRaises(unittest.TestCase):
    def test_truetype_failure_falls_back_to_default(self):
        with mock.patch.object(
            pt.ImageFont, "truetype", side_effect=OSError("cannot open resource")
        ), mock.patch.object(
            pt.ImageFont, "load_default", return_value="DEFAULT"
        ) as ld:
            self.assertEqual(pt._font(20), "DEFAULT")
            ld.assert_called_once_with(size=20)

    def test_old_pillow_without_size_kwarg(self):
        # Pillow < 10.1 load_default() takes no size → TypeError → retry bare.
        with mock.patch.object(
            pt.ImageFont, "truetype", side_effect=OSError("cannot open resource")
        ), mock.patch.object(
            pt.ImageFont, "load_default",
            side_effect=[TypeError("unexpected keyword"), "DEFAULT"],
        ) as ld:
            self.assertEqual(pt._font(20), "DEFAULT")
            self.assertEqual(ld.call_count, 2)

    def test_everything_failing_still_returns_a_font(self):
        with mock.patch.object(
            pt.ImageFont, "truetype", side_effect=OSError("nope")
        ), mock.patch.object(
            pt.ImageFont, "load_default", side_effect=OSError("nope")
        ):
            with self.assertRaises(OSError):
                pt._font(20)  # documented floor: only total PIL breakage raises


class TestMemifyFont(unittest.TestCase):
    def test_pick_font_prefers_bundled(self):
        self.assertEqual(sm._pick_font(), str(BUNDLED))

    def test_pick_font_raises_kangerror_without_any_font(self):
        with mock.patch.object(sm.os.path, "isfile", return_value=False):
            with self.assertRaises(sm.KangError):
                sm._pick_font()


if __name__ == "__main__":
    unittest.main(verbosity=2)
