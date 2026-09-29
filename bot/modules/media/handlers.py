"""Media handlers — auto-detect links + manual/admin commands.

Commands (new name + legacy aliases):
  /dl <url|reply>        — download YouTube/TikTok/Instagram media (any user)
  /mediasettings         — toggle board (admin)          [alias /igsettings]
  /mediastats            — module metrics (admin)        [alias /igstats]
  /mediacache [clear]    — file_id cache (owner)         [alias /igcache]
  /mediabench <url>      — time a resolve (owner)        [alias /igbenchmark]

Auto: groups + DMs when auto mode is on for that chat (group 14).
Delivery: file_id cache → direct CDN URL → download + upload.
"""

from __future__ import annotations

import asyncio
import shutil
import time
from html import escape
from typing import Optional, Tuple

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.types import CallbackQuery, Message

from bot.async_bridge import adb
from bot.config import settings as bot_settings
from bot.database import db
from bot.emojis import E
from bot.reply import reply_text
from bot.responses import action_card, error_card

from .cache import (
    cache_stats,
    clear_cache,
    get_file_ids,
    get_resolved,
    invalidate_resolved,
    put_file_id,
    put_resolved,
)
from .config import ig_config
from .downloader import download_post, ensure_temp_root, job_workdir
from .exceptions import (
    IGDisabled,
    IGError,
    IGInvalidUrl,
    IGRateRejected,
    IGTooLarge,
)
from .keyboards import error_keyboard, open_on_ig, settings_keyboard
from .metrics import ig_log, metrics
from .models import MediaKind, PostType
from .platforms import (
    PLATFORM_INSTAGRAM,
    PLATFORM_TIKTOK,
    PLATFORM_YOUTUBE,
    media_key,
    pick_media_url,
    resolve_target,
)
from .ratelimit import limiter
from .resolver import ensure_yt_dlp, resolve_media
from .singleflight import GlobalSemaphore, run_exclusive
from .transport import TelegramTransport, _caption_html
from .url_utils import classify_post_type

# Global concurrent-job cap (spec §17).
_JOB_SEM = GlobalSemaphore(ig_config.max_active_jobs)

_QUALITY_ORDER = ["auto", "720", "1080", "1440", "2160", "best"]
_MAXMB_ORDER = [50, 100, 200, 500]
_CAPTIONS_ORDER = ["off", "short", "full"]


# ── Helpers ─────────────────────────────────────────────────────

def _is_owner(user_id: Optional[int]) -> bool:
    return bool(user_id and bot_settings.owner_id and user_id == bot_settings.owner_id)


