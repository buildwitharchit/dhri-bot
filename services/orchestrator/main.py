# services/orchestrator/main.py
#
# Slice 1: minimal end-to-end flow.
#   identity → lock → persist user msg → hardcoded route → agent →
#   persist assistant msg → cache turns → release lock → return.
#
# Stubbed (filled in later slices):
#   - Planner LLM call (slice 4)
#   - Onboarding FSM (slice 6)
#   - Rate limiting / spend caps (slice 4)
#   - Profile-aware context (slice 5)
#   - Episodic / specific-message retrieval (slice 7+)
#   - Session lifecycle (slice 3)
#   - Observer events (slice 8)

import json
import logging
from typing import Any, Optional

from shared.db.client import db
from shared.redis.client import acquire_lock, release_lock

from services.memory.main import append_turn, get_recent_turns
from services.profile.main import ensure_profile, get_minimal_brief
from services.varc.main import handle as varc_handle
from services.mentor.main import handle as mentor_handle

logger = logging.getLogger(__name__)

LOCK_TTL_SECONDS = 5
RECENT_TURNS_FOR_CONTEXT = 10


async def handle_message(
    tg_id: int,
    content: str,
    content_type: str = "text",
    source_metadata: Optional[dict] = None,
) -> dict:
    """Top-level entry. Returns an AgentResponse-shaped dict for the bus to render."""
    source_metadata = source_metadata or {}

    student_id = await _ensure_student(tg_id, source_metadata.get("first_name"))
    await ensure_profile(student_id)

    lock_key = f"lock:user:{tg_id}"
    if not await acquire_lock(lock_key, LOCK_TTL_SECONDS):
        return _canned("Still working on your last message — hang on a sec.")

    try:
        user_msg_id = await _persist_message(
            student_id=student_id,
            role="user",
            content=content,
            content_type=content_type,
            metadata={
                "tg_message_id": source_metadata.get("tg_message_id"),
                "tg_chat_id": source_metadata.get("tg_chat_id"),
            },
        )

        recent_turns = await get_recent_turns(tg_id, limit=RECENT_TURNS_FOR_CONTEXT)

        # Stubbed planner — slice 4 replaces this with a real LLM call.
        intent = {
            "domain": "varc",
            "action": "practice_request",
            "continuation": "new_session",
            "emotional_tone": "neutral",
        }

        profile_brief = await get_minimal_brief(student_id)

        agent_context = {
            "student_id": student_id,
            "tg_id": tg_id,
            "recent_turns": recent_turns,
            "profile_brief": profile_brief,
            "intent": intent,
            "current_message": {
                "content": content,
                "content_type": content_type,
                "message_id": user_msg_id,
            },
        }

        if intent["domain"] == "varc":
            response = await varc_handle(agent_context)
        else:
            response = await mentor_handle(agent_context)

        await _persist_message(
            student_id=student_id,
            role="assistant",
            content=response["content"],
            content_type=response.get("content_type", "text"),
            metadata={
                "intent_classification": intent,
                **(response.get("meta") or {}),
            },
        )

        await append_turn(tg_id, {
            "role": "user",
            "content": content,
            "content_type": content_type,
            "message_id": user_msg_id,
        })
        await append_turn(tg_id, {
            "role": "assistant",
            "content": response["content"],
            "content_type": response.get("content_type", "text"),
        })

        return response
    except Exception:
        logger.exception("v5 orchestrator: unhandled error tg_id=%s", tg_id)
        return _canned("Something went wrong on my end — try again in a moment.")
    finally:
        await release_lock(lock_key)


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
        tg_id,
        display_name,
    )
    return str(row["student_id"])


async def _persist_message(
    *,
    student_id: str,
    role: str,
    content: str,
    content_type: str,
    metadata: Optional[dict],
) -> str:
    row = await db.fetchrow(
        """
        INSERT INTO v5.messages (student_id, role, content, content_type, metadata)
        VALUES ($1::uuid, $2, $3, $4, $5::jsonb)
        RETURNING message_id
        """,
        student_id,
        role,
        content,
        content_type,
        json.dumps(_clean_metadata(metadata)),
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
        "memory_deltas": {},
        "observer_events": [],
        "meta": {"agent": "orchestrator", "canned": True},
    }
