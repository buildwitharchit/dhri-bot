# services/varc/main.py
#
# Slice 3 changes layered on slice 2.5:
#   - LLM-generated explanations for answers and skips (Haiku via
#     MODEL_VARC_TUTOR), with the system prompt enforcing Principle 1
#     (no inline new question / no A/B/C/D in the model's output).
#   - Resume prompt for returning-after-break (Sonnet via MODEL_VARC_RESUME).
#   - resume_question action that re-serves a specific unanswered question.
#   - All attempt rows now carry session_id (threaded from orchestrator).
#   - Every LLM call is logged to v5.llm_calls.
#
# Slice 4 changes:
#   - intent.subskill / intent.difficulty supplied by planner drive retrieval.
#   - intent.secondary_signal + response_guidance flow into the explanation
#     LLM prompt for tone calibration (Bug 15).
#   - "v5fail" free-text test trigger removed — real LLM error handling now
#     wraps every openrouter call and returns _error_fallback_response on
#     failure. To exercise the fallback path, break OPENROUTER_API_KEY.
#
# Slice 5 changes:
#   - Difficulty fallback chain now: intent.difficulty (planner) →
#     context.default_difficulty (profile-derived, threaded by orchestrator
#     from profile_service.get_default_difficulty) → "medium" (defensive
#     final fallback). The slice-2-era hardcoded DEFAULT_DIFFICULTY
#     constant is gone (Bug 23).
#
# Preserved from slice 2.5 + 3:
#   - 6-tier retrieval ladder
#   - Skip flow with continuation buttons
#   - Mid-question doubt + show_current_question + continue_doubt
#   - Continuation-button discipline (Principle 1)
#   - LLM-generated explanations + resume prompt
#   - HTML-escape of LLM and DB content before delivery

import json
import logging
from typing import Any, Optional

from config import settings
from services.memory.main import persist_observer_event
from shared.db.client import db
from shared.llm.openrouter import LLMCallResult, chat_with_metadata
from shared.observability.llm_log import record_llm_call
from shared.telegram.utils import escape_html

logger = logging.getLogger(__name__)

DEFAULT_SUBSKILL = "inference_basic"
# DEFAULT_DIFFICULTY removed in slice 5: replaced by profile-derived
# context.default_difficulty (Bug 23). The remaining "medium" inside
# _handle_practice_request is a defensive final fallback for the case where
# both the planner and the profile lookup return null — should never trigger
# in practice once profiles are populated.
STALE_REPEAT_DAYS = 7

TIER5_PREFIX = "We've seen this passage before — let's try it with fresh eyes."
TIER6_PREFIX = (
    "I'm running low on new questions in this category — let me serve one we "
    "did a while back to see how your thinking has changed."
)

LLM_FALLBACK_TEXT = "Hmm, having trouble thinking right now. Try again in a moment?"
# Note: the slice-2.5 "v5fail" test trigger was removed in slice 4. Real LLM
# error handling now wraps the actual openrouter call in every LLM site,
# so the fallback path can be tested by breaking OPENROUTER_API_KEY.

_RECENT_TURNS_FOR_PROMPT = 6


# ─── public entry point ─────────────────────────────────────────────────────


async def handle(context: dict) -> dict:
    """Action dispatcher. context['intent']['action'] selects the handler."""
    action = context["intent"]["action"]

    # Returning-after-break: orchestrator attached a candidate iff this is the
    # first message of a new session and there's a recent unanswered question.
    if (
        action == "practice_request"
        and context.get("session_resume_candidate") is not None
    ):
        return await _handle_resume_prompt(context)

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
    if action == "resume_question":
        return await _handle_resume_question(context)
    # subskill_switch removed in slice 4 verification: the [Different subskill]
    # button is now an orchestrator-direct picker, and picker selections route
    # as practice_request with intent.subskill set. If a stale subskill_switch
    # action somehow reaches here, fall through to a normal practice_request.
    return await _handle_practice_request(context)


