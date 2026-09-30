"""Social profile search cards — /tiktok /x /yt /ig.

Instant profile lookup in groups and DMs: fetches the profile,
renders it onto the platform's decorated template (name / handle /
bio / three stat pills + avatar), and replies with the card plus a
blue "View on …" URL button.

Data sources (no API keys):
  TikTok   tikwm user/info (same host the media module already uses)
            → paced retries for its ~1/s rate limit
  X        api.fxtwitter.com profile JSON → syndication fallback
  YouTube  channel HTML (ytInitialData) + /about for total views
  Instagram  web_profile_info (x-ig-app-id) → i.instagram.com mirror
"""

from __future__ import annotations

import asyncio
import io
import json
import re
import time
from html import escape
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
from aiogram.enums import ParseMode
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup, Message

from bot.command_handler import cmd
from bot.emojis import E
from bot.keyboards.colored import btn_url
from bot.logger import logger
from bot.pipeline import on
from bot.reply import reply_photo, reply_text
from bot.responses import error_card

# ── Static assets ────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parent.parent
_FONT_PATH = _ROOT / "assets" / "NotoSans-Bold.ttf"
_TEMPLATES = _ROOT / "templates"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_TIMEOUT = httpx.Timeout(8.0, connect=6.0)
_CACHE_TTL = 300.0  # seconds — repeat searches answer instantly
_RETRY_DELAY = 1.3   # seconds between tikwm attempts (its rate limit is ~1/s)
_IG_DELAY = 2.0      # seconds between Instagram 429 retries

#: Platform display spec: template file, blue button label, profile URL.
_PLATFORMS: Dict[str, Dict[str, str]] = {
    "tiktok": {
        "label": "TikTok",
        "template": "profile_tiktok.png",
        "button": "View on TikTok",
        "url": "https://www.tiktok.com/@{u}",
        "cmd": "tiktok",
    },
    "x": {
        "label": "X",
        "template": "profile_x.jpg",
        "button": "View on X",
        "url": "https://x.com/{u}",
        "cmd": "x",
    },
    "youtube": {
        "label": "YouTube",
        "template": "profile_youtube.png",
        "button": "View on YouTube",
        "url": "https://www.youtube.com/@{u}",
        "cmd": "yt",
    },
    "instagram": {
        "label": "Instagram",
        "template": "profile_instagram.png",
        "button": "View on Instagram",
        "url": "https://www.instagram.com/{u}",
        "cmd": "ig",
    },
}

# ── Template geometry (measured from the shipped banners) ───────
# Bars/pills are identical across the 2172×724 banners (TikTok,
# YouTube, Instagram); X has its own 2048×683 layout.
_BARS_STD = [
    (589, 158, 1270, 223),   # name
    (589, 253, 925, 299),    # handle
    (589, 329, 1520, 382),   # bio
]
_PILLS_STD = [
    (593, 494, 891, 572),
    (909, 494, 1206, 572),
    (1223, 494, 1516, 572),
]
_GEOM: Dict[str, Dict[str, Any]] = {
    "tiktok": {
        "avatar": (82, 137, 520, 577),
        "bars": _BARS_STD,
        "pills": _PILLS_STD,
        "radius": 46,
        "inset": 9,
    },
    "youtube": {
        "avatar": (80, 136, 515, 580),
        "bars": _BARS_STD,
        "pills": _PILLS_STD,
        "radius": 46,
        "inset": 9,
    },
    "instagram": {
        "avatar": (80, 137, 518, 579),
        "bars": _BARS_STD,
        "pills": _PILLS_STD,
        "radius": 46,
        "inset": 9,
    },
    "x": {
        "avatar": (77, 130, 495, 551),
        "bars": [(557, 149, 1175, 211), (557, 240, 870, 283), (557, 312, 1430, 362)],
        "pills": [(562, 463, 839, 546), (861, 463, 1137, 546), (1156, 463, 1430, 546)],
        "radius": 44,
        "inset": 9,
    },
}

# Card text colors (bot text style: white / grey on dark banners).
_C_NAME = (255, 255, 255)
_C_HANDLE = (168, 174, 188)
_C_BIO = (206, 211, 222)
_C_STAT = (255, 255, 255)

