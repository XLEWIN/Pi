"""Sticker suite — boabot's kang system in Pi's style.

Ported from boabot ``Yumeko/modules/sticker.py`` with Pi branding
(``E.*`` custom emoji, action cards, colored buttons) and safer internals:

* ``/kang [pack#] [emoji]`` — add a replied sticker/photo/animation
  (or a bare image URL) to your personal sticker pack.
* ``/unkang`` — remove the replied sticker from your pack.
* ``/getsticker`` / ``/getvidsticker`` / ``/getvideo`` — download
  stickers and GIFs as regular image/video files.
* ``/stickerid`` — show a sticker's file ID.
* ``/stickerinfo`` (``/stinfo``) — sticker details + pack link.
* ``/mmf <text>`` (alias ``/memify``) — meme text onto a replied
  image/video; ``;`` splits top/bottom, ``-back <color>`` sets a bar.

Design
------
* Downloads/sends use the Bot API (PTB). Pack operations
  (GetStickerSet / CreateStickerSet / Add / Remove) run over the shared
  MTProto session that tagging already maintains (``TAG_MTPROTO=1``)
  through ``PresenceManager.raw_client()`` — no second session file.
* Pack short names follow boabot: ``<prefix><#>_<user_id>_by_<bot>``
  where prefix is ``a`` / ``anim`` / ``vid``. The ``_<user_id>_``
  segment is what ``/unkang`` matches for ownership.
* Pack ownership: always the kang user — resolved by @username, or
  from the MTProto entity cache that their own /kang message primes
  via passive updates. Telegram rejects bot-owned sets (USER_IS_BOT),
  so there is no fall-back to the bot: if the requester can't be
  resolved the card says so and asks for a @username. boabot's
  ``PeerIdInvalid`` start-button flow isn't needed because the cache
  is primed by the same message that triggered the command.
* Source bytes are uploaded once to the log channel to mint a real
  ``InputDocument`` (or, for /unkang, the sticker is re-sent there by
  file_id) and the log message is deleted afterwards.
* Fixing boabot on the way: ``-b:v:``/``-preset`` ffmpeg bugs (GIF kangs
  silently failed), video-pack limit 50 (not 120), consistent pack-name
  numbering, ``split(";", 1)`` (2+ semicolons crashed /mmf), LANCZOS
  downscale + real WEBP output, and every reply is a Pi card instead of
  boabot's bold-square unicode text.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
import textwrap
from html import escape
from pathlib import Path
from typing import List, Optional, Tuple

import httpx
from PIL import Image, ImageDraw, ImageFont
from telegram import Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import Application, ContextTypes

from telethon.errors import (
    RPCError,
    StickerpackStickersTooMuchError,
    StickersetInvalidError,
)
from telethon.tl.functions.messages import GetStickerSetRequest
from telethon.tl.functions.stickers import (
    AddStickerToSetRequest,
    CreateStickerSetRequest,
    RemoveStickerFromSetRequest,
)
from telethon.tl.types import (
    InputDocument,
    InputPeerSelf,
    InputStickerSetItem,
    InputStickerSetShortName,
    InputUser,
    InputUserSelf,
)

from bot.command_handler import CommandHandler, parse_command
from bot.config import settings
from bot.emojis import E, EID
from bot.keyboards.colored import btn_url, build_keyboard
from bot.logger import logger
from bot.responses import action_card, field_extra, plain_error

#: Log channel (same as main.py) — kang sources land here to mint an
#: InputDocument; the log message is deleted right after the pack op.
LOG_CHANNEL_ID = -1003845687680

#: Sticker emoji used when neither the sticker nor the command gives one.
DEFAULT_EMOJI = "\u2714\ufe0f"  # ✔️ (boabot's default)

#: 10 MB ceiling for URL kangs (Bot API photos can be larger, but an
#: unauthenticated fetch of a huge file is never worth it).
URL_MAX_BYTES = 10 * 1024 * 1024

#: How many numbered packs /kang will walk before giving up.
PACK_SEARCH_LIMIT = 100


class KangError(Exception):
    """User-facing kang/memify failure — the message is shown as-is."""


# ═════════════════════════════════════════════════════════════════
# Pure helpers (unit-tested)
# ═════════════════════════════════════════════════════════════════

# Emoji base: pictographs + the small set of text-presentation symbols
# Telegram still accepts as sticker emoji. ZWJ/variation-selector glue
# keeps multi-part sequences (👨‍👩‍👧) and flags (🇮🇳) as ONE match.
_EMOJI_BASE = (
    "[\u00a9\u00ae\u2122\u2139"
    "\u203c\u2049\u2194-\u2199\u21a9-\u21aa\u2934-\u2935"
    "\u2b05-\u2b55\u3030\u303d\u3297\u3299\u25aa-\u25fe"
    "\u2600-\u27bf\U0001f000-\U0001faff]"
)
_EMOJI_RE = re.compile(
    _EMOJI_BASE + r"(?:\ufe0f|\u200d" + _EMOJI_BASE + r"|[\U0001f1e6-\U0001f1ff])*"
)
_REGIONAL = {chr(c) for c in range(0x1F1E6, 0x1F200)}


def first_emoji(text: str) -> str:
    """First emoji in ``text`` (ZWJ- and flag-aware), or ``""``.

    Adjacent flags collapse to the first pair — four regional indicators
    in a row are two flags, not one sticker emoji.
    """
    if not text:
        return ""
    m = _EMOJI_RE.search(text)
    if not m:
        return ""
    hit = m.group(0)
    if len(hit) > 2 and all(ch in _REGIONAL for ch in hit):
        hit = hit[:2]
    return hit


def pack_name(prefix: str, user_id: int, num: int, bot_username: str) -> str:
    """``a_42_by_Bot`` / ``a2_42_by_Bot`` — boabot-compatible numbering.

    ``num=0`` renders as the bare pack (``a_42_…``), any positive number
    as ``a2_42_…`` — the ``_<user_id>_`` segment keeps /unkang's
    ownership check exact.
    """
    return f"{prefix}{num if num else ''}_{user_id}_by_{bot_username}"


def split_pack_and_rest(tokens: List[str]) -> Tuple[int, List[str]]:
    """Pop a leading positive pack number off ``tokens``.

    ``["5", "\U0001f525"]`` → ``(5, ["\U0001f525"])``; ``"0"`` and
    non-digits stay in the remainder (boabot requires ``int > 0``).
    """
    tokens = list(tokens or [])
    if tokens and tokens[0].isdigit() and int(tokens[0]) > 0:
        return int(tokens[0]), tokens[1:]
    return 0, tokens


def strip_url_tokens(tokens: List[str]) -> List[str]:
    """Drop http(s) URL tokens — used before pack/emoji parsing."""
    return [t for t in tokens if not t.lower().startswith(("http://", "https://"))]


def classify_media(reply) -> Tuple[Optional[str], Optional[str]]:
    """Map a replied message to ``(kind, file_id)``.

    kind: ``resize`` (static image → 512 WEBP), ``convert`` (GIF/video →
    WEBM via ffmpeg), ``animated`` (.tgs passthrough), ``video``
    (video-sticker passthrough), or ``None`` when un-kangable.
    """
    if reply is None:
        return None, None
    photo = getattr(reply, "photo", None)
    if photo:
        return "resize", photo[-1].file_id
    animation = getattr(reply, "animation", None)
    if animation is not None:
        return "convert", animation.file_id
    video = getattr(reply, "video", None)
    if video is not None:
        return "convert", video.file_id
    doc = getattr(reply, "document", None)
    if doc is not None:
        mime = (getattr(doc, "mime_type", None) or "").lower()
        name = (getattr(doc, "file_name", None) or "").lower()
        if "image" in mime:
            return "resize", doc.file_id
        if "tgsticker" in mime or name.endswith(".tgs"):
            return "animated", doc.file_id
        if mime.startswith("video/") or mime == "application/video":
            return "convert", doc.file_id
        return None, None
    st = getattr(reply, "sticker", None)
    if st is not None:
        if getattr(st, "is_animated", False):
            return "animated", st.file_id
        if getattr(st, "is_video", False):
            return "video", st.file_id
        if getattr(st, "file_id", None):
            return "resize", st.file_id
        return None, None
    return None, None


def pack_profile(kind: str) -> Tuple[str, int, bool]:
    """``(pack_prefix, max_stickers, needs_ffmpeg)`` for a source kind."""
    if kind == "resize":
        return "a", 120, False
    if kind == "convert":
        return "vid", 50, True
    if kind == "video":
        return "vid", 50, False
    if kind == "animated":
        return "anim", 50, False
    raise ValueError(f"unknown kind: {kind!r}")


def source_suffix(kind: str, reply) -> str:
    """Extension for the downloaded source file."""
    if reply is not None:
        for obj in (getattr(reply, "sticker", None), getattr(reply, "document", None)):
            if obj is None:
                continue
            fn = getattr(obj, "file_name", None)
            if fn and "." in fn:
                ext = os.path.splitext(fn)[1].lower()
                if 1 < len(ext) <= 6 and ext[1:].isalnum():
                    return ext
    if kind == "animated":
        return ".tgs"
    if kind == "video":
        return ".webm"
    if kind == "convert":
        return ".mp4"
    return ".jpg"


def parse_mmf_text(raw: str) -> Tuple[str, Optional[str]]:
    """Split ``/mmf`` payload → ``(text, bg_color)`` via ``-back``."""
    if "-back" in raw:
        head, _, tail = raw.partition("-back")
        parts = tail.split()
        return head.strip(), (parts[0] if parts else None)
    return raw.strip(), None


def pack_title(user, prefix: str, packnum: int) -> str:
    """Boabot's pack title: ``Lewin's Animated-Pack v2`` (≤ 64 chars)."""
    name = (getattr(user, "first_name", None) or "User").strip() or "User"
    title = f"{name}'s"
    if prefix == "anim":
        title += " Animated-Pack"
    elif prefix == "vid":
        title += " Video-Pack"
    if packnum:
        title += f" v{packnum}"
    return title[:64]


def find_url(msg) -> Optional[str]:
    """First URL in the command message (entity or bare token)."""
    for ent in getattr(msg, "entities", None) or []:
        etype = getattr(ent, "type", None)
        if str(getattr(etype, "value", etype)).lower() in ("url", "text_link"):
            if getattr(ent, "url", None):
                return ent.url
            parse = getattr(msg, "parse_entity", None)
            if callable(parse):
                try:
                    return parse(ent)
                except Exception:  # noqa: BLE001 — fall through to tokens
                    pass
    tokens = (getattr(msg, "text", None) or "").split()[1:]
    for tok in tokens:
        if tok.lower().startswith(("http://", "https://")):
            return tok
    return None


def _peer_to_input_user(peer):
    """InputPeer from get_input_entity → the InputUser CreateStickerSet wants."""
    if peer is None:
        return None
    if isinstance(peer, InputPeerSelf):
        return InputUserSelf()
    uid = getattr(peer, "user_id", None)
    ah = getattr(peer, "access_hash", None)
    if uid and ah is not None:
        return InputUser(user_id=uid, access_hash=ah)
    return None


# ═════════════════════════════════════════════════════════════════
# Media processing
# ═════════════════════════════════════════════════════════════════

def resize_image(filename: str) -> str:
    """Fit within 512×512 and save as WEBP (sticker-format) beside the source."""
    im = Image.open(filename)
    if im.mode not in ("RGB", "RGBA", "P", "L", "LA"):
        im = im.convert("RGBA")
    scale = 512 / max(im.width, im.height)
    size = (max(1, int(im.width * scale)), max(1, int(im.height * scale)))
    if size != im.size:
        im = im.resize(size, Image.LANCZOS)
    out = os.path.splitext(filename)[0] + ".webp"
    im.save(out, "WEBP", quality=80)
    if os.path.getsize(out) > 512 * 1024:  # static sticker cap: 512 KB
        im.save(out, "WEBP", quality=45)
    if out != filename and os.path.exists(filename):
        os.remove(filename)
    return out


def _ffmpeg_bin() -> Optional[str]:
    """Locate ffmpeg: PATH → FFMPEG_PATH → WinGet's ffmpeg packages."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    env = os.getenv("FFMPEG_PATH", "")
    if env and os.path.isfile(env):
        return env
    base = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Packages")
    if os.path.isdir(base):
        for pkg in sorted(os.listdir(base)):
            if not pkg.lower().startswith("yt-dlp.ffmpeg"):
                continue
            inner = os.path.join(base, pkg)
            if not os.path.isdir(inner):
                continue
            for sub in sorted(os.listdir(inner)):
                cand = os.path.join(inner, sub, "bin", "ffmpeg.exe")
                if os.path.isfile(cand):
                    return cand
    return None


