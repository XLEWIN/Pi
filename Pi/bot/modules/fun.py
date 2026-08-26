"""Fun module — Hug, kiss, slap, poke, tickle, and other fun commands.

Adapted from boa2's fun for Pi bot (python-telegram-bot).
Uses text+emoji replies (no external GIF dependencies).
"""

import random
import logging

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode

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
async def hug_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /hug — send a hug."""
    user = update.effective_user
    if context.args:
        target = " ".join(context.args)
    elif update.message.reply_to_message:
        target = update.message.reply_to_message.from_user.first_name
    else:
        target = "everyone"

    text = f"🫂 <b>{user.first_name}</b> hugs <b>{target}</b>!\n{_get_random(HUG_REACTIONS)}"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def kiss_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /kiss — send a kiss."""
    user = update.effective_user
    if context.args:
        target = " ".join(context.args)
    elif update.message.reply_to_message:
        target = update.message.reply_to_message.from_user.first_name
    else:
        target = "everyone"

    text = f"💋 <b>{user.first_name}</b> kisses <b>{target}</b>!\n{_get_random(KISS_REACTIONS)}"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def slap_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /slap — send a slap."""
    user = update.effective_user
    if context.args:
        target = " ".join(context.args)
    elif update.message.reply_to_message:
        target = update.message.reply_to_message.from_user.first_name
    else:
        await update.message.reply_text("❌ Who do you want to slap?")
        return

    text = f"👋 <b>{user.first_name}</b> slaps <b>{target}</b>!\n{_get_random(SLAP_REACTIONS)}"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def poke_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /poke — send a poke."""
    user = update.effective_user
    if context.args:
        target = " ".join(context.args)
    elif update.message.reply_to_message:
        target = update.message.reply_to_message.from_user.first_name
    else:
        target = "everyone"

    text = f"👉 <b>{user.first_name}</b> pokes <b>{target}</b>!\n{_get_random(POKE_REACTIONS)}"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def tickle_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /tickle — send a tickle."""
    user = update.effective_user
    if context.args:
        target = " ".join(context.args)
    elif update.message.reply_to_message:
        target = update.message.reply_to_message.from_user.first_name
    else:
        target = "everyone"

    text = f"🫳 <b>{user.first_name}</b> tickles <b>{target}</b>!\n{_get_random(TICKLE_REACTIONS)}"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def highfive_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /highfive — send a high five."""
    user = update.effective_user
    if context.args:
        target = " ".join(context.args)
    elif update.message.reply_to_message:
        target = update.message.reply_to_message.from_user.first_name
    else:
        target = "everyone"

    text = f"✋ <b>{user.first_name}</b> high-fives <b>{target}</b>!\nNice one! 🙌"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def wave_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /wave — send a wave."""
    user = update.effective_user
    if context.args:
        target = " ".join(context.args)
    elif update.message.reply_to_message:
        target = update.message.reply_to_message.from_user.first_name
    else:
        target = "everyone"

    text = f"👋 <b>{user.first_name}</b> waves at <b>{target}</b>!\nHey there! 😄"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def pat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /pat — pat someone on the head."""
    user = update.effective_user
    if context.args:
        target = " ".join(context.args)
    elif update.message.reply_to_message:
        target = update.message.reply_to_message.from_user.first_name
    else:
        target = "everyone"

    text = f"🤚 <b>{user.first_name}</b> pats <b>{target}</b> on the head!\nThere there... 🥺"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def punch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /punch — punch someone."""
    user = update.effective_user
    if context.args:
        target = " ".join(context.args)
    elif update.message.reply_to_message:
        target = update.message.reply_to_message.from_user.first_name
    else:
        await update.message.reply_text("❌ Who do you want to punch?")
        return

    text = f"👊 <b>{user.first_name}</b> punches <b>{target}</b>!\n{_get_random(PUNCH_REACTIONS)}"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def kill_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /kill — playfully 'kill' someone."""
    user = update.effective_user
    if context.args:
        target = " ".join(context.args)
    elif update.message.reply_to_message:
        target = update.message.reply_to_message.from_user.first_name
    else:
        await update.message.reply_text("❌ Who do you want to eliminate?")
        return

    text = f"🔫 <b>{user.first_name}</b> points a gun at <b>{target}</b>!\n{_get_random(KILL_REACTIONS)}"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def yeet_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /yeet — yeet someone."""
    user = update.effective_user
    if context.args:
        target = " ".join(context.args)
    elif update.message.reply_to_message:
        target = update.message.reply_to_message.from_user.first_name
    else:
        await update.message.reply_text("❌ Who do you want to yeet?")
        return

    text = f"🏈 <b>{user.first_name}</b> YEETS <b>{target}</b> into the void!\n{_get_random(YEET_REACTIONS)}"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# ── Module setup ─────────────────────────────────────────
def setup(app: Application) -> list:
    """Register fun commands."""
    app.add_handler(CommandHandler("hug", hug_command))
    app.add_handler(CommandHandler("kiss", kiss_command))
    app.add_handler(CommandHandler("slap", slap_command))
    app.add_handler(CommandHandler("poke", poke_command))
    app.add_handler(CommandHandler("tickle", tickle_command))
    app.add_handler(CommandHandler("highfive", highfive_command))
    app.add_handler(CommandHandler("wave", wave_command))
    app.add_handler(CommandHandler("pat", pat_command))
    app.add_handler(CommandHandler("punch", punch_command))
    app.add_handler(CommandHandler("kill", kill_command))
    app.add_handler(CommandHandler("yeet", yeet_command))

    return ["hug", "kiss", "slap", "poke", "tickle", "highfive", "wave", "pat", "punch", "kill", "yeet"]
