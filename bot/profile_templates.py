"""
Neon Cyberpunk profile card generator.

Master template: bot/templates/neon_cyberpunk.png (1672 x 941)
Python only draws: name, username, level, rank, stats, avatar.
Everything else (borders, icons, glow, skyline) stays untouched.
"""
import os
import logging
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

# ============================================================
# CONFIG
# ============================================================

TEMPLATE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "templates",
    "neon_cyberpunk.png",
)

WIDTH = 1672
HEIGHT = 941

# ============================================================
# COLORS
# ============================================================

WHITE = (245, 245, 245, 255)
PURPLE = (190, 45, 255, 255)
RED = (255, 30, 35, 255)

# ============================================================
# FONTS — bundled Noto first (works on Railway/Linux), then
# Windows Arial; _font() never raises so imports can't crash.
# ============================================================

_ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")


def _find_font() -> str:
    """First usable TrueType — bundled asset beats OS-specific paths.

    rank_image.py resolves fonts the same way; a hardcoded Windows
    fonts path crashed the bot at import on Linux (Railway).
    """
    bundled = os.path.join(_ASSETS_DIR, "NotoSans-Bold.ttf")
    if os.path.isfile(bundled):
        return bundled
    if os.name == "nt":
        for cand in (
            r"C:\Windows\Fonts\arialbd.ttf",
            r"C:\Windows\Fonts\arial.ttf",
        ):
            if os.path.isfile(cand):
                return cand
    for cand in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ):
        if os.path.isfile(cand):
            return cand
    return bundled  # missing → _font() falls back to load_default()


FONT_BOLD = _find_font()
FONT_REG = FONT_BOLD  # only one face ships in bot/assets


def _font(size: int):
    """TrueType at ``size``; bitmap default as the never-crash floor."""
    try:
        return ImageFont.truetype(FONT_BOLD, size)
    except Exception:
        try:
            return ImageFont.load_default(size=size)
        except TypeError:  # Pillow < 10.1 has no size argument
            return ImageFont.load_default()


FONT_NAME = _font(58)
FONT_USERNAME = _font(35)
FONT_LEVEL_LABEL = _font(31)
FONT_LEVEL_VALUE = _font(31)
FONT_STAT_LABEL = _font(30)
FONT_STAT_VALUE = _font(30)

# ============================================================
# FIXED POSITIONS (1672 x 941 master)
# ============================================================

AVATAR_CENTER = (260, 244)
AVATAR_RADIUS = 157

NAME_X = 474
NAME_Y = 181

USERNAME_X = 474
USERNAME_Y = 278

LEVEL_LABEL_X = 78
LEVEL_LABEL_Y = 485

LEVEL_VALUE_X = 393
LEVEL_VALUE_Y = 485

PROGRESS_X1 = 77
PROGRESS_Y = 589
PROGRESS_X2 = 1575

RIGHT_LEVEL_X = 1450
RIGHT_LEVEL_Y = 573

RANK_LABEL_X = 84
RANK_LABEL_Y = 686

CHAT_LABEL_X = 548
CHAT_LABEL_Y = 686

GLOBAL_LABEL_X = 1114
GLOBAL_LABEL_Y = 686

RANK_VALUE_X = 250
RANK_VALUE_Y = 782

CHAT_VALUE_X = 666
CHAT_VALUE_Y = 782

GLOBAL_VALUE_X = 1232
GLOBAL_VALUE_Y = 782

# ============================================================
# THEMES (color overrides per template_id)
#
# section: "free" | "fictional" | "owner"  — /template grouping
# need:    global-messages milestone to unlock, 0 = free from
#          start. Owner-exclusive templates unlock only for
#          the owner.
# req:     profile requirement checked against the live user —
#          {"kind": "group"}            bot added to a group
#          {"kind": "bio",   "text": s} user bio contains s
#          {"kind": "name",  "text": s} display name contains s
# ============================================================

BIO_TAG = "@PIModulerBot"
NAME_TAG = "@PI"

