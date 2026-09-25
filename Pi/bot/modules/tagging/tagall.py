"""Boa-style mass mention — /tagall, /etagall, @all, @eall (from Yumeko).

Ported from boabot's Yumeko/modules/tagall.py (Telethon) onto Pi's Bot API
stack. Same command surface and pacing, Pi's infrastructure:

    /tagall <text>    mention everyone by NAME, text as the first line
    /etagall <text>   same, but each member linked by a RANDOM emoji from
                      the owner's custom emoji set (bot/emojis.py — boabot
                      used an arbitrary list; Pi uses EMOJI_MAP glyphs)
    reply-only        batches post as replies to the replied message
    @all / @eall      same triggers without a slash
    /cancel           boabot's stop command → Pi's /tagabort handler
                      (shared session-per-chat, so it stops this too)

Boabot rules kept: admin-only, groups-only, text+reply is an error,
5 mentions per message, 3 s between messages, and boabot's own method —
a LIVE MTProto iter_participants stream tagging every member (admins
and bots included; the provided text heads every batch). Pi-only
hardening: cancel-aware sleeps (CancelToken), flood-safe sends, and
the observed registry as fallback when MTProto is off. Only /all
applies Pi's admin/bot exclusion.
"""

from __future__ import annotations

import asyncio
import random
from html import escape

from telegram import Update
from telegram.ext import ContextTypes

from bot.emojis import E, EMOJI_MAP
from bot.logger import logger
from bot.responses import action_card, plain_error, plain_ok

from . import (
    activity_tracker,
    config,
    database as tdb,
    member_registry,
    permissions,
    session as sess_mod,
    settings as settings_mod,
)
from .exceptions import AdminFetchError, NobodyToTagError, SessionCancelled
from .presence import get_manager
from .sender import _FloodExc, _SOFT_NET, failed_card, flood_seconds, nobody_card
from .utils import clean_display_name

# Owner's custom emoji glyphs (unique, order-stable) for /etagall —
# boabot did random.choice(emojis); Pi draws from its own set.
EMOJI_POOL: tuple[str, ...] = tuple(
    dict.fromkeys(
        fallback
        for pairs in EMOJI_MAP.values()
        for fallback, _custom_id in pairs
    )
)


def _anchor(user_id: int, label: str) -> str:
    """One mention anchor: <a href="tg://user?id=…">label</a>."""
    return f'<a href="tg://user?id={user_id}">{escape(label)}</a>'


def _name_anchor(user_id: int, display_name: str) -> str:
    """Name anchor with Pi's sanitisation (escape + control strip + cap)."""
    name = clean_display_name(display_name, user_id, config.NAME_MAX)
    return _anchor(user_id, name)


def parse_input(message) -> tuple[str, str]:
    """Classify the trigger like boabot's pattern_match logic.

    Returns (mode, text) where mode is one of:
        "text"      plain text argument → first line of the first batch
        "reply"     used as a reply → batches quote the replied message
        "one_arg"   text AND reply together → error
        "no_input"  neither → error
    """
    raw = (getattr(message, "text", None) or "").strip()
    parts = raw.split(None, 1)
    arg = parts[1].strip() if len(parts) > 1 else ""
    has_reply = getattr(message, "reply_to_message", None) is not None
    if arg and has_reply:
        return "one_arg", ""
    if has_reply:
        return "reply", ""
    if arg:
        return "text", arg
    return "no_input", ""


# ── Commands ─────────────────────────────────────────────────────

async def tagall_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/tagall — mention everyone by name."""
    await _start(update, context, emoji_mode=False)


async def etagall_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/etagall — mention everyone with random custom emojis."""
    await _start(update, context, emoji_mode=True)


async def at_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """@all / @eall — same flows without a slash (boabot's second pattern)."""
    msg = update.effective_message
    if msg is None:
        return
    raw = (msg.text or "").strip()
    if not raw:
        return
    first = raw.split(None, 1)[0]
    await _start(update, context, emoji_mode=(first == "@eall"))


