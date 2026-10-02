# smash/modules/utils/rank_image.py
"""
Dynamic rank-card renderer for /rank.

The card is built on top of one of the pre-rendered master templates in
``bot/templates/template_<id>.png`` (1708 x 750). Each template already
contains the full dashboard — avatar slot, name/username pills, level
cells, progress track and the three stat cards — so PIL only overlays
the live values (avatar, name, username, level, progress, statistics)
on a 2x supersampled layer and composites it down for smooth, crisp
text.

Two geometry families exist:

v1 (templates 1-6) — flat cards:

    avatar slot      (67, 128) - (238, 292)
    name pill        x=291, baseline center y=184
    username pill    x=291, center y=257
    level box        center (1597, 187)
    progress labels  centers (119, 361) and (1597, 361)
    progress bar     x 194 -> 1522, y 357 -> 377
    stat cards       text x = 90 / 637 / 1164, values y=552, subs y=605

v2 (templates 7-19) — frosted dashboard (see ``V2``): dual level cells,
full-width bar, label chips that cover the baked percentage text, and
value/sub boxes with background-sampled ink.

Within each family the layout is identical across every template — only
the artwork, glow colour and card tint change — so text always lands in
the same place.
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
# track = progress-bar empty track (sampled from the master templates),
# layout = geometry family (v1 = original six, v2 = dashboard cards).
THEME_COLORS = {
    1:  {"accent": (250, 175, 70),  "fill": (250, 163, 47), "track": (12, 6, 1),   "layout": "v1"},
    2:  {"accent": (224, 233, 245), "fill": (224, 233, 245), "track": (16, 17, 22), "layout": "v1"},
    3:  {"accent": (236, 150, 252), "fill": (233, 139, 251), "track": (12, 4, 20),  "layout": "v1"},
    4:  {"accent": (96, 140, 252),  "fill": (57, 105, 239),  "track": (3, 7, 22),   "layout": "v1"},
    5:  {"accent": (170, 224, 253), "fill": (156, 218, 252), "track": (3, 4, 10),   "layout": "v1"},
    6:  {"accent": (150, 251, 185), "fill": (139, 250, 179), "track": (2, 6, 3),    "layout": "v1"},
    7:  {"accent": (150, 185, 225), "fill": (120, 143, 171), "track": (4, 14, 35),  "layout": "v2"},
    8:  {"accent": (240, 120, 120), "fill": (232, 185, 181), "track": (13, 5, 3),   "layout": "v2"},
    9:  {"accent": (230, 230, 230), "fill": (218, 218, 218), "track": (3, 3, 3),    "layout": "v2"},
    10: {"accent": (180, 180, 180), "fill": (138, 138, 138), "track": (6, 6, 6),    "layout": "v2"},
    11: {"accent": (225, 225, 225), "fill": (210, 210, 210), "track": (14, 14, 14), "layout": "v2"},
    12: {"accent": (225, 150, 235), "fill": (211, 172, 212), "track": (16, 8, 20),  "layout": "v2"},
    13: {"accent": (240, 140, 130), "fill": (231, 116, 114), "track": (19, 2, 1),   "layout": "v2"},
    14: {"accent": (170, 215, 240), "fill": (172, 211, 231), "track": (4, 8, 16),   "layout": "v2"},
    15: {"accent": (150, 190, 230), "fill": (179, 206, 225), "track": (8, 21, 42),  "layout": "v2"},
    16: {"accent": (200, 140, 136), "fill": (148, 107, 104), "track": (13, 5, 4),   "layout": "v2"},
    17: {"accent": (225, 225, 228), "fill": (216, 216, 216), "track": (4, 6, 9),    "layout": "v2"},
    18: {"accent": (185, 185, 185), "fill": (134, 134, 134), "track": (11, 11, 11), "layout": "v2"},
    19: {"accent": (170, 205, 235), "fill": (170, 202, 229), "track": (3, 8, 17),   "layout": "v2"},
}

# --------------------------- v2 geometry ------------------------------------
# Dashboard layout shared by templates 7-19 (1708 x 750; measured, all
# thirteen agree within a few px). Boxes are (x0, y0, x1, y1) unless noted.
V2 = {
    "avatar":        (59, 95, 246, 265),
    "avatar_radius": 30,
    "name":          (291, 149, 400),     # x, baseline-center y, max width
    "name_box":      (271, 116, 707, 182),
    "user":          (291, 222, 171),
    "user_box":      (271, 194, 474, 251),
    "cell_label":    (1526, 105, 1643, 166),   # upper cell: "LEVEL"
    "cell_value":    (1526, 174, 1643, 240),   # lower cell: level number
    "lbl_l":         (65, 352),            # left label anchor (lm), center y
    "lbl_r":         (1645, 352),          # right label anchor (rm), center y
    "lbl_size":      23,
    "lbl_l_chip":    (54, 341, 305, 376),  # cover for baked "NN% to next level"
    "lbl_r_chip":    (1428, 341, 1656, 376),  # cover for baked "Next: Level N"
    "bar":           (59, 388, 1660, 411),
    "cards": (
        # value + sub boxes per card (headers are baked into the art)
        (86, 546, 226, 623), (86, 632, 179, 667),
        (626, 546, 766, 623), (626, 632, 719, 667),
        (1153, 546, 1293, 623), (1153, 632, 1246, 667),
    ),
    "chip_fill":     (10, 10, 12, 240),    # solid chip hides baked label text
}

# Measured header geometry per v2 template. Pill/cell centre-y read from each
# art's borders (accent-colour + column scans, cross-checked per template);
# the shared boxes above are only the legacy average. Text is centred on the
# per-template values so name/username/LEVEL/level sit inside their slots on
# every template, including #19 whose art matches #18 after the owner swap.
V2_HDR = {
    7:  {"name": 136.5, "user": 207.5, "cell_u": 123.0, "cell_l": 204.0},
    8:  {"name": 148.5, "user": 215.5, "cell_u": 134.0, "cell_l": 209.0},
    9:  {"name": 137.5, "user": 207.5, "cell_u": 126.5, "cell_l": 202.0},
    10: {"name": 144.0, "user": 211.5, "cell_u": 127.0, "cell_l": 210.0},
    11: {"name": 141.0, "user": 209.0, "cell_u": 116.5, "cell_l": 193.5},
    12: {"name": 137.5, "user": 205.0, "cell_u": 120.0, "cell_l": 201.5},
    13: {"name": 140.5, "user": 205.5, "cell_u": 126.5, "cell_l": 207.0},
    14: {"name": 139.5, "user": 208.5, "cell_u": 123.5, "cell_l": 200.0},
    15: {"name": 166.0, "user": 234.0, "cell_u": 143.5, "cell_l": 224.0},
    16: {"name": 143.5, "user": 212.5, "cell_u": 131.5, "cell_l": 204.5},
    17: {"name": 145.5, "user": 213.0, "cell_u": 125.5, "cell_l": 202.5},
    18: {"name": 145.0, "user": 213.5, "cell_u": 125.0, "cell_l": 200.0},
    19: {"name": 145.0, "user": 213.5, "cell_u": 125.0, "cell_l": 200.0},
}
# Ink centre minus anchor y for the sample strings (measured on renders):
# name/user use anchor "lm", the cells use "mm".
_V2_BIAS = {"name": 0.5, "user": 3.0, "cell_u": 0.5, "cell_l": 1.0}
# Legacy fixed ink centres (pre per-template fix) - fallback for unknown ids.
_V2_LEGACY = {"name": 149.5, "user": 225.0, "cell_u": 136.0, "cell_l": 208.0}


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
def _paste_avatar(overlay, avatar_path, box=None, radius=None):
    """Center-crop + rounded-mask the avatar into the template's slot."""
    x0, y0, x1, y1 = box if box else AVATAR_BOX
    radius = AVATAR_RADIUS if radius is None else radius
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
        (0, 0, w - 1, h - 1), radius=radius * SCALE, fill=255
    )
    av.putalpha(mask)
    overlay.paste(av, (x0 * SCALE, y0 * SCALE), av)


