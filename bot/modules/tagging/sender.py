"""Send loop — copy source, batch mentions, progress, flood, cancel.

Flow:
    1. flush activity → seed registry from shared history → assemble
       candidates (registry/admin/presence)
    2. copy_message of the replied-to source (no re-upload)
    3. send mention batches (thread-aware) with cancel-aware pacing
    4. final card: completion / stop / failure (edit, reply fallback)

Cancellation: checked between every step and raced against FloodWait
sleeps via CancelToken.sleep(), so /tagabort always lands promptly.
"""

from __future__ import annotations

import asyncio
from typing import List, Optional

from telegram import Message
from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut

# PTB 21.6 exposes RetryAfter (the flood-wait exception); older/newer
# lines may expose FloodWait instead — bind whichever exists.
try:  # pragma: no cover - depends on installed PTB version
    from telegram.error import FloodWait as _FloodExc
except ImportError:  # pragma: no cover
    _FloodExc = RetryAfter

from bot.emojis import E
from bot.logger import logger
from bot.responses import action_card

from . import batcher, config, database as tdb, member_registry, session as sess_mod
from .exceptions import (
    FloodTooLongError,
    NobodyToTagError,
    SessionCancelled,
    TaggingError,
)
from .keyboards import progress_keyboard
from .models import Candidate, TagSettings
from .session import TagSession
from .utils import fmt_duration, fmt_n, pct, progress_bar

# Errors that are expected/soft in a mass-send loop.
_SOFT_NET = (TimedOut, NetworkError)


def flood_seconds(exc) -> int:
    """Seconds to wait: .retry_after (PTB 21.6) or .value (other lines)."""
    secs = getattr(exc, "retry_after", None)
    if not secs:
        secs = getattr(exc, "value", None)
    try:
        return int(secs or 0)
    except (TypeError, ValueError):
        return 0


# ── Cards ─────────────────────────────────────────────────────────

def start_card(st) -> str:
    """Opening card — takes TagSettings (built before the session exists)."""
    return action_card(
        "Tagging Started",
        [
            (E.ANNOUNCE, "Status", "Preparing members…"),
            (E.SETTINGS, "Mode", st.mode.replace("_", " ").title()),
            (E.INFO, "Batch", f"{st.batch_size} units"),
        ],
        icon=E.ANNOUNCE,
    )


def progress_card(s: TagSession) -> str:
    bar = progress_bar(s.tagged, s.total)
    return action_card(
        "Tagging in Progress",
        [
            (E.ANNOUNCE, "Tagged", f"{fmt_n(s.tagged)} / {fmt_n(s.total)}"),
            (E.STAR, "Bar", f"{bar} {pct(s.tagged, s.total)}%"),
            (E.SPEAKER, "Messages", str(s.metrics.messages_sent)),
            (E.TIME, "Elapsed", fmt_duration(s.metrics.elapsed)),
        ],
        icon=E.ANNOUNCE,
    )


def done_card(s: TagSession) -> str:
    return action_card(
        "Tagging Complete",
        [
            (E.ANNOUNCE, "Tagged", f"{fmt_n(s.tagged)} / {fmt_n(s.total)} users"),
            (E.SPEAKER, "Messages", str(s.metrics.messages_sent)),
            (E.TIME, "Duration", fmt_duration(s.metrics.elapsed)),
            (E.INFO, "Rate", f"{s.metrics.rate_per_min:.1f} msg/min"),
        ],
        icon=E.CHECK,
    )


def stopped_card(s: TagSession) -> str:
    return action_card(
        "Tagging Stopped",
        [
            (E.USER, "Tagged", f"{fmt_n(s.tagged)} / {fmt_n(s.total)} users"),
            (E.SPEAKER, "Messages", str(s.metrics.messages_sent)),
            (E.TIME, "Elapsed", fmt_duration(s.metrics.elapsed)),
        ],
        icon=E.CROSS,
    )


def failed_card(detail: str) -> str:
    return action_card(
        "Tagging Failed",
        [(E.INFO, "Detail", detail)],
        icon=E.ERROR,
    )


