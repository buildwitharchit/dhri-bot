# bot/router.py
#
# Per-update dispatch with user-level Redis lock (Section 9).
# Drops concurrent updates for the same tg_id via acquire_lock(ttl=5s).

import logging
from typing import Optional

from telegram import Update

from agent.llm import SpendCapExceededError
from bot.callbacks import route_callback
from bot.commands import route_command
from bot.free_text import handle_free_text
from bot.utils import send_error_to_update
from db.client import db
from memory.session import acquire_lock, release_lock

logger = logging.getLogger(__name__)


def get_tg_id(update: Update) -> Optional[int]:
    if update.effective_user is not None:
        return update.effective_user.id
    return None


async def route_update(update: Update, ptb_app) -> None:
    tg_id = get_tg_id(update)
    if not tg_id:
        return

    lock_key = f"lock:user:{tg_id}"
    lock_acquired = await acquire_lock(lock_key, 5)
    if not lock_acquired:
        return  # another update processing — drop this one

    try:
        await _process_update(update, ptb_app, tg_id)
    except SpendCapExceededError:
        await send_error_to_update(update, "spend_cap", ptb_app.bot)
    except Exception as e:
        logger.exception(f"Unhandled error tg_id={tg_id}: {e}")
        await send_error_to_update(update, "generic", ptb_app.bot)
    finally:
        await release_lock(lock_key)


async def _process_update(update: Update, ptb_app, tg_id: int) -> None:
    # Banned user check
    banned = await db.fetchval(
        "SELECT is_banned FROM tg_users WHERE tg_id = $1", tg_id
    )
    if banned:
        return

    if update.callback_query is not None:
        await route_callback(update, ptb_app, tg_id)
        return

    message = update.message
    if message is None:
        return

    text = (message.text or "").strip()
    if text.startswith("/"):
        command_part, _, rest = text[1:].partition(" ")
        command = command_part.split("@", 1)[0].lower()
        args = rest.strip()
        await route_command(update, command, args, ptb_app, tg_id)
        return

    await handle_free_text(update, ptb_app, tg_id)
