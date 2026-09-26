"""Batch splitting — pack mentions into messages under Telegram's limits.

Hard rules:
    * every batch ≤ HARD_LIMIT (4096 UTF-16 units) — Telegram rejects more
    * aim for TARGET_LEN (3600) so formatting/entities never tip us over
    * ≤ MAX_MENTIONS_PER_BATCH per message (anti-spam sanity cap)
    * one oversized mention never blocks the queue (goes out alone)
"""

from __future__ import annotations

from typing import List

from . import config


def make_batches(
    mentions: List[str],
    *,
    target: int = config.TARGET_LEN,
    hard: int = config.HARD_LIMIT,
    cap: int = config.MAX_MENTIONS_PER_BATCH,
    separator: str = "\n",
) -> List[str]:
    """Split mention strings into ready-to-send message texts.

    Order is preserved; every input mention appears in exactly one batch.
    """
    if not mentions:
        return []
    target = min(target, hard)

    batches: List[str] = []
    current: List[str] = []
    current_len = 0

    for mention in mentions:
        add_len = len(mention.encode("utf-16-le")) // 2
        sep_len = 1 if current else 0  # newline between mentions
        projected = current_len + sep_len + add_len

        if current and (projected > target or len(current) >= cap):
            batches.append(separator.join(current))
            current, current_len = [], 0
            sep_len, projected = 0, add_len

        if not current and projected > hard:
            # Single mention over the hard limit (defensive — NAME_MAX
            # makes this practically impossible): send it alone and let
            # Telegram reject rather than silently dropping the user.
            batches.append(mention)
            continue

        current.append(mention)
        current_len = projected

    if current:
        batches.append(separator.join(current))
    return batches


def count_mentions(text: str) -> int:
    """How many user anchors are in one batch message."""
    return text.count("tg://user?id=")
