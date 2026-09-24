"""Send loop — copy source, batch mentions, progress, flood, cancel.

Flow:
    1. flush activity → assemble candidates (registry/admin/presence)
    2. copy_message of the replied-to source (no re-upload)
    3. send mention batches (thread-aware) with cancel-aware pacing
    4. final card: completion / stop / failure

Cancellation: checked between every step and raced against FloodWait
sleeps via CancelToken.sleep(), so /tagabort always lands promptly.
"""

from __future__ import annotations

import asyncio
import logging
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
from bot.responses import action_card

from . import batcher, config, database as tdb, member_registry, session as sess_mod
from .exceptions import (
    FloodTooLongError,
    NobodyToTagError,
    SessionCancelled,
    TaggingError,
)
from .keyboards import progress_keyboard
from .models import Candidate
from .session import TagSession
from .utils import fmt_duration, fmt_n, pct, progress_bar

logger = logging.getLogger(__name__)

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


# ── Progress editing (rate-limited) ───────────────────────────────

async def _edit_status(
    s: TagSession,
    text: str,
    *,
    keyboard=None,
    force: bool = False,
) -> None:
    """Edit the live card; swallow 'not modified' + edit races."""
    import time as _time

    if not force:
        gap = _time.monotonic() - (s._last_edit or 0)  # type: ignore[attr-defined]
        if gap < config.PROGRESS_EDIT_MIN:
            return
    try:
        await s.status_message.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        s._last_edit = _time.monotonic()  # type: ignore[attr-defined]
        s.metrics.edits += 1
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            logger.debug(f"Tagging status edit failed: {e}")
    except Exception as e:
        logger.debug(f"Tagging status edit error: {e}")


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
        await _edit_status(s, done_card(s), keyboard=None, force=True)
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
        await _edit_status(s, stopped_card(s), keyboard=None, force=True)
        logger.info(f"Tagging stopped chat={s.chat_id} tagged={s.tagged}")

    except NobodyToTagError as e:
        sess_mod.finish(s, "failed")
        tdb.finish_session(
            s.session_id, "failed", total=0, error=str(e)
        )
        await _edit_status(s, failed_card(config.MSG_NOBODY), force=True)

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
        await _edit_status(
            s,
            failed_card(
                f"Rate limited — wait about {fmt_duration(e.seconds)} "
                f"then try again."
            ),
            force=True,
        )

    except TaggingError as e:
        sess_mod.finish(s, "failed")
        tdb.finish_session(
            s.session_id, "failed", total=s.total, tagged=s.tagged,
            messages_sent=s.metrics.messages_sent, error=str(e),
        )
        await _edit_status(s, failed_card(str(e)), force=True)

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
        await _edit_status(s, failed_card("Unexpected error — see logs."), force=True)

    finally:
        s.done.set()
        sess_mod.discard(s)
