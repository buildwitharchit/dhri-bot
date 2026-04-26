# services/orchestrator/main.py
#
# Slice 3 layered on slice 2.5:
#
#   Step 0   Webhook idempotency check via tg_update_id           (Fix 7)
#   Step 1   Identity resolution + ensure profile
#   Step 1.5 Lock acquisition (Redis SETNX)
#   Step 2   Persist user message (try/except — fail loud)         (Fix 6)
#   Step 5   SESSION RESOLUTION (slice 3): continuation vs boundary,
#            DEL state on boundary (Principle 3), detect resume candidate.
#            v5.messages.session_id is now populated.
#   Step 6.5 Deterministic action classification — now session-scoped:
#              - skip / show / continuation / retry callbacks
#              - resume + resume_chat callbacks                    (slice 3)
#              - free-text answer regex
#              - free-text mid-question doubt detection
#   Step 6.7 Orchestrator-direct actions (stats / done / retry / resume_chat)
#   Step 7-10 Build agent context, route to agent
#   Step 11.5 Attach previous_question_message_id to response meta (Fix 3)
#   Step 12  Persist assistant message with session_id              (slice 3)
#   Step 13  Memory cache deltas (try/except — log + continue)
#
# Stubbed for later slices: planner LLM (slice 4), real mentor (slice 8),
# session-end LLM pipeline (slice 7), profile cache (slice 5+).

import json
import logging
import re
from typing import Any, Optional

from shared.db.client import db
from shared.redis.client import acquire_lock, get_state, release_lock

from services.memory.main import (
    append_turn,
    get_recent_turns,
    resolve_session,
)
from services.orchestrator import planner as planner_module
from services.profile.main import ensure_profile, get_minimal_brief, get_session_stats
from services.varc.main import handle as varc_handle
from services.mentor.main import handle as mentor_handle
from shared.telegram.utils import escape_html

logger = logging.getLogger(__name__)

LOCK_TTL_SECONDS = 5
RECENT_TURNS_FOR_CONTEXT = 10

_ANSWER_LETTER_RE = re.compile(r"^\s*([A-Da-d])\s*$")
_ANSWER_NUMBER_RE = re.compile(r"^\s*([1-4])\s*$")
_NUMBER_TO_LETTER = {"1": "A", "2": "B", "3": "C", "4": "D"}


# ─── public entry point ─────────────────────────────────────────────────────