async def _start(update: Update, context: ContextTypes.DEFAULT_TYPE, *,
                 emoji_mode: bool) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if msg is None or chat is None or user is None:
        return

    if chat.type not in ("group", "supergroup"):
        await msg.reply_text(plain_error(config.MSG_NOT_GROUP), parse_mode="HTML")
        return

    mode, text = parse_input(msg)
    if mode == "one_arg":
        await msg.reply_text(plain_error(config.MSG_TAGALL_ONE_ARG), parse_mode="HTML")
        return
    if mode == "no_input":
        await msg.reply_text(plain_error(config.MSG_TAGALL_NO_INPUT), parse_mode="HTML")
        return
    if sess_mod.is_running(chat.id):
        await msg.reply_text(plain_error(config.MSG_RUNNING), parse_mode="HTML")
        return
    if not await permissions.is_admin(chat.id, user.id, context.bot):
        await msg.reply_text(plain_error(config.MSG_NOT_ADMIN), parse_mode="HTML")
        return

    # Authoritative admin exclusion — fetched fresh for every run.
    try:
        admins = await permissions.admin_ids(chat.id, context.bot)
    except AdminFetchError:
        await msg.reply_text(plain_error(config.MSG_ADMIN_FETCH_FAIL), parse_mode="HTML")
        return

    # tagall is boabot's "everybody" flow: ignore the activity window
    # AND the /allsettings max cap — /tagall must tag everyone known,
    # not just the first N. registry_mode="sync" makes the run do a fresh
    # MTProto participant enumeration (when enabled) instead of trusting
    # the observed-activity registry alone.
    st = settings_mod.get(chat.id).replace_(
        mode="all", window_hours=0, max_mentions=0, registry_mode="sync"
    )
    source = msg.reply_to_message if mode == "reply" else msg

    db_id = 0
    try:
        db_id = tdb.create_session(
            chat.id, user.id, mode=st.mode, window_hours=st.window_hours
        )
        session = sess_mod.create(
            chat_id=chat.id,
            session_id=db_id,
            invoker_id=user.id,
            source_message=source,
            status_message=msg,
            settings=st,
            admin_ids=admins,
        )
    except Exception:
        logger.error(f"Tagall session create failed chat={chat.id}", exc_info=True)
        if db_id:
            try:
                tdb.finish_session(db_id, "failed", error="start")
            except Exception:
                pass
        await msg.reply_text(
            plain_error("Could not start tagging — try again."), parse_mode="HTML"
        )
        return

    session.task = asyncio.create_task(
        run(
            session,
            context,
            text=text,
            reply_mode=(mode == "reply"),
            emoji_mode=emoji_mode,
        )
    )


# ── Send helpers ─────────────────────────────────────────────────

async def _send(context, s, text: str, *, reply_to_id: int = None) -> None:
    """Flood-safe, cancel-aware send (boabot's plain send_message + Pi's rules)."""
    attempts = 0
    while True:
        s.token.check()
        try:
            kwargs = dict(chat_id=s.chat_id, text=text, parse_mode="HTML")
            if reply_to_id:
                kwargs["reply_to_message_id"] = reply_to_id
            elif s.thread_id:
                kwargs["message_thread_id"] = s.thread_id
            await context.bot.send_message(**kwargs)
            return
        except _FloodExc as e:
            secs = flood_seconds(e)
            s.metrics.floodwaits += 1
            if secs > config.FLOOD_MAX:
                raise
            logger.info(f"Tagall flood wait {secs}s in chat {s.chat_id}")
            await s.token.sleep(secs)
        except _SOFT_NET as e:
            attempts += 1
            if attempts > config.SEND_RETRIES:
                raise
            logger.warning(f"Tagall send retry ({e})")
            s.metrics.retries += 1
            await s.token.sleep(1.0)


async def _final_reply(s, text: str) -> None:
    """Terminal card as a fresh reply (never edits the invoker's message)."""
    try:
        await s.status_message.reply_text(text, parse_mode="HTML")
    except Exception as e:
        logger.warning(f"Tagall final reply failed chat={s.chat_id}: {e}")


def _done_card(s) -> str:
    return action_card(
        "Tagging Complete",
        [
            (E.ANNOUNCE, "Tagged", f"{s.tagged} / {s.total} users"),
            (E.SPEAKER, "Messages", str(s.metrics.messages_sent)),
        ],
        icon=E.CHECK,
    )


# ── Member sources (boabot: live iter_participants stream) ─────────

async def _live_members(stream, s, *, emoji_mode: bool):
    """Boabot's live stream → (user_id, label); counts total as it goes."""
    async for user_id, name in stream:
        s.total += 1
        yield user_id, (
            random.choice(EMOJI_POOL) if emoji_mode
            else clean_display_name(name, user_id, config.NAME_MAX)
        )


def _registry_members(candidates, *, emoji_mode: bool):
    """Fallback when MTProto is off — registry rows as (user_id, label)."""

    async def _gen():
        for c in candidates:
            yield c.user_id, (
                random.choice(EMOJI_POOL) if emoji_mode
                else clean_display_name(c.display_name, c.user_id,
                                        config.NAME_MAX)
            )

    return _gen()


