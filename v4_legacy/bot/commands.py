# bot/commands.py
#
# /-command handlers. Commands never count against the rate limit.

import logging

from telegram import Update

from v4_legacy.db.queries import get_or_create_user
from v4_legacy.handlers.concept import handle_concept
from v4_legacy.handlers.doubt import handle_doubt
from v4_legacy.handlers.home import show_home
from v4_legacy.handlers.onboarding import handle_start
from v4_legacy.handlers.practice.pj import start_pj_session
from v4_legacy.handlers.practice.rc import start_rc_session
from v4_legacy.handlers.practice.va import show_va_menu
from v4_legacy.handlers.resume import handle_resume
from v4_legacy.handlers.stats import handle_stats
from shared.redis.client import clear_state, get_state
from v4_legacy.handlers.practice.common import close_session

logger = logging.getLogger(__name__)

COMMAND_LIST = [
    "start", "rc", "pj", "va", "doubt", "concept",
    "stats", "weak", "resume", "settings", "feedback",
    "done", "help", "broadcast", "ban",
]


async def route_command(update: Update, command: str, args: str, ptb_app, tg_id: int) -> None:
    bot = ptb_app.bot
    state = await get_state(tg_id) or {"state": "IDLE"}

    if command == "start":
        await handle_start(update, bot)
        return

    profile = await get_or_create_user(
        tg_id, update.effective_user.username, update.effective_user.first_name
    )

    if command == "rc":
        await start_rc_session(update, profile, bot)
    elif command == "pj":
        await start_pj_session(update, profile, bot)
    elif command == "va":
        await show_va_menu(update, profile, bot)
    elif command == "stats":
        await handle_stats(update.message, state, profile, bot)
    elif command == "weak":
        # "Drill my weakest" shortcut
        from v4_legacy.handlers.home import show_home as _show_home
        await _show_home(update, profile, bot)
    elif command == "resume":
        await handle_resume(update, profile, state, bot)
    elif command == "done":
        await _end_current_session(state, bot, update.message.chat_id, tg_id, profile)
    elif command == "help":
        await _send_help(update.message.chat_id, bot)
    elif command == "doubt":
        if not args:
            await update.message.reply_text(
                "Send your question after /doubt, e.g. "
                "<code>/doubt why is C wrong</code>",
                parse_mode="HTML",
            )
            return
        await handle_doubt(update, profile, state, args, bot)
    elif command == "concept":
        if not args:
            await update.message.reply_text(
                "Send a topic after /concept, e.g. "
                "<code>/concept inference</code>",
                parse_mode="HTML",
            )
            return
        await handle_concept(update, profile, state, args, bot)
    elif command in ("settings", "feedback"):
        await update.message.reply_text(
            "That command isn't wired up yet. Beta scope: /rc /pj /va /stats /resume /doubt /concept."
        )
    elif command in ("broadcast", "ban"):
        await _admin_only_stub(update, command)
    else:
        await _send_help(update.message.chat_id, bot)


async def _send_help(chat_id: int, bot) -> None:
    text = (
        "<b>Commands</b>\n"
        "/rc — Reading Comprehension passage\n"
        "/pj — Para Jumble\n"
        "/va — Vocab &amp; Grammar (menu)\n"
        "/doubt &lt;message&gt; — ask for tutoring help\n"
        "/concept &lt;topic&gt; — learn a concept\n"
        "/stats — your skill breakdown\n"
        "/resume — resume an unfinished session\n"
        "/done — end the current session\n"
    )
    await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")


async def _admin_only_stub(update: Update, command: str) -> None:
    await update.message.reply_text(f"/{command} is admin-only and not wired up yet.")


async def _end_current_session(
    state: dict, bot, chat_id: int, tg_id: int, profile: dict
) -> None:
    session_id = state.get("session_id") if state else None
    if not session_id:
        await bot.send_message(chat_id=chat_id, text="No active session to end.")
        return
    from v4_legacy.memory.summarizer import generate_session_summary
    try:
        summary = await generate_session_summary(session_id, tg_id)
    except Exception as e:
        logger.warning(f"summary failed on /done: {e}")
        summary = None
    await close_session(session_id, was_completed=True, summary=summary)
    await clear_state(tg_id)
    tail = "Session ended."
    if summary:
        tail += f"\n{summary}"
    await bot.send_message(chat_id=chat_id, text=tail)