async def handle_message(
    tg_id: int,
    content: str,
    content_type: str = "text",
    source_metadata: Optional[dict] = None,
) -> dict:
    source_metadata = source_metadata or {}
    tg_update_id = source_metadata.get("tg_update_id")

    # Step 0: webhook idempotency (Fix 7)
    if tg_update_id is not None:
        retry_response = await _check_telegram_retry(tg_update_id)
        if retry_response is not None:
            return retry_response

    # Step 1: identity
    student_id = await _ensure_student(tg_id, source_metadata.get("first_name"))
    await ensure_profile(student_id)

    # Step 1.5: lock
    lock_key = f"lock:user:{tg_id}"
    if not await acquire_lock(lock_key, LOCK_TTL_SECONDS):
        return _canned("Still working on your last message — hang on a sec.")

    try:
        # Step 5 (ahead of message persist so we have session_id for the insert):
        # session lifecycle + returning-after-break detection.
        session_id, resume_candidate, prior_question_msg_id = await resolve_session(
            student_id, tg_id,
        )

        # Step 2: persist user message (Fix 6 — fail loud BEFORE response)
        try:
            user_msg_id = await _persist_message(
                student_id=student_id,
                session_id=session_id,
                role="user",
                content=content,
                content_type=content_type,
                metadata={
                    "tg_message_id": source_metadata.get("tg_message_id"),
                    "tg_chat_id": source_metadata.get("tg_chat_id"),
                },
                tg_update_id=tg_update_id,
            )
        except Exception:
            logger.exception("step2 db failure persisting user message tg_id=%s", tg_id)
            return _canned("Hmm, something went wrong saving your message. Try once more?")

        recent_turns = await get_recent_turns(student_id, tg_id, limit=RECENT_TURNS_FOR_CONTEXT)

        # Pre-fetch most-recent unanswered attempt (scoped to current session per Principle 3).
        last_unanswered = await _fetch_last_unanswered_attempt(student_id, session_id)

        # Step 6.5: deterministic action classification.
        # Returns None when no deterministic rule matched — then we run the
        # planner LLM (slice 4) for intent classification.
        deterministic = await _classify_action_deterministic(
            content, content_type, last_unanswered,
        )
        if deterministic is not None:
            intent, payload = deterministic
            context_needs: Optional[dict] = None
            response_guidance = ""
        else:
            # Step 6: planner LLM call. classify() never raises — on any
            # failure it returns DEFAULT_INTENT (small_talk).
            classification = await planner_module.classify(
                message=content,
                recent_turns=recent_turns,
                active_session_summary=_session_summary_text(
                    session_id, last_unanswered,
                ),
                student_id=student_id,
                session_id=session_id,
                message_id=user_msg_id,
            )
            intent = classification["intent"]
            payload = {}
            context_needs = classification["context_needs"]
            response_guidance = classification["response_guidance"]

        # Step 7 + 6.7: route by intent.
        response = await _route_intent(
            intent=intent,
            payload=payload,
            response_guidance=response_guidance,
            context_needs=context_needs,
            student_id=student_id,
            tg_id=tg_id,
            session_id=session_id,
            resume_candidate=resume_candidate,
            recent_turns=recent_turns,
            current_unanswered=last_unanswered,
            content=content,
            content_type=content_type,
            user_msg_id=user_msg_id,
        )

        # Step 11.5: keyboard close hint (Fix 3 / Principle 2). When a session
        # boundary just happened, prefer the prior session's last_question_message_id
        # so the OLD question's keyboard is closed too.
        if response.get("requires_keyboard_close"):
            fresh_state = await get_state(tg_id) or {}
            response.setdefault("meta", {})["previous_question_message_id"] = (
                prior_question_msg_id
                if prior_question_msg_id is not None
                else fresh_state.get("last_question_message_id")
            )

        # Step 12: persist assistant message (Fix 6 — fail loud)
        try:
            await _persist_message(
                student_id=student_id,
                session_id=session_id,
                role="assistant",
                content=response["content"],
                content_type=response.get("content_type", "text"),
                metadata={
                    "intent_classification": intent,
                    **(response.get("meta") or {}),
                },
            )
        except Exception:
            logger.exception("step12 db failure persisting assistant message tg_id=%s", tg_id)
            return _canned("Hmm, something went wrong saving the response. Try once more?")

        # Step 13: memory cache deltas (Fix 6 — log + continue)
        try:
            await append_turn(tg_id, {
                "role": "user", "content": content, "content_type": content_type,
                "message_id": user_msg_id,
            })
            await append_turn(tg_id, {
                "role": "assistant",
                "content": response["content"],
                "content_type": response.get("content_type", "text"),
            })
        except Exception:
            logger.exception("step13 cache write failure tg_id=%s (continuing)", tg_id)

        return response
    except Exception:
        logger.exception("v5 orchestrator: unhandled error tg_id=%s", tg_id)
        return _canned("Something went wrong on my end — try again in a moment.")
    finally:
        await release_lock(lock_key)


# ─── Step 0: idempotency (Fix 7) ────────────────────────────────────────────


async def _check_telegram_retry(tg_update_id: int) -> Optional[dict]:
    duplicate = await db.fetchrow(
        """
        SELECT message_id, student_id, created_at
        FROM v5.messages WHERE tg_update_id = $1 LIMIT 1
        """,
        tg_update_id,
    )
    if duplicate is None:
        return None
    logger.info("idempotency: telegram retry detected for update_id=%s", tg_update_id)
    paired = await db.fetchrow(
        """
        SELECT content, content_type
        FROM v5.messages
        WHERE student_id = $1 AND role = 'assistant' AND created_at > $2
        ORDER BY created_at ASC LIMIT 1
        """,
        duplicate["student_id"], duplicate["created_at"],
    )
    if paired is not None:
        return {
            "content": paired["content"],
            "content_type": paired["content_type"],
            "keyboard": None,
            "requires_keyboard_close": False,
            "memory_deltas": {},
            "observer_events": [],
            "meta": {
                "agent": "orchestrator",
                "response_type": "retry_redelivery",
                "is_retry": True,
            },
        }
    return {
        "content": "Still working on your previous message — hang on.",
        "content_type": "text",
        "keyboard": None,
        "requires_keyboard_close": False,
        "memory_deltas": {},
        "observer_events": [],
        "meta": {
            "agent": "orchestrator",
            "response_type": "retry_in_flight",
            "is_retry": True,
        },
    }


