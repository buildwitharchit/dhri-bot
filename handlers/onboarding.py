# handlers/onboarding.py
#
# Two-step onboarding: target year → experience level → home.
# No diagnostic — immediate practice after selection per Section 11.

import logging

from telegram import Update

from bot.keyboards import (
    home_quick_keyboard,
    onboarding_level_keyboard,
    onboarding_year_keyboard,
)
from db.client import db
from db.queries import get_or_create_user, initialize_skill_scores
from memory.session import set_state

logger = logging.getLogger(__name__)


async def handle_start(update: Update, bot) -> None:
    tg_id = update.effective_user.id
    username = update.effective_user.username
    first_name = update.effective_user.first_name

    profile = await get_or_create_user(tg_id, username, first_name)

    # Legacy defensive — seed scores even if existing user somehow has none
    existing_scores = await db.fetchval(
        "SELECT count(*) FROM user_skill_scores WHERE tg_id = $1", tg_id
    )
    if not existing_scores:
        await initialize_skill_scores(tg_id)

    if not profile.get("target_year"):
        await set_state(tg_id, {"state": "ONBOARD_YEAR"})
        await bot.send_message(
            chat_id=update.effective_chat.id,
            text=(
                "Welcome to the DHRI VARC bot. Which CAT are you targeting?"
            ),
            reply_markup=onboarding_year_keyboard(),
        )
        return

    await set_state(tg_id, {"state": "IDLE"})
    await bot.send_message(
        chat_id=update.effective_chat.id,
        text="Welcome back. Pick a mode below to start practising.",
        reply_markup=home_quick_keyboard(profile),
    )


async def handle_year_selection(update: Update, year: int, bot) -> None:
    tg_id = update.effective_user.id
    await db.execute(
        "UPDATE tg_users SET target_year = $1 WHERE tg_id = $2", year, tg_id
    )
    await set_state(tg_id, {"state": "ONBOARD_LEVEL"})
    await update.callback_query.message.reply_text(
        f"Targeting CAT {year}. How would you describe your current level?",
        reply_markup=onboarding_level_keyboard(),
    )


async def handle_level_selection(update: Update, level: str, bot) -> None:
    tg_id = update.effective_user.id
    await db.execute(
        "UPDATE tg_users SET experience = $1 WHERE tg_id = $2", level, tg_id
    )
    profile = await get_or_create_user(
        tg_id, update.effective_user.username, update.effective_user.first_name
    )
    await set_state(tg_id, {"state": "IDLE"})
    await update.callback_query.message.reply_text(
        "You're set. Tap a mode below to jump in.",
        reply_markup=home_quick_keyboard(profile),
    )
