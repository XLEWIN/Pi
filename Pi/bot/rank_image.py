# smash/modules/utils/rank_image.py
"""
Dynamic rank-card renderer for /myrank.

Reproduces the reference layout: 1708x750 dark anime/gaming profile dashboard.
Every value (name, username, avatar, level, progress, rank, statistics,
background artwork) is passed in by the caller — nothing is hardcoded around
placeholder data. Rendered at 2x supersampling and downscaled for smooth edges.
"""

import os
import logging
import random
import threading
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageEnhance

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS_DIR = os.path.join(BASE_DIR, "bot", "assets")
OUTPUT_DIR = os.path.join(BASE_DIR, "temp_profiles")

# Optional ambient artwork; if absent the caller may pass an avatar image to
# reuse as the watermark background, otherwise a procedural backdrop is drawn.
BG_IMAGE_PATH = os.path.join(ASSETS_DIR, "rank_bg.png")
BG_IMAGE_PATH_ALT = os.path.join(ASSETS_DIR, "rank_bg.jpg")

if not os.path.exists(OUTPUT_DIR):
    os.makedirs(OUTPUT_DIR, exist_ok=True)

# ----------------------------- palette --------------------------------------
COLOR_BG = (14, 14, 14)          # 0E0E0E
COLOR_BURGUNDY = (100, 19, 22)   # 641316  statistic pills
COLOR_TRACK = (128, 21, 25)      # 801519  progress track
COLOR_RED = (229, 37, 40)        # E52528  progress fill
COLOR_BRIGHT = (239, 41, 44)     # EF292C  badge / icons circles / indicator
COLOR_WHITE = (245, 245, 245)    # F5F5F5
COLOR_MUTED = (220, 220, 220)    # DCDCDC
COLOR_ICON = (8, 8, 8)           # 080808

# ----------------------------- canvas metrics -------------------------------
CANVAS_W, CANVAS_H = 1708, 750
PAD_X = 95

AVATAR_SIZE = 185
AVATAR_RADIUS = 38
AVATAR_X, AVATAR_Y = PAD_X, 78

BADGE_W, BADGE_H, BADGE_RADIUS = 88, 58, 20
BADGE_OFFSET_X, BADGE_OFFSET_Y = -26, -46   # relative to avatar bottom-left corner

TEXT_STACK_X = AVATAR_X + AVATAR_SIZE + 48
NAME_SIZE, USERNAME_SIZE = 40, 36

LEVEL_LABEL_Y = 340
LEVEL_TEXT_SIZE = 34

BAR_X, BAR_Y, BAR_W, BAR_H = PAD_X, 392, CANVAS_W - 2 * PAD_X, 48
INDICATOR_R = 34
NEXT_NUM_PAD = 40

HEADINGS_Y = 502
HEADING_SIZE = 32
PILL_Y = 550
PILL_W, PILL_H, PILL_RADIUS = 370, 95, 48
ICON_DIA, ICON_MARGIN_X = 70, 11
VALUE_SIZE = 38
COL_STEP = 506

SCALE = 2


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
    key = size
    if key not in _font_cache:
        try:
            _font_cache[key] = ImageFont.truetype(_FONT_FILE, size)
        except Exception:
            _font_cache[key] = ImageFont.load_default()
    return _font_cache[key]


def _fit_text(draw, text, font_size, max_width):
    """Shrink/truncate with an ellipsis so text never overflows its slot."""
    if text is None:
        text = ""
    text = str(text)
    font = _font(font_size)
    while draw.textlength(text, font=font) > max_width and len(text) > 1:
        text = text[:-2].rstrip() + "\u2026"
        if len(text) <= 2:
            break
    return text, font


def _rounded(draw, box, radius, fill):
    draw.rounded_rectangle(box, radius=radius, fill=fill)


# ------------------------------ icons ---------------------------------------
def _icon_bar_chart(d, cx, cy):
    s = SCALE
    bw, gap = 12 * s, 12 * s
    x0 = cx - (bw * 3 + gap * 2) // 2
    heights = [22 * s, 38 * s, 54 * s]
    base = cy + 27 * s
    for i, h in enumerate(heights):
        bx = x0 + i * (bw + gap)
        d.rounded_rectangle((bx, base - h, bx + bw, base), radius=5 * s, fill=COLOR_ICON)