# ----------------------------- progress bar ---------------------------------
def _bar_layer(pct: float, theme: dict, rect=None):
    """Build the glowing progress bar (incl. feathered shade) for ``pct``.

    The shade neutralises the template's baked placeholder fill so the
    real progress can start from zero without a ghost glow.
    Returns (layer, paste_position).
    """
    s = SCALE
    bx0, by0, bx1, by1 = rect if rect else (BAR_X0, BAR_Y0, BAR_X1, BAR_Y1)
    bw, bh = (bx1 - bx0) * s, (by1 - by0) * s
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
    return layer, (bx0 * s - pad, by0 * s - pad)


# ------------------------------- card ---------------------------------------
def _split_rank(rank_text):
    """``#3/57`` -> (``#3``, ``of 57``) — value + context line."""
    text = "" if rank_text is None else str(rank_text)
    if "/" in text:
        value, _, total = text.partition("/")
        value, total = value.strip(), total.strip()
        return value, (f"of {total}" if total else "")
    return text, ""


# ------------------------------- contrast -----------------------------------
def _bg_lum(base, box):
    """Mean luminance of a box interior (15% inset) on the base art."""
    x0, y0, x1, y1 = box
    ix, iy = max(2, int((x1 - x0) * 0.15)), max(2, int((y1 - y0) * 0.15))
    reg = base.crop((x0 + ix, y0 + iy, x1 - ix, y1 - iy)).convert("RGB").resize((6, 6))
    px = list(reg.getdata())
    return sum(0.299 * r + 0.587 * g + 0.114 * b for r, g, b in px) / len(px)


