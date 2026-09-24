"""Tagging helpers — formatting, truncation, UTF-16 length math."""

from __future__ import annotations

import re
from typing import Optional

_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


def utf16_len(text: str) -> int:
    """Length in UTF-16 code units — Telegram's message-limit unit."""
    return len(text.encode("utf-16-le")) // 2


def truncate(text: str, max_len: int) -> str:
    """Cut to max_len characters with an ellipsis when too long."""
    text = text.strip()
    if len(text) <= max_len:
        return text
    if max_len <= 1:
        return text[:max_len]
    return text[: max_len - 1].rstrip() + "…"


def clean_display_name(name: Optional[str], fallback_id: int, max_len: int) -> str:
    """Sanitize a user display name for HTML mention anchors."""
    if not name:
        return str(fallback_id)
    name = _CTRL_RE.sub(" ", str(name))
    name = " ".join(name.split())  # collapse whitespace/newlines
    if not name:
        return str(fallback_id)
    return truncate(name, max_len)


def fmt_n(n: int) -> str:
    """Thousands-separated number: 1234 → '1,234'."""
    return f"{int(n):,}"


def fmt_duration(seconds: float) -> str:
    """Short duration: 8.3 → '8s', 75 → '1m 15s', 3672 → '1h 1m'."""
    total = int(max(0, seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def progress_bar(done: int, total: int, width: int = 10) -> str:
    """Unicode bar: [████░░░░] done/total (never overflows)."""
    total = max(0, total)
    done = max(0, min(done, total))
    if total <= 0:
        filled = 0
    else:
        filled = round(done * width / total)
    return "[" + "█" * filled + "░" * (width - filled) + "]"


def pct(done: int, total: int) -> int:
    if total <= 0:
        return 0
    return int(min(100, round(done * 100 / total)))
