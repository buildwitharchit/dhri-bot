# services/varc/main.py
#
# Slice 2:
#   - 6-tier fallback ladder (data model doc §"VARC Retrieval Fallback Ladder")
#   - Records every serve in v5.student_question_attempts
#   - Scores answers when context.last_question_attempt is provided
#   - Tier 5 / tier 6 acknowledgements prepended to the response
#
# Hardcoded for slice 2: subskill='inference_basic', difficulty='medium'.
# Profile signals (tier 1's bonus filter) land in slice 5; until then tier 1
# is a no-op and retrieval starts at tier 2.

import json
import logging
from typing import Any, Optional

from shared.db.client import db

logger = logging.getLogger(__name__)

# Hardcoded retrieval criteria for slice 2. Slice 4's planner replaces these.
DEFAULT_SUBSKILL = "inference_basic"
DEFAULT_DIFFICULTY = "medium"
STALE_REPEAT_DAYS = 7  # tier 5 boundary

TIER5_PREFIX = "We've seen this passage before — let's try it with fresh eyes."
TIER6_PREFIX = (
    "I'm running low on new questions in this category — let me serve one we "
    "did a while back to see how your thinking has changed."
)


# ─── public entry point ─────────────────────────────────────────────────────


async def handle(context: dict) -> dict:
    student_id = context["student_id"]

    # 1. If the orchestrator detected an answer to the last served question,
    #    score it and produce an explanation block to prepend.
    explanation_block = ""
    last_attempt_info = context.get("last_question_attempt")
    if last_attempt_info:
        explanation_block = await _process_answer(student_id, last_attempt_info)

    # 2. Retrieve the next question via the 6-tier ladder.
    question, tier = await _retrieve_with_fallback(
        student_id=student_id,
        subskill=DEFAULT_SUBSKILL,
        difficulty=DEFAULT_DIFFICULTY,
        profile_signals=None,  # slice 5 populates these
    )

    if question is None:
        return _stub_response(
            (explanation_block + "\n\n" if explanation_block else "")
            + "I'm out of fresh material right now — please try again later."
        )

    # 3. Record the serve.
    await _record_attempt(student_id, question["question_id"])

    # 4. Build the response content: explanation + acknowledgement + question.
    sections: list[str] = []
    if explanation_block:
        sections.append(explanation_block)
    if tier == 5:
        sections.append(TIER5_PREFIX)
    elif tier == 6:
        sections.append(TIER6_PREFIX)
    sections.append(await _format_question(question))
    content = "\n\n".join(sections)

    options = _parse_options(question["options"])
    keyboard = _options_keyboard(question["question_id"], options) if options else None

    meta: dict[str, Any] = {
        "agent": "varc",
        "retrieved_question_id": question["question_id"],
        "fallback_tier": tier,
        "subskill": question["subskill"],
        "difficulty": question["difficulty"],
    }
    if last_attempt_info:
        meta["scored_question_id"] = last_attempt_info["attempt_row"]["question_id"]
        meta["was_correct"] = last_attempt_info.get("was_correct")

    return {
        "content": content,
        "content_type": "text_with_keyboard" if keyboard else "text",
        "keyboard": keyboard,
        "memory_deltas": {},
        "observer_events": [],
        "meta": meta,
    }


# ─── 6-tier retrieval ladder ────────────────────────────────────────────────


async def _retrieve_with_fallback(
    *,
    student_id: str,
    subskill: str,
    difficulty: str,
    profile_signals: Optional[dict],
) -> tuple[Optional[dict], int]:
    """Try tiers 1..6 in order. Returns (question_row, tier) or (None, 0)."""

    # Tier 1: profile-bonus filter on top of subskill+difficulty (unseen).
    # No profile signals exist until slice 5, so this tier currently no-ops.
    if profile_signals:
        q = await _query_unseen(
            student_id, subskill=subskill, difficulty=difficulty,
            profile_signals=profile_signals,
        )
        if q:
            return q, 1

    # Tier 2: unseen + subskill + difficulty.
    q = await _query_unseen(student_id, subskill=subskill, difficulty=difficulty)
    if q:
        return q, 2

    # Tier 3: unseen + subskill (drop difficulty).
    q = await _query_unseen(student_id, subskill=subskill)
    if q:
        return q, 3

    # Tier 4: unseen + any subskill.
    q = await _query_unseen(student_id)
    if q:
        return q, 4

    # Tier 5: seen, but most recent serve was > STALE_REPEAT_DAYS ago, subskill match.
    q = await _query_stale_seen(student_id, subskill=subskill, days=STALE_REPEAT_DAYS)
    if q:
        return q, 5

    # Tier 6: any seen, oldest last-serve first.
    q = await _query_oldest_seen(student_id)
    if q:
        return q, 6

    return None, 0


_QUESTION_COLUMNS = (
    "question_id, question_text, options, correct_option, explanation, "
    "passage_id, subskill, difficulty"
)
# Filtering on correct_option IS NOT NULL keeps PJ (order-based) and other
# non-letter answer types out of the ladder. Slice 2 only handles A/B/C/D.
_BASE_FILTERS = (
    "is_active = true AND needs_review = false AND correct_option IS NOT NULL"
)