async def convert_video(filename: str) -> Optional[str]:
    """GIF/video → 3 s, 512×512, 30 fps VP9 WEBM. None on failure.

    Fixes boabot's broken argv: ``-b:v:`` (invalid flag) and
    ``-preset ultrafast`` (x264-only, rejected by VP9) meant GIF kangs
    never produced an output file at all.
    """
    ff = _ffmpeg_bin()
    if ff is None:
        logger.warning("kang convert: ffmpeg not found")
        return None
    out = os.path.splitext(filename)[0] + ".webm"
    cmd = [
        ff, "-loglevel", "error", "-y",
        "-i", filename,
        "-t", "3",
        "-vf", "fps=30,scale=512:512:force_original_aspect_ratio=increase,crop=512:512",
        "-c:v", "vp9", "-b:v", "500k",
        "-deadline", "realtime", "-cpu-used", "8",
        "-an", out,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
    except OSError as e:
        logger.warning("kang convert: ffmpeg launch failed: %s", e)
        return None
    if proc.returncode != 0 or not os.path.exists(out):
        tail = (err or b"").decode(errors="replace")[-400:].strip()
        logger.warning("kang convert failed (rc=%s): %s", proc.returncode, tail)
        if os.path.exists(out):
            os.remove(out)
        return None
    if out != filename and os.path.exists(filename):
        os.remove(filename)
    return out


def _pick_font() -> str:
    """First usable TrueType font — Windows ships these, Linux has DejaVu."""
    names = (
        "arialbd.ttf", "arial.ttf", "segoeuib.ttf", "segoeui.ttf",
        "tahoma.ttf", "verdana.ttf", "consolab.ttf", "consola.ttf",
    )
    windir = os.environ.get("WINDIR", r"C:\Windows")
    for name in names:
        cand = os.path.join(windir, "Fonts", name)
        if os.path.isfile(cand):
            return cand
    for cand in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ):
        if os.path.isfile(cand):
            return cand
    raise KangError("No usable font found for memifying (arial.ttf missing).")


