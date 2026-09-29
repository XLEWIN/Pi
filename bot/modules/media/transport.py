"""Telegram transport — standard Bot API (PTB), optional local base_url.

Delivery priority (spec §36):
  1. cached file_id          — instant, no network beyond Telegram
  2. direct HTTP URL send    — Telegram fetches the CDN itself (fastest
                               for first-time downloads; no local file)
  3. local Bot API upload    — for files over the standard 50 MB cap
  4. multipart upload        — standard server
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from aiogram.enums import ParseMode
from aiogram.types import FSInputFile, Message

from .config import ig_config
from .metrics import ig_log, metrics
from .models import MediaKind
from .singleflight import GlobalSemaphore

# Global upload cap (spec §17): never pile more than N sends into Telegram.
_UPLOAD_SEM = GlobalSemaphore(ig_config.max_active_uploads)


def _caption_html(post_title: str, uploader: str, webpage: str, extra: str = "") -> str:
    """Fixed caption line for media sends (HTML)."""
    from bot.config import settings
    from bot.emojis import E

    handle = settings.bot_username or "PiModulerBot"
    if not handle.startswith("@"):
        handle = f"@{handle}"
    return (
        f"{E.SPARKLE} By "
        f'<a href="https://t.me/{handle.lstrip("@")}">{handle}</a>'
    )


class TelegramTransport:
    """Sends files to a chat; Application may point at LOCAL_BOT_API_URL."""

    def __init__(self, bot) -> None:
        self.bot = bot

    async def send_remote(
        self,
        chat_id: int,
        url: str,
        kind: MediaKind,
        *,
        caption: str = "",
        reply_to_message_id: Optional[int] = None,
        duration: Optional[int] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
    ) -> Optional[Message]:
        """Mode A: hand Telegram the CDN URL and let it fetch the file.

        Returns the sent Message (its file_id is cacheable). Raises on
        failure — callers fall back to download + upload.
        """
        if not ig_config.direct_url or not url.lower().startswith("https://"):
            raise ValueError("direct URL send disabled")
        common = dict(
            chat_id=chat_id,
            caption=caption or None,
            parse_mode=ParseMode.HTML if caption else None,
        )
        if reply_to_message_id:
            common["reply_to_message_id"] = reply_to_message_id

        async with _UPLOAD_SEM:
            if kind == MediaKind.PHOTO:
                msg = await self.bot.send_photo(photo=url, **common)
            elif kind == MediaKind.VIDEO:
                extra = {}
                if duration:
                    extra["duration"] = int(duration)
                if width:
                    extra["width"] = int(width)
                if height:
                    extra["height"] = int(height)
                msg = await self.bot.send_video(
                    video=url, supports_streaming=True, **extra, **common
                )
            elif kind == MediaKind.ANIMATION:
                msg = await self.bot.send_animation(animation=url, **common)
            elif kind == MediaKind.AUDIO:
                msg = await self.bot.send_audio(audio=url, **common)
            else:
                msg = await self.bot.send_document(document=url, **common)
        metrics.bump("uploads")
        metrics.bump("direct_ok")
        return msg

    async def send_file(
        self,
        chat_id: int,
        path: Path,
        kind: MediaKind,
        caption: str = "",
        reply_to_message_id: Optional[int] = None,
        disable_notification: bool = False,
        duration: Optional[int] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
    ) -> Optional[Message]:
        """Upload *path* as the right Telegram media type. Returns sent Message."""
        size = path.stat().st_size if path.exists() else 0
        local_ok = bool(ig_config.use_local_bot_api and ig_config.local_bot_api_url)
        if size > ig_config.max_file_bytes and not local_ok:
            from .exceptions import IGTooLarge

            raise IGTooLarge(size)

        async with _UPLOAD_SEM:
            try:
                msg = await self._send_via_ptb(
                    chat_id, path, kind, caption, reply_to_message_id,
                    disable_notification, duration, width, height,
                )
                metrics.bump("uploads")
                return msg
            except Exception as e:
                metrics.bump("upload_fail", error=str(e)[:200])
                ig_log(f"upload fail path={path.name}: {e}")
                raise

    async def _send_via_ptb(
        self,
        chat_id: int,
        path: Path,
        kind: MediaKind,
        caption: str,
        reply_to_message_id: Optional[int],
        disable_notification: bool,
        duration: Optional[int] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
    ) -> Message:
        common = dict(
            chat_id=chat_id,
            caption=caption or None,
            parse_mode=ParseMode.HTML if caption else None,
            disable_notification=disable_notification,
        )
        if reply_to_message_id:
            common["reply_to_message_id"] = reply_to_message_id

        upload = FSInputFile(path, filename=path.name)
        if kind == MediaKind.PHOTO:
            return await self.bot.send_photo(photo=upload, **common)
        if kind == MediaKind.VIDEO:
            extra = {}
            if duration:
                extra["duration"] = int(duration)
            if width:
                extra["width"] = int(width)
            if height:
                extra["height"] = int(height)
            return await self.bot.send_video(
                video=upload, supports_streaming=True, **extra, **common
            )
        if kind == MediaKind.AUDIO:
            return await self.bot.send_audio(audio=upload, **common)
        if kind == MediaKind.ANIMATION:
            return await self.bot.send_animation(animation=upload, **common)
        return await self.bot.send_document(document=upload, **common)