THEMES = {
    # ---- free: milestone unlocks (global messages) ----
    1:  {"name": "AMBER GLOW",     "accent": (250, 163, 47, 255),  "text": WHITE, "section": "free", "need": 1000},
    2:  {"name": "NEON SILVER",    "accent": (224, 233, 245, 255), "text": WHITE, "section": "free", "need": 5000},
    3:  {"name": "PURPLE NEON",    "accent": (233, 139, 251, 255), "text": WHITE, "section": "free", "need": 0},
    4:  {"name": "BLUE LIGHTNING", "accent": (77, 126, 247, 255),  "text": WHITE, "section": "free", "need": 3000},
    5:  {"name": "NEON GALAXY",    "accent": (156, 218, 252, 255), "text": WHITE, "section": "free", "need": 2500},
    6:  {"name": "NEON GREEN",     "accent": (139, 250, 179, 255), "text": WHITE, "section": "free", "need": 3500},
    # ---- fictional: profile requirements (see fetch_unlock_profile) ----
    8:  {"name": "CRIMSON FEATHER", "accent": (232, 150, 150, 255), "text": WHITE, "section": "fictional", "req": {"kind": "group"}},
    9:  {"name": "MONO MANGA",      "accent": (218, 218, 218, 255), "text": WHITE, "section": "fictional", "req": {"kind": "bio", "text": BIO_TAG}},
    10: {"name": "MONO HUD",        "accent": (170, 170, 170, 255), "text": WHITE, "section": "fictional", "req": {"kind": "name", "text": NAME_TAG}},
    11: {"name": "INK OVERLAY",     "accent": (210, 210, 210, 255), "text": WHITE, "section": "fictional", "req": {"kind": "bio", "text": BIO_TAG}},
    12: {"name": "NEON FANTASY",    "accent": (211, 172, 212, 255), "text": WHITE, "section": "fictional", "req": {"kind": "name", "text": NAME_TAG}},
    13: {"name": "ROMAN TRIUMPH",   "accent": (231, 116, 114, 255), "text": WHITE, "section": "fictional", "req": {"kind": "bio", "text": BIO_TAG}},
    14: {"name": "CYBER NOIR",      "accent": (172, 211, 231, 255), "text": WHITE, "section": "fictional", "req": {"kind": "bio", "text": BIO_TAG}},
    15: {"name": "RETRO GRUNGE",    "accent": (179, 206, 225, 255), "text": WHITE, "section": "fictional", "req": {"kind": "bio", "text": BIO_TAG}},
    16: {"name": "GRUNGE PEGASUS",  "accent": (200, 140, 136, 255), "text": WHITE, "section": "fictional", "req": {"kind": "bio", "text": BIO_TAG}},
    17: {"name": "CYBER GRUNGE",    "accent": (216, 216, 216, 255), "text": WHITE, "section": "fictional", "req": {"kind": "bio", "text": BIO_TAG}},
    18: {"name": "SAMURAI INK",     "accent": (170, 170, 170, 255), "text": WHITE, "section": "fictional", "req": {"kind": "bio", "text": BIO_TAG}},
    # ---- owner exclusive: equipped through /wear ----
    19: {"name": "PHANTOM RIDER",   "accent": (170, 202, 229, 255), "text": WHITE, "section": "owner", "need": None},
}

# Section button order for /template.
SECTIONS = ("free", "fictional")

SECTION_TITLES = {"free": "Free", "fictional": "Fictional"}


def templates_in_section(section: str) -> dict:
    """Ordered {template_id: theme} for one section."""
    return {tid: t for tid, t in THEMES.items() if t.get("section") == section}


def check_unlock(template_id, global_messages=0, is_owner=False, profile=None):
    """Is ``template_id`` equippable by this user?

    Returns ``(ok, reason)`` — reason is a short user-facing string for
    locked templates ("" when ok). Unlocking is evaluated at equip time
    only; an already-equipped template always renders.

    ``profile`` is the snapshot from :func:`fetch_unlock_profile` and is
    only required for templates carrying a ``req`` entry; without it a
    requirement counts as unmet.
    """
    theme = THEMES.get(template_id)
    if theme is None:
        return False, "Unknown template."
    if is_owner:
        return True, ""
    if theme.get("section") == "owner":
        return False, "Owner exclusive."
    req = theme.get("req")
    if req is not None:
        profile = profile or {}
        kind = req.get("kind")
        if kind == "group":
            if profile.get("in_group"):
                return True, ""
            return False, "Add the bot to one group first."
        if kind == "bio":
            bio = (profile.get("bio") or "").lower()
            if req["text"].lower() in bio:
                return True, ""
            return False, f"Add {req['text']} to your Telegram bio."
        if kind == "name":
            name = (profile.get("name") or "").lower()
            if req["text"].lower() in name:
                return True, ""
            return False, f"Add {req['text']} to your name."
        return False, "Requirements coming soon."
    need = theme.get("need")
    if need is None:
        return False, "Requirements coming soon."
    if need <= 0:
        return True, ""
    if int(global_messages or 0) >= need:
        return True, ""
    return False, f"Needs {need:,} global messages."


