"""Template module — /template command with preview image and inline selection.

Shows all rank card templates in one preview image with numbered inline buttons.
"""

import os
import logging
import tempfile

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.constants import ParseMode

from bot.database import db
from bot.profile_templates import THEMES, generate_template_preview

logger = logging.getLogger(__name__)


def _build_template_buttons():
    """Build inline keyboard with template numbers."""
    buttons = []
    row = []
    for tid, theme in THEMES.items():
        row.append(InlineKeyboardButton(
            f"{tid}. {theme['name']}",
            callback_data=f"template:{tid}",
            api_kwargs={"style": "primary" if tid == 1 else "success" if tid % 2 == 0 else "danger"}
        ))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(buttons)


async def template_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /template — show all rank card templates with preview image."""
    if update.effective_chat.type != "private":
        await update.message.reply_text("Use this command in my DM for privacy.")
        return

    user = update.effective_user
    user_id = user.id

    # Get user data for the preview
    user_data = db.get_user_level(user_id)
    level = user_data.get("global_level", 1)
    global_msgs = user_data.get("global_messages", 0)

    # Download avatar
    avatar_bytes = None
    try:
        from io import BytesIO
        photos = await context.bot.get_user_profile_photos(user_id, limit=1)
        if photos.photos:
            f = await context.bot.get_file(photos.photos[0][-1].file_id)
            buf = BytesIO()
            await f.download_to_memory(buf)
            buf.seek(0)
            avatar_bytes = buf.read()
    except Exception as e:
        logger.warning(f"Avatar download failed: {e}")

    # Generate preview image with all templates
    try:
        preview = generate_template_preview(
            name=user.first_name or "User",
            username=user.username or "user",
            level=level,
            rank="#1",
            chat_messages=f"{global_msgs // 2:,}",
            global_messages=f"{global_msgs:,}",
            avatar_bytes=avatar_bytes,
        )

        # Send preview image with inline buttons
        await update.message.reply_photo(
            photo=preview,
            caption=(
                f"<b>🎨 Rank Templates</b>\n\n"
                f"Choose a template for your rank card:\n\n"
                f"{get_theme_list()}\n\n"
                f"Click a button below to select:"
            ),
            reply_markup=_build_template_buttons(),
            parse_mode=ParseMode.HTML,
        )
        await update.message.delete()
    except Exception as e:
        logger.error(f"Failed to generate template preview: {e}")
        await update.message.reply_text(
            f"<b>🎨 Rank Templates</b>\n\n"
            f"{get_theme_list()}\n\n"
            f"<b>Usage:</b> /template &lt;number&gt;\n"
            f"<b>Example:</b> /template 3",
            reply_markup=_build_template_buttons(),
            parse_mode=ParseMode.HTML,
        )


async def template_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle template selection callback."""
    query = update.callback_query
    data = query.data

    if not data.startswith("template:"):
        return

    try:
        template_id = int(data.split(":")[1])
    except (ValueError, IndexError):
        return

    if template_id not in THEMES:
        await query.answer("Invalid template.", show_alert=True)
        return

    # Save template choice
    db.set_template(query.from_user.id, template_id)
    theme_name = THEMES[template_id]["name"]

    await query.answer(f"Template set to {theme_name}!", show_alert=False)

    # Update the message with confirmation
    try:
        await query.edit_message_caption(
            caption=(
                f"<b>✅ Template Selected!</b>\n\n"
                f"You chose: <b>{theme_name}</b> (#{template_id})\n\n"
                f"Your rank card will now use this template.\n"
                f"Use /template again to change it."
            ),
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass


def get_theme_list():
    """Get formatted theme list."""
    lines = []
    for tid, t in THEMES.items():
        lines.append(f"  {tid}. {t['name']}")
    return "\n".join(lines)


def setup(app: Application) -> list[str]:
    """Register template commands."""
    app.add_handler(CommandHandler("template", template_command))
    app.add_handler(CallbackQueryHandler(template_callback, pattern=r"^template:"))
    return ["/template", "template:* callbacks"]