def _split_text(text: str) -> Tuple[str, str]:
    """``"top;bottom"`` → top/bottom halves (one split — boabot crashed on 2+)."""
    if ";" in text:
        upper, lower = text.split(";", 1)
        return upper.strip(), lower.strip()
    return text.strip(), ""


class Memify:
    """Meme-text overlay for images (PIL) and videos (moviepy, lazy)."""

    def __init__(self, font_path: str) -> None:
        self.font_path = font_path

    def draw_text(self, media_path: str, text: str, bg_color: Optional[str] = None) -> str:
        if media_path.lower().endswith((".webm", ".mp4", ".mov", ".mkv", ".avi")):
            return self._process_video(media_path, text, bg_color)
        return self._process_image(media_path, text, bg_color)

    # ── image ────────────────────────────────────────────────────
    def _process_image(self, media_path: str, text: str, bg_color: Optional[str]) -> str:
        img = Image.open(media_path)
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGBA")
        out = os.path.splitext(media_path)[0] + "_mmf.png"
        self._draw_frame(img, text, bg_color).save(out, "PNG")
        return out

    def _draw_frame(self, img, text: str, bg_color: Optional[str]):
        i_width, i_height = img.size
        font_size = max(12, int((70 / 640) * i_width))
        font = ImageFont.truetype(self.font_path, font_size)
        upper_text, lower_text = _split_text(text)
        draw = ImageDraw.Draw(img)

        def outline_text(t: str, position) -> None:
            x, y = position
            for dx in (-2, -1, 1, 2):
                for dy in (-2, -1, 1, 2):
                    draw.text((x + dx, y + dy), t, font=font, fill="black")
            draw.text((x, y), t, font=font, fill="white")

        def block(lines: List[str], start_y: int) -> None:
            current = start_y
            for line in lines:
                bbox = draw.textbbox((0, 0), line, font=font)
                tw = bbox[2] - bbox[0]
                th = bbox[3] - bbox[1]
                if bg_color:
                    draw.rectangle(
                        [((i_width - tw) / 2 - 5, current - 5),
                         ((i_width + tw) / 2 + 5, current + th + 5)],
                        fill=bg_color,
                    )
                outline_text(line, ((i_width - tw) / 2, current))
                current += th + 10

        block(textwrap.wrap(upper_text, width=20), 10)
        lower_lines = textwrap.wrap(lower_text, width=20)
        block(lower_lines, max(10, i_height - len(lower_lines) * font_size - 10))
        return img

    # ── video (moviepy imported lazily — heavy, only when needed) ─
    def _process_video(self, media_path: str, text: str, bg_color: Optional[str]) -> str:
        from moviepy import CompositeVideoClip, TextClip, VideoFileClip

        clip = VideoFileClip(media_path)
        out = os.path.splitext(media_path)[0] + "_mmf.webm"
        font_size = max(24, int((70 / 640) * clip.w))
        upper_text, lower_text = _split_text(text)

        def make(txt: str, position: str):
            if not txt:
                return []
            max_chars = max(8, int(clip.w / (font_size * 0.6)))
            lines = textwrap.wrap(txt, width=max_chars) or [txt]
            spacing = font_size + 10
            total = len(lines) * spacing
            built = []
            for i, line in enumerate(lines):
                if position == "top":
                    y = max(0, (clip.h - total) // 2 - (len(lines) - i - 1) * spacing - 110)
                else:
                    y = min(clip.h - spacing, clip.h - total + i * spacing - 20)
                built.append(
                    TextClip(
                        text=line,
                        font=self.font_path,
                        font_size=font_size,
                        color="white",
                        bg_color=bg_color,
                        stroke_color="black",
                        stroke_width=2,
                    )
                    .with_position(("center", int(y)))
                    .with_duration(clip.duration)
                )
            return built

        final = CompositeVideoClip([clip] + make(upper_text, "top") + make(lower_text, "bottom"))
        try:
            final.write_videofile(out, codec="libvpx-vp9", audio=False, logger=None)
        finally:
            final.close()
            clip.close()
        return out


# ═════════════════════════════════════════════════════════════════
# MTProto access (lazy — avoids importing tagging at module import)
# ═════════════════════════════════════════════════════════════════

async def _mtproto_client():
    """Shared Telethon client from the tagging presence manager, or None."""
    from bot.modules.tagging.presence.manager import get_manager

    return await get_manager().raw_client()


async def _resolve_owner(client, user):
    """InputUser for the kang user — @username first, then the entity cache.

    Telethon exposes ``get_input_entity`` — boabot's ``resolve_peer`` is a
    pyrogram API and does not exist here (calling it raised AttributeError
    into the old bare excepts, which silently returned None and pushed
    every kang onto the bot-self fallback → USER_IS_BOT).

    The numeric path is cache-only: Telethon populates that cache from
    passive MTProto updates, so the very /kang message usually primes it
    before this runs; the caller wait-retries once to cover the race.
    """
    username = getattr(user, "username", None)
    if username:
        try:
            got = _peer_to_input_user(await client.get_input_entity(username))
            if got is not None:
                return got
        except Exception:  # noqa: BLE001 — unknown username → try the id
            pass
    try:
        got = _peer_to_input_user(await client.get_input_entity(user.id))
        if got is not None:
            return got
    except Exception:  # noqa: BLE001 — not cached yet → caller retries
        pass
    return None


async def _resolve_pack(
    client, prefix: str, user_id: int, start_num: int, bot_username: str, max_stickers: int
) -> Tuple[str, bool]:
    """Walk numbered packs until one exists with room (or must be created)."""
    num = start_num
    for _ in range(PACK_SEARCH_LIMIT):
        name = pack_name(prefix, user_id, num, bot_username)
        try:
            rs = await client(GetStickerSetRequest(InputStickerSetShortName(name), hash=0))
        except StickersetInvalidError:
            return name, False
        if (rs.set.count or 0) >= max_stickers:
            num += 1
            continue
        return name, True
    raise KangError("All your numbered packs are full — try a much higher pack number.")


async def _fetch_url(url: str, dest_base: str) -> str:
    """Download an image URL to ``dest_base + ext`` → path. Raises KangError."""
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=30.0,
            headers={"User-Agent": "Mozilla/5.0 (PiBot sticker kang)"},
        ) as hc:
            resp = await hc.get(url)
    except Exception as e:  # noqa: BLE001 — any transport failure is user-facing
        raise KangError(f"Couldn't download that URL ({type(e).__name__}).") from e
    if resp.status_code != 200:
        raise KangError(f"Couldn't download that URL (HTTP {resp.status_code}).")
    ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
    ext = {
        "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
        "image/webp": ".webp", "image/gif": ".gif", "image/bmp": ".bmp",
    }.get(ctype, "")
    if not ext:
        raise KangError(f"That URL isn't an image ({ctype or 'unknown type'}).")
    if len(resp.content) > URL_MAX_BYTES:
        raise KangError("That image is too large (10 MB max).")
    path = dest_base + ext
    with open(path, "wb") as fh:
        fh.write(resp.content)
    return path


