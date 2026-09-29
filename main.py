"""Phi pi - Pure Bot API entry point.

Runs aiogram (Bot API) with colored inline buttons.
No Telethon dependency.
"""

import asyncio
import signal
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramRetryAfter

from bot import pipeline
from bot.config import settings
from bot.constants import BOT_NAME, LOG_CHANNEL_ID
from bot.errors import on_error
from bot.loader import load_modules
from bot.logger import logger


async def _startup_log(bot: Bot, me, privacy_on: bool) -> None:  # noqa: ANN001
    try:
        from bot.database import db

        privacy_line = (
            "<b>Privacy:</b> ON — plain messages HIDDEN! "
            "BotFather /setprivacy → Disable"
            if privacy_on else
            "<b>Privacy:</b> OFF — all group messages visible"
        )
        startup_msg = (
            f"<b>Bot Started Successfully!</b>\n\n"
            f"<b>Bot:</b> @{me.username}\n"
            f"<b>Bot ID:</b> <code>{me.id}</code>\n"
            f"<b>Time:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            f"<b>Modules:</b> Loaded\n"
            f"<b>Database:</b> {db.backend}\n"
            f"<b>Colored Buttons:</b> Active (aiogram)\n"
            f"<b>Logging:</b> Active\n"
            f"{privacy_line}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━"
        )
        await bot.send_message(
            chat_id=LOG_CHANNEL_ID,
            text=startup_msg,
            parse_mode="HTML",
        )
        logger.info("Startup log sent to channel")
    except Exception as e:
        logger.warning(f"Startup log not sent: {e}")


async def _warm_read_caches() -> None:
    """Fill the hot read-through caches before the first update arrives.

    Every group message runs a serial gate chain (bind settings,
    blocklist, filters, watch words) plus per-join welcome/shield reads;
    on a cold process each of those was one Mongo round-trip until the
    first read filled its cache.  This warms them once in the
    background so the first message after a deploy doesn't pay the
    whole chain, and /rank-style commands start warm too.

    Purely best-effort: every failure is swallowed with a warning —
    the normal read-through path just loads the entry later, exactly
    as it would without this task.  Reads only; nothing is written.
    """
    try:
        from bot.async_bridge import adb
        from bot.database import db

        warmed = 0
        try:
            await adb(db.get_sudo_users())
            warmed += 1
        except Exception as e:
            logger.warning(f"sudo cache warm skipped: {e}")
        try:
            chat_ids = await adb(db.get_all_chat_ids())
        except Exception as e:
            logger.warning(f"warmup chat list failed: {e}")
            chat_ids = []
        for chat_id in chat_ids:
            loaders = (
                lambda c=chat_id: db.get_shield_settings(c),
                lambda c=chat_id: db.get_blocklist(c),
                lambda c=chat_id: db.get_filters(c),
                lambda c=chat_id: db.get_welcome_settings(c),
                lambda c=chat_id: db.get_welcome_message(c),
                lambda c=chat_id: db.get_all_watch_words(c),
                # Bind gate — read on EVERY group message by
                # gate_message_handler.  Warmed through the async
                # read-through directly: the module's own entry point
                # (bind.database.get_settings) is a sync facade meant
                # for executor threads.
                lambda c=chat_id: db._cached_read(
                    "get_bind_settings",
                    (c,),
                    lambda: db._find_one("bind_settings", {"chat_id": c}),
                ),
            )
            for load in loaders:
                try:
                    await adb(load())
                    warmed += 1
                except Exception:
                    pass  # read-through fills it on first use
        logger.info(
            f"Warmed {warmed} read-cache entr{'y' if warmed == 1 else 'ies'} "
            f"for {len(chat_ids)} chat(s)"
        )
    except Exception as e:
        logger.warning(f"cache warmup skipped: {e}")


async def _flush() -> None:
    """post_shutdown parity — persist write-behind buffers on exit."""
    try:
        from bot.database import db
        await db.flush_buffers()
    except Exception as e:
        logger.warning(f"final buffer flush failed: {e}")
    try:
        from bot.modules.tagging import activity_tracker
        await asyncio.to_thread(activity_tracker.flush_now)
    except Exception as e:
        logger.warning(f"final activity flush failed: {e}")


