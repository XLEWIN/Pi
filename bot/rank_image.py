# smash/modules/utils/rank_image.py
"""
Dynamic rank-card renderer for /rank.

The card is built on top of one of six pre-rendered master templates in
``bot/templates/template_<id>.png`` (1708 x 750). Each template already
contains the full dashboard — avatar slot, name/username pills, LEVEL
box, progress track and the three stat cards — so PIL only overlays the
live values (avatar, name, username, level, progress, statistics) on a
2x supersampled layer and composites it down for smooth, crisp text.

Template geometry (measured once from the master art, 1708 x 750):

    avatar slot      (67, 128) - (238, 292)
    name pill        x=291, baseline center y=184
    username pill    x=291, center y=257
    level box        center (1597, 187)
    progress labels  centers (119, 361) and (1597, 361)
    progress bar     x 194 -> 1522, y 357 -> 377
    stat cards       text x = 90 / 637 / 1164, values y=552, subs y=605

The layout is identical across every template — only the artwork, glow
colour and card tint change — so text lands in the same place always.
"""

import os
import logging
import threading
from PIL import Image, ImageDraw, ImageFilter, ImageFont

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS_DIR = os.path.join(BASE_DIR, "bot", "assets")
TEMPLATES_DIR = os.path.join(BASE_DIR, "bot", "templates")
OUTPUT_DIR = os.path.join(BASE_DIR, "temp_profiles")

if not os.path.exists(OUTPUT_DIR):
    os.makedirs(OUTPUT_DIR, exist_ok=True)

# ----------------------------- canvas ---------------------------------------
CANVAS_W, CANVAS_H = 1708, 750
SCALE = 2  # foreground overlay supersampling

# --------------------------- fixed geometry ---------------------------------
AVATAR_BOX = (67, 128, 238, 292)      # inner area of the avatar slot
AVATAR_RADIUS = 36

NAME_X, NAME_CY, NAME_SIZE, NAME_MAX_W = 291, 184, 46, 440
USER_X, USER_CY, USER_SIZE, USER_MAX_W = 291, 257, 26, 178

LEVEL_CX, LEVEL_CY, LEVEL_SIZE, LEVEL_MAX_W = 1597, 187, 62, 96

LEFT_LABEL_C, RIGHT_LABEL_C = (119, 361), (1597, 361)
LABEL_SIZE, LABEL_MAX_W = 20, 102

BAR_X0, BAR_Y0, BAR_X1, BAR_Y1 = 194, 357, 1522, 377

CARD_TEXT_X = (90, 637, 1164)         # left padding of the three cards
CARD_MAX_W = (450, 435, 450)
VALUE_CY, SUB_CY = 552, 605
VALUE_SIZE, SUB_SIZE = 50, 24

# ----------------------------- colours --------------------------------------
WHITE = (245, 245, 245, 255)
MUTED = (190, 190, 198, 255)
LABEL = (235, 235, 240, 255)
PLACEHOLDER = (150, 150, 158, 255)

# Per-template palette: accent = text accent, fill = progress-bar fill,
# track = progress-bar empty track (sampled from the master templates).
THEME_COLORS = {
    1: {"accent": (250, 175, 70),  "fill": (250, 163, 47), "track": (12, 6, 1)},
    2: {"accent": (224, 233, 245), "fill": (224, 233, 245), "track": (16, 17, 22)},
    3: {"accent": (236, 150, 252), "fill": (233, 139, 251), "track": (12, 4, 20)},
    4: {"accent": (96, 140, 252),  "fill": (57, 105, 239),  "track": (3, 7, 22)},
    5: {"accent": (170, 224, 253), "fill": (156, 218, 252), "track": (3, 4, 10)},
    6: {"accent": (150, 251, 185), "fill": (139, 250, 179), "track": (2, 6, 3)},
}


def _get_theme_colors(template_id: int) -> dict:
    """Theme palette for a template, falling back to template 1."""
    return THEME_COLORS.get(template_id, THEME_COLORS[1])


# ----------------------------- fonts ----------------------------------------
def _font_path():
    bold = os.path.join(ASSETS_DIR, "NotoSans-Bold.ttf")
    if os.path.exists(bold):
        return bold
    if os.name == "nt":
        return "arialbd.ttf"
    return "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


_FONT_FILE = _font_path()
_font_cache = {}


def _font(size):
    if size not in _font_cache:
        try:
            _font_cache[size] = ImageFont.truetype(_FONT_FILE, size)
        except Exception:
            _font_cache[size] = ImageFont.load_default()
    return _font_cache[size]