_MTProto_OFF = "Sticker packs need MTProto (set TAG_MTPROTO=1 and restart)."

# The /kang MTProto update can land a hair after the Bot API update that
# triggered us — _resolve_owner's numeric path reads that cache, so give
# it one beat before declaring the requester unresolvable. Patchable in tests.
_OWNER_RETRY_WAIT = 1.0


# ── Send seams: Pi cards carry <tg-emoji> markup, so every reply in
#    this module must be parsed as HTML (the codebase convention —
#    see admin.py / tagall.py). setdefault keeps explicit overrides.
async def _send_reply(msg, text, **kw):
    kw.setdefault("parse_mode", ParseMode.HTML)
    _fn = msg.reply_text
    return await _fn(text, **kw)


async def _edit_reply(target, text, **kw):
    kw.setdefault("parse_mode", ParseMode.HTML)
    return await target.edit_text(text, **kw)


# ═════════════════════════════════════════════════════════════════
# /kang
# ═════════════════════════════════════════════════════════════════

async def kang_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if msg is None:
        return
    if user is None:
        await _send_reply(msg, plain_error("You can't use this command anonymously."))
        return

    reply = msg.reply_to_message
    parsed = parse_command(msg.text)
    args: List[str] = list(parsed[1]) if parsed else []

    url: Optional[str] = None
    if reply is None:
        url = find_url(msg)
        if url is None:
            await _send_reply(msg, plain_error(
                "Usage: reply to a photo, sticker, or animation with /kang "
                "\u2014 or send an image URL with it."
            ))
            return
        kind = "resize"
        tokens = strip_url_tokens(args)
    else:
        kind, file_id = classify_media(reply)
        tokens = args
        if kind is None:
            await _send_reply(msg, plain_error(
                "Unable to kang this type \u2014 reply with a photo, sticker, or animation."
            ))
            return

    packnum, rest = split_pack_and_rest(tokens)
    sticker_emoji = DEFAULT_EMOJI
    st = getattr(reply, "sticker", None) if reply is not None else None
    if st is not None and getattr(st, "emoji", None):
        sticker_emoji = st.emoji
    sticker_emoji = first_emoji(" ".join(rest)) or sticker_emoji

    prefix, max_stickers, needs_ffmpeg = pack_profile(kind)
    prog = await _send_reply(msg, f"{E.SPARKLE} Processing your request\u2026")

    client = await _mtproto_client()
    if client is None:
        await _edit_reply(prog, plain_error(_MTProto_OFF))
        return

    tmp = tempfile.mkdtemp(prefix="pi_kang_")
    log_msg = None
    try:
        # 1. source bytes ─────────────────────────────────────────
        if url is not None:
            path = await _fetch_url(url, os.path.join(tmp, "kang_src"))
        else:
            suffix = source_suffix(kind, reply)
            path = os.path.join(tmp, f"kang_src{suffix}")
            try:
                tgfile = await context.bot.get_file(file_id)
                await tgfile.download_to_drive(path)
            except Exception as e:  # noqa: BLE001 — surfaced as a card
                raise KangError(f"Download failed ({type(e).__name__}).") from e
            if not os.path.isfile(path):
                raise KangError("Download failed \u2014 no file arrived.")

        # 2. processing ───────────────────────────────────────────
        if kind == "resize":
            path = resize_image(path)
        elif kind == "convert":
            converted = await convert_video(path)
            if converted is None:
                if _ffmpeg_bin() is None:
                    raise KangError(
                        "ffmpeg not found \u2014 video kangs need it "
                        "(install ffmpeg or set FFMPEG_PATH)."
                    )
                raise KangError("Video conversion failed \u2014 that clip may be unsupported.")
            path = converted

        # 3. pack resolution ──────────────────────────────────────
        packname, exists = await _resolve_pack(
            client, prefix, user.id, packnum, settings.bot_username, max_stickers
        )

        # 4. mint a real InputDocument via the log channel ────────
        log_msg = await client.send_file(LOG_CHANNEL_ID, path, force_document=True)
        doc = getattr(log_msg.media, "document", None) if log_msg.media else None
        if doc is None:
            raise KangError("Couldn't prepare the sticker file for upload.")
        item = InputStickerSetItem(
            document=InputDocument(
                id=doc.id,
                access_hash=doc.access_hash,
                file_reference=doc.file_reference or b"",
            ),
            emoji=sticker_emoji,
        )

        # 5. add to the existing pack, or create it ───────────────
        if exists:
            await _edit_reply(prog, f"{E.ADD} Adding to your existing pack\u2026")
            try:
                await client(AddStickerToSetRequest(
                    stickerset=InputStickerSetShortName(packname), sticker=item
                ))
            except StickerpackStickersTooMuchError as e:
                raise KangError(
                    "That pack is full \u2014 kang into another one, e.g. /kang 2"
                ) from e
        else:
            await _edit_reply(prog, f"{E.ADD} Creating a new sticker pack\u2026")
            title = pack_title(user, prefix, packnum)
            # Owner must be the requester: Telegram rejects bot-owned sets
            # (USER_IS_BOT — our MTProto session IS the bot). The entity
            # cache is primed by this very message; one wait-retry covers
            # the arrival race, then fail with a clear card.
            owner = await _resolve_owner(client, user)
            if owner is None:
                await asyncio.sleep(_OWNER_RETRY_WAIT)
                owner = await _resolve_owner(client, user)
            if owner is None:
                raise KangError(
                    "Couldn't resolve your account for the sticker pack \u2014 "
                    "set a @username and try again."
                )
            await client(CreateStickerSetRequest(
                user_id=owner,
                title=title,
                short_name=packname,
                stickers=[item],
            ))

        # 6. success ──────────────────────────────────────────────
        markup = build_keyboard([[
            btn_url("View Sticker Pack", f"https://t.me/addstickers/{packname}",
                    icon_emoji_id=EID.ADD),
        ]])
        card = action_card(
            "Sticker added successfully",
            [
                field_extra(E.SETTINGS, "Pack", f"<code>{escape(packname)}</code>"),
                field_extra(E.INFO, "Emoji", escape(sticker_emoji)),
            ],
            icon=E.CHECK,
        )
        await _edit_reply(prog, card, parse_mode=ParseMode.HTML, reply_markup=markup)

    except KangError as e:
        await _edit_reply(prog, plain_error(escape(str(e))))
    except RPCError as e:
        logger.warning("kang RPC failure: %s: %s", type(e).__name__, e)
        await _edit_reply(prog, plain_error(f"Telegram rejected the sticker: {escape(str(e))}"))
    except Exception as e:  # noqa: BLE001 — last-resort card, never a crash
        logger.exception("kang failed unexpectedly")
        await _edit_reply(prog, plain_error(f"Something went wrong ({type(e).__name__})."))
    finally:
        if log_msg is not None:
            try:
                await client.delete_messages(LOG_CHANNEL_ID, [log_msg.id])
            except Exception:  # noqa: BLE001 — cleanup is best-effort
                pass
        shutil.rmtree(tmp, ignore_errors=True)


