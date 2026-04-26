# services/varc/main.py
#
# Slice 2.5 changes layered on slice 2:
#   - Question-serve keyboard now has a [Skip / I don't know] row (Fix 1)
#   - After answer/skip, response ends with continuation buttons; NO auto-serve
#     of the next question (Principle 1)
#   - Mid-question doubt acknowledgement (Fix 2)
#   - Show-current-question re-render (Fix 2)
#   - "Continue with a different question" doubt ack (Fix 2)
#   - LLM error_fallback shape ready, plus a slice-2.5 test trigger ("v5fail")
#     so Fix 5 is exercisable before slice 4 brings real LLM calls
#
# Slice 2 retrieval ladder (tiers 1–6) is preserved unchanged.

import json
import logging
from typing import Any, Optional

from shared.db.client import db

logger = logging.getLogger(__name__)

# Hardcoded retrieval criteria for slice 2 / 2.5. Slice 4's planner replaces.
DEFAULT_SUBSKILL = "inference_basic"
DEFAULT_DIFFICULTY = "medium"
STALE_REPEAT_DAYS = 7

TIER5_PREFIX = "We've seen this passage before — let's try it with fresh eyes."
TIER6_PREFIX = (
    "I'm running low on new questions in this category — let me serve one we "
    "did a while back to see how your thinking has changed."
)

LLM_FALLBACK_TEXT = "Hmm, having trouble thinking right now. Try again in a moment?"

# Test hook: a free-text message of "v5fail" (case-insensitive) makes VARC
# return the LLM error_fallback shape. Lets Fix 5 be tested in slice 2.5
# before any real LLM call exists. Removed once slice 4 lands the planner.
_FAIL_TEST_TRIGGER = "v5fail"


# ─── public entry point ─────────────────────────────────────────────────────


async def handle(context: dict) -> dict:
    """Action dispatcher. context['intent']['action'] selects the handler."""
    action = context["intent"]["action"]

    # Test hook for Fix 5 — only on practice_request to avoid hijacking real flows.
    msg_content = (context.get("current_message") or {}).get("content", "") or ""
    if action == "practice_request" and msg_content.strip().lower() == _FAIL_TEST_TRIGGER:
        return _error_fallback_response(context["intent"])

    if action == "answer_to_question":
        return await _handle_answer(context)
    if action == "skip_request":
        return await _handle_skip(context)
    if action == "doubt_about_current":
        return _handle_mid_question_doubt(context)
    if action == "show_current_question":
        return await _handle_show_question(context)
    if action == "continue_doubt":
        return _handle_continue_doubt(context)
    if action == "subskill_switch":
        return await _handle_practice_request(
            context,
            prefix="Sticking with inference for now — the subskill picker lands in slice 4.",
        )
    return await _handle_practice_request(context)


# ─── handlers ───────────────────────────────────────────────────────────────


async def _handle_practice_request(context: dict, prefix: Optional[str] = None) -> dict:
    student_id = context["student_id"]
    question, tier = await _retrieve_with_fallback(
        student_id=student_id,
        subskill=DEFAULT_SUBSKILL,
        difficulty=DEFAULT_DIFFICULTY,
        profile_signals=None,
    )
    if question is None:
        return _terminal_response("I'm out of fresh material right now — please try again later.")

    attempt_id = await _record_attempt(student_id, question["question_id"])

    sections: list[str] = []
    if prefix:
        sections.append(prefix)
    if tier == 5:
        sections.append(TIER5_PREFIX)
    elif tier == 6:
        sections.append(TIER6_PREFIX)
    sections.append(await _format_question(question))
    content = "\n\n".join(sections)

    options = _parse_options(question["options"])
    keyboard = _question_keyboard(question["question_id"], options, attempt_id)

    return {
        "content": content,
        "content_type": "text_with_keyboard",
        "keyboard": keyboard,
        "requires_keyboard_close": True,
        "track_question_attempt_id": attempt_id,
        "memory_deltas": {},
        "observer_events": [],
        "meta": {
            "agent": "varc",
            "response_type": "question_serve",
            "retrieved_question_id": question["question_id"],
            "fallback_tier": tier,
            "subskill": question["subskill"],
            "difficulty": question["difficulty"],
            "attempt_id": attempt_id,
        },
    }