# ─── Step 6.5: deterministic action classification (no LLM) ────────────────
#
# Runs FIRST. If matched, the planner is never invoked — button taps and
# answer regexes are 100%-confidence signals that don't need LLM inference.
# Returns None when no rule matched, signalling the caller to run the planner.


async def _classify_action_deterministic(
    content: str,
    content_type: str,
    last_unanswered: Optional[dict],
) -> Optional[tuple[dict, dict]]:
    if content_type == "button":
        if content.startswith("v5_skip_"):
            attempt_id = content[len("v5_skip_"):]
            attempt = await _fetch_attempt_by_id(attempt_id)
            if attempt and attempt["answered_at"] is None:
                return _intent("varc", "skip_request"), {"skipped_attempt": attempt}

        elif content.startswith("v5_show_question_"):
            attempt_id = content[len("v5_show_question_"):]
            attempt = await _fetch_attempt_by_id(attempt_id)
            if attempt:
                return _intent("varc", "show_current_question"), {"show_attempt": attempt}

        elif content.startswith("v5_resume_"):
            tail = content[len("v5_resume_"):]
            if tail == "chat":
                return _intent("orchestrator", "resume_chat"), {}
            return _intent("varc", "resume_question"), {"resume_question_id": tail}

        elif content.startswith("v5_continue_"):
            sub = content[len("v5_continue_"):]
            if sub == "next":
                return _intent("varc", "practice_request"), {}
            if sub == "stats":
                return _intent("orchestrator", "stats_request"), {}
            if sub == "doubt":
                return _intent("varc", "continue_doubt"), {}
            if sub == "subskill":
                return _intent("varc", "subskill_switch"), {}
            if sub == "done":
                return _intent("orchestrator", "session_end"), {}

        elif content == "v5_strategy_chat":
            # New in slice 4: paired with the out_of_scope soft-redirect's
            # [Strategy chat] button. Open-ended prompt; user's next free-text
            # turn flows through the planner like any other text input.
            return _intent("orchestrator", "strategy_chat"), {}

        elif content == "v5_retry":
            return _intent("orchestrator", "retry_last"), {}

        elif content.startswith("v5_answer_"):
            detected = _extract_answer_letter_from_callback(content)
            if detected and last_unanswered is not None:
                return _intent("varc", "answer_to_question"), {
                    "last_question_attempt": {
                        "detected_answer": detected, "attempt_row": last_unanswered,
                    }
                }

    if content_type == "text":
        detected = _detect_text_answer(content)
        if detected and last_unanswered is not None:
            return _intent("varc", "answer_to_question"), {
                "last_question_attempt": {
                    "detected_answer": detected, "attempt_row": last_unanswered,
                }
            }
        if last_unanswered is not None and detected is None:
            # Mid-question doubt: deterministic override per spec, even with
            # planner active. Stops planner from misclassifying the doubt as
            # a fresh practice_request.
            return _intent("varc", "doubt_about_current"), {
                "current_unanswered_attempt": last_unanswered,
            }

    return None


def _intent(domain: str, action: str) -> dict:
    """Deterministic-side intent shape. Compatible with planner's IntentClassification
    so downstream code can read `.subskill` / `.difficulty` / `.secondary_signal`
    uniformly via `.get(...)` regardless of source."""
    return {
        "domain": domain,
        "action": action,
        "subskill": None,
        "difficulty": None,
        "emotional_tone": "neutral",
        "secondary_signal": None,
        "confidence": 1.0,
        "continuation": (
            "new_session" if action == "practice_request" else "continues_current_session"
        ),
    }