# ═════════════════════════════════════════════════════════════════
# /unkang
# ═════════════════════════════════════════════════════════════════

async def unkang_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if msg is None:
        return
    if user is None:
        await _send_reply(msg, plain_error("You can't use this command anonymously."))
        return

    reply = msg.reply_to_message
    st = getattr(reply, "sticker", None) if reply is not None else None
    if st is None:
        await _send_reply(msg, plain_error(
            "Reply to the sticker you want to remove from your pack."
        ))
        return
    set_name = getattr(st, "set_name", None)
    if not set_name or f"_{user.id}_" not in set_name:
        await _send_reply(msg, plain_error("That sticker isn't in your pack."))
        return

    prog = await _send_reply(msg, f"{E.SPARKLE} Removing sticker from your pack\u2026")
    client = await _mtproto_client()
    if client is None:
        await _edit_reply(prog, plain_error(_MTProto_OFF))
        return

    log_msg = None
    try:
        # Re-send by file_id → the log channel holds the SAME document,
        # giving us id/access_hash/file_reference without a file_id decode.
        log_msg = await context.bot.send_sticker(LOG_CHANNEL_ID, sticker=st.file_id)
        fetched = await client.get_messages(LOG_CHANNEL_ID, ids=log_msg.message_id)
        doc = getattr(fetched.media, "document", None) if fetched is not None and fetched.media else None
        if doc is None:
            raise KangError("Couldn't read that sticker's file reference.")
        await client(RemoveStickerFromSetRequest(
            sticker=InputDocument(
                id=doc.id,
                access_hash=doc.access_hash,
                file_reference=doc.file_reference or b"",
            )
        ))
    except KangError as e:
        await _edit_reply(prog, plain_error(escape(str(e))))
    except RPCError as e:
        logger.warning("unkang RPC failure: %s: %s", type(e).__name__, e)
        await _edit_reply(prog, plain_error(f"Telegram rejected the removal: {escape(str(e))}"))
    except Exception as e:  # noqa: BLE001
        logger.exception("unkang failed unexpectedly")
        await _edit_reply(prog, plain_error(f"Something went wrong ({type(e).__name__})."))
    else:
        card = action_card(
            "Sticker removed successfully",
            [field_extra(E.SETTINGS, "Pack", f"<code>{escape(set_name)}</code>")],
            icon=E.CHECK,
        )
        await _edit_reply(prog, card, parse_mode=ParseMode.HTML)
    finally:
        if log_msg is not None:
            try:
                await context.bot.delete_message(LOG_CHANNEL_ID, log_msg.message_id)
            except Exception:  # noqa: BLE001
                pass