async def _handle_answer(context: dict) -> dict:
    info = context["last_question_attempt"]
    explanation_block = await _process_answer(context["student_id"], info)
    return {
        "content": explanation_block,
        "content_type": "text_with_keyboard",
        "keyboard": _continuation_keyboard(),
        "requires_keyboard_close": False,
        "memory_deltas": {},
        "observer_events": [],
        "meta": {
            "agent": "varc",
            "response_type": "answer_explanation",
            "scored_question_id": info["attempt_row"]["question_id"],
            "was_correct": info.get("was_correct"),
        },
    }


async def _handle_skip(context: dict) -> dict:
    attempt = context["skipped_attempt"]
    q = await db.fetchrow(
        "SELECT correct_option, explanation FROM public.questions WHERE question_id = $1",
        attempt["question_id"],
    )
    await db.execute(
        """
        UPDATE v5.student_question_attempts
        SET answered_at = now(),
            skipped = true,
            is_correct = NULL,
            student_answer = NULL,
            explanation_shown = true
        WHERE id = $1::uuid
        """,
        attempt["id"],
    )

    if q is None or q["correct_option"] is None:
        head = "Skipped — couldn't fetch the answer for this one."
        explanation_text = ""
    else:
        head = f"Skipped — the answer was {q['correct_option']}."
        explanation_text = (q["explanation"] or "").strip()
    content = f"{head}\n\n{explanation_text}" if explanation_text else head

    return {
        "content": content,
        "content_type": "text_with_keyboard",
        "keyboard": _continuation_keyboard(),
        "requires_keyboard_close": False,
        "memory_deltas": {},
        "observer_events": [],
        "meta": {
            "agent": "varc",
            "response_type": "skip_explanation",
            "skipped_question_id": attempt["question_id"],
        },
    }


def _handle_mid_question_doubt(context: dict) -> dict:
    attempt = context["current_unanswered_attempt"]
    return {
        "content": (
            "Got it — I'll come back to that. First, let's finish the current "
            "question or skip it. What works?"
        ),
        "content_type": "text_with_keyboard",
        "keyboard": {"inline_keyboard": [
            [{"text": "Back to the question",
              "callback_data": f"v5_show_question_{attempt['id']}"}],
            [{"text": "Skip this question",
              "callback_data": f"v5_skip_{attempt['id']}"},
             {"text": "I have a different question",
              "callback_data": "v5_continue_doubt"}],
        ]},
        "requires_keyboard_close": False,
        "memory_deltas": {},
        "observer_events": [],
        "meta": {
            "agent": "varc",
            "response_type": "mid_question_doubt_ack",
            "doubt_about_attempt_id": str(attempt["id"]),
        },
    }


async def _handle_show_question(context: dict) -> dict:
    attempt = context["show_attempt"]
    q = await db.fetchrow(
        f"SELECT {_QUESTION_COLUMNS} FROM public.questions WHERE question_id = $1",
        attempt["question_id"],
    )
    if q is None:
        return _terminal_response("Couldn't find that question — let's try something else.")

    qd = dict(q)
    options = _parse_options(qd["options"])
    content = await _format_question(qd)
    keyboard = _question_keyboard(qd["question_id"], options, attempt["id"])

    return {
        "content": content,
        "content_type": "text_with_keyboard",
        "keyboard": keyboard,
        # Re-rendering closes any prior copy of this same question (Principle 2).
        "requires_keyboard_close": True,
        "track_question_attempt_id": str(attempt["id"]),
        "memory_deltas": {},
        "observer_events": [],
        "meta": {
            "agent": "varc",
            "response_type": "question_serve",
            "retrieved_question_id": qd["question_id"],
            "fallback_tier": 0,  # re-render, not a fresh tier walk
            "subskill": qd["subskill"],
            "difficulty": qd["difficulty"],
            "attempt_id": str(attempt["id"]),
            "rerender": True,
        },
    }


def _handle_continue_doubt(context: dict) -> dict:
    """The 'I have a different question' / continuation-row 'I have a doubt' tap.
    Offers a single [Back to current question] button if there's still an
    unanswered attempt; otherwise just the prompt with no keyboard."""
    attempt = context.get("current_unanswered_attempt")
    keyboard_rows: list[list[dict]] = []
    if attempt:
        keyboard_rows.append([{
            "text": "Back to current question",
            "callback_data": f"v5_show_question_{attempt['id']}",
        }])
    return {
        "content": "Got it, what's your question?",
        "content_type": "text_with_keyboard" if keyboard_rows else "text",
        "keyboard": {"inline_keyboard": keyboard_rows} if keyboard_rows else None,
        "requires_keyboard_close": False,
        "memory_deltas": {},
        "observer_events": [],
        "meta": {"agent": "varc", "response_type": "continue_doubt_ack"},
    }