def _session_summary_text(
    session_id: Optional[str], last_unanswered: Optional[dict],
) -> str:
    """Compact one-line session description for the planner prompt."""
    parts: list[str] = []
    if session_id:
        parts.append(f"session {session_id[:8]}")
    if last_unanswered is not None:
        parts.append(
            f"unanswered question in flight (attempt {last_unanswered['id'][:8]})"
        )
    return ", ".join(parts) if parts else "no active context"


def _detect_text_answer(content: str) -> Optional[str]:
    if not content:
        return None
    m = _ANSWER_LETTER_RE.match(content)
    if m:
        return m.group(1).upper()
    m = _ANSWER_NUMBER_RE.match(content)
    if m:
        return _NUMBER_TO_LETTER[m.group(1)]
    return None


def _extract_answer_letter_from_callback(content: str) -> Optional[str]:
    if not content.startswith("v5_answer_"):
        return None
    tail = content.rsplit("_", 1)[-1].upper()
    return tail if tail in {"A", "B", "C", "D"} else None


# ─── Step 7 / 6.7: routing dispatcher ───────────────────────────────────────
#
# Single entry point for "given an intent (deterministic or planner-derived),
# produce a response". Orchestrator-direct actions skip agent invocation;
# domain=varc/mentor invokes the agent.


async def _route_intent(
    *,
    intent: dict,
    payload: dict,
    response_guidance: str,
    context_needs: Optional[dict],
    student_id: str,
    tg_id: int,
    session_id: str,
    resume_candidate: Optional[dict],
    recent_turns: list[dict],
    current_unanswered: Optional[dict],
    content: str,
    content_type: str,
    user_msg_id: str,
) -> dict:
    domain = intent.get("domain")
    action = intent.get("action")

    # Out-of-scope guardrail — soft-redirect, no agent, no LLM (Step 7).
    if domain == "out_of_scope" or action == "off_topic":
        return _build_out_of_scope_response()

    # Orchestrator-direct actions.
    if domain == "orchestrator":
        return await _handle_orchestrator_action(
            intent, payload, student_id, tg_id, session_id,
        )
    if action == "small_talk":
        return _build_small_talk_response()
    if action == "stats_request":
        return await _build_stats_response(student_id, session_id)

    # Agent invocation (varc / mentor).
    agent_context = {
        "student_id": student_id,
        "tg_id": tg_id,
        "session_id": session_id,
        "session_resume_candidate": resume_candidate,
        "recent_turns": recent_turns,
        "profile_brief": await get_minimal_brief(student_id),
        "intent": intent,
        "response_guidance": response_guidance,
        "context_needs": context_needs,
        "current_unanswered_attempt": current_unanswered,
        "current_message": {
            "content": content,
            "content_type": content_type,
            "message_id": user_msg_id,
        },
        **payload,
    }
    if domain == "mentor":
        return await mentor_handle(agent_context)
    return await varc_handle(agent_context)


# ─── Step 6.7: orchestrator-direct actions ──────────────────────────────────


async def _handle_orchestrator_action(
    intent: dict,
    payload: dict,  # noqa: ARG001  reserved
    student_id: str,
    tg_id: int,
    session_id: str,
) -> dict:
    action = intent["action"]
    if action == "stats_request":
        return await _build_stats_response(student_id, session_id)
    if action == "session_end":
        return _build_session_end_response()
    if action == "retry_last":
        return await _build_retry_response(student_id, tg_id, session_id)
    if action == "resume_chat":
        return _build_resume_chat_response()
    if action == "strategy_chat":
        return _build_strategy_chat_response()
    return _canned("Something's not quite right — let's start fresh.")