async def fetch_unlock_profile(bot, user) -> dict:
    """Live requirement context for one user (name, bio, group add).

    * name — from the ``User`` object Telegram already sent (first +
      last name), no API call.
    * bio — via ``getChat``; bots only see it through that call, and an
      error just means "not met".
    * in_group — this user sharing at least one group with the bot
      (the my_chat_member add event records the adder immediately;
      any group message registers the sender too).
    """
    first = getattr(user, "first_name", None) or ""
    last = getattr(user, "last_name", None) or ""
    profile = {
        "name": f"{first} {last}".strip(),
        "bio": "",
        "in_group": False,
    }
    try:
        chat = await bot.get_chat(user.id)
        profile["bio"] = getattr(chat, "bio", None) or ""
    except Exception:
        pass
    try:
        from bot.async_bridge import adb
        from bot.database import db
        profile["in_group"] = (await adb(db.count_user_groups(user.id))) >= 1
    except Exception:
        pass
    return profile


# ============================================================
# AVATAR
# ============================================================

def paste_avatar(base, avatar_bytes):
    """Paste user profile picture into the circular avatar area."""
    try:
        avatar = Image.open(BytesIO(avatar_bytes)).convert("RGBA")

        # Square crop
        side = min(avatar.size)
        left = (avatar.width - side) // 2
        top = (avatar.height - side) // 2
        avatar = avatar.crop((left, top, left + side, top + side))

        # Resize to avatar radius
        avatar = avatar.resize(
            (AVATAR_RADIUS * 2, AVATAR_RADIUS * 2),
            Image.Resampling.LANCZOS,
        )

        # Circular mask
        mask = Image.new("L", avatar.size, 0)
        ImageDraw.Draw(mask).ellipse(
            (0, 0, avatar.width - 1, avatar.height - 1), fill=255
        )

        # Paste
        x = AVATAR_CENTER[0] - AVATAR_RADIUS
        y = AVATAR_CENTER[1] - AVATAR_RADIUS
        base.paste(avatar, (x, y), mask)
    except Exception as e:
        logger.warning(f"Failed to paste avatar: {e}")


# ============================================================
# TEXT HELPERS
# ============================================================

def draw_text_centered(draw, text, font, center_x, y, fill):
    """Draw centered text."""
    bbox = draw.textbbox((0, 0), str(text), font=font)
    width = bbox[2] - bbox[0]
    draw.text((center_x - width / 2, y), str(text), font=font, fill=fill)


# ============================================================
# MAIN RENDERER
# ============================================================

def generate_profile_card(
    name,
    username,
    level,
    rank,
    chat_messages,
    global_messages,
    avatar_bytes=None,
    progress=50,
    template_id=1,
):
    """
    Generate Neon Cyberpunk profile card.
    Returns BytesIO ready for Telegram reply_photo().
    """
    # Load master template
    base = Image.open(TEMPLATE_PATH).convert("RGBA")
    base = base.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)
    draw = ImageDraw.Draw(base)

    # Theme colors
    theme = THEMES.get(template_id, THEMES[1])
    accent = theme["accent"]
    text_color = theme["text"]

    # Avatar
    if avatar_bytes:
        paste_avatar(base, avatar_bytes)

    # Name
    draw.text(
        (NAME_X, NAME_Y),
        str(name),
        font=FONT_NAME,
        fill=text_color,
    )

    # Username
    draw.text(
        (USERNAME_X, USERNAME_Y),
        f"@{username}",
        font=FONT_USERNAME,
        fill=accent,
    )

    # Current Level
    draw.text(
        (LEVEL_LABEL_X, LEVEL_LABEL_Y),
        "Current Level:",
        font=FONT_LEVEL_LABEL,
        fill=text_color,
    )
    draw.text(
        (LEVEL_VALUE_X, LEVEL_VALUE_Y),
        f"{level}",
        font=FONT_LEVEL_VALUE,
        fill=accent,
    )

    # Rank
    draw_text_centered(
        draw, str(rank), FONT_STAT_VALUE, 319, RANK_VALUE_Y, text_color
    )

    # Chat Messages
    draw_text_centered(
        draw, str(chat_messages), FONT_STAT_VALUE, 790, CHAT_VALUE_Y, text_color
    )

    # Global Messages
    draw_text_centered(
        draw, str(global_messages), FONT_STAT_VALUE, 1350, GLOBAL_VALUE_Y, text_color
    )

    # Output
    output = BytesIO()
    output.name = "profile.png"
    base.save(output, format="PNG", optimize=True)
    output.seek(0)
    return output


# ============================================================
# THEME LIST
# ============================================================