async def _is_admin(message: Message, bot: Bot) -> bool:
    user = message.from_user
    chat = message.chat
    if not user or not chat:
        return False
    if _is_owner(user.id):
        return True
    try:
        member = await bot.get_chat_member(chat.id, user.id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False


async def _member_is_admin(bot, chat_id: int, user_id: int) -> bool:
    if _is_owner(user_id):
        return True
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False


def _media_defaults(chat_id: int) -> dict:
    """Per-chat settings with MEDIA_* env fallbacks (rows may predate fields)."""
    return {
        "chat_id": chat_id,
        "auto_download": 1 if ig_config.auto_download else 0,
        "max_items": ig_config.max_items,
        "yt_enabled": 1 if ig_config.youtube_enabled else 0,
        "tt_enabled": 1 if ig_config.tiktok_enabled else 0,
        "videos": 1,
        "shorts": 1,
        "quality": ig_config.quality,
        "captions": ig_config.captions,
        "max_mb": ig_config.max_video_mb,
        "delete_source": 0,
        "progress": 1,
    }


def _media_settings(chat_id: int) -> dict:
    row = db.ig_get_settings(chat_id)
    if not row:
        return _media_defaults(chat_id)
    base = _media_defaults(chat_id)
    base.update(row)
    return base


def _safe_str(st: dict, field: str, fallback: str) -> str:
    val = st.get(field)
    return str(val) if val else fallback


async def _safe_edit(msg, text: str, reply_markup=None) -> None:
    try:
        await msg.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    except Exception:
        pass


def _error_text(exc: Exception) -> str:
    if isinstance(exc, IGError):
        return error_card("Download Failed", escape(exc.user))
    return error_card("Download Failed", escape(str(exc)[:200]))


def _build_caption(post, mode: str, norm_url: str) -> Optional[str]:
    """Caption by per-chat mode: off (None) / short (credit) / full (title+credit)."""
    mode = (mode or ig_config.captions or "short").lower()
    if mode == "off":
        return None
    credit = _caption_html(
        post.title or post.caption or "", post.uploader, post.webpage_url or norm_url
    )
    if mode == "full":
        title = escape((post.title or post.caption or "").strip()[:180])
        if title:
            head = f"<b>{title}</b>\n"
            cap = head + credit
            return cap
    return credit


def _effective_max_bytes(st: dict) -> int:
    """Global cap, bounded by the per-chat File Size setting."""
    chat_mb = int(st.get("max_mb") or ig_config.max_video_mb)
    chat_cap = chat_mb * 1024 * 1024
    local_ok = bool(ig_config.use_local_bot_api and ig_config.local_bot_api_url)
    if local_ok:
        return chat_cap
    return min(ig_config.max_file_bytes, chat_cap)


def _gate_allows(st: dict, platform: str, url: str) -> bool:
    """Per-chat platform/content gates for the auto-detect path."""
    if platform == PLATFORM_YOUTUBE:
        if not int(st.get("yt_enabled", 1)):
            return False
        from .platforms import is_youtube_short

        if is_youtube_short(url):
            return bool(int(st.get("shorts", 1)))
        return bool(int(st.get("videos", 1)))
    if platform == PLATFORM_TIKTOK:
        return bool(int(st.get("tt_enabled", 1)))
    return True


async def _send_cached(
    bot: Bot,
    chat_id: int,
    cached: dict,
    cache_keys: list,
    caption: Optional[str],
    reply_to_message_id: Optional[int],
) -> bool:
    """Resend known file_ids. Returns True only if every row sent."""
    for i, key in enumerate(cache_keys):
        file_id, kind_s = cached[key]
        kind = MediaKind(kind_s)
        cap = caption if i == 0 else None
        rid = reply_to_message_id if i == 0 else None
        parse = ParseMode.HTML if cap else None
        if kind == MediaKind.PHOTO:
            await bot.send_photo(
                chat_id, photo=file_id, caption=cap, parse_mode=parse, reply_to_message_id=rid
            )
        elif kind == MediaKind.VIDEO:
            await bot.send_video(
                chat_id, video=file_id, caption=cap, parse_mode=parse,
                reply_to_message_id=rid, supports_streaming=True,
            )
        elif kind == MediaKind.AUDIO:
            await bot.send_audio(
                chat_id, audio=file_id, caption=cap, parse_mode=parse, reply_to_message_id=rid
            )
        elif kind == MediaKind.ANIMATION:
            await bot.send_animation(
                chat_id, animation=file_id, caption=cap, parse_mode=parse, reply_to_message_id=rid
            )
        else:
            await bot.send_document(
                chat_id, document=file_id, caption=cap, parse_mode=parse, reply_to_message_id=rid
            )
        await adb(db.ig_touch_file_id(key))
        metrics.bump("uploads")
    return True


def _extract_file_id(message) -> Optional[str]:
    if message is None:
        return None
    try:
        if getattr(message, "photo", None):
            return message.photo[-1].file_id
        if getattr(message, "video", None):
            return message.video.file_id
        if getattr(message, "audio", None):
            return message.audio.file_id
        if getattr(message, "animation", None):
            return message.animation.file_id
        if getattr(message, "document", None):
            return message.document.file_id
    except Exception:
        return None
    return None


async def process_url(
    bot: Bot,
    chat_id: int,
    url: str,
    *,
    reply_to_message_id: Optional[int] = None,
    requester_id: int = 0,
    settings: Optional[dict] = None,
) -> bool:
    """
    Resolve → (cache | direct URL | download) → upload for one media URL.
    Single-flight per platform:id key; waiters attach to the leader.
    Returns True if ≥1 media sent.
    """
    if not ig_config.enabled:
        raise IGDisabled()
    ensure_yt_dlp()
    ensure_temp_root()

    platform, norm = resolve_target(url)
    if platform == PLATFORM_INSTAGRAM and classify_post_type(norm) == PostType.PROFILE:
        raise IGInvalidUrl()

    key = media_key(platform, norm)
    if settings is None:
        # DB read happens on the event loop — keep it off-loop (scanner).
        settings = await asyncio.to_thread(_media_settings, chat_id)
    st = settings

    # Rate limits (spec §77): requests/min + concurrent jobs per user.
    if requester_id:
        if not limiter.check_request(requester_id):
            metrics.bump("rate_rejects")
            raise IGRateRejected()
        if not limiter.try_acquire_job(requester_id):
            metrics.bump("rate_rejects")
            raise IGRateRejected()

    try:
        async with _JOB_SEM:
            return await _run_job(
                bot, chat_id, platform, norm, key, st,
                reply_to_message_id=reply_to_message_id,
                requester_id=requester_id,
            )
    except IGError:
        raise
    except Exception as e:
        ig_log(f"job error: {e}")
        metrics.bump("resolved_fail", error=str(e)[:200])
        try:
            await adb(db.ig_log_download(
                chat_id, requester_id, "error", "unknown", 0, 0, str(e)[:200]
            ))
        except Exception:
            pass
        raise IGError("Something went wrong processing that link.") from e
    finally:
        if requester_id:
            limiter.release_job(requester_id)


async def _run_job(
    bot: Bot,
    chat_id: int,
    platform: str,
    norm: str,
    key: str,
    st: dict,
    *,
    reply_to_message_id: Optional[int],
    requester_id: int,
) -> bool:
    async def _job() -> bool:
        t0 = time.perf_counter()
        post = get_resolved(key)
        if post is None:
            post = await resolve_media(platform, norm)
            put_resolved(key, post)
        else:
            ig_log(f"resolve-cache hit {post.media_id}")
        transport = TelegramTransport(bot)
        caption = _build_caption(post, _safe_str(st, "captions", ig_config.captions), norm)
        max_bytes = _effective_max_bytes(st)

        cache_keys = post.file_id_keys()
        cached = await get_file_ids(cache_keys)

        # Fast path: every row already has a stored Telegram file_id.
        if post.assets and len(cached) == len(cache_keys):
            try:
                await _send_cached(bot, chat_id, cached, cache_keys, caption, reply_to_message_id)
                elapsed = int((time.perf_counter() - t0) * 1000)
                await adb(db.ig_log_download(
                    chat_id, requester_id, "cache", post.post_type.value,
                    len(cache_keys), elapsed, None,
                ))
                ig_log(f"cache-hit send {post.media_id} in {elapsed}ms")
                return True
            except Exception as e:
                ig_log(f"file_id path failed, re-download: {e}")

        # Mode A: let Telegram fetch the CDN URL directly (no local file).
        if (
            ig_config.direct_url
            and len(post.assets) == 1
            and not post.needs_merge
        ):
            a = post.assets[0]
            if a.kind in (MediaKind.VIDEO, MediaKind.PHOTO, MediaKind.ANIMATION):
                size_ok = not a.filesize or a.filesize <= max_bytes
                if size_ok:
                    try:
                        sent = await transport.send_remote(
                            chat_id, a.url, a.kind,
                            caption=caption or "",
                            reply_to_message_id=reply_to_message_id,
                            duration=int(a.duration or 0) or None,
                            width=a.width, height=a.height,
                        )
                        file_id = _extract_file_id(sent)
                        if file_id:
                            await put_file_id(
                                cache_keys[0], file_id, a.kind.value, post.canonical_url
                            )
                        elapsed = int((time.perf_counter() - t0) * 1000)
                        await adb(db.ig_log_download(
                            chat_id, requester_id, "ok", post.post_type.value,
                            1, elapsed, None,
                        ))
                        ig_log(f"direct-URL send {post.media_id} in {elapsed}ms")
                        return True
                    except Exception as e:
                        metrics.bump("direct_fallback")
                        ig_log(f"direct URL failed, downloading: {e}")

        job_dir = job_workdir(post.media_id)
        try:
            try:
                files = await download_post(post, job_dir, max_bytes=max_bytes)
            except IGTooLarge:
                raise  # size won't shrink on a re-resolve — skip the retry
            except Exception:
                # CDN URLs may have expired (resolve cache) — refresh once.
                invalidate_resolved(key)
                post = await resolve_media(platform, norm)
                put_resolved(key, post)
                caption = _build_caption(post, _safe_str(st, "captions", ig_config.captions), norm)
                cache_keys = post.file_id_keys()
                files = await download_post(post, job_dir, max_bytes=max_bytes)
            sent_any = False
            first = True
            for f in files:
                cap = caption if first else None
                rid = reply_to_message_id if first else None
                try:
                    sent = await transport.send_file(
                        chat_id, f.path, f.kind, caption=cap or "",
                        reply_to_message_id=rid,
                        duration=int(f.asset.duration or 0) or None,
                        width=f.asset.width, height=f.asset.height,
                    )
                except IGTooLarge:
                    ig_log(f"skip too-large {f.path.name}")
                    continue
                sent_any = True
                file_id = _extract_file_id(sent)
                if file_id:
                    await put_file_id(f.cache_key, file_id, f.kind.value, post.canonical_url)
                first = False

            if sent_any:
                elapsed = int((time.perf_counter() - t0) * 1000)
                await adb(db.ig_log_download(
                    chat_id, requester_id, "ok", post.post_type.value,
                    len(files), elapsed, None,
                ))
                ig_log(f"sent {len(files)} for {post.media_id} in {elapsed}ms")
            return sent_any
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)

    return await run_exclusive(key, _job, timeout=ig_config.job_timeout)


