"""
Pi Bot — Custom Emoji System
Uses the Robin-style custom Telegram emoji display:
<tg-emoji emoji-id="CUSTOM_EMOJI_ID">fallback</tg-emoji>

Custom emoji IDs provided by owner:
- 5217890643321300022 ✈️
- 5310224206732996002 ⭐
- 5258039825805624495 ⭐
- 5904248647972820334 💭
- 5904630315946611415 👤
- 5890925363067886150 ✨
- 5794164805065514131 1⃣
- 5805506958995758422 📁
- 5805553606635559688 👑
- 6055188418822937956 👍
- 6325687241536440125 ✅
- 5042334757040423886 ⚡️
- 5039623284056917259 👀
- 5039844895779455925 ✔️
- 5040042498634810056 ❌
- 5039891861246838069 ➕
- 5039644681583985437 🔥
- 5042186567783809934 🌐
- 5040034664614462519 ❗
- 5039826874096681939 🔖
- 6097973409851906183 📣
- 6098245027878672529 🔈
- 5084572732444640127 ⭕️
- 5084772285215147326 ‼️
- 5123163417326126159 ✅
- 5121063440311386962 👎
- 4902715076873553054 🩵
- 4904882772637648609 ⏰
- 4918438965029110683 🆕
- 5104989658350093220 📍
- 5001534655082529641 🎸
- 5422529445380532104 👋
- 5420405566872788904 💼
"""

import random
import logging
from typing import Optional, List, Dict, Tuple

logger = logging.getLogger("pi.emojis")


# ========== CUSTOM EMOJI IDS ==========
CUSTOM_EMOJI_IDS = [
    "5217890643321300022",
    "5310224206732996002",
    "5258039825805624495",
    "5904248647972820334",
    "5904630315946611415",
    "5890925363067886150",
    "5794164805065514131",
    "5805506958995758422",
    "5805553606635559688",
    "6055188418822937956",
    "6325687241536440125",
    "5042334757040423886",
    "5039623284056917259",
    "5039844895779455925",
    "5040042498634810056",
    "5039891861246838069",
    "5039644681583985437",
    "5042186567783809934",
    "5040034664614462519",
    "5039826874096681939",
    "6097973409851906183",
    "6098245027878672529",
    "5084572732444640127",
    "5084772285215147326",
    "5123163417326126159",
    "5121063440311386962",
    "4902715076873553054",
    "4904882772637648609",
    "4918438965029110683",
    "5104989658350093220",
    "5001534655082529641",
    "5422529445380532104",
    "5420405566872788904",
]


# ========== EMOJI CATEGORY MAPPING ==========
# Maps category -> list of (fallback_emoji, custom_emoji_id) tuples
# Categories themed for Pi Bot moderation/management

