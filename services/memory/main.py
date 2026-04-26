# services/memory/main.py
#
# Slice 3 expansion:
#   - Working-memory cache (Redis LIST, slice 1)
#   - Active session state (Redis JSON, per the data model doc)
#   - Session lifecycle: open / close / resume detection / cleanup-cron
#   - process_session_end is a no-op stub here; slice 7 fills it in with the
#     LLM summary + extraction pipeline.
#
# Postgres-side fallback for get_recent_turns is real now: if the Redis cache
# is shorter than `limit`, we read from v5.messages and repopulate the cache.

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from shared.db.client import db
from shared.redis.client import (
    redis,
    get_state,
    set_state,
    update_state,
    clear_state,
    STATE_TTL,
)

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 86_400      # 24h working-memory window
CACHE_MAX_ITEMS = 50

SESSION_GAP_MINUTES = 30        # boundary threshold (Principle 3)
RESUME_LOOKBACK_DAYS = 14       # don't offer to resume questions older than this


# ─── working-memory cache (Redis list, with Postgres fallback) ─────────────


def _cache_key(tg_id: int) -> str:
    return f"memory:tg:{tg_id}"


async def append_turn(tg_id: int, turn: dict) -> None:
    """Slice-3 contract: orchestrator persists v5.messages directly (it owns
    the table); this just maintains the recent-turns cache so the next call
    to get_recent_turns is a single Redis hop."""
    key = _cache_key(tg_id)
    await redis.lpush(key, json.dumps(turn, default=str))
    await redis.ltrim(key, 0, CACHE_MAX_ITEMS - 1)
    await redis.expire(key, CACHE_TTL_SECONDS)


async def get_recent_turns(
    student_id: str, tg_id: int, limit: int = 20,
) -> list[dict]:
    """Try Redis first; fall back to v5.messages if the cache is short.
    Repopulate the cache from Postgres on miss so the next call is fast."""
    raw = await redis.lrange(_cache_key(tg_id), 0, max(limit, 1) - 1)
    cached: list[dict] = []
    for item in raw or []:
        try:
            cached.append(json.loads(item))
        except (TypeError, ValueError):
            logger.warning("memory cache: skipping malformed entry tg_id=%s", tg_id)

    if len(cached) >= limit:
        return cached

    # Cache miss / partial — load from Postgres source of truth.
    rows = await db.fetch(
        """
        SELECT message_id, role, content, content_type, metadata, created_at
        FROM v5.messages
        WHERE student_id = $1::uuid
        ORDER BY created_at DESC
        LIMIT $2
        """,
        student_id, max(limit, CACHE_MAX_ITEMS),
    )
    if not rows:
        return cached

    rebuilt: list[dict] = []
    for row in rows:
        rebuilt.append({
            "role": row["role"],
            "content": row["content"],
            "content_type": row["content_type"],
            "message_id": str(row["message_id"]),
            "created_at": row["created_at"].isoformat(),
        })

    # Repopulate cache (newest first; LPUSH oldest first so newest ends up at index 0).
    if rebuilt:
        try:
            await redis.delete(_cache_key(tg_id))
            for item in reversed(rebuilt[:CACHE_MAX_ITEMS]):
                await redis.lpush(_cache_key(tg_id), json.dumps(item, default=str))
            await redis.ltrim(_cache_key(tg_id), 0, CACHE_MAX_ITEMS - 1)
            await redis.expire(_cache_key(tg_id), CACHE_TTL_SECONDS)
        except Exception:
            logger.exception("memory cache: rebuild failed tg_id=%s (continuing)", tg_id)

    return rebuilt[:limit]


# ─── active session state (Redis JSON) ─────────────────────────────────────
# Thin re-exports for callers that want a memory_service-only import surface.

get_active_session = get_state
set_active_session = set_state
update_active_session = update_state
clear_active_session = clear_state
ACTIVE_SESSION_TTL = STATE_TTL


# ─── session lifecycle (Postgres v5.sessions) ──────────────────────────────


async def resolve_session(student_id: str, tg_id: int) -> tuple[str, Optional[dict], Optional[int]]:
    """Step-5 entry point.

    Returns (session_id, resume_candidate_or_None, prior_question_message_id).

    - If the Redis state's session_id is still active (last_activity_at within
      SESSION_GAP_MINUTES) → return that session_id, no resume, no prior msg.
    - If it's stale (gap exceeded OR ended_at set) → close the old session,
      DEL state:tg:{tg_id} (Principle 3), check for a resume candidate,
      create a new session, set fresh state. The third return value carries
      the prior session's last_question_message_id so the orchestrator can
      tell the bus to close that keyboard.
    - If no state at all → check resume candidate, create new session.
    """
    state_before = await get_state(tg_id) or {}
    state_session_id = state_before.get("session_id")
    prior_question_msg_id: Optional[int] = state_before.get("last_question_message_id")

    if state_session_id:
        session = await db.fetchrow(
            """
            SELECT session_id, last_activity_at, ended_at
            FROM v5.sessions
            WHERE session_id = $1::uuid
            """,
            state_session_id,
        )
        if session is not None and session["ended_at"] is None:
            gap_seconds = (
                datetime.now(tz=timezone.utc) - session["last_activity_at"]
            ).total_seconds()
            if gap_seconds < SESSION_GAP_MINUTES * 60:
                # Continuation — bump activity AND turn counter atomically,
                # keep the state, no resume offer. Convention: message_count
                # increments ONCE per orchestrator handle_message turn (per
                # the slice-3 verification fix; not per persisted message).
                await db.execute(
                    """
                    UPDATE v5.sessions
                    SET last_activity_at = now(),
                        message_count = message_count + 1
                    WHERE session_id = $1::uuid
                    """,
                    state_session_id,
                )
                return state_session_id, None, None
            # Boundary — close and clear.
            await close_session(state_session_id, "inactivity_timeout")
            await clear_state(tg_id)
        elif session is not None and session["ended_at"] is not None:
            # Cron already closed it. Clear state.
            await clear_state(tg_id)
        else:
            # State referenced a session row that no longer exists. Clear.
            await clear_state(tg_id)

    resume_candidate = await detect_session_resume_candidate(student_id)
    new_session_id = await _open_session(student_id, primary_agent="varc")
    await set_state(tg_id, {
        "session_id": new_session_id,
        "student_id": student_id,
        "started_at": datetime.now(tz=timezone.utc).isoformat(),
        "primary_agent": "varc",
    })
    return new_session_id, resume_candidate, prior_question_msg_id