# ── Delayed status message (spec §48) ───────────────────────────

async def _delayed_status(message, delay: float, enabled: bool, *, quote: bool):
    """Send 'Fetching media…' only when the job exceeds *delay* seconds."""
    if not enabled or delay <= 0:
        return None
    try:
        await asyncio.sleep(delay)
        return await reply_text(
            message,
            f"{E.SPARKLE} Fetching media…",
            parse_mode=ParseMode.HTML,
            quote=quote,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        return None


async def _finish_status(status_task, *, success: bool, error: Optional[Exception] = None,
                         message=None, url: Optional[str] = None) -> None:
    """Cancel/collect the delayed status; surface errors when it was sent."""
    status = None
    if status_task.done() and not status_task.cancelled():
        try:
            status = status_task.result()
        except Exception:
            status = None
    else:
        status_task.cancel()
        try:
            await status_task
        except (asyncio.CancelledError, Exception):
            pass

    if success:
        if status is not None:
            try:
                await status.delete()
            except Exception:
                pass
        return

    # Failure: edit the status if it exists, else send a fresh error card.
    if status is not None:
        await _safe_edit(status, _error_text(error or Exception("failed")),
                         reply_markup=error_keyboard(url))
    elif message is not None:
        try:
            await reply_text(
                message,
                _error_text(error or Exception("failed")),
                parse_mode=ParseMode.HTML,
                reply_markup=error_keyboard(url),
            )
        except Exception:
            pass


# ── Auto-detect handler (group 14) ──────────────────────────────

async def auto_download_handler(message: Message, bot: Bot) -> None:
    """Catch non-command text containing YT/TikTok/IG URLs when auto is on."""
    if not ig_config.enabled:
        return
    chat = message.chat
    user = message.from_user
    if not message or not chat:
        return

    text = message.text or message.caption or ""
    found = pick_media_url(text)
    if not found:
        return
    platform, url = found

    st = await asyncio.to_thread(_media_settings, chat.id)
    if not st.get("auto_download"):
        return
    if not _gate_allows(st, platform, url):
        return

    metrics.bump("auto_triggers")
    status_task = asyncio.create_task(
        _delayed_status(
            message, ig_config.status_delay,
            bool(int(st.get("progress", 1))), quote=True,
        )
    )
    try:
        await process_url(
            bot,
            chat.id,
            url,
            requester_id=user.id if user else 0,
            settings=st,
        )
        await _finish_status(status_task, success=True)
        if int(st.get("delete_source", 0)):
            try:
                await message.delete()
            except Exception:
                pass
    except Exception as e:
        await _finish_status(
            status_task, success=False, error=e, message=message, url=url
        )


# ── /dl (alias /igdl /ytdl /ttdl) ──────────────────────────────

def _url_from_args_or_reply(message: Message, args: list) -> Optional[Tuple[str, str]]:
    """First supported URL from /dl args, else from the replied message."""
    chunks = []
    if args:
        chunks.append(" ".join(args))
    if message.reply_to_message:
        rt = message.reply_to_message
        chunks.append(rt.text or rt.caption or "")
    for text in chunks:
        found = pick_media_url(text)
        if found:
            return found
    return None


async def dl_command(message: Message, bot: Bot, args: list) -> None:
    if not message or not message.chat:
        return
    if not ig_config.enabled:
        await reply_text(
            message,
            error_card("Download Failed", escape("Module is disabled.")),
            parse_mode=ParseMode.HTML,
        )
        return

    found = _url_from_args_or_reply(message, args)
    if not found:
        await reply_text(
            message,
            action_card(
                "Media Download",
                [
                    (E.INFO, "Detail", "Send /dl &lt;url&gt; or reply to a media link with /dl"),
                    (E.WEB, "Supported", "YouTube · TikTok · Instagram"),
                    (E.GUITAR, "Examples", "/dl youtu.be/…  ·  /dl tiktok.com/@…"),
                ],
                icon=E.SPARKLE,
            ),
            parse_mode=ParseMode.HTML,
        )
        return

    url = found[1]
    status_task = asyncio.create_task(
        _delayed_status(message, ig_config.status_delay, True, quote=True)
    )
    try:
        await process_url(
            bot,
            message.chat.id,
            url,
            reply_to_message_id=None,
            requester_id=message.from_user.id if message.from_user else 0,
        )
        await _finish_status(status_task, success=True)
    except Exception as e:
        await _finish_status(
            status_task, success=False, error=e, message=message, url=url
        )


# ── /mediasettings (alias /igsettings) ─────────────────────────

def _settings_card(st: dict) -> str:
    def onoff(field: str) -> str:
        return "On" if int(st.get(field, 1)) else "Off"

    quality = _safe_str(st, "quality", ig_config.quality).upper()
    captions = _safe_str(st, "captions", ig_config.captions).capitalize()
    max_mb = int(st.get("max_mb") or ig_config.max_video_mb)
    return action_card(
        "Media Download Settings",
        [
            (E.SETTINGS, "Auto download", onoff("auto_download")),
            (E.WEB, "YouTube", onoff("yt_enabled")),
            (E.GUITAR, "TikTok", onoff("tt_enabled")),
            (E.CIRCLE, "Videos", onoff("videos")),
            (E.FIRE, "Shorts", onoff("shorts")),
            (E.STAR, "Max quality", quality),
            (E.INFO, "Captions", captions),
            (E.FOLDER, "Max file size", f"{max_mb} MB"),
            (E.CROSS, "Delete source link", onoff("delete_source")),
            (E.TIME, "Progress", onoff("progress")),
            (E.NUMBER_1, "Usage", "/mediasettings auto on|off • /mediasettings max N"),
        ],
        icon=E.SETTINGS,
    )


async def mediasettings_command(message: Message, bot: Bot, args: list) -> None:
    if not message or not message.chat:
        return
    if not await _is_admin(message, bot):
        await reply_text(
            message,
            error_card("Not allowed", escape("Admins only.")),
            parse_mode=ParseMode.HTML,
        )
        return

    chat_id = message.chat.id
    args = [a.lower() for a in (args or [])]

    if args:
        if args[0] in {"auto", "autodl"} and len(args) >= 2:
            on = args[1] in {"on", "1", "true", "yes"}
            await adb(db.ig_set_settings(chat_id, auto_download=1 if on else 0))
        elif args[0] == "max" and len(args) >= 2:
            try:
                n = max(1, min(int(args[1]), 50))
            except ValueError:
                n = ig_config.max_items
            await adb(db.ig_set_settings(chat_id, max_items=n))

    st = await asyncio.to_thread(_media_settings, chat_id)
    await reply_text(
        message,
        _settings_card(st),
        parse_mode=ParseMode.HTML,
        reply_markup=settings_keyboard(st),
    )


# ── /mediastats (alias /igstats) ───────────────────────────────

async def mediastats_command(message: Message, bot: Bot) -> None:
    if not message:
        return
    if not await _is_admin(message, bot):
        await reply_text(
            message,
            error_card("Not allowed", escape("Admins only.")),
            parse_mode=ParseMode.HTML,
        )
        return

    snap = metrics.snapshot()
    cst = await cache_stats()
    total_c = int(snap["cache_hits"]) + int(snap["cache_misses"])
    hit_rate = round(100 * int(snap["cache_hits"]) / total_c) if total_c else 0

    text = action_card(
        "Media Stats",
        [
            (E.INFO, "Requests", f"{snap['requests']} total"),
            (E.WEB, "By platform",
             f"YT {snap['youtube_n']} · TT {snap['tiktok_n']} · IG {snap['instagram_n']}"),
            (E.CHECK, "Resolved", f"{snap['resolved_ok']} ok / {snap['resolved_fail']} fail"),
            (E.FIRE, "Downloads", f"{snap['downloads']} (avg {snap['avg_download_ms']} ms)"),
            (E.SPARKLE, "Cache", f"{hit_rate}% hit • L1 {cst['l1']} • DB {cst['rows']}"),
            (E.GLOBE, "Uploads", f"{snap['uploads']} ok / {snap['upload_fail']} fail"),
            (E.ARROW, "Direct URL", f"{snap['direct_ok']} ok / {snap['direct_fallback']} fallback"),
            (E.CIRCLE, "Merges", str(snap["merges"])),
            (E.STAR, "Stream mirrors", str(snap["stream_hits"])),
            (E.NUMBER_1, "Auto triggers", str(snap["auto_triggers"])),
            (E.TIME, "Avg resolve", f"{snap['avg_resolve_ms']} ms"),
            (E.WARN, "Rate/busy rejects", f"{snap['rate_rejects']} / {snap['busy_rejects']}"),
            (E.INFO, "Last error", snap["last_error"] or "—"),
        ],
        icon=E.CHART,
    )
    await reply_text(message, text, parse_mode=ParseMode.HTML)


# ── /mediacache (alias /igcache) ───────────────────────────────

async def mediacache_command(message: Message, args: list) -> None:
    if not message or not message.from_user:
        return
    if not _is_owner(message.from_user.id):
        await reply_text(
            message,
            error_card("Not allowed", escape("Owner only.")),
            parse_mode=ParseMode.HTML,
        )
        return

    args = [a.lower() for a in (args or [])]
    if args and args[0] == "clear":
        n = await clear_cache()
        text = action_card(
            "Media Cache",
            [(E.CHECK, "Cleared", f"{n} DB row(s); L1 flushed")],
            icon=E.CHECK,
        )
    else:
        cst = await cache_stats()
        text = action_card(
            "Media Cache",
            [
                (E.FOLDER, "L1 memory", str(cst["l1"])),
                (E.FOLDER, "DB rows", str(cst["rows"])),
                (E.INFO, "DB hits", str(cst["hits"])),
                (E.INFO, "Usage", "/mediacache clear"),
            ],
            icon=E.FOLDER,
        )
    await reply_text(message, text, parse_mode=ParseMode.HTML)


# ── /mediabench (alias /igbenchmark) ───────────────────────────

async def mediabench_command(message: Message, args: list) -> None:
    if not message or not message.from_user:
        return
    if not _is_owner(message.from_user.id):
        await reply_text(
            message,
            error_card("Not allowed", escape("Owner only.")),
            parse_mode=ParseMode.HTML,
        )
        return

    found = None
    if args:
        found = pick_media_url(" ".join(args))
    if not found:
        await reply_text(
            message,
            action_card(
                "Media Benchmark",
                [(E.INFO, "Detail", "Usage: /mediabench &lt;url&gt;")],
                icon=E.TIME,
            ),
            parse_mode=ParseMode.HTML,
        )
        return

    platform, url = found
    status = await reply_text(
        message, f"{E.TIME} Benchmarking…", parse_mode=ParseMode.HTML
    )
    t0 = time.perf_counter()
    try:
        ensure_yt_dlp()
        plat, norm = resolve_target(url)
        post = await resolve_media(plat, norm)
        elapsed = int((time.perf_counter() - t0) * 1000)
        height = 0
        for a in post.assets:
            height = max(height, a.height or 0)
        text = action_card(
            "Media Benchmark",
            [
                (E.CHECK, "Status", "OK"),
                (E.TIME, "Resolve", f"{elapsed} ms"),
                (E.WEB, "Platform", plat),
                (E.INFO, "Type", post.post_type.value),
                (E.FIRE, "Assets", f"{len(post.assets)} (merge={post.needs_merge})"),
                (E.SETTINGS, "Resolver", post.resolver),
                (E.CHART, "Quality", f"{height}p" if height else "—"),
            ],
            icon=E.TIME,
        )
        await _safe_edit(status, text, reply_markup=open_on_ig(post.webpage_url or norm))
    except Exception as e:
        await _safe_edit(status, _error_text(e), reply_markup=error_keyboard(url))


# ── Callbacks (settings toggles / close) ────────────────────────

async def ig_callback(callback_query: CallbackQuery, bot: Bot) -> None:
    query = callback_query
    if not query or not query.data or not query.data.startswith("ig:"):
        return
    await query.answer()
    data = query.data
    if data == "ig:close":
        try:
            await query.message.delete()
        except Exception:
            pass
        return

    if not query.message or not query.from_user:
        return
    # aiogram: Message has no .chat_id (it's .chat.id); an
    # InaccessibleMessage has no .chat at all — bail out instead of crash.
    chat = getattr(query.message, "chat", None)
    chat_id = getattr(chat, "id", None)
    if chat_id is None:
        return
    if not await _member_is_admin(bot, chat_id, query.from_user.id):
        await query.answer("Admins only.", show_alert=True)
        return

    if data in {"ig:set:auto", "ig:set:max"}:
        st = await asyncio.to_thread(_media_settings, chat_id)
        if data == "ig:set:auto":
            new = 0 if st.get("auto_download") else 1
            await adb(db.ig_set_settings(chat_id, auto_download=new))
        else:
            cur = int(st.get("max_items") or ig_config.max_items)
            order = [1, 5, 10, 20]
            nxt = order[(order.index(cur) + 1) % len(order)] if cur in order else 5
            await adb(db.ig_set_settings(chat_id, max_items=nxt))
    elif data.startswith("ig:set:"):
        await _toggle_setting(chat_id, data.split(":", 2)[2])
    else:
        return

    st = await asyncio.to_thread(_media_settings, chat_id)
    try:
        await query.message.edit_text(
            _settings_card(st),
            parse_mode=ParseMode.HTML,
            reply_markup=settings_keyboard(st),
        )
    except Exception:
        pass


async def _toggle_setting(chat_id: int, field: str) -> None:
    """Flip/cycle one /mediasettings field and persist it."""
    # Callbacks use short names — map them to the row columns, otherwise
    # the YouTube / TikTok / Delete buttons silently do nothing.
    field = {
        "yt": "yt_enabled",
        "tt": "tt_enabled",
        "delete": "delete_source",
    }.get(field, field)
    st = await asyncio.to_thread(_media_settings, chat_id)
    flip = {"yt_enabled", "tt_enabled", "videos", "shorts", "delete_source", "progress"}
    if field in flip:
        await adb(db.ig_set_settings(chat_id, **{field: 0 if int(st.get(field, 1)) else 1}))
    elif field == "quality":
        cur = _safe_str(st, "quality", ig_config.quality).lower()
        nxt = _QUALITY_ORDER[(_QUALITY_ORDER.index(cur) + 1) % len(_QUALITY_ORDER)] \
            if cur in _QUALITY_ORDER else "auto"
        await adb(db.ig_set_settings(chat_id, quality=nxt))
    elif field == "maxmb":
        cur = int(st.get("max_mb") or ig_config.max_video_mb)
        nxt = _MAXMB_ORDER[(_MAXMB_ORDER.index(cur) + 1) % len(_MAXMB_ORDER)] \
            if cur in _MAXMB_ORDER else 50
        await adb(db.ig_set_settings(chat_id, max_mb=nxt))
    elif field == "captions":
        cur = _safe_str(st, "captions", ig_config.captions).lower()
        nxt = _CAPTIONS_ORDER[(_CAPTIONS_ORDER.index(cur) + 1) % len(_CAPTIONS_ORDER)] \
            if cur in _CAPTIONS_ORDER else "short"
        await adb(db.ig_set_settings(chat_id, captions=nxt))


# ── Legacy aliases (old command names keep working) ─────────────

async def igdl_command(message: Message, bot: Bot, args: list) -> None:
    await dl_command(message, bot, args)


async def igsettings_command(message: Message, bot: Bot, args: list) -> None:
    await mediasettings_command(message, bot, args)


async def igstats_command(message: Message, bot: Bot) -> None:
    await mediastats_command(message, bot)


async def igcache_command(message: Message, args: list) -> None:
    await mediacache_command(message, args)


async def igbenchmark_command(message: Message, args: list) -> None:
    await mediabench_command(message, args)