# Lookup cache: (platform, handle_lower) -> (monotonic_ts, profile_dict)
_CACHE: Dict[Tuple[str, str], Tuple[float, Dict[str, Any]]] = {}


class ProfileError(Exception):
    """User-facing lookup failure — message is card-ready (pre-escape)."""


# ── Number formatting ────────────────────────────────────────────
def compact(value: Any) -> str:
    """241727911 → '241.7M', 1414 → '1.4K', 373 → '373', 0 → '0'."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return str(value or "0")
    if n < 0:
        n = 0
    if n < 1000:
        return str(n)
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if n >= div:
            s = f"{n / div:.1f}"
            if s.endswith(".0"):
                s = s[:-2]
            return s + suf
    return str(n)


def _num(token: str) -> int:
    """'26.9M' → 26900000, '1,024' → 1024, '373' → 373."""
    token = (token or "").strip().replace(",", "")
    m = re.fullmatch(r"([\d.]+)([KMB])?", token, re.I)
    if not m:
        return 0
    try:
        val = float(m.group(1))
    except ValueError:
        return 0
    mult = {"k": 1e3, "m": 1e6, "b": 1e9}.get((m.group(2) or "").lower(), 1)
    return int(val * mult)


# ── Pure parsers (unit-tested without network) ───────────────────
def _jstr(s: str) -> str:
    """JSON string-unescape with a forgiving fallback."""
    try:
        return json.loads(f'"{s}"')
    except Exception:
        return (
            s.replace("\\n", "\n").replace("\\t", " ")
            .replace('\\"', '"').replace("\\\\", "\\")
        )


def _re1(pattern: str, text: str) -> Optional[str]:
    m = re.search(pattern, text)
    return m.group(1) if m else None


def _parse_tikwm(payload: Dict[str, Any]) -> Dict[str, Any]:
    """tikwm /api/user/info response → profile dict."""
    data = payload.get("data") or {}
    user, stats = data.get("user") or {}, data.get("stats") or {}
    handle = user.get("uniqueId") or ""
    if not handle:
        raise ProfileError("user not found")
    return {
        "name": user.get("nickname") or handle,
        "handle": f"@{handle}",
        "bio": (user.get("signature") or "").strip(),
        "avatar": (user.get("avatarLarger") or user.get("avatarMedium")
                   or user.get("avatarThumb") or ""),
        "stats": (
            f"{compact(stats.get('followerCount'))} Followers",
            f"{compact(stats.get('heartCount'))} Likes",
            f"{compact(stats.get('videoCount'))} Videos",
        ),
    }


def _parse_tiktok_direct(payload: Dict[str, Any]) -> Dict[str, Any]:
    """www.tiktok.com/api/user/detail response → profile dict."""
    info = payload.get("userInfo") or {}
    user, stats = info.get("user") or {}, info.get("stats") or {}
    handle = user.get("uniqueId") or ""
    if not handle:
        raise ProfileError("user not found")
    return {
        "name": user.get("nickname") or handle,
        "handle": f"@{handle}",
        "bio": (user.get("signature") or "").strip(),
        "avatar": user.get("avatarLarger") or user.get("avatarThumb") or "",
        "stats": (
            f"{compact(stats.get('followerCount'))} Followers",
            f"{compact(stats.get('heartCount'))} Likes",
            f"{compact(stats.get('videoCount'))} Videos",
        ),
    }


def _parse_x(html: str) -> Dict[str, Any]:
    """syndication timeline-profile HTML (JSON blob) → profile dict."""
    screen = _re1(r'"screen_name":"([^"]{1,40})"', html or "")
    if not screen:
        raise ProfileError("user not found")
    name = _re1(r'"name":"((?:[^"\\]|\\.)*)"', html) or screen
    bio = _re1(r'"description":"((?:[^"\\]|\\.)*)"', html) or ""
    avatar = _re1(r'"profile_image_url_https":"([^"]+)"', html) or ""
    if "_normal." in avatar:
        avatar = avatar.replace("_normal.", "_400x400.")
    followers = _re1(r'"followers_count":(\d+)', html) or "0"
    following = _re1(r'"friends_count":(\d+)', html) or "0"
    tweets = _re1(r'"statuses_count":(\d+)', html) or "0"
    return {
        "name": _jstr(name),
        "handle": f"@{screen}",
        "bio": _jstr(bio).strip(),
        "avatar": avatar,
        "stats": (
            f"{compact(int(followers))} Followers",
            f"{compact(int(following))} Following",
            f"{compact(int(tweets))} Tweets",
        ),
    }


def _parse_fxtwitter(payload: Dict[str, Any]) -> Dict[str, Any]:
    """api.fxtwitter.com /{screen_name} JSON → profile dict."""
    user = payload.get("user") or {}
    handle = user.get("screen_name") or ""
    if not handle:
        raise ProfileError("user not found")
    avatar = user.get("avatar_url") or ""
    if "_normal." in avatar:
        avatar = avatar.replace("_normal.", "_400x400.")
    raw = user.get("raw_description")
    bio = user.get("description") or (
        raw.get("text") if isinstance(raw, dict) else ""
    )
    return {
        "name": user.get("name") or handle,
        "handle": f"@{handle}",
        "bio": (bio or "").strip(),
        "avatar": avatar,
        "stats": (
            f"{compact(user.get('followers'))} Followers",
            f"{compact(user.get('following'))} Following",
            f"{compact(user.get('tweets'))} Tweets",
        ),
    }


def _parse_youtube(main_html: str, about_html: str) -> Dict[str, Any]:
    """Channel HTML (+ /about for lifetime views) → profile dict."""
    handle = _re1(r'"canonicalBaseUrl":"/(@[^"]{1,60})"', main_html or "")
    name = _re1(
        r'"channelMetadataRenderer":\{"title":"((?:[^"\\]|\\.)*)"', main_html or ""
    ) or _re1(r'<meta property="og:title" content="([^"]{1,120})"', main_html)
    if not name:
        raise ProfileError("channel not found")
    avatar = _re1(r'<meta property="og:image" content="([^"]+)"', main_html) or ""
    # Channel description: first "description" after channelMetadataRenderer.
    bio = ""
    cmd_pos = (main_html or "").find('"channelMetadataRenderer":')
    if cmd_pos >= 0:
        m = re.search(
            r'"description":"((?:[^"\\]|\\.)*)"',
            main_html[cmd_pos:cmd_pos + 4000],
        )
        if m:
            bio = _jstr(m.group(1)).strip()
    subs_tok = _re1(r'"content":"([\d.,]+[KMB]?)\s*subscribers?"', main_html) or ""
    videos_tok = _re1(r'"content":"([\d.,]+[KMB]?)\s*videos?"', main_html) or ""
    views_raw = [
        int(x.replace(",", ""))
        for x in re.findall(r"([\d,]+)\s+views", about_html or "")
    ]
    views = max(views_raw) if views_raw else 0
    return {
        "name": _jstr(name),
        "handle": handle or "",
        "bio": bio,
        "avatar": avatar,
        "stats": (
            f"{subs_tok or '—'} Subscribers",
            f"{videos_tok or '—'} Videos",
            f"{compact(views)} Views",
        ),
    }


def _parse_instagram(payload: Dict[str, Any]) -> Dict[str, Any]:
    """web_profile_info response → profile dict."""
    user = (payload.get("data") or {}).get("user") or payload.get("user") or {}
    handle = user.get("username") or ""
    if not handle:
        raise ProfileError("user not found")
    return {
        "name": user.get("full_name") or handle,
        "handle": f"@{handle}",
        "bio": (user.get("biography") or "").strip(),
        "avatar": user.get("profile_pic_url_hd") or user.get("profile_pic_url") or "",
        "stats": (
            f"{compact((user.get('edge_followed_by') or {}).get('count'))} Followers",
            f"{compact((user.get('edge_follow') or {}).get('count'))} Following",
            f"{compact((user.get('edge_owner_to_timeline_media') or {}).get('count'))} Posts",
        ),
    }


# ── Network fetchers ─────────────────────────────────────────────
def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={
            "User-Agent": _UA,
            "Accept-Language": "en-US,en;q=0.9",
        },
        timeout=_TIMEOUT,
        follow_redirects=True,
    )


async def _json(client: httpx.AsyncClient, url: str, **kwargs: Any) -> Dict[str, Any]:
    r = await client.get(url, **kwargs)
    try:
        data = r.json()
    except Exception as e:
        raise ProfileError(f"unexpected reply (HTTP {r.status_code})") from e
    if not isinstance(data, dict):
        raise ProfileError(f"unexpected reply (HTTP {r.status_code})")
    return data


async def _fetch_tiktok(client: httpx.AsyncClient, user: str) -> Dict[str, Any]:
    """tikwm user/info with paced retries.

    This is the host the media module already reaches from Railway.
    www.tiktok.com's own /api/user/detail answers with an HTML app
    shell (200) unless browser cookies are present, so it is useless
    as a data source — rate-limit codes and flaky pages are retried
    instead, then a friendly "busy" error is raised.
    """
    url = "https://www.tikwm.com/api/user/info"
    headers = {"Referer": "https://www.tikwm.com/", "Accept": "application/json"}
    timeout = httpx.Timeout(5.0, connect=4.0)
    for attempt in range(3):
        if attempt:
            await asyncio.sleep(_RETRY_DELAY)
        try:
            r = await client.get(url, params={"unique_id": user},
                                 headers=headers, timeout=timeout)
        except Exception:
            continue
        if r.status_code == 404:
            raise ProfileError("user not found")
        try:
            data = r.json()
        except Exception:
            continue  # challenge page / rate-limit HTML
        if not isinstance(data, dict):
            continue
        if data.get("code") == 0:
            return _parse_tikwm(data)
        msg = str(data.get("msg") or "").lower()
        if "not found" in msg or "exist" in msg:
            raise ProfileError("user not found")
        # any other non-zero code = busy / rate limit → pace and retry
    raise ProfileError("TikTok is not responding right now — try again "
                       "in a few seconds.")


async def _fetch_x(client: httpx.AsyncClient, user: str) -> Dict[str, Any]:
    """fxtwitter profile JSON (fast, no auth) → syndication fallback."""
    # 1) api.fxtwitter.com — small JSON, usually reachable anywhere.
    try:
        r = await client.get(
            f"https://api.fxtwitter.com/{user}",
            headers={"Accept": "application/json", "Referer": "https://x.com/"},
            timeout=httpx.Timeout(6.0, connect=4.0),
        )
        if r.status_code == 200 and r.text.lstrip().startswith("{"):
            data = r.json()
            if data.get("code") == 200 and data.get("user"):
                return _parse_fxtwitter(data)
    except Exception:
        pass
    # 2) syndication timeline page (works from some IPs, blocks others).
    try:
        r = await client.get(
            f"https://syndication.twitter.com/srv/timeline-profile/"
            f"screen-name/{user}",
            headers={"Referer": "https://twitter.com/", "Accept": "text/html,*/*"},
        )
        if r.status_code == 404:
            raise ProfileError("user not found")
        if r.status_code == 200 and '"screen_name"' in r.text:
            return _parse_x(r.text)
    except ProfileError:
        raise
    except Exception:
        pass
    raise ProfileError("Could not reach X right now — try again in a moment.")


async def _fetch_youtube(client: httpx.AsyncClient, user: str) -> Dict[str, Any]:
    url = f"https://www.youtube.com/@{user}"
    main_r, about_r = await asyncio.gather(
        client.get(url), client.get(f"{url}/about")
    )
    if main_r.status_code == 404:
        raise ProfileError("channel not found")
    if main_r.status_code != 200:
        raise ProfileError("could not reach YouTube right now")
    return _parse_youtube(main_r.text, about_r.text if about_r.status_code == 200 else "")


async def _fetch_instagram(client: httpx.AsyncClient, user: str) -> Dict[str, Any]:
    headers = {
        "X-IG-App-ID": "936619743392459",
        "X-ASBD-ID": "129477",
        "Accept": "*/*",
        "Referer": f"https://www.instagram.com/{user}/",
    }
    urls = (
        "https://www.instagram.com/api/v1/users/web_profile_info/",
        "https://i.instagram.com/api/v1/users/web_profile_info/",
    )
    last: Optional[Exception] = None
    for base in urls:
        for _try in (0, 1):  # one patient retry per host on 429/503
            try:
                r = await client.get(base, params={"username": user},
                                     headers=headers)
            except Exception as e:
                last = e
                break
            if r.status_code == 200 and r.text.strip().startswith("{"):
                try:
                    return _parse_instagram(r.json())
                except ProfileError:
                    raise
                except Exception as e:
                    last = e
                    break
            if r.status_code == 404:
                raise ProfileError("user not found")
            if r.status_code in (429, 503):
                last = ProfileError(
                    "Instagram is rate-limiting this server — try again "
                    "in a few minutes."
                )
                await asyncio.sleep(_IG_DELAY)
                continue
            last = ProfileError(
                "Instagram is not responding right now — try again "
                "in a few minutes."
            )
            break  # non-retryable status on this host — try the next one
    if isinstance(last, ProfileError):
        raise last
    raise ProfileError("Could not reach Instagram right now — try again "
                       "in a moment.")


async def _fetch_avatar(client: httpx.AsyncClient, url: str) -> Optional[bytes]:
    if not url:
        return None
    try:
        r = await client.get(url, timeout=httpx.Timeout(6.0))
        if r.status_code == 200 and r.content:
            return r.content
    except Exception as e:
        logger.debug(f"[PROFILES] avatar fetch failed: {e}")
    return None


async def _lookup(platform: str, user: str) -> Dict[str, Any]:
    key = (platform, user.lower())
    hit = _CACHE.get(key)
    if hit and time.monotonic() - hit[0] < _CACHE_TTL:
        return hit[1]
    async with _client() as client:
        if platform == "tiktok":
            prof = await _fetch_tiktok(client, user)
        elif platform == "x":
            prof = await _fetch_x(client, user)
        elif platform == "youtube":
            prof = await _fetch_youtube(client, user)
        else:
            prof = await _fetch_instagram(client, user)
    _CACHE[key] = (time.monotonic(), prof)
    if len(_CACHE) > 512:  # cheap bound — drop oldest half
        for old in sorted(_CACHE, key=lambda k: _CACHE[k][0])[:256]:
            _CACHE.pop(old, None)
    return prof


# ── Card rendering (Pillow) ──────────────────────────────────────
def _font(px: int):  # noqa: ANN202 - PIL type is Any for pyflakes peace
    from PIL import ImageFont

    return ImageFont.truetype(str(_FONT_PATH), px)


def _fit_font(draw: Any, text: str, max_w: int, start: int, min_px: int):  # noqa: ANN201
    px = start
    f = _font(px)
    while px > min_px and draw.textlength(text, font=f) > max_w:
        px -= 2
        f = _font(px)
    return f


def _ellipsize(draw: Any, text: str, font: Any, max_w: int) -> str:
    if not text:
        return ""
    if draw.textlength(text, font=font) <= max_w:
        return text
    trimmed = text
    while trimmed and draw.textlength(trimmed + "…", font=font) > max_w:
        trimmed = trimmed[:-1]
    trimmed = trimmed.rstrip()
    return (trimmed + "…") if trimmed else "…"


def _ink_dy(draw: Any, text: str, font: Any, anchor: str) -> int:
    """Vertical shift so the visible glyph ink centres on the anchor.

    ``anchor="lm"/"mm"`` centre the line box (ascent+descent), which sits
    a pixel or two below the ink centre when the text has descenders —
    enough to poke outside the bar.  Measure the real ink bbox and nudge.
    """
    if not text:
        return 0
    _, top, _, bottom = draw.textbbox((0, 0), text, font=font, anchor=anchor)
    return -((top + bottom) // 2)


def _wrap(draw: Any, text: str, font: Any, max_w: int, max_lines: int) -> List[str]:
    """Greedy word-wrap into ≤ max_lines, last line ellipsized."""
    words = [w for w in re.split(r"\s+", (text or "").strip()) if w]
    if not words:
        return []
    lines: List[str] = []
    cur = ""
    for w in words:
        cand = f"{cur} {w}".strip()
        if draw.textlength(cand, font=font) <= max_w:
            cur = cand
        else:
            if cur:
                lines.append(cur)
            cur = w
            if len(lines) >= max_lines:
                break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    lines = lines[:max_lines]
    return [_ellipsize(draw, ln, font, max_w) for ln in lines]


def _cover_crop(im: Any, w: int, h: int) -> Any:
    """Center-crop *im* to exactly w×h (fill, no distortion)."""
    iw, ih = im.size
    scale = max(w / iw, h / ih)
    im = im.resize((max(1, round(iw * scale)), max(1, round(ih * scale))))
    left = (im.size[0] - w) // 2
    top = (im.size[1] - h) // 2
    return im.crop((left, top, left + w, top + h))


def render_card(platform: str, prof: Dict[str, Any], avatar: Optional[bytes]) -> bytes:
    """Compose *prof* onto its template → PNG bytes.

    Every text element is font-shrunk / ellipsized to stay inside its
    measured slot — values never overflow the bars or pills.
    """
    from PIL import Image, ImageDraw

    geom = _GEOM[platform]
    base = Image.open(_TEMPLATES / _PLATFORMS[platform]["template"]).convert("RGB")
    draw = ImageDraw.Draw(base)

    # Avatar — rounded square inside the template's neon border.
    ax0, ay0, ax1, ay1 = geom["avatar"]
    ins = geom["inset"]
    ix0, iy0, ix1, iy1 = ax0 + ins, ay0 + ins, ax1 - ins, ay1 - ins
    if avatar:
        try:
            av = Image.open(io.BytesIO(avatar)).convert("RGB")
            av = _cover_crop(av, ix1 - ix0, iy1 - iy0)
            mask = Image.new("L", av.size, 0)
            ImageDraw.Draw(mask).rounded_rectangle(
                (0, 0, av.size[0] - 1, av.size[1] - 1),
                radius=geom["radius"],
                fill=255,
            )
            base.paste(av, (ix0, iy0), mask)
        except Exception as e:  # corrupt avatar image — card still ships
            logger.debug(f"[PROFILES] avatar paste failed: {e}")

    name_b, handle_b, bio_b = geom["bars"]

    # Name (bold white, left-aligned inside bar 1).
    name = (prof.get("name") or "").strip() or "Unknown"
    name_f = _fit_font(draw, name, (name_b[2] - name_b[0]) - 44, 52, 30)
    name_txt = _ellipsize(draw, name, name_f, (name_b[2] - name_b[0]) - 44)
    draw.text(
        ((name_b[0] + 22),
         (name_b[1] + name_b[3]) // 2 + _ink_dy(draw, name_txt, name_f, "lm")),
        name_txt, font=name_f, fill=_C_NAME, anchor="lm",
    )

    # Handle (grey, inside bar 2).
    handle = (prof.get("handle") or "").strip()
    if handle:
        handle_f = _fit_font(draw, handle, (handle_b[2] - handle_b[0]) - 44, 36, 24)
        handle_txt = _ellipsize(draw, handle, handle_f,
                                (handle_b[2] - handle_b[0]) - 44)
        draw.text(
            ((handle_b[0] + 22),
             (handle_b[1] + handle_b[3]) // 2
             + _ink_dy(draw, handle_txt, handle_f, "lm")),
            handle_txt, font=handle_f, fill=_C_HANDLE, anchor="lm",
        )

    # Bio — line 1 inside bar 3, line 2 directly below (max 2 lines).
    bio = (prof.get("bio") or "").strip()
    if bio:
        bio = re.sub(r"\s+", " ", bio)
        bio_w = (bio_b[2] - bio_b[0]) - 44
        bio_f = _fit_font(draw, bio, bio_w, 30, 22)
        lines = _wrap(draw, bio, bio_f, bio_w, 2)
        if lines:
            draw.text(
                ((bio_b[0] + 22),
                 (bio_b[1] + bio_b[3]) // 2
                 + _ink_dy(draw, lines[0], bio_f, "lm")),
                lines[0], font=bio_f, fill=_C_BIO, anchor="lm",
            )
        if len(lines) > 1:
            line2_y = bio_b[3] + 16 + (bio_f.size // 2)
            draw.text(
                ((bio_b[0] + 22),
                 line2_y + _ink_dy(draw, lines[1], bio_f, "lm")),
                lines[1], font=bio_f, fill=_C_BIO, anchor="lm",
            )

    # Stat pills — value + label centered, shrink-to-fit per pill.
    for pill, stat in zip(geom["pills"], prof.get("stats") or ()):
        if stat is None:
            continue
        stat = str(stat).strip()
        if not stat:
            continue
        px0, py0, px1, py1 = pill
        inner_w = (px1 - px0) - 36
        stat_f = _fit_font(draw, stat, inner_w, 34, 17)
        stat_txt = _ellipsize(draw, stat, stat_f, inner_w)
        draw.text(
            ((px0 + px1) // 2,
             (py0 + py1) // 2 + _ink_dy(draw, stat_txt, stat_f, "mm")),
            stat_txt, font=stat_f, fill=_C_STAT, anchor="mm",
        )

    out = io.BytesIO()
    base.save(out, format="PNG")
    return out.getvalue()


# ── Commands ─────────────────────────────────────────────────────
def _clean_handle(raw: str) -> str:
    return (raw or "").strip().lstrip("@").rstrip("/")


async def _profile_command(
    message: Message, bot: Any, args: list, platform: str
) -> None:
    if not message or not message.chat:
        return
    spec = _PLATFORMS[platform]
    user = _clean_handle(args[0] if args else "")
    if not user or not re.fullmatch(r"[A-Za-z0-9._]{1,40}", user):
        await reply_text(
            message,
            error_card(
                "Profile Search",
                escape(f"Usage: /{spec['cmd']} <username>"),
                icon=E.INFO,
            ),
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        prof = await _lookup(platform, user)
    except ProfileError as e:
        await reply_text(
            message,
            error_card(
                f"{spec['label']} Profile",
                escape(str(e)),
                icon=E.ERROR,
            ),
            parse_mode=ParseMode.HTML,
        )
        return
    except Exception as e:  # network/parse surprises stay user-friendly
        logger.warning(f"[PROFILES] {platform}@{user} failed: {e}")
        await reply_text(
            message,
            error_card(
                f"{spec['label']} Profile",
                escape("Lookup failed — try again in a moment."),
                icon=E.ERROR,
            ),
            parse_mode=ParseMode.HTML,
        )
        return

    avatar: Optional[bytes] = None
    try:
        async with _client() as client:
            avatar = await _fetch_avatar(client, prof.get("avatar") or "")
    except Exception:
        avatar = None

    try:
        photo_bytes = render_card(platform, prof, avatar)
    except Exception as e:
        logger.warning(f"[PROFILES] render failed {platform}@{user}: {e}")
        await reply_text(
            message,
            error_card(
                f"{spec['label']} Profile",
                escape("Card rendering failed — try again."),
                icon=E.ERROR,
            ),
            parse_mode=ParseMode.HTML,
        )
        return

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [btn_url(spec["button"], spec["url"].format(u=user), style="primary")],
        ]
    )
    await reply_photo(
        message,
        photo=BufferedInputFile(photo_bytes, filename=f"{user}.png"),
        reply_markup=kb,
    )


async def tiktok_command(message: Message, bot: Any, args: list) -> None:
    await _profile_command(message, bot, args, "tiktok")


async def x_command(message: Message, bot: Any, args: list) -> None:
    await _profile_command(message, bot, args, "x")


async def yt_command(message: Message, bot: Any, args: list) -> None:
    await _profile_command(message, bot, args, "youtube")


async def ig_command(message: Message, bot: Any, args: list) -> None:
    await _profile_command(message, bot, args, "instagram")


def setup() -> list[str]:
    """Register the four profile search commands (groups + DMs)."""
    on("message", tiktok_command, flt=cmd("tiktok"))
    on("message", x_command, flt=cmd("x"))
    on("message", yt_command, flt=cmd("yt"))
    on("message", ig_command, flt=cmd("ig"))
    logger.info("[PROFILES] registered /tiktok /x /yt /ig")
    return ["/tiktok", "/x", "/yt", "/ig"]