EMOJI_MAP: Dict[str, List[Tuple[str, str]]] = {
    # ─── Owner / Authority ───────────────────────────────────
    "crown": [
        ("👑", "5805553606635559688"),
    ],
    "admin": [
        ("⚡", "5042334757040423886"),
        ("👑", "5805553606635559688"),
        ("💼", "5420405566872788904"),
    ],

    # ─── Success / Confirm ───────────────────────────────────
    "success": [
        ("✅", "6325687241536440125"),
        ("✔️", "5039844895779455925"),
        ("👍", "6055188418822937956"),
    ],
    "check": [
        ("✅", "6325687241536440125"),
        ("✔️", "5039844895779455925"),
    ],

    # ─── Error / Deny ────────────────────────────────────────
    "error": [
        ("❌", "5040042498634810056"),
    ],
    "cross": [
        ("❌", "5040042498634810056"),
    ],
    "disapprove": [
        ("👎", "5121063440311386962"),
    ],

    # ─── Warning / Alert ─────────────────────────────────────
    "warning": [
        ("❗", "5040034664614462519"),
        ("‼️", "5084772285215147326"),
    ],
    "alert": [
        ("❗", "5040034664614462519"),
        ("‼️", "5084772285215147326"),
        ("📣", "6097973409851906183"),
    ],

    # ─── Moderation ──────────────────────────────────────────
    "mute": [
        ("🔈", "6098245027878672529"),
    ],
    "ban": [
        ("❌", "5040042498634810056"),
        ("‼️", "5084772285215147326"),
    ],
    "unban": [
        ("✅", "6325687241536440125"),
        ("✔️", "5039844895779455925"),
    ],
    "kick": [
        ("⚡", "5042334757040423886"),
    ],
    "warn": [
        ("‼️", "5084772285215147326"),
        ("❗", "5040034664614462519"),
    ],

    # ─── Watch / Monitor ─────────────────────────────────────
    "watch": [
        ("👀", "5039623284056917259"),
    ],
    "eyes": [
        ("👀", "5039623284056917259"),
    ],

    # ─── Info / Settings ─────────────────────────────────────
    "info": [
        ("💭", "5904248647972820334"),
        ("🌐", "5042186567783809934"),
    ],
    "settings": [
        ("📁", "5805506958995758422"),
        ("💼", "5420405566872788904"),
    ],
    "folder": [
        ("📁", "5805506958995758422"),
    ],

    # ─── Time ────────────────────────────────────────────────
    "time": [
        ("⏰", "4904882772637648609"),
    ],
    "clock": [
        ("⏰", "4904882772637648609"),
    ],

    # ─── New / Fresh ─────────────────────────────────────────
    "new": [
        ("🆕", "4918438965029110683"),
    ],
    "fresh": [
        ("🆕", "4918438965029110683"),
        ("✨", "5890925363067886150"),
    ],

    # ─── Level / Rank ────────────────────────────────────────
    "level": [
        ("🔥", "5039644681583985437"),
        ("⭐", "5310224206732996002"),
        ("⭐", "5258039825805624495"),
    ],
    "star": [
        ("⭐", "5310224206732996002"),
        ("⭐", "5258039825805624495"),
    ],
    "fire": [
        ("🔥", "5039644681583985437"),
    ],
    "medal_1": [
        ("⭐", "5310224206732996002"),
    ],
    "medal_2": [
        ("⭐", "5258039825805624495"),
    ],
    "medal_3": [
        ("✨", "5890925363067886150"),
    ],

    # ─── User / Person ───────────────────────────────────────
    "user": [
        ("👤", "5904630315946611415"),
    ],
    "person": [
        ("👤", "5904630315946611415"),
    ],

    # ─── Welcome / Goodbye ───────────────────────────────────
    "welcome": [
        ("👋", "5422529445380532104"),
        ("✨", "5890925363067886150"),
    ],
    "wave": [
        ("👋", "5422529445380532104"),
    ],
    "goodbye": [
        ("✈️", "5217890643321300022"),
        ("👋", "5422529445380532104"),
    ],

    # ─── Travel / Forward ────────────────────────────────────
    "travel": [
        ("✈️", "5217890643321300022"),
    ],
    "forward": [
        ("✈️", "5217890643321300022"),
    ],

    # ─── Web / Network ───────────────────────────────────────
    "web": [
        ("🌐", "5042186567783809934"),
    ],
    "globe": [
        ("🌐", "5042186567783809934"),
    ],

    # ─── Bookmark / Save ─────────────────────────────────────
    "bookmark": [
        ("🔖", "5039826874096681939"),
    ],
    "save": [
        ("🔖", "5039826874096681939"),
    ],

    # ─── Location / Pin ──────────────────────────────────────
    "location": [
        ("📍", "5104989658350093220"),
    ],
    "pin": [
        ("📍", "5104989658350093220"),
    ],

    # ─── Music / Fun ─────────────────────────────────────────
    "guitar": [
        ("🎸", "5001534655082529641"),
    ],
    "music": [
        ("🎸", "5001534655082529641"),
    ],

    # ─── Add / Plus ──────────────────────────────────────────
    "add": [
        ("➕", "5039891861246838069"),
    ],
    "plus": [
        ("➕", "5039891861246838069"),
    ],

    # ─── Circle / Priority ───────────────────────────────────
    "circle": [
        ("⭕", "5084572732444640127"),
    ],
    "priority": [
        ("⭕", "5084572732444640127"),
    ],

    # ─── Heart ───────────────────────────────────────────────
    "heart": [
        ("🩵", "4902715076873553054"),
    ],

    # ─── Number ──────────────────────────────────────────────
    "number_1": [
        ("1⃣", "5794164805065514131"),
    ],

    # ─── Sparkle ─────────────────────────────────────────────
    "sparkle": [
        ("✨", "5890925363067886150"),
    ],

    # ─── Announcement ────────────────────────────────────────
    "announce": [
        ("📣", "6097973409851906183"),
    ],
    "speaker": [
        ("🔈", "6098245027878672529"),
    ],
}