async def _build_stats_response(student_id: str, session_id: str) -> dict:
    stats = await get_session_stats(student_id, session_id=session_id)
    # Subskill names like "inference_basic" don't contain HTML-special chars
    # but we escape on principle — every dynamic value interpolated into a
    # template that runs through ParseMode.HTML must be escaped.
    subskills = (
        escape_html(", ".join(stats["top_subskills"]))
        if stats["top_subskills"]
        else "—"
    )
    lines = [
        "<b>This session so far:</b>",
        f"• Attempted: {stats['attempted']}, Correct: {stats['correct']}, Skipped: {stats['skipped']}",
        f"• Accuracy: {stats['accuracy_pct']}%",
        f"• Subskills practiced: {subskills}",
        f"• Time: {stats['duration_min']} min",
    ]
    return {
        "content": "\n".join(lines),
        "content_type": "text_with_keyboard",
        "keyboard": _continuation_keyboard_for_stats(),
        "requires_keyboard_close": False,
        "memory_deltas": {},
        "observer_events": [],
        "meta": {"agent": "orchestrator", "response_type": "session_stats", **stats},
    }


def _build_session_end_response() -> dict:
    return {
        "content": "See you next time! 👋",
        "content_type": "text",
        "keyboard": None,
        "requires_keyboard_close": False,
        "memory_deltas": {},
        "observer_events": [],
        "meta": {"agent": "orchestrator", "response_type": "session_end"},
    }


def _build_resume_chat_response() -> dict:
    return {
        "content": "Sure, what's on your mind?",
        "content_type": "text",
        "keyboard": None,
        "requires_keyboard_close": False,
        "memory_deltas": {},
        "observer_events": [],
        "meta": {"agent": "orchestrator", "response_type": "resume_chat_ack"},
    }


def _build_strategy_chat_response() -> dict:
    return {
        "content": (
            "Sure — what would you like to talk through? Could be your prep "
            "approach, weak areas, time management, or anything else."
        ),
        "content_type": "text",
        "keyboard": None,
        "requires_keyboard_close": False,
        "memory_deltas": {},
        "observer_events": [],
        "meta": {"agent": "orchestrator", "response_type": "strategy_chat_ack"},
    }


def _build_small_talk_response() -> dict:
    """Slice 4 / Bug 15: warm acknowledgment + continuation buttons. Never
    serves a question — that's exactly what makes small_talk safe as the
    planner's failure default."""
    return {
        "content": "Got it. Want to keep going, or take a different angle?",
        "content_type": "text_with_keyboard",
        "keyboard": _continuation_keyboard_full(),
        "requires_keyboard_close": False,
        "memory_deltas": {},
        "observer_events": [],
        "meta": {"agent": "orchestrator", "response_type": "small_talk_ack"},
    }


def _build_out_of_scope_response() -> dict:
    """Slice 4: soft-redirect for quant/LRDI/general off-topic. No agent
    invocation, no LLM call — pure templated response with two buttons."""
    return {
        "content": (
            "I'm focused on CAT VARC for now, so I can't help with that. "
            "Want a VARC question, or want to talk through a strategy concern?"
        ),
        "content_type": "text_with_keyboard",
        "keyboard": _out_of_scope_keyboard(),
        "requires_keyboard_close": False,
        "memory_deltas": {},
        "observer_events": [
            {"event_type": "out_of_scope_query", "payload": {}},
        ],
        "meta": {"agent": "orchestrator", "response_type": "off_topic_redirect"},
    }


def _continuation_keyboard_full() -> dict:
    """5-button continuation row used by small_talk and other orchestrator
    responses where all options should be available."""
    return {"inline_keyboard": [
        [{"text": "Next question", "callback_data": "v5_continue_next"},
         {"text": "Different subskill", "callback_data": "v5_continue_subskill"}],
        [{"text": "Show my stats", "callback_data": "v5_continue_stats"},
         {"text": "I have a doubt", "callback_data": "v5_continue_doubt"},
         {"text": "I'm done", "callback_data": "v5_continue_done"}],
    ]}


def _out_of_scope_keyboard() -> dict:
    return {"inline_keyboard": [
        [{"text": "VARC question", "callback_data": "v5_continue_next"},
         {"text": "Strategy chat", "callback_data": "v5_strategy_chat"}],
    ]}


