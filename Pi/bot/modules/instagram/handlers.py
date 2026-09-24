"""Instagram handlers — auto-detect links + manual/admin commands.

Commands:
  /igdl <url|reply>   — download a post/reel (any user)
  /igsettings         — toggle auto-download + max items (admin)
  /igstats            — module metrics (admin)
  /igcache [clear]    — file_id cache stats / clear (owner)
  /igbenchmark <url>  — time a resolve (owner)

Auto: groups + DMs when auto mode is on for that chat (group 14).
"""

from __future__ import annotations

import asyncio
import shutil
import time
from html import escape
from typing import Optional

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from bot.config import settings as bot_settings
from bot.database import db
from bot.emojis import E
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
from .exceptions import IGDisabled, IGError, IGInvalidUrl, IGTooLarge
from .keyboards import error_keyboard, open_on_ig, settings_keyboard
from .metrics import ig_log, metrics
from .models import MediaKind, PostType
from .resolver import ensure_yt_dlp, resolver_chain
from .singleflight import run_exclusive
from .transport import TelegramTransport, _caption_html
from .url_utils import classify_post_type, find_instagram_urls, first_post_url, normalize_url


# ── Helpers ─────────────────────────────────────────────────────

def _is_owner(user_id: Optional[int]) -> bool:
    return bool(user_id and bot_settings.owner_id and user_id == bot_settings.owner_id)


async def _is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return False
    if _is_owner(user.id):
        return True
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
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


def _ig_settings(chat_id: int) -> dict:
    row = db.ig_get_settings(chat_id)
    return row or {
        "chat_id": chat_id,
        "auto_download": 1 if ig_config.auto_download else 0,
        "max_items": ig_config.max_items,
        "send_spoiler": 0,
    }


async def _safe_edit(msg, text: str, reply_markup=None) -> None:
    try:
        await msg.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    except Exception:
        pass


def _error_text(exc: Exception) -> str:
    if isinstance(exc, IGError):
        return error_card("Instagram Download Failed", escape(exc.user))
    return error_card("Instagram Download Failed", escape(str(exc)[:200]))


def _first_caption_html(post, norm_url: str) -> str:
    caption = _caption_html(
        (post.title or post.caption or "")[:180],
        post.uploader,
        post.webpage_url or norm_url,
    )
    if len(caption) > 1000:
        caption = caption[:1000] + "…"
    return caption