def _icon_chat(d, cx, cy):
    s = SCALE
    bw, bh = 56 * s, 42 * s
    x0, y0 = cx - bw // 2, cy - bh // 2 - 3 * s
    d.rounded_rectangle((x0, y0, x0 + bw, y0 + bh), radius=13 * s, fill=COLOR_ICON)
    d.polygon(
        [(x0 + 12 * s, y0 + bh - 4 * s), (x0 + 30 * s, y0 + bh - 4 * s), (x0 + 14 * s, y0 + bh + 14 * s)],
        fill=COLOR_ICON,
    )


def _icon_globe(d, cx, cy):
    s = SCALE
    r, lw = 28 * s, 5 * s
    d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=COLOR_ICON, width=lw)
    d.line((cx - r, cy, cx + r, cy), fill=COLOR_ICON, width=lw)
    d.ellipse((cx - int(r * 0.45), cy - r, cx + int(r * 0.45), cy + r), outline=COLOR_ICON, width=lw)


ICONS = {"bar": _icon_bar_chart, "chat": _icon_chat, "globe": _icon_globe}


# ---------------------------- background ------------------------------------
def _cover_resize(img, size):
    tw, th = size
    iw, ih = img.size
    scale = max(tw / iw, th / ih)
    img = img.resize((max(1, int(iw * scale)), max(1, int(ih * scale))), Image.LANCZOS)
    left = (img.width - tw) // 2
    top = (img.height - th) // 2
    return img.crop((left, top, left + tw, top + th))


def _ambient_artwork(avatar_img):
    """Return the heavily-darkened, blurred, red-tinted artwork layer."""
    src = None
    for path in (BG_IMAGE_PATH, BG_IMAGE_PATH_ALT):
        if os.path.exists(path):
            try:
                src = Image.open(path).convert("RGB")
                break
            except Exception:
                pass
    if src is None and avatar_img is not None:
        src = avatar_img.convert("RGB").resize((760, 760), Image.LANCZOS)
    if src is None:
        return None

    art = _cover_resize(src, (CANVAS_W, CANVAS_H))
    art = art.filter(ImageFilter.GaussianBlur(7))
    art = ImageEnhance.Brightness(art).enhance(0.42)
    art = ImageEnhance.Color(art).enhance(0.55)
    tint = Image.new("RGB", art.size, COLOR_BURGUNDY)
    art = Image.blend(art, tint, 0.32)
    return art


_CONST_LOCK = threading.Lock()
_const_cache = {}


def _constant_layers():
    """Glow / noise / vignette are user-independent — build them exactly once."""
    with _CONST_LOCK:
        if "vignette" in _const_cache:
            return _const_cache

        glow = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow)
        gd.ellipse((CANVAS_W * 0.42, -CANVAS_H * 0.55, CANVAS_W * 1.35, CANVAS_H * 0.85),
                   fill=COLOR_RED + (16,))
        glow = glow.filter(ImageFilter.GaussianBlur(180))

        noise = Image.effect_noise((CANVAS_W, CANVAS_H), 10).convert("L")
        noise_mask = noise.point(lambda v: 14 if v > 200 else 0)

        vignette = Image.new("L", (CANVAS_W, CANVAS_H), 0)
        vd = ImageDraw.Draw(vignette)
        vd.rectangle((0, 0, CANVAS_W, CANVAS_H), fill=90)
        vd.ellipse((-CANVAS_W * 0.18, -CANVAS_H * 0.30, CANVAS_W * 1.18, CANVAS_H * 1.30), fill=0)
        vignette = vignette.filter(ImageFilter.GaussianBlur(120))

        _const_cache["glow"] = glow
        _const_cache["noise"] = noise_mask
        _const_cache["vignette"] = vignette
        return _const_cache


def _build_background(avatar_img):
    glow_layer = _constant_layers()["glow"]
    noise_mask = _constant_layers()["noise"]
    vignette_mask = _constant_layers()["vignette"]

    canvas = Image.new("RGBA", (CANVAS_W, CANVAS_H), COLOR_BG + (255,))
    art = _ambient_artwork(avatar_img)
    if art is not None:
        gray = art.convert("L")
        mask = gray.point(lambda v: int(v * 0.20))          # watermark-level visibility
        canvas.paste(art, (0, 0), mask)

    canvas = Image.alpha_composite(canvas, glow_layer)

    bright = ImageEnhance.Brightness(canvas).enhance(1.02)
    canvas = Image.composite(bright, canvas, noise_mask)

    canvas = Image.composite(Image.new("RGBA", canvas.size, (0, 0, 0, 255)), canvas, vignette_mask)
    return canvas


