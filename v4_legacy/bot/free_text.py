# bot/free_text.py
#
# Free-text intent router. Counts against the 50/day rate limit.

import logging

from telegram import Update

from v4_legacy.agent.classifier import classify_free_text
from shared.telegram.utils import send_error_to_update
from v4_legacy.db.queries import check_and_increment_rate_limit, get_or_create_user
from v4_legacy.handlers.concept import handle_concept
from v4_legacy.handlers.doubt import handle_doubt
from v4_legacy.handlers.practice.pj import handle_pj_answer_text
from shared.redis.client import get_state

logger = logging.getLogger(__name__)

_ACTIVE_STATES = {"RC_ACTIVE", "PJ_ACTIVE", "VA_ACTIVE"}


async def handle_free_text(update: Update, ptb_app, tg_id: int) -> None:
    text = (update.message.text or "").strip() if update.message else ""
    if not text:
        return

    bot = ptb_app.bot
    state = await get_state(tg_id) or {"state": "IDLE"}
    profile = await get_or_create_user(
        tg_id,
        update.effective_user.username,
        update.effective_user.first_name,
    )

    in_active_practice = state.get("state") in _ACTIVE_STATES
    intent = classify_free_text(text, in_active_practice=in_active_practice)

    # PJ answer is free-text but does NOT call the LLM → don't charge rate limit.
    if intent == "pj_answer" and state.get("state") == "PJ_ACTIVE":
        await handle_pj_answer_text(update, profile, state, text, bot)
        return

    # All other free-text routes hit the LLM → rate-limit first.
    allowed, count = await check_and_increment_rate_limit(tg_id)
    if not allowed:
        await send_error_to_update(update, "rate_limit", bot)
        return

    if intent == "concept":
        await handle_concept(update, profile, state, text, bot)
    else:
        await handle_doubt(update, profile, state, text, bot)
