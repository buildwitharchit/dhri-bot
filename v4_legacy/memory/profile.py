# memory/profile.py
#
# Per-user scoring + trap tracking. Scores live at subskill level; the
# student-facing "weakest_skill" is aggregated from subskill scores.

import json

from config import SUBSKILL_TO_SKILL, MIN_ATTEMPTS_FOR_WEAKEST_SKILL
from shared.db.client import db
from shared.redis.client import redis

ALPHA = 0.15


async def update_skill_score(tg_id: int, subskill: str, is_correct: bool) -> None:
    row = await db.fetchrow("""
        SELECT score, attempts_count FROM user_skill_scores
        WHERE tg_id = $1 AND subskill = $2
    """, tg_id, subskill)

    current = row['score'] if row else 0.5
    new_score = current * (1 - ALPHA) + (1.0 if is_correct else 0.0) * ALPHA

    await db.execute("""
        INSERT INTO user_skill_scores (tg_id, subskill, score, attempts_count)
        VALUES ($1, $2, $3, 1)
        ON CONFLICT (tg_id, subskill) DO UPDATE SET
            score = $3,
            attempts_count = user_skill_scores.attempts_count + 1,
            updated_at = now()
    """, tg_id, subskill, round(new_score, 4))

    await db.execute("""
        UPDATE user_profiles SET
            total_attempts = total_attempts + 1,
            total_correct = total_correct + $1,
            updated_at = now()
        WHERE tg_id = $2
    """, 1 if is_correct else 0, tg_id)

    total = await db.fetchval(
        "SELECT total_attempts FROM user_profiles WHERE tg_id = $1", tg_id
    )
    if total and total >= MIN_ATTEMPTS_FOR_WEAKEST_SKILL:
        weakest = await get_weakest_student_skill(tg_id)
        await db.execute(
            "UPDATE user_profiles SET weakest_skill = $1 WHERE tg_id = $2",
            weakest, tg_id
        )

    await redis.delete(f"profile:{tg_id}")


async def get_weakest_student_skill(tg_id: int) -> str:
    """Aggregate subskill scores to student-facing skills."""
    rows = await db.fetch("""
        SELECT subskill, score FROM user_skill_scores WHERE tg_id = $1
    """, tg_id)

    skill_scores: dict[str, list[float]] = {}
    for row in rows:
        student_skill = SUBSKILL_TO_SKILL.get(row['subskill'])
        if not student_skill:
            continue
        if student_skill not in skill_scores:
            skill_scores[student_skill] = []
        skill_scores[student_skill].append(row['score'])

    if not skill_scores:
        return "inference"  # safe default

    avg_scores = {
        skill: sum(scores) / len(scores)
        for skill, scores in skill_scores.items()
    }
    return min(avg_scores, key=avg_scores.get)


async def get_weakest_subskill_in_group(tg_id: int, subskill_group: list) -> str:
    """Return subskill with lowest score within a group."""
    row = await db.fetchrow("""
        SELECT subskill FROM user_skill_scores
        WHERE tg_id = $1 AND subskill = ANY($2)
        ORDER BY score ASC LIMIT 1
    """, tg_id, subskill_group)
    return row['subskill'] if row else subskill_group[0]


async def update_trap_counts(tg_id: int, trap: str) -> None:
    profile = await db.fetchrow(
        "SELECT trap_counts FROM user_profiles WHERE tg_id = $1", tg_id
    )
    raw = profile['trap_counts'] if profile else None
    if isinstance(raw, str):
        try:
            trap_counts = json.loads(raw)
        except ValueError:
            trap_counts = {}
    else:
        trap_counts = dict(raw or {})
    trap_counts[trap] = trap_counts.get(trap, 0) + 1
    most_common = max(trap_counts, key=trap_counts.get)
    await db.execute("""
        UPDATE user_profiles SET trap_counts = $1, most_common_trap = $2
        WHERE tg_id = $3
    """, json.dumps(trap_counts), most_common, tg_id)
    await redis.delete(f"profile:{tg_id}")


async def get_most_common_trap(tg_id: int) -> str:
    """Return user's dominant trap for reranking. Returns 'none' if no data."""
    row = await db.fetchrow(
        "SELECT most_common_trap FROM user_profiles WHERE tg_id = $1", tg_id
    )
    return row['most_common_trap'] if row else 'none'
