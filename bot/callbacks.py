# bot/callbacks.py
#
# Callback query router. Never counts against rate limit.

import logging

from telegram import Update

from db.client import db
from db.queries import get_or_create_user
from handlers.home import show_home
from handlers.onboarding import handle_level_selection, handle_year_selection
from handlers.practice.pj import start_pj_session
from handlers.practice.rc import handle_rc_answer, start_rc_session
from handlers.practice.va import handle_va_answer, handle_va_type_selection, show_va_menu
from handlers.resume import handle_resume_selection
from handlers.stats import handle_stats
from memory.session import get_state

logger = logging.getLogger(__name__)


async def route_callback(update: Update, ptb_app, tg_id: int) -> None:
    callback = update.callback_query
    if callback is None:
        return
    data = callback.data or ""
    bot = ptb_app.bot

    profile = await get_or_create_user(
        tg_id, update.effective_user.username, update.effective_user.first_name
    )
    state = await get_state(tg_id) or {"state": "IDLE"}

    # Onboarding
    if data.startswith("onboard_year_"):
        year = int(data.removeprefix("onboard_year_"))
        await callback.answer()
        await handle_year_selection(update, year, bot)
        return
    if data.startswith("onboard_level_"):
        level = data.removeprefix("onboard_level_")
        await callback.answer()
        await handle_level_selection(update, level, bot)
        return

    # Mode entry
    if data == "mode_rc":
        await callback.answer()
        await start_rc_session(update, profile, bot)
        return
    if data == "mode_pj":
        await callback.answer()
        await start_pj_session(update, profile, bot)
        return
    if data == "mode_va":
        await callback.answer()
        await show_va_menu(update, profile, bot)
        return

    # VA type selection
    if data.startswith("va_type_"):
        va_type = data.removeprefix("va_type_")
        await handle_va_type_selection(update, va_type, profile, bot)
        return

    # Answer buttons (A-D) — route by active mode
    if data.startswith("answer_"):
        letter = data.removeprefix("answer_")
        await callback.answer()
        if state.get("state") == "RC_ACTIVE":
            await handle_rc_answer(update, profile, state, letter, bot)
        elif state.get("state") == "VA_ACTIVE":
            await handle_va_answer(update, profile, state, letter, bot)
        else:
            await callback.message.reply_text(
                "No active question to answer. Tap /rc, /pj or /va to start."
            )
        return

    # Stats
    if data == "stats_full":
        await callback.answer()
        await handle_stats(callback.message, state, profile, bot)
        return
    if data == "drill_weakest":
        await callback.answer()
        weakest = profile.get("weakest_skill")
        if not weakest:
            await callback.message.reply_text(
                "I need at least 10 attempts before I know your weakest skill. "
                "Keep practising — tap /rc, /pj or /va."
            )
            return
        # Route to RC by default; RC selector will pull weakest RC subskill
        if weakest in {"para_jumbles"}:
            await start_pj_session(update, profile, bot)
        elif weakest in {"vocab_grammar"}:
            await show_va_menu(update, profile, bot)
        else:
            await start_rc_session(update, profile, bot)
        return

    # Resume
    if data.startswith("resume_"):
        session_id = data.removeprefix("resume_")
        await callback.answer()
        await handle_resume_selection(update, session_id, profile, bot)
        return

    logger.warning(f"unhandled callback data: {data}")
    await callback.answer("Not wired up yet.")
