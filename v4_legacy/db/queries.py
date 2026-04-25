# db/queries.py
#
# Pure-DB helpers. All user-session plumbing lives here.
# Nothing here touches Telegram objects — just asyncpg + Redis.

import json
import logging
import uuid
from typing import Any, Optional

from config import ALL_SUBSKILLS
from shared.db.client import db
from shared.redis.client import get_state

logger = logging.getLogger(__name__)


async def initialize_skill_scores(tg_id: int) -> None:
    """Insert 0.5 baseline row for every subskill."""
    await db.executemany(
        """
        INSERT INTO user_skill_scores (tg_id, subskill, score)
        VALUES ($1, $2, 0.5) ON CONFLICT DO NOTHING
        """,
        [(tg_id, subskill) for subskill in ALL_SUBSKILLS],
    )


async def get_or_create_user(
    tg_id: int, username: Optional[str], first_name: Optional[str]
) -> dict:
    """Upsert tg_users + user_profiles; seed skill scores on first creation."""
    existing = await db.fetchrow("SELECT tg_id FROM tg_users WHERE tg_id = $1", tg_id)
    if existing is None:
        await db.execute(
            """
            INSERT INTO tg_users (tg_id, username, first_name)
            VALUES ($1, $2, $3)
            ON CONFLICT (tg_id) DO NOTHING
            """,
            tg_id, username, first_name,
        )
        await db.execute(
            """
            INSERT INTO user_profiles (tg_id) VALUES ($1)
            ON CONFLICT (tg_id) DO NOTHING
            """,
            tg_id,
        )
        await initialize_skill_scores(tg_id)
    else:
        await db.execute(
            """
            UPDATE tg_users
            SET username = COALESCE($2, username),
                first_name = COALESCE($3, first_name),
                last_active_at = now()
            WHERE tg_id = $1
            """,
            tg_id, username, first_name,
        )

    user = await db.fetchrow("SELECT * FROM tg_users WHERE tg_id = $1", tg_id)
    profile = await db.fetchrow(
        "SELECT * FROM user_profiles WHERE tg_id = $1", tg_id
    )
    merged: dict = dict(user) if user else {"tg_id": tg_id}
    if profile:
        merged.update(dict(profile))
    merged["tg_id"] = tg_id
    return merged


async def create_session(tg_id: int, mode: str) -> str:
    row = await db.fetchrow(
        """
        INSERT INTO sessions (tg_id, mode)
        VALUES ($1, $2)
        RETURNING session_id
        """,
        tg_id, mode,
    )
    return str(row["session_id"])


async def write_message(
    session_id: str,
    tg_id: int,
    role: str,
    content: str,
    message_type: str = "text",
    question_id: Optional[str] = None,
    tg_message_id: Optional[int] = None,
) -> None:
    await db.execute(
        """
        INSERT INTO messages (session_id, tg_id, tg_message_id, role, content,
                              message_type, question_id)
        VALUES ($1::uuid, $2, $3, $4, $5, $6, $7)
        """,
        session_id, tg_id, tg_message_id, role, content, message_type, question_id,
    )


async def write_session_snapshot(
    session_id: str, tg_id: int, state: dict
) -> None:
    """Upsert session_snapshots from the uniform Redis state."""
    questions_in_set = state.get("questions_in_set") or []
    questions_remaining = state.get("questions_remaining") or []
    questions_answered = state.get("questions_answered") or {}
    current_idx = state.get("current_question_index", 0)
    current_qid: Optional[str] = None
    if questions_in_set and 0 <= current_idx < len(questions_in_set):
        current_qid = questions_in_set[current_idx]

    await db.execute(
        """
        INSERT INTO session_snapshots (
            session_id, tg_id, current_mode, current_question_id, passage_id,
            questions_in_set, questions_answered, questions_remaining
        ) VALUES ($1::uuid, $2, $3, $4, $5, $6, $7::jsonb, $8)
        ON CONFLICT (session_id) DO UPDATE SET
            current_mode = EXCLUDED.current_mode,
            current_question_id = EXCLUDED.current_question_id,
            passage_id = EXCLUDED.passage_id,
            questions_in_set = EXCLUDED.questions_in_set,
            questions_answered = EXCLUDED.questions_answered,
            questions_remaining = EXCLUDED.questions_remaining,
            snapped_at = now()
        """,
        session_id,
        tg_id,
        state.get("mode"),
        current_qid,
        state.get("passage_id"),
        questions_in_set,
        json.dumps(questions_answered),
        questions_remaining,
    )