def _fit(draw, text, size, max_width):
    """Shrink the font so ``text`` never overflows its slot."""
    text = "" if text is None else str(text)
    if not text:
        return text, _font(size)
    while size > 12 and draw.textlength(text, font=_font(size)) > max_width:
        size -= 1
    font = _font(size)
    if draw.textlength(text, font=font) > max_width:
        while len(text) > 1 and draw.textlength(text + "\u2026", font=font) > max_width:
            text = text[:-1]
        text = text.rstrip() + "\u2026"
        font = _font(size)
    return text, font


# ----------------------------- templates ------------------------------------
_tpl_lock = threading.Lock()
_tpl_cache = {}


def _load_template(template_id: int) -> Image.Image:
    """Master template RGB image (cached; callers must not mutate it)."""
    with _tpl_lock:
        hit = _tpl_cache.get(template_id)
    if hit is not None:
        return hit
    path = os.path.join(TEMPLATES_DIR, f"template_{template_id}.png")
    try:
        img = Image.open(path).convert("RGB")
        if img.size != (CANVAS_W, CANVAS_H):
            img = img.resize((CANVAS_W, CANVAS_H), Image.LANCZOS)
    except Exception as e:
        logger.warning(f"[rank_image] template {template_id} load failed: {e}")
        img = Image.new("RGB", (CANVAS_W, CANVAS_H), (14, 14, 16))
    with _tpl_lock:
        _tpl_cache[template_id] = img
    return img


