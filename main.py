"""Phi pi - Pure Bot API entry point.

Runs aiogram (Bot API) with colored inline buttons.
No Telethon dependency.
"""

import asyncio
import signal
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import UpdateType
from aiogram.client.default import DefaultBotProperties

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


async def _flush() -> None:
    """post_shutdown parity — persist write-behind buffers on exit."""
    try:
        from bot.database import db
        await asyncio.to_thread(db.flush_buffers)
    except Exception as e:
        logger.warning(f"final buffer flush failed: {e}")
    try:
        from bot.modules.tagging import activity_tracker
        await asyncio.to_thread(activity_tracker.flush_now)
    except Exception as e:
        logger.warning(f"final activity flush failed: {e}")


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


def main() -> None:
    logger.info(f"Starting {BOT_NAME} (Pure Bot API Mode)...")

    # Regular Bot API HTTP timeouts (sendMessage, etc.).
    bot = Bot(
        token=settings.bot_token,
        session=AiohttpSession(timeout=30.0, limit=20),
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

    # Which database is this process actually using? (mongodb (pi_bot)
    # in production, mongomock only under tests — print it so a missing
    # MONGO_URI can never hide again.)
    from bot.database import db

    logger.info(f"Database backend: {db.backend}")

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

            # PTB run_polling(drop_pending_updates=True) parity: clear the
            # backlog and detach any webhook before long-polling.
            await bot.delete_webhook(drop_pending_updates=True)

            await dp.start_polling(
                bot,
                allowed_updates=[t.value for t in UpdateType],
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
            try:
                await bot.session.close()
            except Exception:
                pass
            logger.info("Shutdown complete")

    asyncio.run(amain())


if __name__ == "__main__":
    main()
