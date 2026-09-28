"""Fun module — Hug, kiss, slap, poke, tickle, and other fun commands.

Adapted from boa2's fun for Pi bot (python-telegram-bot).
Uses text+emoji replies (no external GIF dependencies).
"""

import random
import logging

from aiogram.enums import ParseMode
from aiogram.types import Message

from bot.emojis import E
from bot.pipeline import cmd, on
from bot.reply import reply_text

logger = logging.getLogger(__name__)

# ── Reaction lists ───────────────────────────────────────
SLAP_REACTIONS = [
    "Ohhhhh! That's gotta hurt! 😆",
    "R.I.P. 💀",
    "Ouch! That looked painful! 😬",
    "Well, they asked for it! 🤷",
    "Someone call an ambulance! 🚑",
    "That's gonna leave a mark! 🩹",
]

HUG_REACTIONS = [
    "Aww, that's so sweet! 🥰",
    "Hugs are the best! 🤗",
    "Sending good vibes! ✨",
    "Group hug! 🫂",
    "Everyone deserves a hug! 💕",
]

KISS_REACTIONS = [
    "Aww, how cute! 😘",
    "Smooth operator! 😎",
    "Love is in the air! 💕",
    "That's adorable! 🥰",
]

POKE_REACTIONS = [
    "Hey! Stop poking me! 😤",
    "*pokes back* 👉",
    "That tickles! 😆",
    "*giggles* 🤭",
]

TICKLE_REACTIONS = [
    "Hahaha! Stop it! 🤣",
    "I'm gonna pee myself! 😂",
    "Too much! Too much! 🫢",
    "*wheeze* 🤣",
]

PUNCH_REACTIONS = [
    " POW! Right in the kisser! 👊",
    "That's gonna swell up! 😤",
    "FATALITY! 💀",
    "Direct hit! 🎯",
]

YEET_REACTIONS = [
    "Into the void they go! 🌌",
    "YEET! 🏈",
    "Gone. Reduced to atoms. 💨",
    "And they're gone! 🫡",
]

KILL_REACTIONS = [
    "*dramatic gasp* 😱",
    "R.I.P. 🪦",
    "Gone but not forgotten... 🕯️",
    "Called the police! 🚔",
]


def _get_random(reactions: list) -> str:
    return random.choice(reactions)


# ── Command handlers ─────────────────────────────────────
async def hug_command(message: Message, args: list):
    """Handle /hug — send a hug."""
    user = message.from_user
    if args:
        target = " ".join(args)
    elif message.reply_to_message:
        target = message.reply_to_message.from_user.first_name
    else:
        target = "everyone"

    text = f"🫂 <b>{user.first_name}</b> hugs <b>{target}</b>!\n{_get_random(HUG_REACTIONS)}"
    await reply_text(message, text, parse_mode=ParseMode.HTML)


async def kiss_command(message: Message, args: list):
    """Handle /kiss — send a kiss."""
    user = message.from_user
    if args:
        target = " ".join(args)
    elif message.reply_to_message:
        target = message.reply_to_message.from_user.first_name
    else:
        target = "everyone"

    text = f"💋 <b>{user.first_name}</b> kisses <b>{target}</b>!\n{_get_random(KISS_REACTIONS)}"
    await reply_text(message, text, parse_mode=ParseMode.HTML)


async def slap_command(message: Message, args: list):
    """Handle /slap — send a slap."""
    user = message.from_user
    if args:
        target = " ".join(args)
    elif message.reply_to_message:
        target = message.reply_to_message.from_user.first_name
    else:
        await reply_text(message, f"{E.ERROR} Who do you want to slap?",
            parse_mode=ParseMode.HTML)
        return

    text = f"{E.WAVE} <b>{user.first_name}</b> slaps <b>{target}</b>!\n{_get_random(SLAP_REACTIONS)}"
    await reply_text(message, text, parse_mode=ParseMode.HTML)


async def poke_command(message: Message, args: list):
    """Handle /poke — send a poke."""
    user = message.from_user
    if args:
        target = " ".join(args)
    elif message.reply_to_message:
        target = message.reply_to_message.from_user.first_name
    else:
        target = "everyone"

    text = f"👉 <b>{user.first_name}</b> pokes <b>{target}</b>!\n{_get_random(POKE_REACTIONS)}"
    await reply_text(message, text, parse_mode=ParseMode.HTML)