async def get_session_messages(
    session_id: str, limit: Optional[int] = None
) -> list[dict]:
    if limit is not None:
        rows = await db.fetch(
            """
            SELECT role, content, message_type, question_id, created_at
            FROM messages
            WHERE session_id = $1::uuid
            ORDER BY created_at ASC
            LIMIT $2
            """,
            session_id, limit,
        )
    else:
        rows = await db.fetch(
            """
            SELECT role, content, message_type, question_id, created_at
            FROM messages
            WHERE session_id = $1::uuid
            ORDER BY created_at ASC
            """,
            session_id,
        )
    return [dict(r) for r in rows]


async def get_week_stats(tg_id: int) -> dict:
    row = await db.fetchrow(
        """
        SELECT
            COUNT(*) AS questions,
            COALESCE(AVG(CASE WHEN is_correct THEN 1.0 ELSE 0.0 END), 0.0) AS accuracy_frac
        FROM attempts
        WHERE tg_id = $1
          AND attempted_at > now() - interval '7 days'
        """,
        tg_id,
    )
    questions = int(row["questions"]) if row else 0
    accuracy = float(row["accuracy_frac"]) * 100 if row else 0.0
    return {"questions": questions, "accuracy": accuracy}


async def get_state_from_db_or_redis(
    tg_id: int, session_id: str
) -> Optional[dict]:
    """Prefer live Redis state; fall back to persisted session_snapshots."""
    state = await get_state(tg_id)
    if state and state.get("session_id") == session_id:
        return state

    row = await db.fetchrow(
        """
        SELECT current_mode, current_question_id, passage_id,
               questions_in_set, questions_answered, questions_remaining
        FROM session_snapshots
        WHERE session_id = $1::uuid
        """,
        session_id,
    )
    if not row:
        return None

    questions_in_set = list(row["questions_in_set"] or [])
    current_qid = row["current_question_id"]
    try:
        current_idx = (
            questions_in_set.index(current_qid) if current_qid in questions_in_set else 0
        )
    except ValueError:
        current_idx = 0

    answered_raw: Any = row["questions_answered"]
    if isinstance(answered_raw, str):
        try:
            answered = json.loads(answered_raw)
        except ValueError:
            answered = {}
    else:
        answered = dict(answered_raw or {})

    mode = row["current_mode"]
    state_label = {
        "rc": "RC_ACTIVE",
        "pj": "PJ_ACTIVE",
        "va": "VA_ACTIVE",
    }.get(mode, "IDLE")

    return {
        "state": state_label,
        "session_id": session_id,
        "mode": mode,
        "passage_id": row["passage_id"],
        "questions_in_set": questions_in_set,
        "current_question_index": current_idx,
        "questions_answered": answered,
        "questions_remaining": list(row["questions_remaining"] or []),
    }


# ─── RATE LIMIT ─────────────────────────────────────────────────────────────

async def check_and_increment_rate_limit(tg_id: int) -> tuple[bool, int]:
    """Return (allowed, current_count). Does NOT increment if denied."""
    from shared.redis.client import redis
    from shared.telegram.utils import get_ist_date, get_seconds_until_ist_midnight

    key = f"rl:msg:{tg_id}:{get_ist_date()}"
    current = await redis.get(key)
    count = int(current) if current else 0
    if count >= 50:
        return False, count
    new_count = await redis.incr(key)
    if new_count == 1:
        await redis.expire(key, get_seconds_until_ist_midnight())
    return True, new_count