async def _send_cached(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    post,
    cached: dict,
    caption: str,
    reply_to_message_id: Optional[int],
) -> bool:
    """Resend known file_ids. Returns True only if every asset sent."""
    for i in range(len(post.assets)):
        key = f"{post.media_id}:{i}"
        file_id, kind_s = cached[key]
        kind = MediaKind(kind_s)
        cap = caption if i == 0 else None
        rid = reply_to_message_id if i == 0 else None
        parse = ParseMode.HTML if cap else None
        if kind == MediaKind.PHOTO:
            await context.bot.send_photo(
                chat_id, photo=file_id, caption=cap, parse_mode=parse, reply_to_message_id=rid
            )
        elif kind == MediaKind.VIDEO:
            await context.bot.send_video(
                chat_id, video=file_id, caption=cap, parse_mode=parse,
                reply_to_message_id=rid, supports_streaming=True,
            )
        elif kind == MediaKind.AUDIO:
            await context.bot.send_audio(
                chat_id, audio=file_id, caption=cap, parse_mode=parse, reply_to_message_id=rid
            )
        elif kind == MediaKind.ANIMATION:
            await context.bot.send_animation(
                chat_id, animation=file_id, caption=cap, parse_mode=parse, reply_to_message_id=rid
            )
        else:
            await context.bot.send_document(
                chat_id, document=file_id, caption=cap, parse_mode=parse, reply_to_message_id=rid
            )
        db.ig_touch_file_id(key)
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
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    url: str,
    *,
    reply_to_message_id: Optional[int] = None,
    requester_id: int = 0,
) -> bool:
    """
    Resolve → download → upload for one Instagram URL.
    Single-flight per normalized URL. Returns True if ≥1 media sent.
    """
    if not ig_config.enabled:
        raise IGDisabled()
    ensure_yt_dlp()
    ensure_temp_root()

    norm = normalize_url(url)
    if classify_post_type(norm) == PostType.PROFILE:
        raise IGInvalidUrl()

    key = norm.split("?")[0]

    async def _job() -> bool:
        t0 = time.perf_counter()
        post = get_resolved(key)
        if post is None:
            post = await resolver_chain.resolve(norm)
            put_resolved(key, post)
        else:
            ig_log(f"resolve-cache hit {post.media_id}")
        transport = TelegramTransport(context.bot)
        caption = _first_caption_html(post, norm)

        cache_keys = [f"{post.media_id}:{i}" for i in range(len(post.assets))]
        cached = get_file_ids(cache_keys)

        # Fast path: every asset already has a stored Telegram file_id.
        if post.assets and len(cached) == len(cache_keys):
            try:
                await _send_cached(
                    context, chat_id, post, cached, caption, reply_to_message_id
                )
                elapsed = int((time.perf_counter() - t0) * 1000)
                db.ig_log_download(
                    chat_id, requester_id, "cache", post.post_type.value,
                    len(post.assets), elapsed, None,
                )
                ig_log(f"cache-hit send {post.media_id} in {elapsed}ms")
                return True
            except Exception as e:
                ig_log(f"file_id path failed, re-download: {e}")

        job_dir = job_workdir(post.media_id)
        try:
            try:
                files = await download_post(post, job_dir)
            except Exception:
                # CDN URLs may have expired (resolve cache) — refresh once.
                invalidate_resolved(key)
                post = await resolver_chain.resolve(norm)
                put_resolved(key, post)
                caption = _first_caption_html(post, norm)
                files = await download_post(post, job_dir)
            sent_any = False
            first = True
            for f in files:
                cap = caption if first else None
                rid = reply_to_message_id if first else None
                try:
                    sent = await transport.send_file(
                        chat_id, f.path, f.kind, caption=cap or "", reply_to_message_id=rid
                    )
                except IGTooLarge:
                    ig_log(f"skip too-large {f.path.name}")
                    continue
                sent_any = True
                file_id = _extract_file_id(sent)
                if file_id:
                    put_file_id(f.cache_key, file_id, f.kind.value, post.canonical_url)
                first = False

            if sent_any:
                elapsed = int((time.perf_counter() - t0) * 1000)
                db.ig_log_download(
                    chat_id, requester_id, "ok", post.post_type.value,
                    len(files), elapsed, None,
                )
                ig_log(f"sent {len(files)} for {post.media_id} in {elapsed}ms")
            return sent_any
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)

    try:
        return await run_exclusive(key, _job)
    except IGError:
        raise
    except Exception as e:
        ig_log(f"job error: {e}")
        metrics.bump("resolved_fail", error=str(e)[:200])
        try:
            db.ig_log_download(chat_id, requester_id, "error", "unknown", 0, 0, str(e)[:200])
        except Exception:
            pass
        raise IGError("Something went wrong processing that link.") from e


# ── Auto-detect handler (group 14) ──────────────────────────────