async def _query_unseen(
    student_id: str,
    *,
    subskill: Optional[str] = None,
    difficulty: Optional[str] = None,
    profile_signals: Optional[dict] = None,  # noqa: ARG001  reserved for slice 5
) -> Optional[dict]:
    clauses = [_BASE_FILTERS]
    args: list[Any] = []
    if subskill is not None:
        args.append(subskill)
        clauses.append(f"q.subskill = ${len(args)}")
    if difficulty is not None:
        args.append(difficulty)
        clauses.append(f"q.difficulty = ${len(args)}")
    args.append(student_id)
    sql = f"""
        SELECT {_QUESTION_COLUMNS}
        FROM public.questions q
        WHERE {' AND '.join(clauses)}
          AND NOT EXISTS (
              SELECT 1 FROM v5.student_question_attempts a
              WHERE a.student_id = ${len(args)}::uuid
                AND a.question_id = q.question_id
          )
        ORDER BY random()
        LIMIT 1
    """
    row = await db.fetchrow(sql, *args)
    return dict(row) if row else None


async def _query_stale_seen(
    student_id: str, *, subskill: str, days: int
) -> Optional[dict]:
    sql = f"""
        SELECT {_QUESTION_COLUMNS}
        FROM public.questions q
        WHERE {_BASE_FILTERS}
          AND q.subskill = $1
          AND EXISTS (
              SELECT 1 FROM v5.student_question_attempts a
              WHERE a.student_id = $2::uuid AND a.question_id = q.question_id
          )
          AND NOT EXISTS (
              SELECT 1 FROM v5.student_question_attempts a
              WHERE a.student_id = $2::uuid
                AND a.question_id = q.question_id
                AND a.served_at > now() - make_interval(days => $3)
          )
        ORDER BY random()
        LIMIT 1
    """
    row = await db.fetchrow(sql, subskill, student_id, days)
    return dict(row) if row else None


async def _query_oldest_seen(student_id: str) -> Optional[dict]:
    sql = f"""
        SELECT {_QUESTION_COLUMNS}
        FROM public.questions q
        JOIN (
            SELECT question_id, max(served_at) AS last_served
            FROM v5.student_question_attempts
            WHERE student_id = $1::uuid
            GROUP BY question_id
        ) seen ON seen.question_id = q.question_id
        WHERE {_BASE_FILTERS}
        ORDER BY seen.last_served ASC
        LIMIT 1
    """
    row = await db.fetchrow(sql, student_id)
    return dict(row) if row else None


# ─── attempt persistence ────────────────────────────────────────────────────


async def _record_attempt(student_id: str, question_id: str) -> None:
    await db.execute(
        """
        INSERT INTO v5.student_question_attempts (student_id, question_id)
        VALUES ($1::uuid, $2)
        """,
        student_id, question_id,
    )


async def _process_answer(student_id: str, info: dict) -> str:
    """Score the answer, update the attempt row, return formatted explanation."""
    detected = info["detected_answer"]
    attempt_row = info["attempt_row"]
    question_id = attempt_row["question_id"]
    attempt_id = attempt_row["id"]

    q = await db.fetchrow(
        "SELECT correct_option, explanation FROM public.questions WHERE question_id = $1",
        question_id,
    )
    if q is None or q["correct_option"] is None:
        # Question vanished (shouldn't happen) — mark answered with nulls.
        await db.execute(
            """
            UPDATE v5.student_question_attempts
            SET answered_at = now(), student_answer = $2, explanation_shown = true
            WHERE id = $1::uuid
            """,
            attempt_id, detected,
        )
        return "Couldn't score that answer — let's try a fresh question."

    correct_option = (q["correct_option"] or "").strip().upper()
    is_correct = detected == correct_option
    info["was_correct"] = is_correct

    await db.execute(
        """
        UPDATE v5.student_question_attempts
        SET answered_at = now(),
            is_correct = $2,
            student_answer = $3,
            explanation_shown = true
        WHERE id = $1::uuid
        """,
        attempt_id, is_correct, detected,
    )

    explanation_text = (q["explanation"] or "").strip()
    if is_correct:
        head = f"Correct ✓  ({detected})"
    else:
        head = f"Not quite — you picked {detected}, the answer was {correct_option}."
    if explanation_text:
        return f"{head}\n\n{explanation_text}"
    return head


# ─── formatting ─────────────────────────────────────────────────────────────


async def _format_question(question: dict) -> str:
    options = _parse_options(question["options"])
    parts: list[str] = []
    if question.get("passage_id"):
        passage = await db.fetchrow(
            "SELECT full_text FROM public.passages WHERE passage_id = $1",
            question["passage_id"],
        )
        if passage and passage["full_text"]:
            parts.append(f"Passage:\n\n{passage['full_text']}")
    parts.append(f"Question: {question['question_text']}")
    if options:
        parts.append("\n".join(f"{k}) {v}" for k, v in options.items()))
    return "\n\n".join(parts)


def _parse_options(raw: Any) -> dict:
    if raw is None:
        return {}
    if isinstance(raw, str):
        try:
            return json.loads(raw) or {}
        except (TypeError, ValueError):
            return {}
    if isinstance(raw, dict):
        return raw
    return {}


def _options_keyboard(question_id: str, options: dict) -> dict:
    buttons = [
        {"text": key, "callback_data": f"v5_answer_{question_id}_{key}"}
        for key in options.keys()
    ]
    return {"inline_keyboard": [buttons]}


def _stub_response(text: str) -> dict:
    return {
        "content": text,
        "content_type": "text",
        "keyboard": None,
        "memory_deltas": {},
        "observer_events": [],
        "meta": {"agent": "varc", "fallback_tier": 0},
    }