def nobody_card(known: int, st: TagSettings) -> str:
    """Enriched 'nobody matched' card — says why and how to fix it.

    `known` = members currently in the registry (post-seed). The plain
    failed_card left users stuck on a dead end with zero explanation.
    """
    fields = [
        (E.INFO, "Detail", config.MSG_NOBODY),
        (E.USER, "Registry", f"{fmt_n(known)} members known"),
    ]
    if known <= 0:
        fields.append((E.INFO, "Hint", "Members are learned as they chat."))
    elif st.mode == "all":
        fields.append(
            (E.SETTINGS, "Hint", "Known members are admins, bots or left.")
        )
    else:
        fields.append(
            (E.SETTINGS, "Hint", "/allsettings mode all — ignore the window")
        )
    return action_card("No One to Tag", fields, icon=E.ERROR)


# ── Progress editing (rate-limited) ───────────────────────────────

async def _edit_status(
    s: TagSession,
    text: str,
    *,
    keyboard=None,
    force: bool = False,
) -> bool:
    """Edit the live card; swallow 'not modified' + edit races.

    Returns True when the card shows `text` (now or already), False when
    the edit was rate-limited or genuinely failed. Terminal paths pass
    force=True and route a False through _finalize_status() so a broken
    edit can never leave the chat stuck on the opening card.
    """
    import time as _time

    if not force:
        gap = _time.monotonic() - (s._last_edit or 0)  # type: ignore[attr-defined]
        if gap < config.PROGRESS_EDIT_MIN:
            return False
    try:
        await s.status_message.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        s._last_edit = _time.monotonic()  # type: ignore[attr-defined]
        s.metrics.edits += 1
        return True
    except BadRequest as e:
        if "not modified" in str(e).lower():
            return True  # the card already shows this text
        # Terminal (force) failures must be visible; progress-only
        # failures stay quiet to avoid spamming a long run.
        (logger.warning if force else logger.debug)(
            f"Tagging status edit failed: {e}"
        )
        return False
    except Exception as e:
        (logger.warning if force else logger.debug)(
            f"Tagging status edit error: {e}"
        )
        return False


async def _finalize_status(s: TagSession, text: str) -> None:
    """Land the terminal card no matter what.

    Tries the status-message edit first; if that fails (deleted card,
    stale message, edit race), falls back to a fresh reply in the chat
    so the user always sees the outcome instead of a frozen card.
    """
    if await _edit_status(s, text, keyboard=None, force=True):
        return
    try:
        await s.status_message.reply_text(text, parse_mode="HTML")
        logger.warning(
            f"Tagging final-card edit failed — sent fallback reply "
            f"chat={s.chat_id}"
        )
    except Exception as e:
        logger.error(f"Tagging final-card fallback failed chat={s.chat_id}: {e}")


# ── Sending ───────────────────────────────────────────────────────

async def _send_batch(context, s: TagSession, text: str) -> int:
    """Send one batch with flood/retry handling; returns mention count."""
    token = s.token
    attempts = 0
    while True:
        token.check()
        try:
            kwargs = dict(
                chat_id=s.chat_id,
                text=text,
                parse_mode="HTML",
            )
            if s.thread_id:
                kwargs["message_thread_id"] = s.thread_id
            await context.bot.send_message(**kwargs)
            return batcher.count_mentions(text)
        except _FloodExc as e:
            secs = flood_seconds(e)
            s.metrics.floodwaits += 1
            if secs > config.FLOOD_MAX:
                raise FloodTooLongError(secs) from e
            logger.info(f"Tagging flood wait {secs}s in chat {s.chat_id}")
            await token.sleep(secs)  # raises SessionCancelled if aborted
            # Flood-waited sends were rejected — retry the same batch.
        except _SOFT_NET as e:
            attempts += 1
            if attempts > config.SEND_RETRIES:
                raise
            logger.warning(f"Tagging send retry ({e})")
            s.metrics.retries += 1
            await token.sleep(1.0)


async def _copy_source(context, s: TagSession) -> Optional[Message]:
    """copy_message of the replied source (no re-upload of media)."""
    try:
        kwargs = dict(
            chat_id=s.chat_id,
            from_chat_id=s.source_chat_id,
            message_id=s.source_message_id,
        )
        if s.thread_id:
            kwargs["message_thread_id"] = s.thread_id
        return await context.bot.copy_message(**kwargs)
    except Exception as e:
        logger.warning(f"Tagging source copy failed (continuing): {e}")
        return None


# ── Main entry ────────────────────────────────────────────────────