def _ink(lum, color=WHITE, dark=(24, 26, 34, 255)):
    """Readable text colour: flip to dark ink on bright frosted boxes."""
    return dark if lum > 155 else color


def _accent_ink(lum, accent):
    """Accent colour, darkened when the background is bright."""
    if lum > 155:
        return tuple(int(c * 0.5) for c in accent[:3]) + (255,)
    return accent


# ------------------------------- v1 drawing ---------------------------------
def _draw_v1(base, overlay, theme, name, username, level, next_level,
             progress_pct, rank_text, messages, global_messages, avatar_path):
    accent = theme["accent"] + (255,)
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


# ------------------------------- v2 drawing ---------------------------------
def _draw_v2(base, overlay, theme, name, username, level, next_level,
             progress_pct, rank_text, messages, global_messages, avatar_path,
             template_id=1):
    """Dashboard layout for templates 7-19.

    Labels above the bar are baked into the art ("72% to next level" /
    "Next: Level 29"), so a solid chip covers them before the live text
    is drawn; card headers are baked and left untouched. Every frosted
    box gets its ink sampled so light variants stay readable. Header text
    is centred on the per-template pill/cell geometry from V2_HDR.
    """
    accent = theme["accent"] + (255,)
    draw = ImageDraw.Draw(overlay)
    s = SCALE
    hdr = V2_HDR.get(template_id, _V2_LEGACY)

    def sc(box):
        return tuple(int(v * s) for v in box)

    def cxy(box):
        return ((box[0] + box[2]) / 2 * s, (box[1] + box[3]) / 2 * s)

    def rebox(box, centre, legacy):
        """Recentre a frosted box on the template's measured slot centre."""
        dy = centre - legacy
        return (box[0], box[1] + dy, box[2], box[3] + dy)

    # ---- profile header ----
    _paste_avatar(overlay, avatar_path, V2["avatar"], V2["avatar_radius"])

    nx, _, nmax = V2["name"]
    ncy = hdr["name"] - _V2_BIAS["name"]
    name_box = rebox(V2["name_box"], hdr["name"], _V2_LEGACY["name"])
    name_lum = _bg_lum(base, name_box)
    name_txt, name_font = _fit(draw, str(name), 44 * s, nmax * s)
    if name_txt:
        draw.text((nx * s, ncy * s), name_txt, font=name_font,
                  fill=_ink(name_lum), anchor="lm")

    ux, _, umax = V2["user"]
    ucy = hdr["user"] - _V2_BIAS["user"]
    user_box = rebox(V2["user_box"], hdr["user"], _V2_LEGACY["user"])
    user_lum = _bg_lum(base, user_box)
    user_txt, user_font = _fit(draw, username, 26 * s, umax * s)
    if user_txt:
        draw.text((ux * s, ucy * s), user_txt, font=user_font,
                  fill=_accent_ink(user_lum, accent), anchor="lm")

    # ---- level cells: LEVEL label (upper) + number (lower) ----
    c1 = rebox(V2["cell_label"], hdr["cell_u"], _V2_LEGACY["cell_u"])
    c2 = rebox(V2["cell_value"], hdr["cell_l"], _V2_LEGACY["cell_l"])
    c1_lum, c2_lum = _bg_lum(base, c1), _bg_lum(base, c2)
    lbl_txt, lbl_font = _fit(draw, "LEVEL", 20 * s, (c1[2] - c1[0] - 14) * s)
    draw.text((cxy(c1)[0], (hdr["cell_u"] - _V2_BIAS["cell_u"]) * s),
              lbl_txt, font=lbl_font,
              fill=_ink(c1_lum, LABEL), anchor="mm")
    lvl_txt, lvl_font = _fit(draw, str(level), 42 * s, (c2[2] - c2[0] - 14) * s)
    draw.text((cxy(c2)[0], (hdr["cell_l"] - _V2_BIAS["cell_l"]) * s),
              lvl_txt, font=lvl_font,
              fill=_accent_ink(c2_lum, accent), anchor="mm")

    # ---- progress labels (chip covers baked text) + bar ----
    pct = max(0.0, min(100.0, float(progress_pct or 0)))
    draw.rounded_rectangle(sc(V2["lbl_l_chip"]), radius=9 * s,
                           fill=V2["chip_fill"])
    draw.rounded_rectangle(sc(V2["lbl_r_chip"]), radius=9 * s,
                           fill=V2["chip_fill"])

    lx, lcy = V2["lbl_l"]
    lbl_txt, lbl_font = _fit(draw, f"{round(pct)}% to next level",
                             V2["lbl_size"] * s, (V2["lbl_l_chip"][2] - lx - 8) * s)
    draw.text((lx * s, lcy * s), lbl_txt, font=lbl_font, fill=LABEL, anchor="lm")

    rx, rcy = V2["lbl_r"]
    nxt_txt, nxt_font = _fit(draw, f"Next: Level {next_level}",
                             V2["lbl_size"] * s, (rx - V2["lbl_r_chip"][0] - 8) * s)
    draw.text((rx * s, rcy * s), nxt_txt, font=nxt_font, fill=LABEL, anchor="rm")

    bar, pos = _bar_layer(pct, theme, V2["bar"])
    overlay.alpha_composite(bar, dest=pos)

    # ---- statistics: centred inside the frosted value/sub boxes ----
    rank_value, rank_sub = _split_rank(rank_text)
    cards = (
        (rank_value, rank_sub),
        (messages, "this chat"),
        (global_messages, "all chats"),
    )
    boxes = V2["cards"]
    for i, (value, sub) in enumerate(cards):
        vbox, sbox = boxes[i * 2], boxes[i * 2 + 1]
        vlum, slum = _bg_lum(base, vbox), _bg_lum(base, sbox)
        val_txt, val_font = _fit(draw, value, 46 * s, (vbox[2] - vbox[0] - 14) * s)
        draw.text(cxy(vbox), val_txt, font=val_font,
                  fill=_ink(vlum), anchor="mm")
        if sub:
            sub_txt, sub_font = _fit(draw, sub, 19 * s, (sbox[2] - sbox[0] - 10) * s)
            draw.text(cxy(sbox), sub_txt, font=sub_font,
                      fill=_ink(slum, MUTED, dark=(70, 72, 80, 255)), anchor="mm")


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
      template_id            theme template (1-19)
    Returns output_path on success, None on failure.
    """
    theme = _get_theme_colors(template_id)

    try:
        base = _load_template(template_id)
        overlay = Image.new(
            "RGBA", (CANVAS_W * SCALE, CANVAS_H * SCALE), (0, 0, 0, 0)
        )

        args = (base, overlay, theme, name, username, level, next_level,
                progress_pct, rank_text, messages, global_messages, avatar_path)
        if theme.get("layout") == "v2":
            _draw_v2(*args, template_id=template_id)
        else:
            _draw_v1(*args)

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