# --------------------------- avatar + badge ---------------------------------
def _paste_avatar(canvas, avatar_img):
    draw = ImageDraw.Draw(canvas)
    ax, ay, s = AVATAR_X, AVATAR_Y, AVATAR_SIZE

    if avatar_img is not None:
        av = avatar_img.convert("RGB").resize((s * SCALE, s * SCALE), Image.LANCZOS).convert("RGBA")
    else:
        ph = Image.new("RGB", (s * SCALE, s * SCALE), COLOR_BURGUNDY)
        pd = ImageDraw.Draw(ph)
        pd.text((s * SCALE // 2, s * SCALE // 2), "?", font=_font(90 * SCALE),
                fill=COLOR_WHITE, anchor="mm")
        av = ph.convert("RGBA")

    mask = Image.new("L", av.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, av.width - 1, av.height - 1),
                                           radius=AVATAR_RADIUS * SCALE, fill=255)
    av.putalpha(mask)                       # carry alpha so paste works on a clear overlay
    canvas.paste(av, (ax * SCALE, ay * SCALE))

    bx = ax + BADGE_OFFSET_X
    by = ay + AVATAR_SIZE + BADGE_OFFSET_Y
    _rounded(draw,
             (bx * SCALE, by * SCALE, (bx + BADGE_W) * SCALE, (by + BADGE_H) * SCALE),
             BADGE_RADIUS * SCALE, COLOR_BRIGHT)
    return bx, by


def _draw_badge_text(canvas, badge_x, badge_y, rank_text):
    draw = ImageDraw.Draw(canvas)
    text, size = str(rank_text), 30
    while size > 20 and draw.textlength(text, font=_font(size * SCALE)) > (BADGE_W - 16) * SCALE:
        size -= 2
    draw.text(((badge_x + BADGE_W / 2) * SCALE, (badge_y + BADGE_H / 2) * SCALE),
              text, font=_font(size * SCALE), fill=COLOR_WHITE, anchor="mm")