async def _drop_update_backlog(bot: Bot) -> int:
    """Fetch-and-confirm every update Telegram queued while the bot was off.

    Telegram holds undelivered updates for up to 24 hours — every command
    sent during an outage sits in that queue — and only *confirms* an
    update once getUpdates is called with an offset above its update_id.
    Booting into the backlog answers a pile of old /commands in one burst
    and trips Telegram's flood limits.

    Walks the queue in batches of 100 with ``timeout=0`` (short poll —
    the real long-poll starts with ``start_polling`` right after) and
    stops on the empty fetch that confirms the lot.  Returns how many
    stale updates were dropped.
    """
    dropped = 0
    offset = None
    while True:
        try:
            batch = await bot.get_updates(offset=offset, limit=100, timeout=0)
        except TelegramRetryAfter as e:
            # Telegram caps how often getUpdates may be called; draining a
            # huge backlog can hit that.  Wait out the penalty and retry
            # the same offset — nothing is confirmed until it succeeds.
            await asyncio.sleep(e.retry_after + 0.5)
            continue
        if not batch:
            return dropped
        dropped += len(batch)
        offset = batch[-1].update_id + 1


def _install_signal_handlers(loop, task, on_stop=None) -> None:  # noqa: ANN001
    """Make Railway's SIGTERM reach the shutdown flush.

    Railway Restart/Redeploy stops the container with SIGTERM. The
    default action kills the process instantly — no ``finally``, no
    ``atexit`` — so the last ~5s of buffered message counters were lost
    on every restart. Cancelling the main task instead lets
    ``amain()``'s ``finally`` block flush before exit.

    ``on_stop`` (if given) runs first so the cancellation can be
    recognised as intentional (vs. an unrelated cancel).
    """
    def _stop() -> None:
        if on_stop is not None:
            on_stop()
        if not task.done():
            task.cancel()

    try:
        loop.add_signal_handler(signal.SIGTERM, _stop)
    except (NotImplementedError, RuntimeError, ValueError):
        # Windows event loop policy or non-main thread (tests): fall
        # back to a plain handler where the platform allows it.
        try:
            signal.signal(signal.SIGTERM, lambda *_: _stop())
        except (ValueError, OSError, RuntimeError):
            pass


def _json_hooks() -> dict:
    """orjson (Rust) plugged into aiogram's session json hooks.

    BaseSession exposes ``json_loads``/``json_dumps`` single-argument
    callables used for every incoming update parse and outgoing request
    build; orjson is roughly 2-5x faster than stdlib json there. Falls
    back cleanly to stdlib json when orjson is not installed. orjson
    returns bytes, so dumps is wrapped to honor the ``-> str`` contract.
    """
    try:
        import orjson
    except ImportError:
        return {}

    def _dumps(value):  # noqa: ANN001 — orjson returns bytes, session wants str
        return orjson.dumps(value).decode("utf-8")

    return {"json_loads": orjson.loads, "json_dumps": _dumps}


def _install_uvloop() -> bool:
    """Use libuv's event loop (uvloop) where wheels exist.

    Railway runs Linux and gets the faster loop for polling and
    handler scheduling; Windows dev machines have no uvloop wheels and
    keep the default asyncio loop. Returns which loop is active so
    startup can log it.
    """
    if sys.platform == "win32":
        return False
    try:
        import uvloop
    except ImportError:
        return False
    uvloop.install()
    return True