async def _open_session(student_id: str, *, primary_agent: str) -> str:
    # message_count starts at 1 because opening a session is itself triggered
    # by the user's first turn — staying at 0 would undercount the new
    # session's first turn vs. every subsequent turn (which gets +1 in the
    # continuation branch above).
    row = await db.fetchrow(
        """
        INSERT INTO v5.sessions (student_id, primary_agent, message_count)
        VALUES ($1::uuid, $2, 1)
        RETURNING session_id
        """,
        student_id, primary_agent,
    )
    return str(row["session_id"])


async def close_session(session_id: str, end_reason: str) -> None:
    await db.execute(
        """
        UPDATE v5.sessions
        SET ended_at = now(), end_reason = $2
        WHERE session_id = $1::uuid AND ended_at IS NULL
        """,
        session_id, end_reason,
    )


async def cleanup_inactive_sessions() -> int:
    """Cron entry point. Closes any session whose last_activity_at is older
    than SESSION_GAP_MINUTES, clears the owning student's Redis active state,
    then runs the (slice-3-stub) post-close pipeline.

    Per Principle 3 / Bug 13: closing a session in Postgres without also
    clearing state:tg:{tg_id} leaves Redis pointing at a closed session.
    The next request's resolve_session would still detect the boundary and
    recover, but only the cron + the user's next message together close the
    loop — the cron alone leaves the Redis state stale for that window.
    Clearing per-session keeps Redis in sync with Postgres at cron time."""
    rows = await db.fetch(
        f"""
        SELECT s.session_id, st.tg_id
        FROM v5.sessions s
        JOIN v5.students st ON st.student_id = s.student_id
        WHERE s.ended_at IS NULL
          AND s.last_activity_at < now() - interval '{SESSION_GAP_MINUTES} minutes'
        """
    )
    closed = 0
    for row in rows:
        sid = str(row["session_id"])
        await close_session(sid, "inactivity_timeout")
        if row["tg_id"] is not None:
            # Only DEL the specific tg_id whose session was just closed.
            await clear_active_session(row["tg_id"])
        await process_session_end(sid)
        closed += 1
    if closed:
        logger.info(
            "v5 cleanup: closed %d inactive session(s) and cleared Redis state",
            closed,
        )
    return closed


async def process_session_end(session_id: str) -> None:  # noqa: ARG001
    """Slice-3 stub. Slice 7 wires the LLM summary + notes-extraction pipeline."""
    return


# ─── returning-after-break detection (Bug 2) ───────────────────────────────


async def detect_session_resume_candidate(student_id: str) -> Optional[dict]:
    """Return a dict describing a recently-ended session's unanswered question
    if one exists within RESUME_LOOKBACK_DAYS, else None."""
    row = await db.fetchrow(
        f"""
        SELECT s.session_id,
               s.ended_at,
               s.primary_agent,
               a.id          AS attempt_id,
               a.question_id,
               q.subskill,
               q.question_text
        FROM v5.sessions s
        JOIN v5.student_question_attempts a
             ON a.session_id = s.session_id
            AND a.answered_at IS NULL
            AND a.skipped = false
        JOIN public.questions q ON q.question_id = a.question_id
        WHERE s.student_id = $1::uuid
          AND s.ended_at IS NOT NULL
          AND s.ended_at > now() - interval '{RESUME_LOOKBACK_DAYS} days'
        ORDER BY s.ended_at DESC, a.served_at DESC
        LIMIT 1
        """,
        student_id,
    )
    if row is None:
        return None
    days_since_break = max(
        1,
        (datetime.now(tz=timezone.utc) - row["ended_at"]).days or 1,
    )
    brief = (row["question_text"] or "").strip().splitlines()[0][:80]
    return {
        "session_id": str(row["session_id"]),
        "last_attempt_id": str(row["attempt_id"]),
        "last_question_id": row["question_id"],
        "subskill": row["subskill"],
        "brief_topic": brief,
        "days_since_break": days_since_break,
        "ended_at": row["ended_at"].isoformat(),
    }