# ─── error fallback (Fix 5) ─────────────────────────────────────────────────


def _error_fallback_response(original_intent: dict) -> dict:
    """Wraps the LLM-failure recovery shape. Stashes the original intent in
    metadata so v5_retry can re-run the request from where it failed."""
    return {
        "content": LLM_FALLBACK_TEXT,
        "content_type": "text_with_keyboard",
        "keyboard": {"inline_keyboard": [
            [{"text": "Try again", "callback_data": "v5_retry"}]
        ]},
        "requires_keyboard_close": False,
        "memory_deltas": {},
        "observer_events": [{
            "event_type": "llm_failure",
            "payload": {"agent": "varc"},
        }],
        "meta": {
            "agent": "varc",
            "response_type": "error_fallback",
            "model_used": None,
            "cost_usd": 0.0,
            "stashed_intent": original_intent,
        },
    }


# ─── 6-tier retrieval ladder (preserved from slice 2) ──────────────────────


async def _retrieve_with_fallback(
    *,
    student_id: str,
    subskill: str,
    difficulty: str,
    profile_signals: Optional[dict],
) -> tuple[Optional[dict], int]:
    if profile_signals:
        q = await _query_unseen(
            student_id, subskill=subskill, difficulty=difficulty,
            profile_signals=profile_signals,
        )
        if q:
            return q, 1
    q = await _query_unseen(student_id, subskill=subskill, difficulty=difficulty)
    if q:
        return q, 2
    q = await _query_unseen(student_id, subskill=subskill)
    if q:
        return q, 3
    q = await _query_unseen(student_id)
    if q:
        return q, 4
    q = await _query_stale_seen(student_id, subskill=subskill, days=STALE_REPEAT_DAYS)
    if q:
        return q, 5
    q = await _query_oldest_seen(student_id)
    if q:
        return q, 6
    return None, 0


_QUESTION_COLUMNS = (
    "question_id, question_text, options, correct_option, explanation, "
    "passage_id, subskill, difficulty"
)
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


async def _query_stale_seen(student_id: str, *, subskill: str, days: int) -> Optional[dict]:
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


async def _record_attempt(student_id: str, question_id: str) -> str:
    row = await db.fetchrow(
        """
        INSERT INTO v5.student_question_attempts (student_id, question_id)
        VALUES ($1::uuid, $2)
        RETURNING id
        """,
        student_id, question_id,
    )
    return str(row["id"])


async def _process_answer(student_id: str, info: dict) -> str:
    detected = info["detected_answer"]
    attempt_row = info["attempt_row"]
    question_id = attempt_row["question_id"]
    attempt_id = attempt_row["id"]

    q = await db.fetchrow(
        "SELECT correct_option, explanation FROM public.questions WHERE question_id = $1",
        question_id,
    )
    if q is None or q["correct_option"] is None:
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
            skipped = false,
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
    return f"{head}\n\n{explanation_text}" if explanation_text else head


# ─── formatting & keyboards ─────────────────────────────────────────────────


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
    return raw if isinstance(raw, dict) else {}


def _question_keyboard(question_id: str, options: dict, attempt_id: str) -> dict:
    answer_row = [
        {"text": k, "callback_data": f"v5_answer_{question_id}_{k}"}
        for k in options.keys()
    ]
    skip_row = [{
        "text": "Skip / I don't know",
        "callback_data": f"v5_skip_{attempt_id}",
    }]
    return {"inline_keyboard": [answer_row, skip_row]}


def _continuation_keyboard() -> dict:
    """5-button two-row continuation row used after answer / skip / stats."""
    return {"inline_keyboard": [
        [{"text": "Next question", "callback_data": "v5_continue_next"},
         {"text": "Different subskill", "callback_data": "v5_continue_subskill"}],
        [{"text": "Show my stats", "callback_data": "v5_continue_stats"},
         {"text": "I have a doubt", "callback_data": "v5_continue_doubt"},
         {"text": "I'm done", "callback_data": "v5_continue_done"}],
    ]}


def _terminal_response(text: str) -> dict:
    return {
        "content": text,
        "content_type": "text",
        "keyboard": None,
        "requires_keyboard_close": False,
        "memory_deltas": {},
        "observer_events": [],
        "meta": {"agent": "varc", "response_type": "terminal"},
    }