def custom_emoji(emoji_char: str, custom_emoji_id: str) -> str:
    """
    Format a custom emoji in HTML format for Telegram.

    Format: <tg-emoji emoji-id="CUSTOM_EMOJI_ID">fallback</tg-emoji>

    Args:
        emoji_char: The fallback emoji character
        custom_emoji_id: The custom_emoji_id from Telegram

    Returns:
        Formatted HTML string
    """
    if not custom_emoji_id:
        return emoji_char
    return f'<tg-emoji emoji-id="{custom_emoji_id}">{emoji_char}</tg-emoji>'


def get_emoji_from_category(category: str) -> Tuple[str, str]:
    """
    Get a random (fallback_emoji, custom_emoji_id) tuple from a category.

    Args:
        category: Category name from EMOJI_MAP

    Returns:
        Tuple of (fallback_emoji, custom_emoji_id)
    """
    if category in EMOJI_MAP and EMOJI_MAP[category]:
        return random.choice(EMOJI_MAP[category])

    # Default fallback
    return ("✨", "5890925363067886150")


def get_emoji(category: str = None) -> str:
    """
    Get a formatted custom emoji in HTML format.

    Args:
        category: Optional category name. If None, returns random from all.

    Returns:
        HTML formatted custom emoji string
    """
    if category:
        fallback, custom_id = get_emoji_from_category(category)
    else:
        categories = list(EMOJI_MAP.keys())
        cat = random.choice(categories)
        fallback, custom_id = get_emoji_from_category(cat)

    return custom_emoji(fallback, custom_id)


def get_emoji_text(category: str = None) -> str:
    """
    Get just the fallback emoji text (no custom formatting).

    Args:
        category: Optional category name

    Returns:
        Plain emoji character
    """
    if category:
        fallback, _ = get_emoji_from_category(category)
    else:
        categories = list(EMOJI_MAP.keys())
        cat = random.choice(categories)
        fallback, _ = get_emoji_from_category(cat)

    return fallback


# ========== QUICK ACCESS FUNCTIONS ==========

def crown() -> str:
    """Crown emoji."""
    return get_emoji("crown")

def admin() -> str:
    """Random admin emoji."""
    return get_emoji("admin")

def success() -> str:
    """Random success emoji."""
    return get_emoji("success")

def check() -> str:
    """Check mark emoji."""
    return get_emoji("check")

def error() -> str:
    """Error emoji."""
    return get_emoji("error")

def cross() -> str:
    """Cross mark emoji."""
    return get_emoji("cross")

def warning() -> str:
    """Warning emoji."""
    return get_emoji("warning")

def alert() -> str:
    """Alert emoji."""
    return get_emoji("alert")

def mute() -> str:
    """Mute emoji."""
    return get_emoji("mute")