# ═════════════════════════════════════════════════════════════════
# Download helpers: /getsticker /getvidsticker /getvideo /stickerid
# ═════════════════════════════════════════════════════════════════

async def _download(context: ContextTypes.DEFAULT_TYPE, file_id: str, dest: str) -> str:
    try:
        tgfile = await context.bot.get_file(file_id)
        await tgfile.download_to_drive(dest)
    except Exception as e:  # noqa: BLE001
        raise KangError(f"Download failed ({type(e).__name__}).") from e
    if not os.path.isfile(dest):
        raise KangError("Download failed \u2014 no file arrived.")
    return dest


async def getsticker_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None:
        return
    reply = msg.reply_to_message
    st = getattr(reply, "sticker", None) if reply is not None else None
    if st is None:
        await _send_reply(msg, plain_error("Reply to a sticker to use this command."))
        return
    if getattr(st, "is_animated", False):
        await _send_reply(msg, plain_error(
            "Animated (.tgs) stickers can't be saved as images."
        ))
        return
    if getattr(st, "is_video", False):
        await _send_reply(msg, plain_error(
            "That's a video sticker \u2014 use /getvidsticker instead."
        ))
        return

    tmp = tempfile.mkdtemp(prefix="pi_getsticker_")
    try:
        src_ext = source_suffix("resize", reply)
        src = os.path.join(tmp, f"sticker{src_ext}")
        await _download(context, st.file_id, src)
        img = Image.open(src)
        if img.mode not in ("RGB", "RGBA", "P", "L", "LA"):
            img = img.convert("RGBA")
        out = os.path.join(tmp, "sticker.png")
        img.save(out, "PNG")
        caption = action_card(
            "Sticker downloaded",
            [
                field_extra(E.INFO, "Emoji", escape(getattr(st, "emoji", None) or DEFAULT_EMOJI)),
                field_extra(E.SETTINGS, "File ID", f"<code>{escape(st.file_id)}</code>"),
            ],
            icon=E.CHECK,
        )
        await msg.reply_document(document=Path(out), caption=caption, parse_mode=ParseMode.HTML)
    except KangError as e:
        await _send_reply(msg, plain_error(escape(str(e))))
    except Exception as e:  # noqa: BLE001
        logger.exception("getsticker failed")
        await _send_reply(msg, plain_error(f"Something went wrong ({type(e).__name__})."))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def getvidsticker_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None:
        return
    reply = msg.reply_to_message
    st = getattr(reply, "sticker", None) if reply is not None else None
    if st is None:
        await _send_reply(msg, plain_error(
            "Reply to a video sticker to download it as MP4."
        ))
        return
    if not getattr(st, "is_video", False):
        await _send_reply(msg, plain_error(
            "That's not a video sticker \u2014 use /getsticker for static ones."
        ))
        return

    tmp = tempfile.mkdtemp(prefix="pi_getvid_")
    try:
        src = os.path.join(tmp, "sticker.webm")
        await _download(context, st.file_id, src)
        caption = action_card(
            "Video sticker downloaded",
            [
                field_extra(E.INFO, "Emoji", escape(getattr(st, "emoji", None) or DEFAULT_EMOJI)),
                field_extra(E.SETTINGS, "File ID", f"<code>{escape(st.file_id)}</code>"),
            ],
            icon=E.CHECK,
        )
        try:
                await msg.reply_animation(animation=Path(src), caption=caption, parse_mode=ParseMode.HTML)
        except BadRequest:
            # Bot API won't always take a raw .webm as an animation
                await msg.reply_document(document=Path(src), caption=caption, parse_mode=ParseMode.HTML)
    except KangError as e:
        await _send_reply(msg, plain_error(escape(str(e))))
    except Exception as e:  # noqa: BLE001
        logger.exception("getvidsticker failed")
        await _send_reply(msg, plain_error(f"Something went wrong ({type(e).__name__})."))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def getvideo_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None:
        return
    reply = msg.reply_to_message
    anim = getattr(reply, "animation", None) if reply is not None else None
    if anim is None:
        await _send_reply(msg, plain_error(
            "Reply to a GIF to download it as an MP4 video."
        ))
        return

    tmp = tempfile.mkdtemp(prefix="pi_getvideo_")
    try:
        src = await _download(context, anim.file_id, os.path.join(tmp, "video.mp4"))
        caption = action_card(
            "Video downloaded",
            [field_extra(E.SETTINGS, "File ID", f"<code>{escape(anim.file_id)}</code>")],
            icon=E.CHECK,
        )
        await msg.reply_video(video=Path(src), caption=caption, parse_mode=ParseMode.HTML)
    except KangError as e:
        await _send_reply(msg, plain_error(escape(str(e))))
    except Exception as e:  # noqa: BLE001
        logger.exception("getvideo failed")
        await _send_reply(msg, plain_error(f"Something went wrong ({type(e).__name__})."))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def stickerid_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None:
        return
    reply = msg.reply_to_message
    st = getattr(reply, "sticker", None) if reply is not None else None
    if st is None:
        await _send_reply(msg, plain_error("Reply to a sticker to get its file ID."))
        return
    card = action_card(
        "Sticker ID",
        [field_extra(E.SETTINGS, "File ID", f"<code>{escape(st.file_id)}</code>")],
        icon=E.INFO,
    )
    await _send_reply(msg, card, parse_mode=ParseMode.HTML)


