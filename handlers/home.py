# handlers/home.py
#
# Home screen. Shows mode buttons + optional "Work on X" for weakest skill.

from telegram import Update

from bot.keyboards import home_quick_keyboard
from bot.utils import reply_to
from memory.session import set_state


async def show_home(update: Update, profile: dict, bot) -> None:
    tg_id = profile["tg_id"]
    await set_state(tg_id, {"state": "IDLE"})
    text = "What do you want to work on?"
    message_or_cb = update.callback_query or update.message
    if message_or_cb is None:
        return
    await reply_to(message_or_cb, text, reply_markup=home_quick_keyboard(profile))