# ── Run loop ─────────────────────────────────────────────────────

async def run(session, context, *, text: str = "", reply_mode: bool = False,
              emoji_mode: bool = False) -> None:
    """Execute one tagall session (always terminal-states itself).

    Source: boabot's LIVE iter_participants stream (MTProto), falling
    back to the registry. Batches: 5 mentions per message, 3 s apart
    (boabot's pacing) — cancel-aware sleeps, flood-safe sends.
    """
    s = session
    try:
        # Ensure MTProto is configured+connected before candidate assembly:
        # 'sync' registry mode enumerates live participants there. Idempotent
        # and a fast no-op when MTProto is disabled.
        await get_manager().start()

        # Fresh activity + history backfill — the registry only knows
        # members the bot has observed (Bot API cannot enumerate).
        try:
            await activity_tracker.flush_async()
        except Exception as e:
            logger.warning(f"Tagall pre-run flush failed: {e}")
        try:
            tdb.seed_from_history(s.chat_id)
        except Exception as e:
            logger.warning(f"Tagall history seed failed: {e}")

        # Boabot's method: stream participants LIVE over MTProto exactly
        # like boa's iter_participants — every member, no exclusions.
        # Fall back to the observed registry (parity flags, same "tag
        # everyone" semantics) when MTProto is off.
        stream = get_manager().live_member_stream(s.chat_id)
        if stream is not None:
            members = _live_members(stream, s, emoji_mode=emoji_mode)
        else:
            candidates = await member_registry.assemble_candidates(
                s.chat_id, s.settings, s.admin_ids,
                exclude_bots=False, exclude_admins=False,
            )
            if not candidates:
                raise NobodyToTagError()
            s.total = len(candidates)
            members = _registry_members(candidates, emoji_mode=emoji_mode)

        reply_to_id = s.source_message_id if reply_mode else None
        safe_text = escape(text) if text else ""
        batch: list[str] = []
        sent = 0

        async def _flush() -> None:
            nonlocal sent
            if sent:
                # Boabot slept 3 s between messages; cancel-aware here.
                await s.token.sleep(config.TAGALL_BATCH_DELAY)
            body = ", ".join(batch)
            if safe_text and not reply_mode:
                # Boabot: the provided text heads EVERY batch message.
                body = f"{safe_text}\n{body}"
            await _send(context, s, body, reply_to_id=reply_to_id)
            s.tagged += len(batch)
            s.metrics.record_send(len(batch))
            batch.clear()
            sent += 1

        async for user_id, label in members:
            s.token.check()
            batch.append(_anchor(user_id, label))
            if len(batch) >= config.TAGALL_BATCH_SIZE:
                await _flush()

        if not s.total:
            raise NobodyToTagError()
        if batch:
            # Flush the final partial batch — boabot dropped its <5 tail.
            await _flush()

        sess_mod.finish(s, "completed")
        tdb.finish_session(
            s.session_id,
            "completed",
            total=s.total,
            tagged=s.tagged,
            messages_sent=s.metrics.messages_sent,
        )
        await _final_reply(s, _done_card(s))
        logger.info(
            f"Tagall completed chat={s.chat_id} tagged={s.tagged}/{s.total}"
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
        await _final_reply(
            s,
            plain_ok(
                config.MSG_STOPPED_FMT.format(tagged=s.tagged, total=s.total)
            ),
        )
        logger.info(f"Tagall stopped chat={s.chat_id} tagged={s.tagged}")

    except NobodyToTagError:
        sess_mod.finish(s, "failed")
        known = tdb.count_members(s.chat_id)
        tdb.finish_session(s.session_id, "failed", total=0, error="nobody")
        await _final_reply(s, nobody_card(known, s.settings))

    except asyncio.CancelledError:
        sess_mod.finish(s, "failed")
        tdb.finish_session(
            s.session_id, "failed", total=s.total, tagged=s.tagged,
            messages_sent=s.metrics.messages_sent, error="task cancelled",
        )
        raise

    except Exception as e:
        logger.error(f"Tagall session crashed chat={s.chat_id}", exc_info=e)
        sess_mod.finish(s, "failed")
        tdb.finish_session(
            s.session_id, "failed", total=s.total, tagged=s.tagged,
            messages_sent=s.metrics.messages_sent, error=type(e).__name__,
        )
        await _final_reply(s, failed_card("Unexpected error — see logs."))

    finally:
        s.done.set()
        sess_mod.discard(s)
