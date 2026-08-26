"""
Neon Cyberpunk profile card generator.

Master template: bot/templates/neon_cyberpunk.png (1672 x 941)
Python only draws: name, username, level, rank, stats, avatar.
Everything else (borders, icons, glow, skyline) stays untouched.
"""
import os
import logging
from io import BytesIO
from typing import Optional

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
# FONTS — Windows Arial fallback (DejaVu not available)
# ============================================================

FONT_BOLD = r"C:\Windows\Fonts\arialbd.ttf"
FONT_REG = r"C:\Windows\Fonts\arial.ttf"

FONT_NAME = ImageFont.truetype(FONT_BOLD, 58)
FONT_USERNAME = ImageFont.truetype(FONT_BOLD, 35)
FONT_LEVEL_LABEL = ImageFont.truetype(FONT_BOLD, 31)
FONT_LEVEL_VALUE = ImageFont.truetype(FONT_BOLD, 31)
FONT_STAT_LABEL = ImageFont.truetype(FONT_BOLD, 30)
FONT_STAT_VALUE = ImageFont.truetype(FONT_BOLD, 30)

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
# ============================================================

THEMES = {
    1: {"name": "NEON CYBERPUNK", "accent": PURPLE, "text": WHITE},
    2: {"name": "RED DARK",       "accent": RED,    "text": WHITE},
    3: {"name": "ICE FANTASY",    "accent": (100, 200, 255, 255), "text": WHITE},
    4: {"name": "MILITARY",       "accent": (115, 220, 45, 255),  "text": WHITE},
    5: {"name": "CINEMA",         "accent": (220, 180, 50, 255),  "text": WHITE},
    6: {"name": "PIRATE",         "accent": (210, 145, 40, 255),  "text": WHITE},
}


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
