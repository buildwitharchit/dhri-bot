# services/orchestrator/planner.py
#
# Slice 4 — single-call intent classifier (Gemini Flash via MODEL_PLANNER).
#
# Used by orchestrator.handle_message AFTER step 6.5 deterministic detection
# (skip / continuation / answer regex / mid-question doubt) hasn't already
# matched. Returns the IntentClassification dict the rest of the orchestrator
# routes on.
#
# Failure-mode contract (per Principle 5 + Bug 15):
#   - LLM down / timeout / malformed JSON / out-of-enum values
#     → return DEFAULT_INTENT (action=small_talk).
#   - Returning small_talk is the safe default because the orchestrator's
#     small_talk handler asks the student what they want next; it never
#     auto-serves a question. Defaulting to practice_request would risk
#     unsolicited question serves on every planner outage.

import json
import logging
from typing import Optional

from config import settings
from shared.llm.openrouter import LLMCallResult, chat_with_metadata
from shared.observability.llm_log import record_llm_call

logger = logging.getLogger(__name__)

# ─── enums (validated against planner output) ──────────────────────────────

VALID_DOMAINS = {"varc", "mentor", "out_of_scope"}
VALID_ACTIONS = {
    "practice_request",
    "small_talk",
    "concept_question",
    "review_progress",
    "vent",
    "casual",
    "meta",
    "off_topic",
}
# Granular subskill enum — must match the question bank exactly (Bug 22).
VALID_SUBSKILLS = {
    "inference_basic", "inference_advanced",
    "main_idea_full_passage", "specific_detail",
    "passage_summary", "sentence_insertion", "sentence_odd_one_out",
    "strengthen_weaken", "purpose_of_example", "vocab_in_context",
    "author_tone", "para_jumble",
}
VALID_DIFFICULTIES = {"easy", "medium", "hard"}
VALID_EMOTIONAL_TONES = {"neutral", "stressed", "frustrated", "low", "confident"}


# ─── safe default (Principle 5) ────────────────────────────────────────────

DEFAULT_INTENT: dict = {
    "intent": {
        "domain": "varc",
        "action": "small_talk",
        "subskill": None,
        "difficulty": None,
        "emotional_tone": "neutral",
        "secondary_signal": None,
        "confidence": 0.0,
    },
    "context_needs": {
        "needs_profile": True,
        "needs_notes": False,
        "needs_episodic": False,
        "needs_question_history": False,
    },
    "response_guidance": (
        "Respond warmly. Briefly acknowledge what the student said, then ask what "
        "they want to do next."
    ),
}


# ─── LLM prompt ────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """You are an intent classifier for DHRI, a CAT VARC AI tutor.
Your only job is to read the student's current message + recent conversation and
return a strict JSON classification. Output the JSON object only — no prose, no
markdown, no code fences.

Schema:
{
  "intent": {
    "domain": "varc" | "mentor" | "out_of_scope",
    "action": "practice_request" | "small_talk" | "concept_question"
            | "review_progress" | "vent" | "casual" | "meta" | "off_topic",
    "subskill": "inference_basic" | "inference_advanced"
              | "main_idea_full_passage" | "specific_detail"
              | "passage_summary" | "sentence_insertion" | "sentence_odd_one_out"
              | "strengthen_weaken" | "purpose_of_example" | "vocab_in_context"
              | "author_tone" | "para_jumble"
              | null,
    "difficulty": "easy" | "medium" | "hard" | null,
    "emotional_tone": "neutral" | "stressed" | "frustrated" | "low" | "confident",
    "secondary_signal": null
                      | { "type": "emotional_undertone", "value": "<short label>" },
    "confidence": 0.0..1.0
  },
  "context_needs": {
    "needs_profile": true | false,
    "needs_notes": true | false,
    "needs_episodic": true | false,
    "needs_question_history": true | false
  },
  "response_guidance": "1-2 sentence string telling the agent what tone/angle to take"
}

GUIDANCE:

Domain:
- "varc": about VARC practice, questions, explanations, study mechanics for
  reading comprehension / verbal ability.
- "mentor": emotional venting, strategy questions ("how should I prep"),
  "how am I doing", meta questions about DHRI itself.
- "out_of_scope": quant / LR / DI math questions; off-topic content
  (weather, recipes, news, anything unrelated to CAT prep).

CRITICAL — small_talk vs practice_request (Bug 15):
After a recent question + answer, brief acknowledgments must be small_talk,
NOT practice_request. The bot's response to small_talk is a warm ack +
continuation buttons — NOT a new question.

small_talk examples:
  "ok", "got it", "thanks", "i see", "alright", "hmm", "interesting",
  "makes sense", "okay continue"
practice_request examples:
  "another", "next", "next question", "give me one more", "more",
  "let's continue", "another inference one" (subskill=inference_basic),
  "give me an easy one" (difficulty=easy)

When in doubt: small_talk. The bot will ask what the student wants. It never
auto-serves a question on ambiguous input.

Subskill (Bug 22) — must match the enum exactly:
- "inference" generic → inference_basic
- "main idea" / "summary" → main_idea_full_passage
- unclear → null

Mixed-intent / secondary_signal (Bug 15):
When a message has BOTH a strong action AND an emotional undertone, classify
by the action and capture the emotion in secondary_signal. Example:
  "I'm stressed, give me an easy one"
  → intent.domain=varc, intent.action=practice_request, intent.difficulty=easy
  → intent.emotional_tone=stressed
  → intent.secondary_signal={ "type": "emotional_undertone", "value": "mild_stress" }