# ═════════════════════════════════════════════════════════════════
# /stickerinfo (+ /stinfo)
# ═════════════════════════════════════════════════════════════════

async def stickerinfo_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None:
        return
    reply = msg.reply_to_message
    st = getattr(reply, "sticker", None) if reply is not None else None
    if st is None:
        await _send_reply(msg, plain_error("Reply to a sticker to see its details."))
        return

    if getattr(st, "is_animated", False):
        st_type = "Animated"
    elif getattr(st, "is_video", False):
        st_type = "Video"
    else:
        st_type = "Static"
    date = getattr(st, "date", None)
    date_txt = date.strftime("%Y-%m-%d %H:%M UTC") if hasattr(date, "strftime") else str(date or "N/A")

    fields = [
        field_extra(E.SETTINGS, "Type", st_type),
        field_extra(E.INFO, "Emoji", escape(getattr(st, "emoji", None) or "N/A")),
        field_extra(E.FOLDER, "Pack", escape(getattr(st, "set_name", None) or "N/A")),
        field_extra(E.USER, "Filename", escape(getattr(st, "file_name", None) or "N/A")),
        field_extra(E.STAR, "Unique ID", f"<code>{escape(getattr(st, 'file_unique_id', None) or 'N/A')}</code>"),
        field_extra(E.SETTINGS, "File ID", f"<code>{escape(st.file_id)}</code>"),
        field_extra(E.TIME, "Date", escape(date_txt)),
    ]
    markup = None
    if getattr(st, "set_name", None):
        markup = build_keyboard([[
            btn_url("Add Sticker Pack", f"https://t.me/addstickers/{st.set_name}",
                    icon_emoji_id=EID.ADD),
        ]])
    card = action_card("Sticker information", fields, icon=E.INFO)
    await _send_reply(msg, card, parse_mode=ParseMode.HTML, reply_markup=markup)


