# handlers/doubt.py
#
# Free-text doubt mode. Attaches current question context (if any) and
# asks the LLM to tutor Socratically per SYSTEM_PROMPT_TEMPLATE rule 1.

import logging
from typing import Optional

from telegram import Update

from agent.explainer import explain
from bot.utils import escape_html, send_long_message
from config import settings
from db.client import db
from db.queries import get_session_messages, write_message

logger = logging.getLogger(__name__)


async def handle_doubt(
    update: Update, profile: dict, state: dict, text: str, bot
) -> None:
    tg_id = profile["tg_id"]
    session_id = state.get("session_id") if state else None

    context_snippet = ""
    if session_id and state.get("mode") in ("rc", "pj", "va"):
        qids = state.get("questions_in_set") or []
        idx = state.get("current_question_index", 0)
        if qids and 0 <= idx < len(qids):
            qid = qids[idx]
            q_row = await db.fetchrow(
                "SELECT question_text, explanation, options FROM questions "
                "WHERE question_id = $1",
                qid,
            )
            if q_row:
                context_snippet = (
                    f"Current question: {q_row['question_text']}"
                )

    last_summaries = await _last_session_summaries(tg_id)
    user_message = (
        f"{context_snippet}\n\nStudent message: {text}"
        if context_snippet else text
    )

    history = []
    if session_id:
        history = await get_session_messages(session_id, limit=10)

    reply = await explain(
        profile=profile,
        user_text=user_message,
        history=history,
        last_summaries=last_summaries,
        model=settings.MODEL_COMPLEX,
    )

    if session_id:
        await write_message(session_id, tg_id, "user", text)
        await write_message(session_id, tg_id, "assistant", reply)

    await send_long_message(
        update.message.chat_id, reply, bot
    )


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