def get_theme_list():
    lines = []
    for tid, t in THEMES.items():
        lines.append(f"  {tid}. {t['name']}")
    return "\n".join(lines)


# ============================================================
# TEMPLATE PREVIEW GENERATOR
# ============================================================

def generate_template_preview(
    name,
    username,
    level,
    rank,
    chat_messages,
    global_messages,
    avatar_bytes=None,
):
    """
    Generate a combined preview image showing every template.
    Layout: 2 columns of scaled-down cards with template numbers.
    Returns BytesIO ready for Telegram reply_photo().
    """
    # Card dimensions (scaled down for preview)
    CARD_W = 557  # ~1/3 of original width
    CARD_H = 313  # ~1/3 of original height
    PADDING = 20
    LABEL_H = 40  # Space for template number label below each card

    # Grid layout: 2 columns x 3 rows
    COLS = 2
    ROWS = 3
    GRID_W = COLS * (CARD_W + PADDING) + PADDING
    GRID_H = ROWS * (CARD_H + LABEL_H + PADDING) + PADDING

    # Create canvas
    canvas = Image.new("RGB", (GRID_W, GRID_H), (14, 14, 14))
    draw = ImageDraw.Draw(canvas)

    # Font for labels
    try:
        label_font = ImageFont.truetype(FONT_BOLD, 28)
    except Exception:
        label_font = ImageFont.load_default()

    # Generate each template
    for tid, theme in THEMES.items():
        row = (tid - 1) // COLS
        col = (tid - 1) % COLS

        x = PADDING + col * (CARD_W + PADDING)
        y = PADDING + row * (CARD_H + LABEL_H + PADDING)

        # generate_profile_card returns a BytesIO — decode it before
        # resizing (BytesIO has no .resize; that raised
        # "'_io.BytesIO' object has no attribute 'resize'" on every
        # /template call and forced the text-only fallback).
        card_buf = generate_profile_card(
            name=name,
            username=username,
            level=level,
            rank=rank,
            chat_messages=chat_messages,
            global_messages=global_messages,
            avatar_bytes=avatar_bytes,
            progress=65,
            template_id=tid,
        )
        with Image.open(card_buf) as card_img:
            card = card_img.convert("RGB").resize(
                (CARD_W, CARD_H), Image.Resampling.LANCZOS
            )
        card_buf.close()
        canvas.paste(card, (x, y))

        # Draw template number label
        label = f"{tid}. {theme['name']}"
        label_bbox = draw.textbbox((0, 0), label, font=label_font)
        label_w = label_bbox[2] - label_bbox[0]
        label_x = x + (CARD_W - label_w) // 2
        label_y = y + CARD_H + 5

        # Draw label background
        draw.rounded_rectangle(
            (label_x - 10, label_y - 2, label_x + label_w + 10, label_y + 32),
            radius=8,
            fill=theme["accent"][:3] + (180,) if len(theme["accent"]) == 4 else theme["accent"] + (180,),
        )
        draw.text((label_x, label_y), label, font=label_font, fill=(255, 255, 255, 255))

    # Output
    output = BytesIO()
    output.name = "templates_preview.png"
    canvas.save(output, format="PNG", optimize=True)
    output.seek(0)
    return output


# ============================================================
# ASYNC API (for leveling module compatibility)
# ============================================================

async def generate_rank_card(
    user_id, username, display_name, rank, total_users,
    level, xp, xp_needed, total_messages, chat_messages,
    template_id=1, bot=None,
):
    """Async wrapper — downloads avatar, returns file path."""
    import tempfile

    avatar_bytes = None
    if bot:
        try:
            photos = await bot.get_user_profile_photos(user_id, limit=1)
            if photos.photos:
                f = await bot.get_file(photos.photos[0][-1].file_id)
                buf = BytesIO()
                await f.download_to_memory(buf)
                buf.seek(0)
                avatar_bytes = buf.read()
        except Exception as e:
            logger.warning(f"Avatar download failed: {e}")

    card = generate_profile_card(
        name=display_name or username or "User",
        username=username or "user",
        level=level,
        rank=f"#{rank}/{total_users}",
        chat_messages=chat_messages,
        global_messages=total_messages,
        avatar_bytes=avatar_bytes,
        progress=min(int((xp / xp_needed) * 100), 100) if xp_needed > 0 else 0,
        template_id=template_id,
    )

    output_path = os.path.join(tempfile.gettempdir(), f"rank_{user_id}.png")
    with open(output_path, "wb") as out_f:
        out_f.write(card.read())
    return output_path