# ─── handlers ───────────────────────────────────────────────────────────────


async def _handle_practice_request(context: dict, prefix: Optional[str] = None) -> dict:
    student_id = context["student_id"]
    session_id = context.get("session_id")
    intent = context.get("intent") or {}
    # Slice 5 difficulty resolution chain (Bug 23):
    #   1. intent.difficulty — planner extracted it from the student's message
    #      (e.g., "give me an easy one")
    #   2. context.default_difficulty — orchestrator wrote it from
    #      profile_service.get_default_difficulty(preparation_stage)
    #   3. "medium" — defensive fallback when both upstream values are null.
    # Subskill: planner-supplied or DEFAULT_SUBSKILL for cold-start /
    # deterministic-action paths (e.g., v5_continue_next has no skill hint).
    subskill = intent.get("subskill") or DEFAULT_SUBSKILL
    difficulty = (
        intent.get("difficulty")
        or context.get("default_difficulty")
        or "medium"
    )
    question, tier = await _retrieve_with_fallback(
        student_id=student_id,
        subskill=subskill,
        difficulty=difficulty,
        profile_signals=None,
    )
    if question is None:
        return _terminal_response("I'm out of fresh material right now — please try again later.")

    attempt_id = await _record_attempt(
        student_id, question["question_id"], session_id, fallback_tier=tier,
    )

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

    return _question_serve_response(
        content=content,
        keyboard=keyboard,
        attempt_id=attempt_id,
        question=question,
        tier=tier,
    )