async def auto_download_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Catch non-command text containing Instagram URLs when auto mode is on."""
    if not ig_config.enabled:
        return
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not message or not chat:
        return

    text = message.text or message.caption or ""
    raw_urls = find_instagram_urls(text)
    if not raw_urls:
        return

    st = _ig_settings(chat.id)
    if not st.get("auto_download"):
        return

    try:
        url = first_post_url(raw_urls)
        if not url:
            return
        if classify_post_type(url) == PostType.PROFILE:
            return  # ignore bare profiles in auto mode
    except IGInvalidUrl:
        return

    metrics.bump("auto_triggers")
    # Send status concurrently so resolve/download starts immediately.
    status_task = asyncio.create_task(
        message.reply_text(
            f"{E.SPARKLE} Downloading…",
            parse_mode=ParseMode.HTML,
            quote=True,
        )
    )
    try:
        await process_url(
            context,
            chat.id,
            url,
            requester_id=user.id if user else 0,
        )
        try:
            status = await status_task
            await status.delete()
        except Exception:
            pass
    except Exception as e:
        try:
            status = await status_task
        except Exception:
            status = None
        if status is not None:
            await _safe_edit(status, _error_text(e), reply_markup=error_keyboard(url))
        else:
            try:
                await message.reply_text(
                    _error_text(e),
                    parse_mode=ParseMode.HTML,
                    reply_markup=error_keyboard(url),
                )
            except Exception:
                pass


# ── /igdl ───────────────────────────────────────────────────────

async def igdl_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return
    if not ig_config.enabled:
        await update.message.reply_text(
            error_card("Instagram Download Failed", escape("Module is disabled.")),
            parse_mode=ParseMode.HTML,
        )
        return

    url: Optional[str] = None
    if context.args:
        found = find_instagram_urls(" ".join(context.args))
        try:
            url = first_post_url(found) or (normalize_url(found[0]) if found else None)
        except IGInvalidUrl:
            url = None
    elif update.message.reply_to_message:
        rt = update.message.reply_to_message
        found = find_instagram_urls(rt.text or rt.caption or "")
        try:
            url = first_post_url(found) or (normalize_url(found[0]) if found else None)
        except IGInvalidUrl:
            url = None

    if not url:
        await update.message.reply_text(
            action_card(
                "Instagram Download",
                [
                    (E.INFO, "Detail", "Send /igdl &lt;url&gt; or reply to an IG link with /igdl"),
                    (E.WEB, "Supported", "/p/ /reel/ /tv/ /stories/"),
                ],
                icon=E.SPARKLE,
            ),
            parse_mode=ParseMode.HTML,
        )
        return

    status_task = asyncio.create_task(
        update.message.reply_text(
            f"{E.SPARKLE} Downloading…",
            parse_mode=ParseMode.HTML,
        )
    )
    try:
        await process_url(
            context,
            update.effective_chat.id,
            url,
            requester_id=update.effective_user.id if update.effective_user else 0,
        )
        try:
            status = await status_task
            await status.delete()
        except Exception:
            pass
    except Exception as e:
        try:
            status = await status_task
        except Exception:
            status = None
        if status is not None:
            await _safe_edit(status, _error_text(e), reply_markup=error_keyboard(url))
        else:
            try:
                await update.message.reply_text(
                    _error_text(e),
                    parse_mode=ParseMode.HTML,
                    reply_markup=error_keyboard(url),
                )
            except Exception:
                pass


# ── /igsettings ─────────────────────────────────────────────────

def _settings_card(auto: bool, mx: int) -> str:
    return action_card(
        "Instagram Settings",
        [
            (E.SETTINGS, "Auto download", "On" if auto else "Off"),
            (E.NUMBER_1, "Max items", str(mx)),
            (E.INFO, "Usage", "/igsettings auto on|off • /igsettings max N"),
            (E.WEB, "Commands", "/igdl /igstats /igcache"),
        ],
        icon=E.SETTINGS,
    )


async def igsettings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return
    if not await _is_admin(update, context):
        await update.message.reply_text(
            error_card("Not allowed", escape("Admins only.")),
            parse_mode=ParseMode.HTML,
        )
        return

    chat_id = update.effective_chat.id
    args = [a.lower() for a in (context.args or [])]

    if args:
        if args[0] in {"auto", "autodl"} and len(args) >= 2:
            on = args[1] in {"on", "1", "true", "yes"}
            db.ig_set_settings(chat_id, auto_download=1 if on else 0)
        elif args[0] == "max" and len(args) >= 2:
            try:
                n = max(1, min(int(args[1]), 50))
            except ValueError:
                n = ig_config.max_items
            db.ig_set_settings(chat_id, max_items=n)

    st = _ig_settings(chat_id)
    auto = bool(st.get("auto_download"))
    mx = int(st.get("max_items") or ig_config.max_items)
    await update.message.reply_text(
        _settings_card(auto, mx),
        parse_mode=ParseMode.HTML,
        reply_markup=settings_keyboard(auto, mx),
    )


# ── /igstats ────────────────────────────────────────────────────

async def igstats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not await _is_admin(update, context):
        await update.message.reply_text(
            error_card("Not allowed", escape("Admins only.")),
            parse_mode=ParseMode.HTML,
        )
        return

    snap = metrics.snapshot()
    cst = cache_stats()
    total_c = int(snap["cache_hits"]) + int(snap["cache_misses"])
    hit_rate = round(100 * int(snap["cache_hits"]) / total_c) if total_c else 0

    text = action_card(
        "Instagram Stats",
        [
            (E.CHECK, "Resolved", f"{snap['resolved_ok']} ok / {snap['resolved_fail']} fail"),
            (E.FIRE, "Downloads", str(snap["downloads"])),
            (E.SPARKLE, "Cache", f"{hit_rate}% hit • L1 {cst['l1']} • DB {cst['rows']}"),
            (E.WEB, "Uploads", f"{snap['uploads']} ok / {snap['upload_fail']} fail"),
            (E.INFO, "Auto triggers", str(snap["auto_triggers"])),
            (E.TIME, "Avg resolve", f"{snap['avg_resolve_ms']} ms"),
            (E.WARN, "Busy rejects", str(snap["busy_rejects"])),
            (E.INFO, "Last error", snap["last_error"] or "—"),
        ],
        icon=E.CHART,
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# ── /igcache ────────────────────────────────────────────────────

async def igcache_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    if not _is_owner(update.effective_user.id):
        await update.message.reply_text(
            error_card("Not allowed", escape("Owner only.")),
            parse_mode=ParseMode.HTML,
        )
        return

    args = [a.lower() for a in (context.args or [])]
    if args and args[0] == "clear":
        n = clear_cache()
        text = action_card(
            "Instagram Cache",
            [(E.CHECK, "Cleared", f"{n} DB row(s); L1 flushed")],
            icon=E.CHECK,
        )
    else:
        cst = cache_stats()
        text = action_card(
            "Instagram Cache",
            [
                (E.FOLDER, "L1 memory", str(cst["l1"])),
                (E.FOLDER, "DB rows", str(cst["rows"])),
                (E.INFO, "DB hits", str(cst["hits"])),
                (E.INFO, "Usage", "/igcache clear"),
            ],
            icon=E.FOLDER,
        )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# ── /igbenchmark ────────────────────────────────────────────────

async def igbenchmark_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    if not _is_owner(update.effective_user.id):
        await update.message.reply_text(
            error_card("Not allowed", escape("Owner only.")),
            parse_mode=ParseMode.HTML,
        )
        return

    url = None
    if context.args:
        found = find_instagram_urls(" ".join(context.args))
        try:
            url = first_post_url(found) or (normalize_url(found[0]) if found else None)
        except IGInvalidUrl:
            url = None
    if not url:
        await update.message.reply_text(
            action_card(
                "Instagram Benchmark",
                [(E.INFO, "Detail", "Usage: /igbenchmark &lt;url&gt;")],
                icon=E.TIME,
            ),
            parse_mode=ParseMode.HTML,
        )
        return

    status = await update.message.reply_text(
        f"{E.TIME} Benchmarking…", parse_mode=ParseMode.HTML
    )
    t0 = time.perf_counter()
    try:
        ensure_yt_dlp()
        post = await resolver_chain.resolve(url)
        elapsed = int((time.perf_counter() - t0) * 1000)
        text = action_card(
            "Instagram Benchmark",
            [
                (E.CHECK, "Status", "OK"),
                (E.TIME, "Resolve", f"{elapsed} ms"),
                (E.INFO, "Type", post.post_type.value),
                (E.FIRE, "Assets", str(len(post.assets))),
                (E.SETTINGS, "Resolver", post.resolver),
            ],
            icon=E.TIME,
        )
        await _safe_edit(status, text, reply_markup=open_on_ig(post.webpage_url or url))
    except Exception as e:
        await _safe_edit(status, _error_text(e), reply_markup=error_keyboard(url))


# ── Callbacks (settings toggles / close) ────────────────────────

async def ig_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
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

    if data in {"ig:set:auto", "ig:set:max"}:
        if not query.message or not query.from_user:
            return
        chat_id = query.message.chat_id
        if not await _member_is_admin(context.bot, chat_id, query.from_user.id):
            await query.answer("Admins only.", show_alert=True)
            return

        st = _ig_settings(chat_id)
        if data == "ig:set:auto":
            new = 0 if st.get("auto_download") else 1
            db.ig_set_settings(chat_id, auto_download=new)
        else:
            cur = int(st.get("max_items") or ig_config.max_items)
            order = [1, 5, 10, 20]
            nxt = order[(order.index(cur) + 1) % len(order)] if cur in order else 5
            db.ig_set_settings(chat_id, max_items=nxt)

        st = _ig_settings(chat_id)
        auto = bool(st.get("auto_download"))
        mx = int(st.get("max_items") or ig_config.max_items)
        try:
            await query.edit_message_text(
                _settings_card(auto, mx),
                parse_mode=ParseMode.HTML,
                reply_markup=settings_keyboard(auto, mx),
            )
        except Exception:
            pass