def main() -> None:
    logger.info(f"Starting {BOT_NAME} (Pure Bot API Mode)...")
    loop_kind = "uvloop" if _install_uvloop() else "asyncio (default)"

    hooks = _json_hooks()
    json_kind = "orjson" if hooks else "json (stdlib)"

    # Regular Bot API HTTP timeouts (sendMessage, etc.).
    #
    # `timeout` is the per-request budget for everything EXCEPT getUpdates:
    # aiogram's start_polling overrides it for getUpdates with
    # `int(session.timeout + polling_timeout)` (dispatcher.py), i.e.
    # 30 + 30 = 60s, so a long poll never trips its own client timeout.
    # Leave both alone — raising `timeout` also slows down failure
    # detection on real API calls.
    #
    # `limit` is the aiohttp connector cap (aiogram default 100). It was
    # pinned at 20, which serialized concurrent API calls behind the one
    # connection getUpdates holds open for the whole 30s long poll.
    bot = Bot(
        token=settings.bot_token,
        session=AiohttpSession(timeout=30.0, limit=100, **hooks),
        default=DefaultBotProperties(parse_mode=None),
    )
    dp = Dispatcher()
    dp.errors.register(on_error)

    count = load_modules()
    pipeline.install(dp)
    logger.info(
        f"Loaded {count} module(s) — {BOT_NAME} is ready "
        f"({len(pipeline.snapshot())} handlers — bump this number to "
        "confirm a new deploy is actually live)"
    )
    logger.info(f"Event loop: {loop_kind} · JSON: {json_kind}")

    # Which database is this process actually using? (mongodb (pi_bot)
    # in production, mongomock only under tests — print it so a missing
    # MONGO_URI can never hide again.)
    from bot.database import db

    logger.info(f"Database backend: {db.backend}")
    if db.backend.startswith("mongomock"):
        # Tests never call main(); reaching here on mongomock means test
        # detection misfired in production — refuse instead of running on
        # RAM and wiping every count on restart (this actually happened:
        # aiogram's unittest.mock import fooled the old sys.modules
        # check, so MONGO_URI was ignored on every Railway deploy).
        raise SystemExit(
            "Refusing to start: in-memory test database active in "
            "production. Set MONGO_URI and check _is_test_process() "
            "detection in bot/database.py."
        )

    async def amain() -> None:
        sigterm = {"hit": False}
        try:
            # Handlers are mostly async + the DB layer is cached/buffered;
            # one sized pool keeps slow PIL rank renders from queueing
            # message-count jobs behind them.
            loop = asyncio.get_running_loop()
            loop.set_default_executor(
                ThreadPoolExecutor(max_workers=32, thread_name_prefix="pi-worker")
            )
            _install_signal_handlers(
                loop,
                asyncio.current_task(),
                on_stop=lambda: sigterm.__setitem__("hit", True),
            )

            # Bind the async driver to THIS loop and build the indexes
            # before any handler can run.  Startup is deliberately not
            # boxed (see async_bridge._Facade._UNBOXED): boxing it would
            # hand it to the private executor loop and the driver would
            # bind there for the life of the process.
            await db.startup()

            # Everything Telegram queued while the bot was offline (it
            # keeps undelivered updates for up to 24h) is dropped BEFORE
            # polling: booting into that backlog replies to a pile of old
            # /commands at once and trips Telegram's flood limits.  The
            # documented deleteWebhook(drop_pending_updates=True) alone
            # has not reliably cleared a polling backlog, so also
            # fetch-and-confirm whatever is left — see _drop_update_backlog.
            try:
                await bot.delete_webhook(drop_pending_updates=True)
                dropped = await _drop_update_backlog(bot)
                if dropped:
                    logger.info(
                        f"Dropped {dropped} update(s) queued while the "
                        "bot was offline"
                    )
            except Exception as e:
                logger.warning(f"offline update backlog drop failed: {e}")

            me = await bot.get_me()
            pipeline.BOT_DATA["username"] = me.username
            pipeline.BOT_DATA["name"] = me.full_name
            logger.info(f"Bot API connected as @{me.username} — {me.full_name}")

            # Group privacy mode: when ON, Telegram does not deliver plain
            # group messages to the bot at all.
            privacy_on = not getattr(me, "can_read_all_group_messages", True)
            if privacy_on:
                logger.warning(
                    "Group privacy is ON — plain group messages are NOT delivered to the "
                    "bot (chat rankings, XP and anti-flood won't see them). "
                    "Fix: BotFather → /setprivacy → Disable."
                )

            # Fire-and-forget: start listening for updates immediately.
            asyncio.create_task(_startup_log(bot, me, privacy_on))
            # Fill hot read caches in the background so the first group
            # message after boot doesn't pay the serial cache-miss chain
            # (bind gate, blocklist, filters, watch words, shield).
            asyncio.create_task(_warm_read_caches())

            # Subscribe only to update types that actually have handlers.
            # `UpdateType` has 27 members; 22 of them (message_reaction,
            # message_reaction_count, edited_message, channel_post, poll,
            # business_*, inline_query, chat_boost, ...) were being
            # requested with no observer registered anywhere — pure
            # payload + JSON-parse cost on every getUpdates for updates
            # that were immediately discarded.
            #
            # Derived from the registration queue so this can't drift
            # when a module is added. chat_member is opt-in only: Telegram
            # never delivers it unless allowed_updates lists it, so an
            # unspecified setting would silently kill the tagging
            # membership observers.
            used_updates = sorted({e.event for e in pipeline.entries()})
            if not used_updates:
                used_updates = ["message"]

            await dp.start_polling(
                bot,
                allowed_updates=used_updates,
                polling_timeout=30,
            )
        except asyncio.CancelledError:
            if not sigterm["hit"]:
                raise
            # Railway Restart/Redeploy: our own SIGTERM cancellation —
            # fall through to finally so buffers flush, then exit 0.
            logger.info("SIGTERM received — flushing before exit")
        finally:
            await _flush()
            # Close the async client while its loop is still alive: the
            # client's background tasks (server monitor, RTT sampler)
            # would otherwise outlive asyncio.run and spam
            # "Task was destroyed but it is pending!" on every deploy.
            try:
                await db.shutdown()
            except Exception:
                pass
            try:
                await bot.session.close()
            except Exception:
                pass
            logger.info("Shutdown complete")

    asyncio.run(amain())


if __name__ == "__main__":
    main()
