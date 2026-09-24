"""Presence manager — composes activity (always) + MTProto (optional).

Order of enrichment:
    1. activity — tiers every candidate (never claims online)
    2. mtproto  — upgrades truly-online users to RANK_ONLINE,
                  unless registry_mode == 'registry_only'

`source_label` is what /tagstats reports honestly.
"""

from __future__ import annotations

import asyncio
from typing import List, Sequence

from bot.logger import logger

from ..models import Candidate
from .activity import ActivityPresence
from .base import PresenceProvider
from .mtproto import MtprotoPresence, get_provider as _mtproto_provider


class PresenceManager:
    def __init__(self) -> None:
        self._activity = ActivityPresence()
        self._mtproto: PresenceProvider = _mtproto_provider()
        self._kicked = False

    @property
    def mtproto(self) -> MtprotoPresence:
        return self._mtproto  # type: ignore[return-value]

    @property
    def source_label(self) -> str:
        """Honest label for /tagstats."""
        if self._mtproto.available:
            return "MTProto + activity"
        return "Activity (MTProto off)"

    async def enrich(
        self,
        chat_id: int,
        candidates: Sequence[Candidate],
        now: float,
        *,
        registry_mode: str = "hybrid",
    ) -> None:
        try:
            await self._activity.enrich(chat_id, candidates, now)
        except Exception as e:
            logger.warning(f"Tagging activity presence failed: {e}")
        if registry_mode == "registry_only" or not self._mtproto.available:
            return
        try:
            await self._mtproto.enrich(chat_id, candidates, now)
        except Exception as e:
            logger.warning(f"Tagging MTProto presence failed: {e}")

    async def sync_members(self, chat_id: int, registry_mode: str) -> int:
        """Full participant sync — only in 'sync' mode with MTProto on."""
        if registry_mode != "sync" or not self._mtproto.available:
            return 0
        return await self._mtproto.sync_chat(chat_id)

    async def start(self) -> None:
        """Configure + connect MTProto if enabled (never raises)."""
        try:
            if self._mtproto.configure():
                await self._mtproto.start()
        except Exception as e:
            logger.warning(f"Tagging MTProto startup failed: {e}")

    def kick(self) -> None:
        """Idempotent: schedule start() on the *running* loop.

        setup() runs before run_polling creates the event loop, so
        startup must be deferred to the first update we see.
        """
        if self._kicked:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop yet — next call will retry
        self._kicked = True
        loop.create_task(self._safe_start())

    async def _safe_start(self) -> None:
        try:
            await self.start()
        except Exception as e:
            logger.warning(f"Tagging presence start failed: {e}")

    async def stop(self) -> None:
        try:
            await self._mtproto.stop()
        except Exception as e:
            logger.debug(f"Tagging MTProto stop failed: {e}")


def known_chat_ids() -> List[int]:
    """Chats with registry rows — used by the MTProto refresh loop."""
    from .. import database as tdb

    rows = tdb._conn.execute(
        "SELECT DISTINCT chat_id FROM tag_members LIMIT 100"
    ).fetchall()
    return [int(r[0]) for r in rows]


# Module-level singleton used by handler/sender.
manager = PresenceManager()


def get_manager() -> PresenceManager:
    return manager
