"""Telegram transport — standard Bot API (PTB), optional local base_url."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from telegram import Message
from telegram.constants import ParseMode

from .config import ig_config
from .metrics import ig_log, metrics
from .models import MediaKind


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

    async def send_file(
        self,
        chat_id: int,
        path: Path,
        kind: MediaKind,
        caption: str = "",
        reply_to_message_id: Optional[int] = None,
        disable_notification: bool = False,
    ) -> Optional[Message]:
        """Upload *path* as the right Telegram media type. Returns sent Message."""
        size = path.stat().st_size if path.exists() else 0
        local_ok = bool(ig_config.use_local_bot_api and ig_config.local_bot_api_url)
        if size > ig_config.max_file_bytes and not local_ok:
            from .exceptions import IGTooLarge

            raise IGTooLarge(size)

        try:
            msg = await self._send_via_ptb(
                chat_id, path, kind, caption, reply_to_message_id, disable_notification
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
    ) -> Message:
        common = dict(
            chat_id=chat_id,
            caption=caption or None,
            parse_mode=ParseMode.HTML if caption else None,
            disable_notification=disable_notification,
        )
        if reply_to_message_id:
            common["reply_to_message_id"] = reply_to_message_id

        with path.open("rb") as fh:
            if kind == MediaKind.PHOTO:
                return await self.bot.send_photo(photo=fh, **common)
            if kind == MediaKind.VIDEO:
                return await self.bot.send_video(video=fh, supports_streaming=True, **common)
            if kind == MediaKind.AUDIO:
                return await self.bot.send_audio(audio=fh, **common)
            if kind == MediaKind.ANIMATION:
                return await self.bot.send_animation(animation=fh, **common)
            return await self.bot.send_document(document=fh, filename=path.name, **common)