async def run(session: TagSession, context) -> None:
    """Execute one full tagging session (always terminal-states itself)."""
    s = session
    # Track last progress-edit time (attached dynamically; dataclass has
    # no slot restrictions).
    s._last_edit = 0.0  # type: ignore[attr-defined]

    try:
        # 1. Fresh activity → honest candidates.
        from . import activity_tracker

        try:
            await activity_tracker.flush_async()
        except Exception as e:
            logger.warning(f"Tagging pre-run flush failed: {e}")

        # Registry backfill from shared history — the observer only
        # knows members who chatted since this process started.
        try:
            seeded = tdb.seed_from_history(s.chat_id)
            if seeded:
                logger.info(
                    f"Tagging seeded {seeded} member(s) from history "
                    f"chat={s.chat_id}"
                )
        except Exception as e:
            logger.warning(f"Tagging history seed failed: {e}")

        candidates: List[Candidate] = await member_registry.assemble_candidates(
            s.chat_id, s.settings, s.admin_ids
        )
        if not candidates:
            raise NobodyToTagError()
        s.total = len(candidates)

        # 2. Copy the source message first — the chat sees context instantly.
        await _copy_source(context, s)

        # 3. Mention batches.
        mentions = member_registry.mention_list(candidates)
        batches = batcher.make_batches(
            mentions, target=s.settings.batch_size
        )
        await _edit_status(s, progress_card(s), keyboard=progress_keyboard(), force=True)

        for text in batches:
            s.token.check()
            n = await _send_batch(context, s, text)
            s.tagged += n
            s.metrics.record_send(n)
            await _edit_status(s, progress_card(s), keyboard=progress_keyboard())
            if s.settings.send_mode == "throttled":
                await s.token.sleep(config.THROTTLE_DELAY)
            else:
                # Brief pause even in normal mode to stay under
                # Telegram's group-message pacing.
                await s.token.sleep(0.35)

        sess_mod.finish(s, "completed")
        tdb.finish_session(
            s.session_id,
            "completed",
            total=s.total,
            tagged=s.tagged,
            messages_sent=s.metrics.messages_sent,
        )
        await _finalize_status(s, done_card(s))
        logger.info(
            f"Tagging completed chat={s.chat_id} tagged={s.tagged}/{s.total} "
            f"in {fmt_duration(s.metrics.elapsed)}"
        )

    except SessionCancelled:
        sess_mod.finish(s, "aborted")
        tdb.finish_session(
            s.session_id,
            "aborted",
            total=s.total,
            tagged=s.tagged,
            messages_sent=s.metrics.messages_sent,
        )
        await _finalize_status(s, stopped_card(s))
        logger.info(f"Tagging stopped chat={s.chat_id} tagged={s.tagged}")

    except NobodyToTagError as e:
        sess_mod.finish(s, "failed")
        known = tdb.count_members(s.chat_id)
        tdb.finish_session(
            s.session_id, "failed", total=0, error=str(e) or "nobody",
        )
        logger.info(
            f"Tagging nobody matched chat={s.chat_id} known={known} "
            f"mode={s.settings.mode}"
        )
        await _finalize_status(s, nobody_card(known, s.settings))

    except FloodTooLongError as e:
        sess_mod.finish(s, "failed")
        tdb.finish_session(
            s.session_id,
            "failed",
            total=s.total,
            tagged=s.tagged,
            messages_sent=s.metrics.messages_sent,
            error=f"flood {e.seconds}s",
        )
        await _finalize_status(
            s,
            failed_card(
                f"Rate limited — wait about {fmt_duration(e.seconds)} "
                f"then try again."
            ),
        )

    except TaggingError as e:
        sess_mod.finish(s, "failed")
        tdb.finish_session(
            s.session_id, "failed", total=s.total, tagged=s.tagged,
            messages_sent=s.metrics.messages_sent, error=str(e),
        )
        await _finalize_status(s, failed_card(str(e)))

    except asyncio.CancelledError:
        sess_mod.finish(s, "failed")
        tdb.finish_session(
            s.session_id, "failed", total=s.total, tagged=s.tagged,
            messages_sent=s.metrics.messages_sent, error="task cancelled",
        )
        raise

    except Exception as e:
        logger.error(f"Tagging session crashed chat={s.chat_id}", exc_info=e)
        sess_mod.finish(s, "failed")
        tdb.finish_session(
            s.session_id, "failed", total=s.total, tagged=s.tagged,
            messages_sent=s.metrics.messages_sent, error=type(e).__name__,
        )
        await _finalize_status(s, failed_card("Unexpected error — see logs."))

    finally:
        s.done.set()
        sess_mod.discard(s)