def ban() -> str:
    """Ban emoji."""
    return get_emoji("ban")

def unban() -> str:
    """Unban emoji."""
    return get_emoji("unban")

def kick() -> str:
    """Kick emoji."""
    return get_emoji("kick")

def warn() -> str:
    """Warn emoji."""
    return get_emoji("warn")

def watch() -> str:
    """Watch emoji."""
    return get_emoji("watch")

def info() -> str:
    """Info emoji."""
    return get_emoji("info")

def settings() -> str:
    """Settings emoji."""
    return get_emoji("settings")

def time_() -> str:
    """Time emoji."""
    return get_emoji("time")

def clock() -> str:
    """Clock emoji."""
    return get_emoji("clock")

def new() -> str:
    """New emoji."""
    return get_emoji("new")

def level() -> str:
    """Level emoji."""
    return get_emoji("level")

def star() -> str:
    """Star emoji."""
    return get_emoji("star")

def fire() -> str:
    """Fire emoji."""
    return get_emoji("fire")

def medal_1() -> str:
    """Gold medal."""
    return get_emoji("medal_1")

def medal_2() -> str:
    """Silver medal."""
    return get_emoji("medal_2")

def medal_3() -> str:
    """Bronze medal."""
    return get_emoji("medal_3")

def user() -> str:
    """User emoji."""
    return get_emoji("user")

def welcome() -> str:
    """Welcome emoji."""
    return get_emoji("welcome")

def wave() -> str:
    """Wave emoji."""
    return get_emoji("wave")

def goodbye() -> str:
    """Goodbye emoji."""
    return get_emoji("goodbye")

def travel() -> str:
    """Travel emoji."""
    return get_emoji("travel")

def web() -> str:
    """Web emoji."""
    return get_emoji("web")

def bookmark() -> str:
    """Bookmark emoji."""
    return get_emoji("bookmark")

def location() -> str:
    """Location emoji."""
    return get_emoji("location")

def add() -> str:
    """Add emoji."""
    return get_emoji("add")

def circle() -> str:
    """Circle emoji."""
    return get_emoji("circle")

def heart() -> str:
    """Heart emoji."""
    return get_emoji("heart")

def sparkle() -> str:
    """Sparkle emoji."""
    return get_emoji("sparkle")

def announce() -> str:
    """Announce emoji."""
    return get_emoji("announce")

def disapprove() -> str:
    """Disapprove emoji."""
    return get_emoji("disapprove")


# ========== EMOJI CLASS (for E.XXX access) ==========

class E:
    """
    Emoji constants class for consistent bot branding.
    Uses custom Telegram emojis via <tg-emoji> tags.

    Usage:
        f"{E.CROWN} Hello!"
        f"{E.FIRE} Level up!"
    """
    # Core
    CROWN       = crown()
    FIRE        = fire()
    STAR        = star()
    SPARKLE     = sparkle()
    HEART       = heart()
    CHECK       = check()
    CROSS       = cross()
    DISAPPROVE  = disapprove()

    # Authority
    ADMIN       = admin()
    USER        = user()

    # Status
    SUCCESS     = success()
    WARNING     = warning()
    ALERT       = alert()
    INFO        = info()
    ERROR       = error()

    # Moderation
    MUTE        = mute()
    BAN         = ban()
    UNBAN       = unban()
    KICK        = kick()
    WARN        = warn()

    # Watch
    WATCH       = watch()
    EYES        = watch()

    # Time
    TIME        = time_()
    CLOCK       = clock()

    # New
    NEW         = new()
    FRESH       = new()

    # Level / Rank
    LEVEL       = level()
    MEDAL_1     = medal_1()
    MEDAL_2     = medal_2()
    MEDAL_3     = medal_3()

    # Welcome / Goodbye
    WELCOME     = welcome()
    WAVE        = wave()
    GOODBYE     = goodbye()

    # Travel
    TRAVEL      = travel()
    FORWARD     = travel()

    # Web
    WEB         = web()
    GLOBE       = web()

    # Bookmark
    BOOKMARK    = bookmark()
    SAVE        = bookmark()

    # Location
    LOCATION    = location()
    PIN         = location()

    # Settings
    SETTINGS    = settings()
    FOLDER      = settings()

    # Action
    ADD         = add()
    PLUS        = add()
    CIRCLE      = circle()

    # Announce
    ANNOUNCE    = announce()
    SPEAKER     = announce()

    # Number
    NUMBER_1    = custom_emoji("1⃣", "5794164805065514131")

    # Music
    GUITAR      = custom_emoji("🎸", "5001534655082529641")

    # Separator
    BULLET      = "━━━━━━━━━━━━━━"
    ARROW       = custom_emoji("✈️", "5217890643321300022")