# ═════════════════════════════════════════════════════════════════
# /mmf (alias /memify)
# ═════════════════════════════════════════════════════════════════

async def mmf_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None:
        return
    reply = msg.reply_to_message
    if reply is None:
        await _send_reply(msg, plain_error(
            "Reply to an image, video, or animation with /mmf <text>."
        ))
        return
    kind, file_id = classify_media(reply)
    if kind is None:
        await _send_reply(msg, plain_error(
            "Reply to an image, video, or animation to memify it."
        ))
        return
    if kind == "animated":
        await _send_reply(msg, plain_error("Animated (.tgs) stickers can't be memified."))
        return

    parsed = parse_command(msg.text)
    raw = " ".join(parsed[1]) if parsed and parsed[1] else ""
    if not raw.strip():
        await _send_reply(msg, plain_error(
            "Provide some text \u2014 e.g. /mmf top text;bottom text"
        ))
        return
    text, bg_color = parse_mmf_text(raw)
    if not text:
        await _send_reply(msg, plain_error(
            "Provide some text \u2014 e.g. /mmf top text;bottom text"
        ))
        return

    prog = await _send_reply(msg, f"{E.SPARKLE} Memifying your media\u2026")
    tmp = tempfile.mkdtemp(prefix="pi_mmf_")
    try:
        suffix = source_suffix(kind, reply)
        src = os.path.join(tmp, f"mmf_src{suffix}")
        await _download(context, file_id, src)
        meme = Memify(_pick_font()).draw_text(src, text, bg_color)
        if meme.lower().endswith(".webm"):
            try:
                await msg.reply_sticker(sticker=Path(meme))
            except BadRequest:
                # Arbitrary VP9 rarely passes sticker validation — send as file
                await msg.reply_document(document=Path(meme))
        else:
            await msg.reply_document(document=Path(meme))
        await prog.delete()
    except KangError as e:
        await _edit_reply(prog, plain_error(escape(str(e))))
    except Exception as e:  # noqa: BLE001
        logger.exception("mmf failed unexpectedly")
        await _edit_reply(prog, plain_error(f"Something went wrong ({type(e).__name__})."))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ═════════════════════════════════════════════════════════════════

def setup(app: Application) -> List[str]:
    app.add_handler(CommandHandler("kang", kang_command))
    app.add_handler(CommandHandler("unkang", unkang_command))
    app.add_handler(CommandHandler("getsticker", getsticker_command))
    app.add_handler(CommandHandler("getvidsticker", getvidsticker_command))
    app.add_handler(CommandHandler("getvideo", getvideo_command))
    app.add_handler(CommandHandler("stickerid", stickerid_command))
    app.add_handler(CommandHandler(["stickerinfo", "stinfo"], stickerinfo_command))
    app.add_handler(CommandHandler(["mmf", "memify"], mmf_command))
    return [
        "/kang", "/unkang", "/getsticker", "/getvidsticker",
        "/getvideo", "/stickerid", "/stickerinfo", "/mmf",
    ]
