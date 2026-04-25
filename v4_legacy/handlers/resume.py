# handlers/resume.py
#
# /resume: lists unfinished sessions in last 48h; on tap, validates and
# re-enters the question that was active at snapshot time.

import json
import logging
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update

from shared.telegram.keyboards import answer_keyboard, home_quick_keyboard
from shared.telegram.utils import escape_html, format_ago, reply_to, send_long_message
from shared.db.client import db
from v4_legacy.db.queries import get_state_from_db_or_redis
from v4_legacy.handlers.practice.common import get_question_context, parse_options
from shared.redis.client import set_state

logger = logging.getLogger(__name__)


async def handle_resume(update: Update, profile: dict, state: dict, bot) -> None:
    tg_id = profile["tg_id"]
    rows = await db.fetch(
        """
        SELECT s.session_id, s.mode, s.started_at, s.was_completed, s.ended_at,
               ss.session_id AS snap_session_id, ss.current_question_id
        FROM sessions s
        LEFT JOIN session_snapshots ss ON ss.session_id = s.session_id
        WHERE s.tg_id = $1
          AND s.started_at > now() - interval '48 hours'
          AND (s.ended_at IS NULL OR s.was_completed = false)
          AND ss.session_id IS NOT NULL
        ORDER BY s.started_at DESC
        LIMIT 5
        """,
        tg_id,
    )
    if not rows:
        await reply_to(
            update.callback_query or update.message,
            "No unfinished sessions to resume.",
        )
        return

    buttons = []
    for r in rows:
        sid = str(r["session_id"])
        label = f"⚠️ {r['mode'].upper()} · {format_ago(r['started_at'])}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"resume_{sid}")])
    await reply_to(
        update.callback_query or update.message,
        "Pick a session to resume:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def handle_resume_selection(
    update: Update, session_id: str, profile: dict, bot
) -> None:
    tg_id = profile["tg_id"]
    restored = await get_state_from_db_or_redis(tg_id, session_id)
    if not restored:
        await reply_to(
            update.callback_query or update.message,
            "That session is no longer resumable.",
        )
        return

    qids = restored.get("questions_in_set") or []
    idx = restored.get("current_question_index", 0)
    if idx >= len(qids):
        await reply_to(
            update.callback_query or update.message,
            "That session is already complete.",
        )
        return
    qid = qids[idx]

    q_row = await db.fetchrow(
        """
        SELECT * FROM questions
        WHERE question_id = $1 AND is_active = true AND needs_review = false
        """,
        qid,
    )
    if not q_row:
        await reply_to(
            update.callback_query or update.message,
            "That question is no longer active. Starting fresh is safer — try /rc, /pj or /va.",
        )
        return
    question = dict(q_row)

    passage = None
    if question.get("passage_id"):
        p = await db.fetchrow(
            "SELECT * FROM passages WHERE passage_id = $1", question["passage_id"]
        )
        passage = dict(p) if p else None

    await set_state(tg_id, restored)

    chat_id = (
        update.callback_query.message.chat_id if update.callback_query else update.message.chat_id
    )

    if question["type"] == "rc_question" and passage:
        await send_long_message(
            chat_id,
            f"<b>📖 Resuming RC</b>\n\n{escape_html(passage['full_text'])}",
            bot,
        )

    context_text = get_question_context(question, passage)
    options = parse_options(question.get("options"))
    parts = [f"<b>Question {idx + 1} of {len(qids)}</b>"]
    if context_text and question["type"] != "rc_question":
        parts.append(f"\n<i>{escape_html(context_text)}</i>\n")
    parts.append(escape_html(question["question_text"]))
    for letter in ["A", "B", "C", "D"]:
        if letter in options:
            parts.append(f"\n{letter}) {escape_html(options[letter])}")

    reply_markup: Any = answer_keyboard() if options else None
    await send_long_message(chat_id, "\n".join(parts), bot, reply_markup=reply_markup)
