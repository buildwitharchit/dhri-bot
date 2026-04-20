# handlers/practice/va.py
#
# VA (verbal ability) practice mode. Menu with 7 subtypes, single-question
# sessions, answer via inline keyboard A/B/C/D.

import logging
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update

from bot.keyboards import answer_keyboard, home_quick_keyboard, va_type_keyboard
from bot.utils import escape_html, reply_to, send_long_message
from db.client import db
from db.queries import create_session
from handlers.practice.common import (
    close_session,
    get_question_context,
    parse_options,
    record_attempt,
    resolve_trap_for_selection,
)
from memory.session import set_state
from memory.summarizer import generate_session_summary
from retrieval.selector import PracticeSelector

logger = logging.getLogger(__name__)

VA_TYPE_LABELS = {
    "va_grammar":             "Grammar",
    "va_sentence_correction": "Sentence Correction",
    "va_vocab":               "Vocabulary",
    "va_fill_in_blank":       "Fill in the Blank",
    "va_wrong_one_out":       "Odd One Out",
    "va_sentence_insertion":  "Sentence Insertion",
    "va_summary":             "Passage Summary",
}


async def show_va_menu(update: Update, profile: dict, bot) -> None:
    await reply_to(
        update.callback_query or update.message,
        "What do you want to work on?",
        reply_markup=va_type_keyboard(),
    )


async def handle_va_type_selection(
    update: Update, va_type: str, profile: dict, bot
) -> None:
    """Called when student taps a VA type button."""
    selector = PracticeSelector()
    question = await selector.get_va(profile, va_type)

    callback = update.callback_query
    if callback is not None:
        await callback.answer()

    if not question:
        target = callback.message if callback else update.message
        await target.reply_text(
            f"No {VA_TYPE_LABELS[va_type]} questions available right now. "
            "Try a different type!"
        )
        return

    session_id = await create_session(profile["tg_id"], "va")
    state_update = {
        "state": "VA_ACTIVE",
        "session_id": session_id,
        "mode": "va",
        "passage_id": None,
        "va_type": va_type,
        "questions_in_set": [question["question_id"]],
        "current_question_index": 0,
        "questions_answered": {},
        "questions_remaining": [question["question_id"]],
        "session_started_at": datetime.now(timezone.utc).isoformat(),
    }
    await set_state(profile["tg_id"], state_update)

    context_text = get_question_context(question)
    target_message = callback.message if callback else update.message
    await send_va_question(target_message, question, context_text, va_type, bot)


async def send_va_question(
    message, question: dict, context_text: str, va_type: str, bot
) -> None:
    """Send a VA question with appropriate context and answer buttons."""
    type_label = VA_TYPE_LABELS.get(va_type, "VA")

    parts = [f"📝 <b>{type_label}</b>"]

    if context_text:
        parts.append(f"\n<i>{escape_html(context_text)}</i>\n")

    parts.append(escape_html(question["question_text"]))

    options = parse_options(question.get("options"))
    for letter in ["A", "B", "C", "D"]:
        if letter in options:
            parts.append(f"\n{letter}) {escape_html(options[letter])}")

    text = "\n".join(parts)
    await send_long_message(
        message.chat_id, text, bot, reply_markup=answer_keyboard()
    )


async def handle_va_answer(
    update: Update, profile: dict, state: dict, selected_letter: str, bot
) -> None:
    tg_id = profile["tg_id"]
    session_id = state["session_id"]
    qids = state.get("questions_in_set") or []
    idx = state.get("current_question_index", 0)
    if idx >= len(qids):
        await reply_to(update.callback_query or update.message, "Session already complete.")
        return
    qid = qids[idx]

    question_row = await db.fetchrow(
        "SELECT * FROM questions WHERE question_id = $1", qid
    )
    if not question_row:
        logger.error(f"VA question {qid} vanished mid-session")
        return
    question = dict(question_row)

    correct = question.get("correct_option")
    is_correct = selected_letter == correct
    trap = resolve_trap_for_selection(question, selected_letter)

    await record_attempt(
        tg_id=tg_id, session_id=session_id, question=question,
        selected_option=selected_letter, is_correct=is_correct,
        trap_fallen_for=trap,
    )

    feedback_lines = [
        f"<b>{'✅ Correct' if is_correct else '❌ Not quite'}</b>",
        f"Correct answer: <b>{correct}</b>",
    ]
    if not is_correct and trap and trap != "none":
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
    state["questions_remaining"] = []
    state["current_question_index"] = idx + 1
    await set_state(tg_id, state)

    try:
        summary = await generate_session_summary(session_id, tg_id)
    except Exception as e:
        logger.warning(f"summary failed: {e}")
        summary = None
    await close_session(session_id, was_completed=True, summary=summary)
    await set_state(tg_id, {"state": "IDLE"})

    tail = "\n<b>Session done.</b>"
    if summary:
        tail += f"\n{escape_html(summary)}"
    await bot.send_message(
        chat_id=chat_id, text=tail, parse_mode="HTML",
        reply_markup=home_quick_keyboard(profile),
    )
