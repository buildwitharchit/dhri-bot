# handlers/practice/pj.py
#
# Para jumble practice. Single-question sessions using the uniform v4.1
# state structure.
#
# FIX 7 — current question id is always derived from
# state['questions_in_set'][state['current_question_index']].
# There is no state['current_question_id'] field in v4.1.

import logging
from typing import Optional

from telegram import Update

from v4_legacy.agent.classifier import is_pj_answer
from shared.telegram.keyboards import home_quick_keyboard
from shared.telegram.utils import escape_html, reply_to, send_long_message
from shared.db.client import db
from v4_legacy.db.queries import create_session, write_message
from v4_legacy.handlers.practice.common import (
    close_session,
    get_question_context,
    record_attempt,
    utcnow_iso,
)
from shared.redis.client import set_state
from v4_legacy.memory.summarizer import generate_session_summary
from v4_legacy.retrieval.selector import PracticeSelector

logger = logging.getLogger(__name__)


async def start_pj_session(update: Update, profile: dict, bot) -> None:
    tg_id = profile["tg_id"]
    selector = PracticeSelector()
    question = await selector.get_pj(profile)
    if not question:
        await reply_to(
            update.callback_query or update.message,
            "No fresh para-jumble matches your profile right now. "
            "Try /rc or /va.",
        )
        return

    session_id = await create_session(tg_id, "pj")
    qid = question["question_id"]
    state = {
        "state": "PJ_ACTIVE",
        "session_id": session_id,
        "mode": "pj",
        "passage_id": None,
        "questions_in_set": [qid],
        "current_question_index": 0,
        "questions_answered": {},
        "questions_remaining": [qid],
        "session_started_at": utcnow_iso(),
    }
    await set_state(tg_id, state)
    await _send_pj_prompt(update, question, bot)
    await write_message(session_id, tg_id, "system", f"PJ session started: {qid}")


async def _send_pj_prompt(update: Update, question: dict, bot) -> None:
    target_message = update.callback_query.message if update.callback_query else update.message
    context_text = get_question_context(question)
    parts = [
        "<b>🔀 Para Jumble</b>",
        escape_html(question["question_text"]),
        f"\n{escape_html(context_text)}",
        "\nEnter the sequence (e.g., <code>4,1,2,3</code>)",
    ]
    await send_long_message(target_message.chat_id, "\n".join(parts), bot)


async def handle_pj_answer_text(
    update: Update, profile: dict, state: dict, text: str, bot
) -> None:
    """Handle a free-text PJ answer (4 distinct digits 1-4)."""
    tg_id = profile["tg_id"]
    if not is_pj_answer(text):
        await reply_to(
            update.message,
            "PJ answer needs four distinct digits 1-4, e.g. <code>4,1,2,3</code>.",
        )
        return

    # FIX 7: current question id derived from questions_in_set + index
    qids = state.get("questions_in_set") or []
    idx = state.get("current_question_index", 0)
    if idx >= len(qids):
        await reply_to(update.message, "Session already complete.")
        return
    qid = qids[idx]

    question_row = await db.fetchrow(
        "SELECT * FROM questions WHERE question_id = $1", qid
    )
    if not question_row:
        logger.error(f"PJ question {qid} vanished from DB mid-session")
        return
    question = dict(question_row)

    submitted = "".join(ch for ch in text if ch in "1234")
    correct_order = (question.get("correct_order") or "").replace(" ", "").replace(",", "")
    is_correct = submitted == correct_order

    pj_mistake_type = None
    if not is_correct:
        pj_mistake_type = _diagnose_pj_mistake(submitted, correct_order.replace(",", ""))

    await record_attempt(
        tg_id=tg_id,
        session_id=state["session_id"],
        question=question,
        selected_option=submitted,
        is_correct=is_correct,
        trap_fallen_for="none",
        pj_mistake_type=pj_mistake_type,
    )

    feedback_lines = [
        f"<b>{'✅ Correct' if is_correct else '❌ Not quite'}</b>",
        f"Correct order: <b>{','.join(correct_order)}</b>",
    ]
    if not is_correct and pj_mistake_type:
        feedback_lines.append(
            f"Mistake type: <i>{pj_mistake_type.replace('_', ' ')}</i>"
        )
    explanation = question.get("explanation")
    if explanation:
        feedback_lines.append(f"\n{escape_html(explanation)}")

    await send_long_message(update.message.chat_id, "\n".join(feedback_lines), bot)

    state["questions_answered"][qid] = {
        "selected": submitted,
        "correct": bool(is_correct),
        "pj_mistake_type": pj_mistake_type,
    }
    state["questions_remaining"] = []
    state["current_question_index"] = idx + 1
    await set_state(tg_id, state)

    try:
        summary = await generate_session_summary(state["session_id"], tg_id)
    except Exception as e:
        logger.warning(f"summary failed: {e}")
        summary = None
    await close_session(state["session_id"], was_completed=True, summary=summary)
    await set_state(tg_id, {"state": "IDLE"})

    tail = "\n<b>Session done.</b>"
    if summary:
        tail += f"\n{escape_html(summary)}"
    await bot.send_message(
        chat_id=update.message.chat_id,
        text=tail,
        parse_mode="HTML",
        reply_markup=home_quick_keyboard(profile),
    )


def _diagnose_pj_mistake(submitted: str, correct: str) -> Optional[str]:
    if len(submitted) != len(correct):
        return "wrong_length"
    if sorted(submitted) != sorted(correct):
        return "missing_or_extra_sentence"
    if submitted[0] != correct[0]:
        return "wrong_opener"
    if submitted[-1] != correct[-1]:
        return "wrong_closer"
    return "middle_order"
