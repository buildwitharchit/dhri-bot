# handlers/practice/rc.py
#
# RC practice mode. Loads a passage with 2+ unseen questions, walks
# through them one at a time, records attempts.

import logging
from typing import Optional

from telegram import Update

from shared.telegram.keyboards import answer_keyboard, home_quick_keyboard
from shared.telegram.utils import escape_html, reply_to, send_long_message
from shared.db.client import db
from v4_legacy.db.queries import create_session, write_message
from v4_legacy.handlers.practice.common import (
    close_session,
    get_question_context,
    parse_options,
    record_attempt,
    resolve_trap_for_selection,
    utcnow_iso,
)
from shared.redis.client import set_state
from v4_legacy.memory.summarizer import generate_session_summary
from v4_legacy.retrieval.selector import PracticeSelector

logger = logging.getLogger(__name__)


async def start_rc_session(update: Update, profile: dict, bot) -> None:
    tg_id = profile["tg_id"]
    selector = PracticeSelector()
    result = await selector.get_rc_passage(profile)
    if not result:
        await reply_to(
            update.callback_query or update.message,
            "No fresh RC passage matches your profile right now. "
            "Try /pj or /va — or come back after more content is added.",
        )
        return

    passage = result["passage"]
    questions = result["questions"]
    if not questions:
        await reply_to(
            update.callback_query or update.message,
            "Couldn't load any RC questions. Try again shortly.",
        )
        return

    session_id = await create_session(tg_id, "rc")
    qids = [q["question_id"] for q in questions]
    state = {
        "state": "RC_ACTIVE",
        "session_id": session_id,
        "mode": "rc",
        "passage_id": passage["passage_id"] if passage else None,
        "questions_in_set": qids,
        "current_question_index": 0,
        "questions_answered": {},
        "questions_remaining": list(qids),
        "session_started_at": utcnow_iso(),
    }
    await set_state(tg_id, state)

    passage_text = get_question_context(questions[0], passage)
    target_message = update.callback_query.message if update.callback_query else update.message
    chat_id = target_message.chat_id
    await send_long_message(
        chat_id,
        f"<b>📖 Reading Comprehension</b>\n\n{escape_html(passage_text)}",
        bot,
    )
    await _send_question(
        chat_id=chat_id,
        question=questions[0],
        passage=passage,
        idx=0,
        total=len(questions),
        bot=bot,
    )
    await write_message(
        session_id, tg_id, "system",
        f"RC session started: passage={passage['passage_id'] if passage else 'NA'}, "
        f"questions={qids}",
    )


async def _send_question(
    *, chat_id: int, question: dict, passage: Optional[dict],
    idx: int, total: int, bot
) -> None:
    options = parse_options(question.get("options"))
    parts = [f"<b>Question {idx + 1} of {total}</b>"]
    parts.append(escape_html(question["question_text"]))
    for letter in ["A", "B", "C", "D"]:
        if letter in options:
            parts.append(f"\n{letter}) {escape_html(options[letter])}")
    await send_long_message(
        chat_id,
        "\n".join(parts),
        bot,
        reply_markup=answer_keyboard(),
    )


async def handle_rc_answer(
    update: Update, profile: dict, state: dict, selected_letter: str, bot
) -> None:
    tg_id = profile["tg_id"]
    session_id = state["session_id"]
    qids = state["questions_in_set"]
    idx = state.get("current_question_index", 0)
    if idx >= len(qids):
        await reply_to(update.callback_query or update.message, "Session already complete.")
        return

    qid = qids[idx]
    question = await db.fetchrow("SELECT * FROM questions WHERE question_id = $1", qid)
    if not question:
        logger.error(f"RC question {qid} vanished from DB mid-session")
        return
    question = dict(question)
    passage = None
    if question.get("passage_id"):
        passage_row = await db.fetchrow(
            "SELECT * FROM passages WHERE passage_id = $1", question["passage_id"]
        )
        passage = dict(passage_row) if passage_row else None

    correct = question.get("correct_option")
    is_correct = selected_letter == correct
    trap = resolve_trap_for_selection(question, selected_letter)

    await record_attempt(
        tg_id=tg_id, session_id=session_id, question=question,
        selected_option=selected_letter, is_correct=is_correct,
        trap_fallen_for=trap,
    )

    options = parse_options(question.get("options"))
    feedback_lines = [
        f"<b>{'✅ Correct' if is_correct else '❌ Not quite'}</b>",
        f"Correct answer: <b>{correct}</b>",
    ]
    if not is_correct and trap != "none":
        feedback_lines.append(
            f"Trap you fell for: <i>{trap.replace('_', ' ')}</i>"
        )
    explanation = question.get("explanation")
    if explanation:
        feedback_lines.append(f"\n{escape_html(explanation)}")

    chat_id = (update.callback_query.message if update.callback_query else update.message).chat_id
    await send_long_message(chat_id, "\n".join(feedback_lines), bot)

    state["questions_answered"][qid] = {
        "selected": selected_letter,
        "correct": bool(is_correct),
        "trap": trap,
    }
    state["questions_remaining"] = [q for q in state["questions_remaining"] if q != qid]
    next_idx = idx + 1
    state["current_question_index"] = next_idx
    await set_state(tg_id, state)

    if next_idx >= len(qids):
        await _wrap_rc_session(chat_id, tg_id, session_id, profile, bot)
        return

    next_qid = qids[next_idx]
    next_q_row = await db.fetchrow(
        "SELECT * FROM questions WHERE question_id = $1", next_qid
    )
    if not next_q_row:
        await _wrap_rc_session(chat_id, tg_id, session_id, profile, bot)
        return
    await _send_question(
        chat_id=chat_id,
        question=dict(next_q_row),
        passage=passage,
        idx=next_idx,
        total=len(qids),
        bot=bot,
    )


async def _wrap_rc_session(chat_id: int, tg_id: int, session_id: str, profile: dict, bot) -> None:
    try:
        summary = await generate_session_summary(session_id, tg_id)
    except Exception as e:
        logger.warning(f"summary failed for {session_id}: {e}")
        summary = None
    await close_session(session_id, was_completed=True, summary=summary)
    await set_state(tg_id, {"state": "IDLE"})

    tail = "\n\n<b>Session done.</b>"
    if summary:
        tail += f"\n{escape_html(summary)}"
    await bot.send_message(
        chat_id=chat_id, text=tail, parse_mode="HTML",
        reply_markup=home_quick_keyboard(profile),
    )