The downstream agent uses secondary_signal to soften tone in its response.

Output the JSON only."""


def _format_recent_turns(turns: list[dict], limit: int = 6) -> str:
    if not turns:
        return "(no prior turns)"
    chrono = list(reversed(turns[:limit]))
    lines: list[str] = []
    for t in chrono:
        role = (t.get("role") or "?").strip()
        content = (t.get("content") or "").strip().replace("\n", " ")
        if len(content) > 200:
            content = content[:197] + "…"
        lines.append(f"[{role}]: {content}")
    return "\n".join(lines)


# ─── public entry point ────────────────────────────────────────────────────


async def classify(
    *,
    message: str,
    recent_turns: list[dict],
    active_session_summary: str = "",
    student_id: Optional[str] = None,
    session_id: Optional[str] = None,
    message_id: Optional[str] = None,
) -> dict:
    """Run planner. Always returns a valid IntentClassification dict (never raises)."""
    user_prompt = (
        f"Recent conversation (most recent last):\n"
        f"{_format_recent_turns(recent_turns)}\n\n"
        f"Active session: {active_session_summary or 'none'}\n\n"
        f'Current message: "{message}"\n\n'
        f"Return the JSON classification now."
    )

    try:
        result = await chat_with_metadata(
            system=_SYSTEM_PROMPT,
            user=user_prompt,
            model=settings.MODEL_PLANNER,
        )
    except Exception as e:
        logger.exception("planner: LLM call failed; using DEFAULT_INTENT")
        await record_llm_call(
            service="orchestrator",
            purpose="planner_classify",
            result=None,
            success=False,
            error_message=str(e)[:500],
            fallback_model=settings.MODEL_PLANNER,
            student_id=student_id, session_id=session_id, message_id=message_id,
        )
        return _safe_copy(DEFAULT_INTENT)

    await record_llm_call(
        service="orchestrator",
        purpose="planner_classify",
        result=result,
        student_id=student_id, session_id=session_id, message_id=message_id,
    )

    parsed = _parse_json(result.content)
    if parsed is None:
        logger.warning(
            "planner: malformed JSON; using DEFAULT_INTENT. raw=%r",
            result.content[:300] if result.content else None,
        )
        return _safe_copy(DEFAULT_INTENT)

    return _validate_and_normalize(parsed)


# ─── parsing + validation ──────────────────────────────────────────────────


def _parse_json(raw: Optional[str]) -> Optional[dict]:
    if not raw:
        return None
    s = raw.strip()
    # Strip code fences if the model added them despite the rule.
    if s.startswith("```"):
        first_newline = s.find("\n")
        if first_newline != -1:
            s = s[first_newline + 1:]
        if s.endswith("```"):
            s = s[:-3]
        s = s.strip()
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        return None


def _validate_and_normalize(obj: dict) -> dict:
    """Coerce planner output into a known-good shape. Out-of-enum values get
    silently swapped for safe defaults, with a warning log so we can iterate
    on the prompt."""
    intent_in = obj.get("intent") or {}

    domain = intent_in.get("domain")
    if domain not in VALID_DOMAINS:
        logger.warning("planner: invalid domain=%r; coercing to varc", domain)
        domain = "varc"

    action = intent_in.get("action")
    if action not in VALID_ACTIONS:
        logger.warning("planner: invalid action=%r; coercing to small_talk", action)
        action = "small_talk"

    subskill = intent_in.get("subskill")
    if subskill is not None and subskill not in VALID_SUBSKILLS:
        logger.warning(
            "planner: out-of-enum subskill=%r; falling back to inference_basic",
            subskill,
        )
        subskill = "inference_basic"

    difficulty = intent_in.get("difficulty")
    if difficulty is not None and difficulty not in VALID_DIFFICULTIES:
        logger.warning(
            "planner: invalid difficulty=%r; coercing to None", difficulty,
        )
        difficulty = None

    emotional_tone = intent_in.get("emotional_tone") or "neutral"
    if emotional_tone not in VALID_EMOTIONAL_TONES:
        emotional_tone = "neutral"

    secondary_signal = intent_in.get("secondary_signal")
    if secondary_signal is not None and not isinstance(secondary_signal, dict):
        secondary_signal = None

    try:
        confidence = float(intent_in.get("confidence", 0.0))
        confidence = max(0.0, min(1.0, confidence))
    except (TypeError, ValueError):
        confidence = 0.0

    cn = obj.get("context_needs") or {}
    context_needs = {
        "needs_profile": bool(cn.get("needs_profile", True)),
        "needs_notes": bool(cn.get("needs_notes", False)),
        "needs_episodic": bool(cn.get("needs_episodic", False)),
        "needs_question_history": bool(cn.get("needs_question_history", False)),
    }

    response_guidance = obj.get("response_guidance")
    if not isinstance(response_guidance, str) or not response_guidance.strip():
        response_guidance = DEFAULT_INTENT["response_guidance"]

    return {
        "intent": {
            "domain": domain,
            "action": action,
            "subskill": subskill,
            "difficulty": difficulty,
            "emotional_tone": emotional_tone,
            "secondary_signal": secondary_signal,
            "confidence": confidence,
        },
        "context_needs": context_needs,
        "response_guidance": response_guidance,
    }


def _safe_copy(default: dict) -> dict:
    return {
        "intent": dict(default["intent"]),
        "context_needs": dict(default["context_needs"]),
        "response_guidance": default["response_guidance"],
    }
