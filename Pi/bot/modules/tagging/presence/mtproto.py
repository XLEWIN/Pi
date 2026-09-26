"""Optional MTProto presence — Telethon-backed, guarded, never required.

This bot runs Pure Bot API; Bot API cannot list group members or read
last-seen status. When the owner provides TELEGRAM_API_ID/API_HASH (from
my.telegram.org, a *user* account) and installs telethon, this provider:

    * connects once at startup (uses the existing session file if any)
    * periodically syncs participants → tag_members + presence timestamps
    * lets ActivityPresence upgrade users to RANK_ONLINE honestly

If telethon is missing, creds are absent, TAG_MTPROTO != 1, or the
session is unauthorized, `available` stays False and the module degrades
to activity-only presence. Hard dependency: NEVER.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Dict, Optional, Sequence, Tuple

from bot.database import DB_DIR
from bot.logger import logger

from .. import config
from ..models import RANK_ONLINE, Candidate
from .base import PresenceProvider

try:  # pragma: no cover - exercised only when telethon is installed
    from telethon import TelegramClient
    from telethon.tl.types import UserStatusOnline
    _TELETHON_OK = True
except Exception:  # ImportError and any packaging failure
    TelegramClient = None  # type: ignore
    UserStatusOnline = None  # type: ignore
    _TELETHON_OK = False


class MtprotoPresence(PresenceProvider):
    """Background presence/member sync over Telethon (optional)."""

    name = "mtproto"
    available = False

    def __init__(self) -> None:
        self._client = None
        self._task: Optional[asyncio.Task] = None
        # (chat_id, user_id) → last online timestamp from MTProto.
        self._online: Dict[Tuple[int, int], float] = {}

    # ── lifecycle ─────────────────────────────────────────────────

    def configure(self) -> bool:
        """Decide availability from env + telethon; does not connect yet."""
        if not _TELETHON_OK:
            return False
        if os.getenv(config.ENV_MTPROTO_ENABLED, "") != "1":
            return False
        api_id = os.getenv(config.ENV_API_ID, "").strip()
        api_hash = os.getenv(config.ENV_API_HASH, "").strip()
        if not api_id or not api_hash:
            logger.info("Tagging MTProto: %s/%s not set — activity only",
                        config.ENV_API_ID, config.ENV_API_HASH)
            return False
        try:
            self._client = TelegramClient(
                str(DB_DIR / "pi_tag"), int(api_id), api_hash
            )
        except Exception as e:
            logger.warning(f"Tagging MTProto init failed: {e}")
            self._client = None
            return False
        # available flips True only after a successful connect in start().
        return True

    async def start(self) -> None:
        """Connect + verify authorization, then start the refresh loop."""
        if self._client is None:
            return
        try:
            await self._client.connect()
            if not await self._client.is_user_authorized():
                logger.warning(
                    "Tagging MTProto: session unauthorized — run an "
                    "interactive Telethon login once with this session "
                    "file; falling back to activity presence."
                )
                self.available = False
                await self._close_client()
                return
        except Exception as e:
            logger.warning(f"Tagging MTProto connect failed: {e}")
            self.available = False
            await self._close_client()
            return
        self.available = True
        logger.info("Tagging MTProto presence enabled")
        self._task = asyncio.create_task(self._refresh_loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        await self._close_client()

    async def _close_client(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                pass
            self._client = None

    # ── refresh loop ──────────────────────────────────────────────

    async def _refresh_once(self) -> None:
        """One background sync pass — split out so tests can call it.

        Import the function from the submodule DIRECTLY: the presence
        package re-exports the singleton instance under the attribute
        name `manager`, which shadows the module for `from . import
        manager` (that produced 'PresenceManager' object has no
        attribute 'known_chat_ids' every refresh tick).
        """
        from .manager import known_chat_ids

        for chat_id in known_chat_ids():
            await self.sync_chat(chat_id)

    async def _refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(config.MTPROTO_SYNC_INTERVAL)
            try:
                await self._refresh_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"Tagging MTProto refresh failed: {e}")

    def iter_members(self, chat_id: int):
        """Boabot's method — LIVE participant stream of (user_id, name).

        Returns None without a connected client (caller falls back to the
        registry). Iteration errors propagate to the caller.
        """
        if self._client is None:
            return None

        async def _gen():
            async for user in self._client.iter_participants(chat_id):
                if user.id is None:
                    continue
                # boabot mentioned first_name only.
                yield user.id, (user.first_name or str(user.id))

        return _gen()

    async def sync_chat(self, chat_id: int) -> int:
        """Enumerate participants → registry rows + online timestamps."""
        if not self.available or self._client is None:
            return 0
        from .. import database as tdb

        count = 0
        seen: set = set()
        online_ts = time.time()  # epoch — matches DB timestamps
        try:
            async for user in self._client.iter_participants(chat_id):
                if user.id is None:
                    continue
                name = " ".join(
                    p for p in (user.first_name or "", user.last_name or "") if p
                ) or str(user.id)
                tdb.upsert_member(
                    chat_id,
                    user.id,
                    username=user.username,
                    display_name=name,
                    is_bot=bool(user.bot),
                )
                seen.add(user.id)
                count += 1
                status = getattr(user, "status", None)
                if isinstance(status, UserStatusOnline):
                    self._online[(chat_id, user.id)] = online_ts
                    tdb.set_presence(chat_id, user.id, online_ts)
        except Exception as e:
            logger.warning(f"Tagging MTProto sync chat {chat_id} failed: {e}")
            return count
        # Enumeration completed — reconcile: registry rows not seen live
        # have left (stale ghosts must never be tagged).
        try:
            tdb.mark_left_except(chat_id, seen)
        except Exception as e:
            logger.warning(f"Tagging MTProto reconcile failed: {e}")
        if count:
            logger.info(f"Tagging MTProto: synced {count} members of {chat_id}")
        return count

    # ── presence provider interface ───────────────────────────────

    async def enrich(
        self,
        chat_id: int,
        candidates: Sequence[Candidate],
        now: float,
    ) -> None:
        """Upgrade candidates with fresh MTProto online timestamps."""
        if not self.available:
            return
        for c in candidates:
            ts = self._online.get((chat_id, c.user_id))
            if ts and (now - ts) <= config.PRESENCE_TTL:
                c.presence_at = ts
                c.presence_rank = min(c.presence_rank, RANK_ONLINE)
            elif c.presence_at:
                # Keep stored presence_at from DB but never rank ONLINE
                # from stale data.
                if (now - c.presence_at) > config.PRESENCE_TTL:
                    c.presence_rank = max(c.presence_rank, 1)


_instance: Optional[MtprotoPresence] = None


def get_provider() -> MtprotoPresence:
    global _instance
    if _instance is None:
        _instance = MtprotoPresence()
    return _instance