# ------------------------------- card ---------------------------------------
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
):
    """
    Build the rank card.

    All display values are supplied dynamically:
      name / username       profile header strings
      avatar_path           square source image (any size) or None
      level / next_level    current + upcoming level numbers
      progress_pct          0-100 fill of the level bar
      rank_text             badge string (e.g. "#7")
      messages              statistic pill 2 value
      global_messages       statistic pill 3 value
      output_path           where the PNG is written
    Returns output_path on success, None on failure.
    """
    try:
        avatar_img = None
        if avatar_path and os.path.exists(avatar_path):
            try:
                avatar_img = Image.open(avatar_path)
            except Exception as e:
                logger.warning(f"[rank_image] avatar load failed: {e}")

        # Foreground is drawn on a clear supersampled overlay; the atmospheric
        # background stays at 1x and is composited underneath at the end.
        canvas = Image.new("RGBA", (CANVAS_W * SCALE, CANVAS_H * SCALE), (0, 0, 0, 0))
        draw = ImageDraw.Draw(canvas)

        # ---- profile header ----
        badge_x, badge_y = _paste_avatar(canvas, avatar_img)

        name_txt, name_font = _fit_text(draw, str(name).upper(), NAME_SIZE * SCALE,
                                        (CANVAS_W - TEXT_STACK_X - PAD_X) * SCALE)
        user_txt, user_font = _fit_text(draw, username, USERNAME_SIZE * SCALE,
                                        (CANVAS_W - TEXT_STACK_X - PAD_X) * SCALE)
        draw.text((TEXT_STACK_X * SCALE, (AVATAR_Y + 62) * SCALE), name_txt,
                  font=name_font, fill=COLOR_WHITE, anchor="lm")
        draw.text((TEXT_STACK_X * SCALE, (AVATAR_Y + 122) * SCALE), user_txt,
                  font=user_font, fill=COLOR_BRIGHT, anchor="lm")

        _draw_badge_text(canvas, badge_x, badge_y, rank_text)

        # ---- level section ----
        label = "Current Level:"
        lf = _font(LEVEL_TEXT_SIZE * SCALE)
        draw.text((PAD_X * SCALE, LEVEL_LABEL_Y * SCALE), label,
                  font=lf, fill=COLOR_WHITE, anchor="lm")
        num_w = draw.textlength(str(level), font=lf)
        draw.text(((PAD_X + 16) * SCALE + draw.textlength(label, font=lf), LEVEL_LABEL_Y * SCALE),
                  str(level), font=lf, fill=COLOR_BRIGHT, anchor="lm")

        tx, ty, tw, th = BAR_X, BAR_Y, BAR_W, BAR_H
        _rounded(draw, (tx * SCALE, ty * SCALE, (tx + tw) * SCALE, (ty + th) * SCALE),
                 th // 2 * SCALE, COLOR_TRACK)

        pct = max(0.0, min(100.0, float(progress_pct or 0)))
        fill_w = int(tw * pct / 100)
        fill_w = min(max(fill_w, th // 2), tw - INDICATOR_R // 2)
        if pct > 0:
            _rounded(draw, (tx * SCALE, ty * SCALE, (tx + fill_w) * SCALE, (ty + th) * SCALE),
                     th // 2 * SCALE, COLOR_RED)
        icx = min(tx + fill_w, tx + tw - INDICATOR_R - 6)
        icy = ty + th // 2
        draw.ellipse(((icx - INDICATOR_R) * SCALE, (icy - INDICATOR_R) * SCALE,
                      (icx + INDICATOR_R) * SCALE, (icy + INDICATOR_R) * SCALE),
                     fill=COLOR_BRIGHT)

        nf = _font(NEXT_NUM_PAD * SCALE)
        num_txt = str(next_level)
        # Only draw the upcoming-level number when it clears the indicator dot.
        num_left = tx + tw - NEXT_NUM_PAD - draw.textlength(num_txt, font=nf) / SCALE
        if num_left > icx + INDICATOR_R + 14:
            draw.text(((tx + tw - NEXT_NUM_PAD) * SCALE, icy * SCALE), num_txt,
                      font=nf, fill=COLOR_WHITE, anchor="rm")

        # ---- statistics ----
        stats = [
            ("RANK", str(rank_text), "bar"),
            ("MESSAGES", messages, "chat"),
            ("GLOBAL MESSAGES", global_messages, "globe"),
        ]
        hf = _font(HEADING_SIZE * SCALE)
        vf = _font(VALUE_SIZE * SCALE)
        for i, (heading, value, icon_key) in enumerate(stats):
            col_x = PAD_X + i * COL_STEP
            draw.text((col_x * SCALE, HEADINGS_Y * SCALE), heading,
                      font=hf, fill=COLOR_WHITE, anchor="lm")

            px, py = col_x, PILL_Y
            _rounded(draw, (px * SCALE, py * SCALE, (px + PILL_W) * SCALE, (py + PILL_H) * SCALE),
                     PILL_RADIUS * SCALE, COLOR_BURGUNDY)

            ccx = px + ICON_MARGIN_X + ICON_DIA // 2
            ccy = py + PILL_H // 2
            draw.ellipse(((ccx - ICON_DIA // 2) * SCALE, (ccy - ICON_DIA // 2) * SCALE,
                          (ccx + ICON_DIA // 2) * SCALE, (ccy + ICON_DIA // 2) * SCALE),
                         fill=COLOR_BRIGHT)
            ICONS[icon_key](draw, ccx * SCALE, ccy * SCALE)

            val_txt, _ = _fit_text(draw, str(value), VALUE_SIZE * SCALE,
                                   (PILL_W - ICON_DIA - ICON_MARGIN_X - 34) * SCALE)
            draw.text(((px + ICON_MARGIN_X + ICON_DIA + 24) * SCALE, ccy * SCALE),
                      val_txt, font=vf, fill=COLOR_WHITE, anchor="lm")

        # ---- compose: background -> soft shadows -> foreground ----
        fg = canvas.resize((CANVAS_W, CANVAS_H), Image.LANCZOS)

        shadow = Image.new("L", (CANVAS_W, CANVAS_H), 0)
        sd = ImageDraw.Draw(shadow)
        sd.rounded_rectangle((BAR_X, BAR_Y + 4, BAR_X + BAR_W, BAR_Y + BAR_H + 4),
                             radius=BAR_H // 2, fill=60)
        for i in range(3):
            sx = PAD_X + i * COL_STEP
            sd.rounded_rectangle((sx, PILL_Y + 7, sx + PILL_W, PILL_Y + PILL_H + 7),
                                 radius=PILL_RADIUS, fill=80)
        shadow = shadow.filter(ImageFilter.GaussianBlur(5))

        out = _build_background(avatar_img)
        out = Image.composite(Image.new("RGBA", out.size, (0, 0, 0, 255)), out, shadow)
        out = Image.alpha_composite(out, fg)

        final = out.convert("RGB")
        final.save(output_path, "PNG", compress_level=1)
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
        rank_text="#3",
        messages="1,240",
        global_messages="9,483",
        output_path=out,
        avatar_path=None,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(build_sample())
