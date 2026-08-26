"""Clean, colored terminal logging for Phi π.

Format:  [HH:MM:SS] [TAG] message
Tags:    INF (info) · LOD (module load) · WRN (warning) · ERR (error)
"""

import ctypes
import logging
import sys
from datetime import datetime

# ── ANSI colors ─────────────────────────────────────────
RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
GRAY = "\x1b[90m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
RED = "\x1b[31m"
MAGENTA = "\x1b[35m"

# Custom level for module loading (between INFO and WARNING)
LOAD_LEVEL = 25
logging.addLevelName(LOAD_LEVEL, "LOAD")

LEVEL_STYLES = {
    logging.DEBUG: (GRAY, "DBG"),
    logging.INFO: (GREEN, "INF"),
    LOAD_LEVEL: (MAGENTA, "LOD"),
    logging.WARNING: (YELLOW, "WRN"),
    logging.ERROR: (RED, "ERR"),
    logging.CRITICAL: (RED + BOLD, "CRT"),
}


def _prepare_console() -> None:
    """Make the Windows console UTF-8 capable and ANSI-aware."""
    if sys.platform != "win32":
        return
    # Windows consoles default to cp1252 — force UTF-8 so π, →, ✅ print cleanly
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    # Enable ANSI escape sequences in legacy conhost windows
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    except Exception:
        pass


class BotFormatter(logging.Formatter):
    """Compact one-line formatter: [time] [TAG] message."""

    def format(self, record: logging.LogRecord) -> str:
        color, tag = LEVEL_STYLES.get(record.levelno, (RESET, "???"))
        ts = datetime.now().strftime("%H:%M:%S")
        return f"{DIM}{ts}{RESET} {color}{tag}{RESET} {record.getMessage()}"


def log_load(logger: logging.Logger, message: str) -> None:
    """Log at the custom LOAD level."""
    logger.log(LOAD_LEVEL, message)


def setup_logger(name: str = "phi") -> logging.Logger:
    """Build the root bot logger and quiet noisy third-party loggers."""
    _prepare_console()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(BotFormatter())

    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    logger.propagate = False

    # Keep the terminal clean — only surface real problems from libs
    for noisy in ("httpx", "httpcore", "telegram", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return logger


logger = setup_logger()