# ----------------------------- avatar ---------------------------------------
def _paste_avatar(overlay, avatar_path):
    """Center-crop + rounded-mask the avatar into the template's slot."""
    x0, y0, x1, y1 = AVATAR_BOX
    w, h = (x1 - x0) * SCALE, (y1 - y0) * SCALE

    src = None
    if avatar_path and os.path.exists(avatar_path):
        try:
            src = Image.open(avatar_path).convert("RGB")
        except Exception as e:
            logger.warning(f"[rank_image] avatar load failed: {e}")

    if src is None:
        d = ImageDraw.Draw(overlay)
        d.text(
            (((x0 + x1) // 2) * SCALE, ((y0 + y1) // 2) * SCALE),
            "?", font=_font(78 * SCALE), fill=PLACEHOLDER, anchor="mm",
        )
        return

    side = min(src.size)
    left = (src.width - side) // 2
    top = (src.height - side) // 2
    av = src.crop((left, top, left + side, top + side)).resize(
        (w, h), Image.LANCZOS
    ).convert("RGBA")

    mask = Image.new("L", av.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, w - 1, h - 1), radius=AVATAR_RADIUS * SCALE, fill=255
    )
    av.putalpha(mask)
    overlay.paste(av, (x0 * SCALE, y0 * SCALE), av)


# ----------------------------- progress bar ---------------------------------
def _bar_layer(pct: float, theme: dict):
    """Build the glowing progress bar (incl. feathered shade) for ``pct``.

    The shade neutralises the template's baked placeholder fill so the
    real progress can start from zero without a ghost glow.
    Returns (layer, paste_position).
    """
    s = SCALE
    bw, bh = (BAR_X1 - BAR_X0) * s, (BAR_Y1 - BAR_Y0) * s
    pad = 70 * s
    fill_rgb, track_rgb = theme["fill"], theme["track"]

    layer = Image.new("RGBA", (bw + 2 * pad, bh + 2 * pad), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.rounded_rectangle(
        (pad, pad - 26 * s, pad + bw, pad + bh + 26 * s),
        radius=30 * s, fill=(0, 0, 0, 170),
    )
    layer = layer.filter(ImageFilter.GaussianBlur(14 * s))

    fill_w = int(bw * max(0.0, min(100.0, pct)) / 100)
    if fill_w > 0:
        glow = Image.new("RGBA", layer.size, (0, 0, 0, 0))
        ImageDraw.Draw(glow).rounded_rectangle(
            (pad, pad - 5 * s, pad + max(fill_w, bh), pad + bh + 5 * s),
            radius=bh // 2, fill=fill_rgb + (175,),
        )
        glow = glow.filter(ImageFilter.GaussianBlur(11 * s))
        layer = Image.alpha_composite(layer, glow)

    d = ImageDraw.Draw(layer)
    d.rounded_rectangle(
        (pad, pad, pad + bw, pad + bh), radius=bh // 2, fill=track_rgb + (255,)
    )
    if fill_w > 4 * s:
        d.rounded_rectangle(
            (pad, pad, pad + fill_w, pad + bh),
            radius=min(bh // 2, fill_w // 2),
            fill=fill_rgb + (255,),
        )
    return layer, (BAR_X0 * s - pad, BAR_Y0 * s - pad)


# ------------------------------- card ---------------------------------------
def _split_rank(rank_text):
    """``#3/57`` -> (``#3``, ``of 57``) — value + context line."""
    text = "" if rank_text is None else str(rank_text)
    if "/" in text:
        value, _, total = text.partition("/")
        value, total = value.strip(), total.strip()
        return value, (f"of {total}" if total else "")
    return text, ""


def create_rank_card(
    name,
    username,
    level,
    next_level,
    progress_pct,
    rank_text,
    messages,
    global_messages,
    output_path,
    avatar_path=None,
    template_id=1,
):
    """
    Build the rank card over template_<template_id>.png.

    All display values are supplied dynamically:
      name / username       profile header strings
      avatar_path            square source image (any size) or None
      level / next_level     current + upcoming level numbers
      progress_pct           0-100 fill of the level bar
      rank_text              "rank/total" for stat card 1
      messages               statistic card 2 value
      global_messages        statistic card 3 value
      output_path            where the PNG is written
      template_id            theme template (1-6)
    Returns output_path on success, None on failure.
    """
    theme = _get_theme_colors(template_id)
    accent = theme["accent"] + (255,)

    try:
        base = _load_template(template_id)
        overlay = Image.new(
            "RGBA", (CANVAS_W * SCALE, CANVAS_H * SCALE), (0, 0, 0, 0)
        )
        draw = ImageDraw.Draw(overlay)

        # ---- profile header ----
        _paste_avatar(overlay, avatar_path)

        name_txt, name_font = _fit(
            draw, str(name).upper(), NAME_SIZE * SCALE, NAME_MAX_W * SCALE
        )
        if name_txt:
            draw.text((NAME_X * SCALE, NAME_CY * SCALE), name_txt,
                      font=name_font, fill=WHITE, anchor="lm")

        user_txt, user_font = _fit(
            draw, username, USER_SIZE * SCALE, USER_MAX_W * SCALE
        )
        if user_txt:
            draw.text((USER_X * SCALE, USER_CY * SCALE), user_txt,
                      font=user_font, fill=accent, anchor="lm")

        # ---- level box ----
        lvl_txt, lvl_font = _fit(
            draw, str(level), LEVEL_SIZE * SCALE, LEVEL_MAX_W * SCALE
        )
        draw.text((LEVEL_CX * SCALE, LEVEL_CY * SCALE), lvl_txt,
                  font=lvl_font, fill=accent, anchor="mm")

        # ---- progress labels + bar ----
        pct = max(0.0, min(100.0, float(progress_pct or 0)))
        lbl_txt, lbl_font = _fit(
            draw, f"{round(pct)}%", LABEL_SIZE * SCALE, LABEL_MAX_W * SCALE
        )
        draw.text((LEFT_LABEL_C[0] * SCALE, LEFT_LABEL_C[1] * SCALE), lbl_txt,
                  font=lbl_font, fill=LABEL, anchor="mm")

        nxt_txt, nxt_font = _fit(
            draw, f"NEXT: {next_level}", LABEL_SIZE * SCALE, LABEL_MAX_W * SCALE
        )
        draw.text((RIGHT_LABEL_C[0] * SCALE, RIGHT_LABEL_C[1] * SCALE), nxt_txt,
                  font=nxt_font, fill=LABEL, anchor="mm")

        bar, pos = _bar_layer(pct, theme)
        overlay.alpha_composite(bar, dest=pos)

        # ---- statistics ----
        rank_value, rank_sub = _split_rank(rank_text)
        cards = (
            (rank_value, rank_sub),
            (messages, "this chat"),
            (global_messages, "all chats"),
        )
        for i, (value, sub) in enumerate(cards):
            tx = CARD_TEXT_X[i]
            val_txt, val_font = _fit(
                draw, value, VALUE_SIZE * SCALE, CARD_MAX_W[i] * SCALE
            )
            draw.text((tx * SCALE, VALUE_CY * SCALE), val_txt,
                      font=val_font, fill=WHITE, anchor="lm")
            if sub:
                sub_txt, sub_font = _fit(
                    draw, sub, SUB_SIZE * SCALE, CARD_MAX_W[i] * SCALE
                )
                draw.text((tx * SCALE, SUB_CY * SCALE), sub_txt,
                          font=sub_font, fill=MUTED, anchor="lm")

        # ---- compose ----
        fg = overlay.resize((CANVAS_W, CANVAS_H), Image.LANCZOS)
        out = Image.alpha_composite(base.convert("RGBA"), fg).convert("RGB")
        out.save(output_path, "PNG", compress_level=1)
        return output_path

    except Exception as e:
        logger.error(f"[rank_image] card generation failed: {e}")
        return None


def build_sample():
    """Local preview with placeholder data (not used by the bot)."""
    out = os.path.join(OUTPUT_DIR, "rank_sample.png")
    return create_rank_card(
        name="Anirudh",
        username="@hey_anirudh",
        level=7,
        next_level=8,
        progress_pct=62,
        rank_text="#3/57",
        messages="1,240",
        global_messages="9,483",
        output_path=out,
        avatar_path=None,
        template_id=1,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(build_sample())
