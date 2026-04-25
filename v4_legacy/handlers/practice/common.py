# handlers/practice/common.py
#
# Shared practice helpers: question context renderer (Section 17),
# attempt recording, session closure, trap/score updates.

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from shared.db.client import db
from v4_legacy.memory.profile import update_skill_score, update_trap_counts

logger = logging.getLogger(__name__)


def get_question_context(question: dict, passage: Optional[dict] = None) -> str:
    """
    Return the text context shown alongside a question.
    RC: full passage text
    va_summary / va_sentence_insertion: source paragraph
    PJ / va_wrong_one_out: the labeled sentences
    Others: empty string (question is self-contained)
    """
    q_type = question['type']

    if q_type == 'rc_question':
        return passage['full_text'] if passage else ""

    if q_type in ('va_summary', 'va_sentence_insertion'):
        return question.get('source_text', '') or ""

    if q_type in ('va_wrong_one_out', 'pj'):
        sentences = _coerce_sentences(question.get('sentences'))
        return "\n".join(f"{k}) {v}" for k, v in sentences.items())

    return ""


def _coerce_sentences(value: Any) -> dict:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            return {}
    return {}


def parse_options(value: Any) -> dict:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            return {}
    return {}


def parse_option_traps(value: Any) -> dict:
    return parse_options(value)


async def record_attempt(
    *,
    tg_id: int,
    session_id: str,
    question: dict,
    selected_option: Optional[str],
    is_correct: bool,
    trap_fallen_for: str = "none",
    pj_mistake_type: Optional[str] = None,
    time_taken_secs: Optional[int] = None,
) -> None:
    await db.execute(
        """
        INSERT INTO attempts (
            tg_id, session_id, question_id, selected_option, correct_option,
            is_correct, trap_fallen_for, pj_mistake_type, time_taken_secs
        ) VALUES ($1, $2::uuid, $3, $4, $5, $6, $7, $8, $9)
        """,
        tg_id, session_id, question['question_id'], selected_option,
        question.get('correct_option'), is_correct, trap_fallen_for,
        pj_mistake_type, time_taken_secs,
    )

    subskill = question.get('subskill')
    if subskill:
        await update_skill_score(tg_id, subskill, is_correct)

    if trap_fallen_for and trap_fallen_for != "none":
        await update_trap_counts(tg_id, trap_fallen_for)

    await db.execute(
        """
        UPDATE sessions SET
            questions_attempted = questions_attempted + 1,
            questions_correct = questions_correct + $1,
            last_active_at = now(),
            skills_practiced = (
                CASE
                    WHEN $2::text = ANY(COALESCE(skills_practiced, ARRAY[]::text[]))
                    THEN skills_practiced
                    ELSE array_append(COALESCE(skills_practiced, ARRAY[]::text[]), $2::text)
                END
            )
        WHERE session_id = $3::uuid
        """,
        1 if is_correct else 0, question.get('skill') or 'unknown', session_id,
    )


def resolve_trap_for_selection(
    question: dict, selected_option: Optional[str]
) -> str:
    """Look up option_traps[selected]; default to 'none' on miss/null."""
    if not selected_option:
        return "none"
    option_traps = parse_option_traps(question.get('option_traps'))
    trap = option_traps.get(selected_option)
    return trap if trap else "none"


async def close_session(
    session_id: str, *, was_completed: bool, summary: Optional[str] = None
) -> None:
    await db.execute(
        """
        UPDATE sessions SET
            ended_at = now(),
            was_completed = $1,
            summary = $2,
            duration_mins = EXTRACT(EPOCH FROM (now() - started_at)) / 60
        WHERE session_id = $3::uuid AND ended_at IS NULL
        """,
        was_completed, summary, session_id,
    )


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