async def _build_retry_response(student_id: str, tg_id: int, session_id: str) -> dict:
    row = await db.fetchrow(
        """
        SELECT metadata FROM v5.messages
        WHERE student_id = $1::uuid AND role = 'assistant'
          AND metadata->>'response_type' = 'error_fallback'
        ORDER BY created_at DESC LIMIT 1
        """,
        student_id,
    )
    metadata: Any = (row["metadata"] if row else {}) or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (TypeError, ValueError):
            metadata = {}
    stashed: Any = metadata.get("stashed_intent") if metadata else None

    last_unanswered = await _fetch_last_unanswered_attempt(student_id, session_id)
    base_context = {
        "student_id": student_id,
        "tg_id": tg_id,
        "session_id": session_id,
        "recent_turns": [],
        "profile_brief": await get_minimal_brief(student_id),
        "current_unanswered_attempt": last_unanswered,
        "current_message": {"content": "", "content_type": "text", "message_id": None},
    }
    base_context["intent"] = stashed if isinstance(stashed, dict) else _intent("varc", "practice_request")
    return await varc_handle(base_context)


def _continuation_keyboard_for_stats() -> dict:
    return {"inline_keyboard": [
        [{"text": "Next question", "callback_data": "v5_continue_next"},
         {"text": "Different subskill", "callback_data": "v5_continue_subskill"}],
        [{"text": "I have a doubt", "callback_data": "v5_continue_doubt"},
         {"text": "I'm done", "callback_data": "v5_continue_done"}],
    ]}


# ─── helpers ────────────────────────────────────────────────────────────────


async def _fetch_last_unanswered_attempt(
    student_id: str, session_id: str,
) -> Optional[dict]:
    """Slice 3: scoped to the CURRENT session. Old-session unanswered attempts
    do not bleed into the new session (Principle 3)."""
    row = await db.fetchrow(
        """
        SELECT id, question_id, served_at, answered_at, skipped
        FROM v5.student_question_attempts
        WHERE student_id = $1::uuid
          AND session_id = $2::uuid
          AND answered_at IS NULL
        ORDER BY served_at DESC LIMIT 1
        """,
        student_id, session_id,
    )
    if row is None:
        return None
    out = dict(row)
    out["id"] = str(out["id"])
    return out


async def _fetch_attempt_by_id(attempt_id: str) -> Optional[dict]:
    try:
        row = await db.fetchrow(
            """
            SELECT id, question_id, session_id, served_at, answered_at, skipped
            FROM v5.student_question_attempts
            WHERE id = $1::uuid
            """,
            attempt_id,
        )
    except Exception:
        return None
    if row is None:
        return None
    out = dict(row)
    out["id"] = str(out["id"])
    if out.get("session_id") is not None:
        out["session_id"] = str(out["session_id"])
    return out


async def _ensure_student(tg_id: int, display_name: Optional[str]) -> str:
    row = await db.fetchrow(
        "SELECT student_id FROM v5.students WHERE tg_id = $1 AND deleted_at IS NULL",
        tg_id,
    )
    if row is not None:
        await db.execute(
            "UPDATE v5.students SET last_seen_at = now() WHERE student_id = $1",
            row["student_id"],
        )
        return str(row["student_id"])
    row = await db.fetchrow(
        """
        INSERT INTO v5.students (tg_id, display_name)
        VALUES ($1, $2)
        RETURNING student_id
        """,
        tg_id, display_name,
    )
    return str(row["student_id"])


async def _persist_message(
    *,
    student_id: str,
    session_id: Optional[str],
    role: str,
    content: str,
    content_type: str,
    metadata: Optional[dict],
    tg_update_id: Optional[int] = None,
) -> str:
    row = await db.fetchrow(
        """
        INSERT INTO v5.messages
          (student_id, session_id, role, content, content_type, metadata, tg_update_id)
        VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6::jsonb, $7)
        RETURNING message_id
        """,
        student_id, session_id, role, content, content_type,
        json.dumps(_clean_metadata(metadata)),
        tg_update_id,
    )
    return str(row["message_id"])


def _clean_metadata(metadata: Optional[dict]) -> dict:
    if not metadata:
        return {}
    return {k: v for k, v in metadata.items() if v is not None}


def _canned(text: str) -> dict:
    return {
        "content": text,
        "content_type": "text",
        "keyboard": None,
        "requires_keyboard_close": False,
        "memory_deltas": {},
        "observer_events": [],
        "meta": {"agent": "orchestrator", "canned": True},
    }
