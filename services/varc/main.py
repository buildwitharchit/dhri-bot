# services/varc/main.py
#
# Slice 1: random question from public.questions (the v4 question bank,
# untouched). 6-tier fallback ladder, embedding retrieval, and answer
# processing land in slice 2 and slice 3.

import json
import logging
from typing import Any, Optional

from shared.db.client import db

logger = logging.getLogger(__name__)


async def handle(context: dict) -> dict:
    """Pick a random active question, format it for Telegram, return AgentResponse."""
    row = await db.fetchrow(
        """
        SELECT question_id, question_text, options, passage_id,
               subskill, difficulty
        FROM public.questions
        WHERE is_active = true AND needs_review = false
        ORDER BY random()
        LIMIT 1
        """
    )
    if row is None:
        return _stub_response("No questions available right now.")

    options = _parse_options(row["options"])
    passage_text = await _fetch_passage_text(row["passage_id"])

    body_parts: list[str] = []
    if passage_text:
        body_parts.append(f"Passage:\n\n{passage_text}\n")
    body_parts.append(f"Question: {row['question_text']}")
    if options:
        body_parts.append("")
        body_parts.append("\n".join(f"{k}) {v}" for k, v in options.items()))
    content = "\n".join(body_parts)

    keyboard = _options_keyboard(row["question_id"], options) if options else None

    return {
        "content": content,
        "content_type": "text_with_keyboard" if keyboard else "text",
        "keyboard": keyboard,
        "memory_deltas": {},
        "observer_events": [],
        "meta": {
            "agent": "varc",
            "retrieved_question_id": row["question_id"],
            "fallback_tier": 1,
            "subskill": row["subskill"],
            "difficulty": row["difficulty"],
        },
    }


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


async def _fetch_passage_text(passage_id: Optional[str]) -> Optional[str]:
    if not passage_id:
        return None
    row = await db.fetchrow(
        "SELECT full_text FROM public.passages WHERE passage_id = $1",
        passage_id,
    )
    return row["full_text"] if row else None


def _options_keyboard(question_id: str, options: dict) -> dict:
    """Return the inline-keyboard structure the bus will translate to PTB types."""
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