async def _handle_answer(context: dict) -> dict:
    info = context["last_question_attempt"]
    student_id = context["student_id"]
    session_id = context.get("session_id")
    user_msg_id = (context.get("current_message") or {}).get("message_id")

    detected = info["detected_answer"]
    attempt_row = info["attempt_row"]
    question_id = attempt_row["question_id"]
    attempt_id = attempt_row["id"]

    q = await db.fetchrow(
        """
        SELECT correct_option, explanation, question_text, options, passage_id
        FROM public.questions WHERE question_id = $1
        """,
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
        return _continuation_response("Couldn't score that answer — let's try a fresh question.")

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

    try:
        explanation = await _generate_explanation(
            student_id=student_id,
            session_id=session_id,
            user_msg_id=user_msg_id,
            question_text=q["question_text"],
            options=_parse_options(q["options"]),
            correct_option=correct_option,
            student_choice=detected,
            is_correct=is_correct,
            skipped=False,
            reference_explanation=q["explanation"] or "",
            recent_turns=context.get("recent_turns") or [],
            response_guidance=context.get("response_guidance") or "",
            secondary_signal=context["intent"].get("secondary_signal"),
        )
    except Exception:
        logger.exception("varc: explanation LLM failed; serving error fallback")
        return await _error_fallback_response(context)

    return {
        "content": explanation,
        "content_type": "text_with_keyboard",
        "keyboard": _continuation_keyboard(),
        "requires_keyboard_close": False,
        "memory_deltas": {},
        "observer_events": [],
        "meta": {
            "agent": "varc",
            "response_type": "answer_explanation",
            "scored_question_id": question_id,
            "was_correct": is_correct,
            "model_used": settings.MODEL_VARC_TUTOR,
        },
    }


async def _handle_skip(context: dict) -> dict:
    attempt = context["skipped_attempt"]
    student_id = context["student_id"]
    session_id = context.get("session_id")
    user_msg_id = (context.get("current_message") or {}).get("message_id")

    q = await db.fetchrow(
        """
        SELECT correct_option, explanation, question_text, options
        FROM public.questions WHERE question_id = $1
        """,
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
        return _continuation_response("Skipped — couldn't fetch the answer for this one.")

    correct_option = (q["correct_option"] or "").strip().upper()

    try:
        explanation = await _generate_explanation(
            student_id=student_id,
            session_id=session_id,
            user_msg_id=user_msg_id,
            question_text=q["question_text"],
            options=_parse_options(q["options"]),
            correct_option=correct_option,
            student_choice=None,
            is_correct=None,
            skipped=True,
            reference_explanation=q["explanation"] or "",
            recent_turns=context.get("recent_turns") or [],
            response_guidance=context.get("response_guidance") or "",
            secondary_signal=context["intent"].get("secondary_signal"),
        )
    except Exception:
        logger.exception("varc: skip-explanation LLM failed; serving error fallback")
        return await _error_fallback_response(context)

    return {
        "content": explanation,
        "content_type": "text_with_keyboard",
        "keyboard": _continuation_keyboard(),
        "requires_keyboard_close": False,
        "memory_deltas": {},
        "observer_events": [],
        "meta": {
            "agent": "varc",
            "response_type": "skip_explanation",
            "skipped_question_id": attempt["question_id"],
            "model_used": settings.MODEL_VARC_TUTOR,
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
    keyboard = _question_keyboard(qd["question_id"], options, attempt["id"])
    return _question_serve_response(
        content=await _format_question(qd),
        keyboard=keyboard,
        attempt_id=str(attempt["id"]),
        question=qd,
        tier=0,
        rerender=True,
    )


def _handle_continue_doubt(context: dict) -> dict:
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


# ─── Slice 3: returning-after-break ────────────────────────────────────────


async def _handle_resume_prompt(context: dict) -> dict:
    """First message of a new session, with a recent unanswered question
    available. LLM (Sonnet) composes a warm welcome; buttons are deterministic."""
    student_id = context["student_id"]
    session_id = context.get("session_id")
    user_msg_id = (context.get("current_message") or {}).get("message_id")
    candidate = context["session_resume_candidate"]

    system = (
        "You are DHRI welcoming a student back to CAT VARC practice after a break. "
        "Be warm, brief, specific, and slightly informal. "
        "Output ONLY 1-2 sentences total. "
        "Do NOT include any buttons, options, A/B/C/D, or a new question — "
        "the system appends those. "
        "Output plain text only. Do not use markdown formatting (*, _, backticks) "
        "or HTML tags."
    )
    user = (
        f"It's been about {candidate['days_since_break']} day(s) since the student's "
        f"last session. The last question they didn't finish was on the subskill "
        f"'{candidate['subskill']}'. Brief topic: \"{candidate['brief_topic']}\".\n\n"
        f"Compose a warm welcome that asks if they want to resume that question or "
        f"start fresh."
    )

    try:
        result = await chat_with_metadata(
            system=system, user=user, model=settings.MODEL_VARC_RESUME,
        )
        await record_llm_call(
            service="varc", purpose="resume_prompt", result=result,
            student_id=student_id, session_id=session_id, message_id=user_msg_id,
        )
        # LLM output is treated as untrusted input under ParseMode.HTML —
        # escape any stray <, >, & so the bus's HTML parser can't choke.
        text = escape_html(result.content.strip())
    except Exception as e:
        logger.exception("varc: resume_prompt LLM failed; falling back to hardcoded")
        await record_llm_call(
            service="varc", purpose="resume_prompt", result=None,
            success=False, error_message=str(e)[:500],
            fallback_model=settings.MODEL_VARC_RESUME,
            student_id=student_id, session_id=session_id, message_id=user_msg_id,
        )
        text = (
            f"Welcome back. Last time we were on a {candidate['subskill']} question — "
            f"want to pick that one up, or start fresh?"
        )

    return {
        "content": text,
        "content_type": "text_with_keyboard",
        "keyboard": {"inline_keyboard": [
            [{"text": "Resume that question",
              "callback_data": f"v5_resume_{candidate['last_question_id']}"},
             {"text": "Start fresh",
              "callback_data": "v5_continue_next"}],
            [{"text": "Just chat first", "callback_data": "v5_resume_chat"}],
        ]},
        "requires_keyboard_close": False,
        "memory_deltas": {},
        "observer_events": [],
        "meta": {
            "agent": "varc",
            "response_type": "session_resume_prompt",
            "resume_question_id": candidate["last_question_id"],
            "resume_subskill": candidate["subskill"],
            "days_since_break": candidate["days_since_break"],
            "model_used": settings.MODEL_VARC_RESUME,
        },
    }


async def _handle_resume_question(context: dict) -> dict:
    """User tapped [Resume that question]. Re-serve that specific question
    in the new session as a tier-1 retrieval."""
    student_id = context["student_id"]
    session_id = context.get("session_id")
    question_id = context["resume_question_id"]

    q = await db.fetchrow(
        f"SELECT {_QUESTION_COLUMNS} FROM public.questions WHERE question_id = $1",
        question_id,
    )
    if q is None:
        # Resume target gone — fall through to fresh practice.
        return await _handle_practice_request(context)
    qd = dict(q)
    # Tier 1: re-serving a specific known question is the strongest match.
    attempt_id = await _record_attempt(
        student_id, qd["question_id"], session_id, fallback_tier=1,
    )
    options = _parse_options(qd["options"])
    keyboard = _question_keyboard(qd["question_id"], options, attempt_id)
    return _question_serve_response(
        content=await _format_question(qd),
        keyboard=keyboard,
        attempt_id=attempt_id,
        question=qd,
        tier=1,
        is_resume=True,
    )


# ─── error fallback (slice 2.5 carry-over) ─────────────────────────────────


async def _error_fallback_response(context: dict) -> dict:
    """Inline-persists the llm_failure observer event before returning the
    user-facing fallback. Was synchronous in slice 2.5 — now async so the
    persist call is awaited (best-effort, never blocks; see
    persist_observer_event)."""
    original_intent = context.get("intent") or {}
    payload = {
        "agent": "varc",
        "action": original_intent.get("action"),
    }
    await persist_observer_event(
        student_id=context.get("student_id"),
        session_id=context.get("session_id"),
        event_type="llm_failure",
        payload=payload,
    )
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
            "payload": payload,
        }],
        "meta": {
            "agent": "varc",
            "response_type": "error_fallback",
            "model_used": None,
            "cost_usd": 0.0,
            "stashed_intent": original_intent,
        },
    }


# ─── 6-tier retrieval ladder (preserved) ───────────────────────────────────


async def _retrieve_with_fallback(
    *, student_id: str, subskill: str, difficulty: str, profile_signals: Optional[dict],
) -> tuple[Optional[dict], int]:
    if profile_signals:
        q = await _query_unseen(student_id, subskill=subskill, difficulty=difficulty, profile_signals=profile_signals)
        if q: return q, 1
    q = await _query_unseen(student_id, subskill=subskill, difficulty=difficulty)
    if q: return q, 2
    q = await _query_unseen(student_id, subskill=subskill)
    if q: return q, 3
    q = await _query_unseen(student_id)
    if q: return q, 4
    q = await _query_stale_seen(student_id, subskill=subskill, days=STALE_REPEAT_DAYS)
    if q: return q, 5
    q = await _query_oldest_seen(student_id)
    if q: return q, 6
    return None, 0


_QUESTION_COLUMNS = (
    "question_id, question_text, options, correct_option, explanation, "
    "passage_id, subskill, difficulty"
)
_BASE_FILTERS = (
    "is_active = true AND needs_review = false AND correct_option IS NOT NULL"
)


async def _query_unseen(
    student_id: str, *, subskill: Optional[str] = None,
    difficulty: Optional[str] = None,
    profile_signals: Optional[dict] = None,  # noqa: ARG001
) -> Optional[dict]:
    clauses = [_BASE_FILTERS]
    args: list[Any] = []
    if subskill is not None:
        args.append(subskill); clauses.append(f"q.subskill = ${len(args)}")
    if difficulty is not None:
        args.append(difficulty); clauses.append(f"q.difficulty = ${len(args)}")
    args.append(student_id)
    sql = f"""
        SELECT {_QUESTION_COLUMNS}
        FROM public.questions q
        WHERE {' AND '.join(clauses)}
          AND NOT EXISTS (
              SELECT 1 FROM v5.student_question_attempts a
              WHERE a.student_id = ${len(args)}::uuid AND a.question_id = q.question_id
          )
        ORDER BY random() LIMIT 1
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
        ORDER BY random() LIMIT 1
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
        ORDER BY seen.last_served ASC LIMIT 1
    """
    row = await db.fetchrow(sql, student_id)
    return dict(row) if row else None


# ─── attempt persistence ────────────────────────────────────────────────────


async def _record_attempt(
    student_id: str,
    question_id: str,
    session_id: Optional[str],
    fallback_tier: Optional[int] = None,
) -> str:
    row = await db.fetchrow(
        """
        INSERT INTO v5.student_question_attempts
          (student_id, question_id, session_id, fallback_tier)
        VALUES ($1::uuid, $2, $3::uuid, $4)
        RETURNING id
        """,
        student_id, question_id, session_id, fallback_tier,
    )
    return str(row["id"])


# ─── LLM-generated explanation ──────────────────────────────────────────────


_VARC_TUTOR_SYSTEM_PROMPT = """You are DHRI, a warm CAT VARC tutor. The student just answered or skipped a question.

CRITICAL OUTPUT RULES (do not violate):
1. Produce ONLY: a brief scoring acknowledgement + a clear explanation + at most one "what next" transition sentence.
2. NEVER include a new question with passage and A/B/C/D options. NEVER. The next question is appended by the system.
3. Do NOT include inline buttons, lists of options, or any other interactive elements. The system handles continuation buttons.
4. Output plain text only. Do not use markdown formatting (*, _, backticks) or HTML tags. Emphasis comes through your word choice, not formatting.
5. Be specific and concise. Aim for 3-5 sentences total.
6. If the recent turns include something relevant (a pattern, a follow-up, a doubt), reference it naturally — but only if it adds value.

Tone: warm, specific, slightly informal. Use ✓ for correct."""


def _format_recent_turns_for_prompt(turns: list[dict], limit: int = _RECENT_TURNS_FOR_PROMPT) -> str:
    if not turns:
        return "(no prior turns)"
    # `turns` is most-recent-first (LRANGE 0 N). Reverse for chronological order.
    chrono = list(reversed(turns[:limit]))
    lines = []
    for t in chrono:
        role = (t.get("role") or "?").strip()
        content = (t.get("content") or "").strip().replace("\n", " ")
        if len(content) > 240:
            content = content[:237] + "…"
        lines.append(f"[{role}]: {content}")
    return "\n".join(lines)


async def _generate_explanation(
    *,
    student_id: str,
    session_id: Optional[str],
    user_msg_id: Optional[str],
    question_text: str,
    options: dict,
    correct_option: str,
    student_choice: Optional[str],
    is_correct: Optional[bool],
    skipped: bool,
    reference_explanation: str,
    recent_turns: list[dict],
    response_guidance: str = "",
    secondary_signal: Optional[dict] = None,
) -> str:
    options_block = "\n".join(f"{k}) {v}" for k, v in options.items()) or "(no options listed)"
    if skipped:
        student_state = "Student skipped this question."
    elif is_correct:
        student_state = f"Student picked: {student_choice}  (correct ✓)"
    else:
        student_state = f"Student picked: {student_choice}  (incorrect — correct answer was {correct_option})"

    # Slice 4: planner-supplied tone / undertone signals append to the prompt.
    guidance_block = ""
    if response_guidance:
        guidance_block = f"\nResponse guidance from planner: {response_guidance}\n"
    if isinstance(secondary_signal, dict):
        sig_value = secondary_signal.get("value")
        if sig_value:
            guidance_block += (
                f"Detected emotional undertone: {sig_value}. "
                f"Soften tone slightly without making it the focus.\n"
            )

    user_prompt = (
        f"Question: {question_text}\n\n"
        f"Options:\n{options_block}\n\n"
        f"Correct answer: {correct_option}\n"
        f"{student_state}\n\n"
        f"Reference (canonical) explanation — use as a guide; rephrase in your own warm voice:\n"
        f"{(reference_explanation or '(no reference explanation provided)').strip()}\n\n"
        f"Recent turns (most recent last):\n"
        f"{_format_recent_turns_for_prompt(recent_turns)}\n"
        f"{guidance_block}\n"
        f"Compose your response now."
    )

    try:
        result: LLMCallResult = await chat_with_metadata(
            system=_VARC_TUTOR_SYSTEM_PROMPT,
            user=user_prompt,
            model=settings.MODEL_VARC_TUTOR,
        )
        await record_llm_call(
            service="varc",
            purpose=("skip_explanation" if skipped else "answer_explanation"),
            result=result,
            student_id=student_id,
            session_id=session_id,
            message_id=user_msg_id,
        )
        # ParseMode.HTML on the bus side — escape LLM output so a stray '<'
        # or '&' doesn't break delivery.
        return escape_html(result.content.strip())
    except Exception as e:
        await record_llm_call(
            service="varc",
            purpose=("skip_explanation" if skipped else "answer_explanation"),
            result=None,
            success=False,
            error_message=str(e)[:500],
            fallback_model=settings.MODEL_VARC_TUTOR,
            student_id=student_id,
            session_id=session_id,
            message_id=user_msg_id,
        )
        raise


# ─── formatting & keyboards ─────────────────────────────────────────────────


async def _format_question(question: dict) -> str:
    """Compose the rendered question text. All DB-sourced strings are
    html-escaped because the bus delivers under ParseMode.HTML — a passage
    containing '<' or '&' would otherwise be rejected by Telegram."""
    options = _parse_options(question["options"])
    parts: list[str] = []
    if question.get("passage_id"):
        passage = await db.fetchrow(
            "SELECT full_text FROM public.passages WHERE passage_id = $1",
            question["passage_id"],
        )
        if passage and passage["full_text"]:
            parts.append(f"Passage:\n\n{escape_html(passage['full_text'])}")
    parts.append(f"Question: {escape_html(question['question_text'])}")
    if options:
        parts.append(
            "\n".join(
                f"{escape_html(str(k))}) {escape_html(v)}" for k, v in options.items()
            )
        )
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
    answer_row = [{"text": k, "callback_data": f"v5_answer_{question_id}_{k}"}
                  for k in options.keys()]
    skip_row = [{"text": "Skip / I don't know", "callback_data": f"v5_skip_{attempt_id}"}]
    return {"inline_keyboard": [answer_row, skip_row]}


def _continuation_keyboard() -> dict:
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


def _continuation_response(text: str) -> dict:
    """Used when scoring fails but we still want to offer continuation buttons."""
    return {
        "content": text,
        "content_type": "text_with_keyboard",
        "keyboard": _continuation_keyboard(),
        "requires_keyboard_close": False,
        "memory_deltas": {},
        "observer_events": [],
        "meta": {"agent": "varc", "response_type": "answer_explanation"},
    }


def _question_serve_response(
    *,
    content: str,
    keyboard: dict,
    attempt_id: str,
    question: dict,
    tier: int,
    rerender: bool = False,
    is_resume: bool = False,
) -> dict:
    meta: dict[str, Any] = {
        "agent": "varc",
        "response_type": "question_serve",
        "retrieved_question_id": question["question_id"],
        "fallback_tier": tier,
        "subskill": question["subskill"],
        "difficulty": question["difficulty"],
        "attempt_id": attempt_id,
    }
    if rerender:
        meta["rerender"] = True
    if is_resume:
        meta["is_resume"] = True
    return {
        "content": content,
        "content_type": "text_with_keyboard",
        "keyboard": keyboard,
        "requires_keyboard_close": True,
        "track_question_attempt_id": attempt_id,
        "memory_deltas": {},
        "observer_events": [],
        "meta": meta,
    }