async def tickle_command(message: Message, args: list):
    """Handle /tickle — send a tickle."""
    user = message.from_user
    if args:
        target = " ".join(args)
    elif message.reply_to_message:
        target = message.reply_to_message.from_user.first_name
    else:
        target = "everyone"

    text = f"🫳 <b>{user.first_name}</b> tickles <b>{target}</b>!\n{_get_random(TICKLE_REACTIONS)}"
    await reply_text(message, text, parse_mode=ParseMode.HTML)


async def highfive_command(message: Message, args: list):
    """Handle /highfive — send a high five."""
    user = message.from_user
    if args:
        target = " ".join(args)
    elif message.reply_to_message:
        target = message.reply_to_message.from_user.first_name
    else:
        target = "everyone"

    text = f"✋ <b>{user.first_name}</b> high-fives <b>{target}</b>!\nNice one! 🙌"
    await reply_text(message, text, parse_mode=ParseMode.HTML)


async def wave_command(message: Message, args: list):
    """Handle /wave — send a wave."""
    user = message.from_user
    if args:
        target = " ".join(args)
    elif message.reply_to_message:
        target = message.reply_to_message.from_user.first_name
    else:
        target = "everyone"

    text = f"{E.WAVE} <b>{user.first_name}</b> waves at <b>{target}</b>!\nHey there! 😄"
    await reply_text(message, text, parse_mode=ParseMode.HTML)


async def pat_command(message: Message, args: list):
    """Handle /pat — pat someone on the head."""
    user = message.from_user
    if args:
        target = " ".join(args)
    elif message.reply_to_message:
        target = message.reply_to_message.from_user.first_name
    else:
        target = "everyone"

    text = f"🤚 <b>{user.first_name}</b> pats <b>{target}</b> on the head!\nThere there... 🥺"
    await reply_text(message, text, parse_mode=ParseMode.HTML)


async def punch_command(message: Message, args: list):
    """Handle /punch — punch someone."""
    user = message.from_user
    if args:
        target = " ".join(args)
    elif message.reply_to_message:
        target = message.reply_to_message.from_user.first_name
    else:
        await reply_text(message, f"{E.ERROR} Who do you want to punch?",
            parse_mode=ParseMode.HTML)
        return

    text = f"{E.KICK} <b>{user.first_name}</b> punches <b>{target}</b>!\n{_get_random(PUNCH_REACTIONS)}"
    await reply_text(message, text, parse_mode=ParseMode.HTML)


async def kill_command(message: Message, args: list):
    """Handle /kill — playfully 'kill' someone."""
    user = message.from_user
    if args:
        target = " ".join(args)
    elif message.reply_to_message:
        target = message.reply_to_message.from_user.first_name
    else:
        await reply_text(message, f"{E.ERROR} Who do you want to eliminate?",
            parse_mode=ParseMode.HTML)
        return

    text = f"{E.SPARKLE} <b>{user.first_name}</b> points a gun at <b>{target}</b>!\n{_get_random(KILL_REACTIONS)}"
    await reply_text(message, text, parse_mode=ParseMode.HTML)


async def yeet_command(message: Message, args: list):
    """Handle /yeet — yeet someone."""
    user = message.from_user
    if args:
        target = " ".join(args)
    elif message.reply_to_message:
        target = message.reply_to_message.from_user.first_name
    else:
        await reply_text(message, f"{E.ERROR} Who do you want to yeet?",
            parse_mode=ParseMode.HTML)
        return

    text = f"{E.FIRE} <b>{user.first_name}</b> YEETS <b>{target}</b> into the void!\n{_get_random(YEET_REACTIONS)}"
    await reply_text(message, text, parse_mode=ParseMode.HTML)


# ── Module setup ─────────────────────────────────────────
def setup() -> list:
    """Register fun commands."""
    on("message", hug_command, flt=cmd("hug"))
    on("message", kiss_command, flt=cmd("kiss"))
    on("message", slap_command, flt=cmd("slap"))
    on("message", poke_command, flt=cmd("poke"))
    on("message", tickle_command, flt=cmd("tickle"))
    on("message", highfive_command, flt=cmd("highfive"))
    on("message", wave_command, flt=cmd("wave"))
    on("message", pat_command, flt=cmd("pat"))
    on("message", punch_command, flt=cmd("punch"))
    on("message", kill_command, flt=cmd("kill"))
    on("message", yeet_command, flt=cmd("yeet"))

    return ["hug", "kiss", "slap", "poke", "tickle", "highfive", "wave", "pat", "punch", "kill", "yeet"]
