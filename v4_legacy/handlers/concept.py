# handlers/concept.py
#
# Concept mode — teaches a VARC concept. LLM call with SYSTEM_PROMPT_TEMPLATE,
# no current-question attachment.

import logging

from telegram import Update

from v4_legacy.agent.explainer import explain
from shared.telegram.utils import send_long_message
from config import settings
from shared.db.client import db
from v4_legacy.db.queries import write_message

logger = logging.getLogger(__name__)


async def handle_concept(
    update: Update, profile: dict, state: dict, topic: str, bot
) -> None:
    tg_id = profile["tg_id"]
    session_id = state.get("session_id") if state else None

    last_summaries = await _last_session_summaries(tg_id)
    user_message = (
        "Student wants to understand a VARC concept.\n"
        f"Their question: {topic}\n\n"
        "Teach the concept in under 180 words. Use at least one concrete "
        "example. End with one quick check question."
    )

    reply = await explain(
        profile=profile,
        user_text=user_message,
        history=[],
        last_summaries=last_summaries,
        model=settings.MODEL_CHAT,
    )

    if session_id:
        await write_message(session_id, tg_id, "user", topic)
        await write_message(session_id, tg_id, "assistant", reply)

    await send_long_message(update.message.chat_id, reply, bot)


async def _last_session_summaries(tg_id: int, limit: int = 2) -> list[str]:
    rows = await db.fetch(
        """
        SELECT summary FROM sessions
        WHERE tg_id = $1 AND summary IS NOT NULL
        ORDER BY started_at DESC
        LIMIT $2
        """,
        tg_id, limit,
    )
    return [r["summary"] for r in rows if r["summary"]]
