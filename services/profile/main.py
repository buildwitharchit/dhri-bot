# services/profile/main.py
#
# Slice 1: ensure a v5.student_profile row exists; minimal brief is hardcoded.
# Real assembly (notes, performance stats, episodic recall) lands in slice 5.

import logging
from typing import Any, Optional

from shared.db.client import db

logger = logging.getLogger(__name__)


async def ensure_profile(student_id: str) -> dict:
    """Idempotent: insert a default profile row if missing, then return it."""
    row = await db.fetchrow(
        "SELECT * FROM v5.student_profile WHERE student_id = $1::uuid",
        student_id,
    )
    if row is not None:
        return dict(row)

    await db.execute(
        """
        INSERT INTO v5.student_profile (student_id)
        VALUES ($1::uuid)
        ON CONFLICT (student_id) DO NOTHING
        """,
        student_id,
    )
    row = await db.fetchrow(
        "SELECT * FROM v5.student_profile WHERE student_id = $1::uuid",
        student_id,
    )
    return dict(row) if row else {}


async def get_minimal_brief(student_id: str) -> str:
    """Slice-1 stub. Real template assembly lands in slice 5."""
    return "Archit, CAT 2026 aspirant"


# Placeholder so callers in later slices can import the symbol now.
async def get_tutor_brief(student_id: str) -> str:  # noqa: ARG001
    return await get_minimal_brief(student_id)


# ─── Slice 2.5 / Fix 4: session stats ───────────────────────────────────────

# Slice 3 introduces real session lifecycle; until then `session_id` is None
# and we approximate "this session" with the last 60 minutes of activity.
_SESSION_FALLBACK_WINDOW = "60 minutes"


async def get_session_stats(student_id: str, session_id: Optional[str] = None) -> dict:
    """Return aggregate stats for a session.

    Slice 3: when session_id is supplied (the normal case once orchestrator
    threads it through), filter by session_id directly. Falls back to a
    last-60-minutes window only when session_id is missing — kept so any
    legacy slice-2.5 attempt without session_id can still be queried.
    """
    if session_id is not None:
        rows = await db.fetch(
            """
            SELECT a.is_correct, a.skipped, a.served_at, a.answered_at, q.subskill
            FROM v5.student_question_attempts a
            LEFT JOIN public.questions q ON q.question_id = a.question_id
            WHERE a.student_id = $1::uuid
              AND a.session_id = $2::uuid
            ORDER BY a.served_at ASC
            """,
            student_id, session_id,
        )
    else:
        rows = await db.fetch(
            f"""
            SELECT a.is_correct, a.skipped, a.served_at, a.answered_at, q.subskill
            FROM v5.student_question_attempts a
            LEFT JOIN public.questions q ON q.question_id = a.question_id
            WHERE a.student_id = $1::uuid
              AND a.served_at > now() - interval '{_SESSION_FALLBACK_WINDOW}'
            ORDER BY a.served_at ASC
            """,
            student_id,
        )

    if not rows:
        return {
            "attempted": 0,
            "correct": 0,
            "skipped": 0,
            "accuracy_pct": 0,
            "top_subskills": [],
            "duration_min": 0,
        }

    answered_rows = [r for r in rows if r["is_correct"] is not None]
    skipped_rows = [r for r in rows if r["skipped"]]
    correct_rows = [r for r in rows if r["is_correct"] is True]

    attempted = len(answered_rows)
    skipped = len(skipped_rows)
    correct = len(correct_rows)
    accuracy_pct = round(correct * 100 / attempted) if attempted else 0

    counter: dict[str, int] = {}
    for r in rows:
        sk = r["subskill"]
        if sk:
            counter[sk] = counter.get(sk, 0) + 1
    top_subskills = sorted(counter, key=lambda k: counter[k], reverse=True)[:3]

    earliest = min(r["served_at"] for r in rows)
    latest = max((r["answered_at"] or r["served_at"]) for r in rows)
    duration_min = max(1, int((latest - earliest).total_seconds() / 60))

    return {
        "attempted": attempted,
        "correct": correct,
        "skipped": skipped,
        "accuracy_pct": accuracy_pct,
        "top_subskills": top_subskills,
        "duration_min": duration_min,
    }
