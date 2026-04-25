# ingest/verifier.py
#
# Independent correctness check on a tagged question. Uses MODEL_VERIFIER
# to confirm the claimed correct_option. If the verifier disagrees, the
# question is flagged needs_review.

import logging
from typing import Optional

from shared.llm.openrouter import llm_call_with_retry
from config import settings

logger = logging.getLogger(__name__)

_VERIFIER_SYSTEM = (
    "You are a CAT VARC answer verifier. You are given a question, its "
    "options, the officially claimed correct option, and the explanation. "
    "Independently pick the best option. Reply with exactly one of: "
    "A, B, C, D, or UNSURE. No other text."
)


def _format_prompt(question: dict) -> Optional[str]:
    options = question.get("options") or {}
    if not options:
        return None
    parts = [f"Question: {question.get('question_text', '')}"]
    source = question.get("source_text") or question.get("passage_text")
    if source:
        parts.append(f"Source: {source}")
    sentences = question.get("sentences")
    if sentences:
        lines = "\n".join(f"{k}: {v}" for k, v in sentences.items())
        parts.append(f"Sentences:\n{lines}")
    for letter in ("A", "B", "C", "D"):
        if letter in options:
            parts.append(f"{letter}) {options[letter]}")
    parts.append(f"Claimed correct: {question.get('correct_option', '')}")
    parts.append(f"Claimed explanation: {question.get('explanation', '')}")
    return "\n".join(parts)


async def verify_question(question: dict) -> dict:
    """Return the question dict with _verification_flagged set if verifier disagrees."""
    prompt = _format_prompt(question)
    if not prompt or not question.get("correct_option"):
        # PJ and similar — no option-level verification here.
        return question

    try:
        answer = await llm_call_with_retry(
            system=_VERIFIER_SYSTEM,
            user=prompt,
            model=settings.MODEL_VERIFIER,
        )
        verdict = (answer or "").strip().upper()[:6].split()[0] if answer else ""
    except Exception as e:
        logger.warning(f"verifier error: {e}")
        return question

    claimed = (question.get("correct_option") or "").upper()
    if verdict in ("A", "B", "C", "D") and verdict != claimed:
        logger.warning(
            f"verifier disagreement on {question.get('question_id')}: "
            f"claimed={claimed} verifier={verdict}"
        )
        question["_verification_flagged"] = True
    return question