# ========== EMOJI ID CLASS (for icon_custom_emoji_id in buttons) ==========
class EID:
    """
    Raw custom emoji IDs for use with InlineKeyboardButton icon_custom_emoji_id.
    
    Usage in buttons:
        btn_primary("Help", "data", icon_emoji_id=EID.INFO)
    
    Requirements:
        - Bot owner must have Telegram Premium subscription, OR
        - Bot must have purchased additional usernames on Fragment
    """
    # Core
    CROWN       = "5805553606635559688"
    FIRE        = "5039644681583985437"
    STAR        = "5310224206732996002"
    SPARKLE     = "5890925363067886150"
    HEART       = "4902715076873553054"
    CHECK       = "6325687241536440125"
    CROSS       = "5040042498634810056"
    DISAPPROVE  = "5121063440311386962"
    
    # Authority
    ADMIN       = "5042334757040423886"
    USER        = "5904630315946611415"
    
    # Status
    SUCCESS     = "6325687241536440125"
    WARNING     = "5040034664614462519"
    ALERT       = "6097973409851906183"
    INFO        = "5904248647972820334"
    ERROR       = "5040042498634810056"
    
    # Moderation
    MUTE        = "6098245027878672529"
    BAN         = "5040042498634810056"
    UNBAN       = "6325687241536440125"
    KICK        = "5042334757040423886"
    WARN        = "5084772285215147326"
    
    # Watch
    WATCH       = "5039623284056917259"
    EYES        = "5039623284056917259"
    
    # Time
    TIME        = "4904882772637648609"
    CLOCK       = "4904882772637648609"
    
    # New
    NEW         = "4918438965029110683"
    FRESH       = "4918438965029110683"
    
    # Level / Rank
    LEVEL       = "5039644681583985437"
    MEDAL_1     = "5310224206732996002"
    MEDAL_2     = "5258039825805624495"
    MEDAL_3     = "5890925363067886150"
    
    # Welcome / Goodbye
    WELCOME     = "5422529445380532104"
    WAVE        = "5422529445380532104"
    GOODBYE     = "5217890643321300022"
    
    # Travel
    TRAVEL      = "5217890643321300022"
    FORWARD     = "5217890643321300022"
    
    # Web
    WEB         = "5042186567783809934"
    GLOBE       = "5042186567783809934"
    
    # Bookmark
    BOOKMARK    = "5039826874096681939"
    SAVE        = "5039826874096681939"
    
    # Location
    LOCATION    = "5104989658350093220"
    PIN         = "5104989658350093220"
    
    # Settings
    SETTINGS    = "5805506958995758422"
    FOLDER      = "5805506958995758422"
    
    # Action
    ADD         = "5039891861246838069"
    PLUS        = "5039891861246838069"
    CIRCLE      = "5084572732444640127"
    
    # Announce
    ANNOUNCE    = "6097973409851906183"
    SPEAKER     = "6097973409851906183"
    
    # Number
    NUMBER_1    = "5794164805065514131"
    
    # Music
    GUITAR      = "5001534655082529641"
