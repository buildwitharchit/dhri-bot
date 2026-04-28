# services/profile/main.py
#
# Slice 5: profile reads + cache.
#
#   - get_tutor_brief / get_minimal_brief: real template assembly from
#     v5.student_profile + v5.students + v5.student_notes + recent attempt
#     activity from v5.student_question_attempts. NO LLM call. Cached in
#     Redis under profile:brief:{sid} / profile:brief_minimal:{sid} with
#     30-min TTL.
#
#   - get_default_difficulty: maps preparation_stage → "easy"|"medium"|"hard".
#     Replaces VARC's slice-2-era hardcoded "medium" constant (Bug 23).
#
#   - get_active_notes: SQL with confidence × recency scoring (30-day half-
#     life). Returns top N notes for tutor brief assembly. v5.student_notes
#     is empty until slice 7's extractor lands; the function gracefully
#     returns [] in that case.
#
#   - invalidate_brief_cache: Principle 6 helper. Every write to
#     v5.student_profile / v5.student_notes / v5.student_skill_profile MUST
#     call this after the write. Slice 5 has no write site of its own
#     (update_profile is slice 6 + slice 7's add_note); the helper is here
#     so those slices can wire it without re-implementing.
#
#   - Empty-state fallbacks (Bug 25): briefs read coherently for any
#     student state — cold start, partial onboarding, mid-prep, etc. NO
#     "N/A" placeholders, NO empty sections.

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from shared.db.client import db
from shared.redis.client import redis

logger = logging.getLogger(__name__)


BRIEF_CACHE_TTL_SECONDS = 1800        # 30 min, per spec
RECENT_ATTEMPTS_DAYS = 14             # window for performance summary
NOTES_FOR_BRIEF_LIMIT = 8             # top-N notes in full brief
MIN_ATTEMPTS_FOR_SKILL_BREAKDOWN = 3  # below this we don't claim a "weakest" subskill

_PREPARATION_STAGE_TO_DIFFICULTY = {
    "just_starting": "easy",
    "mid_prep": "medium",
    "final_3_months": "medium",
    "revision": "hard",
}

_SUBSKILL_DISPLAY = {
    "inference_basic": "inference (basic)",
    "inference_advanced": "inference (advanced)",
    "strengthen_weaken": "strengthen/weaken",
    "main_idea_full_passage": "main idea",
    "passage_summary": "passage summary",
    "specific_detail": "specific detail",
    "vocab_in_context": "vocab in context",
    "author_tone": "author tone",
    "purpose_of_example": "purpose of example",
    "logical_structure": "logical structure",
}


def _human_subskill(name: Optional[str]) -> str:
    if not name:
        return "—"
    return _SUBSKILL_DISPLAY.get(name, name.replace("_", " "))


# ─── ensure_profile (preserved from slice 1) ────────────────────────────────


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


# ─── default difficulty (Bug 23 — replaces VARC's hardcoded medium) ────────


async def get_default_difficulty(student_id: str) -> str:
    """Map preparation_stage → difficulty. Returns 'medium' when stage is
    null or unknown (cold-start fallback)."""
    row = await db.fetchrow(
        "SELECT preparation_stage FROM v5.student_profile WHERE student_id = $1::uuid",
        student_id,
    )
    if row is None:
        return "medium"
    return _PREPARATION_STAGE_TO_DIFFICULTY.get(row["preparation_stage"], "medium")


# ─── active notes ──────────────────────────────────────────────────────────


async def get_active_notes(
    student_id: str, filter: Optional[dict] = None,
) -> list[dict]:
    """Top-N notes sorted by confidence × exp(-Δt/30 days).

    Empty list if no notes exist (slice 5 baseline — slice 7's extractor
    populates this table).
    """
    f = filter or {}
    limit = int(f.get("limit", 20))
    categories = f.get("categories")
    exclude_sensitive = bool(f.get("exclude_sensitive", False))

    clauses = [
        "student_id = $1::uuid",
        "is_active = true",
        "(expires_at IS NULL OR expires_at > now())",
    ]
    args: list[Any] = [student_id]
    if categories:
        args.append(list(categories))
        clauses.append(f"category = ANY(${len(args)})")
    if exclude_sensitive:
        clauses.append("sensitive = false")
    args.append(limit)

    sql = f"""
        SELECT note_id, content, category, confidence, source,
               last_reinforced, sensitive
        FROM v5.student_notes
        WHERE {' AND '.join(clauses)}
        ORDER BY (
            confidence
            * exp(-EXTRACT(EPOCH FROM (now() - last_reinforced)) / 2592000)
        ) DESC
        LIMIT ${len(args)}
    """
    rows = await db.fetch(sql, *args)
    return [dict(r) for r in rows]


# ─── tutor brief (full) ────────────────────────────────────────────────────


async def get_tutor_brief(student_id: str) -> str:
    """Assembled tutor brief, ~400-700 tokens, cached for 30 min."""
    cache_key = _full_cache_key(student_id)
    cached = await redis.get(cache_key)
    if cached:
        return cached

    student_row = await _fetch_student(student_id)
    profile_row = await _fetch_profile(student_id)
    perf = await _compute_performance_summary(student_id)
    notes = await get_active_notes(
        student_id,
        {"limit": NOTES_FOR_BRIEF_LIMIT, "exclude_sensitive": True},
    )
    last_session = await _fetch_last_session(student_id)

    sections = [
        _format_facts(student_row, profile_row),
        _format_performance(perf),
        _format_notes(notes),
        _format_recent_activity(last_session, profile_row),
    ]
    brief = "\n\n".join(sections)

    try:
        await redis.set(cache_key, brief, ex=BRIEF_CACHE_TTL_SECONDS)
    except Exception:
        # Cache write failure is non-fatal — Principle 5.
        logger.warning("get_tutor_brief: cache SET failed (continuing)", exc_info=True)
    return brief


# ─── tutor brief (minimal) ─────────────────────────────────────────────────


async def get_minimal_brief(student_id: str) -> str:
    """~50-100 tokens. Used when planner says context_needs.profile=='minimal'."""
    cache_key = _minimal_cache_key(student_id)
    cached = await redis.get(cache_key)
    if cached:
        return cached

    student_row = await _fetch_student(student_id)
    profile_row = await _fetch_profile(student_id)
    perf = await _compute_performance_summary(student_id)

    facts = _format_facts(student_row, profile_row)
    activity = _format_minimal_activity(perf)
    brief = f"{facts} {activity}".strip()

    try:
        await redis.set(cache_key, brief, ex=BRIEF_CACHE_TTL_SECONDS)
    except Exception:
        logger.warning("get_minimal_brief: cache SET failed (continuing)", exc_info=True)
    return brief


# ─── cache invalidation (Principle 6) ──────────────────────────────────────


async def invalidate_brief_cache(student_id: str) -> None:
    """DEL both profile:brief:{sid} keys.

    Per Principle 6: every write to v5.student_profile / v5.student_notes /
    v5.student_skill_profile MUST call this immediately after the write.
    Slice 5 has no in-house write site (update_profile is slice 6;
    add_note is slice 7), so this helper is here for those slices to
    invoke without re-implementing the cache key shape.
    """
    try:
        await redis.delete(
            _full_cache_key(student_id),
            _minimal_cache_key(student_id),
        )
    except Exception:
        logger.warning(
            "invalidate_brief_cache: DEL failed for %s (continuing)",
            student_id, exc_info=True,
        )


def _full_cache_key(student_id: str) -> str:
    return f"profile:brief:{student_id}"


def _minimal_cache_key(student_id: str) -> str:
    return f"profile:brief_minimal:{student_id}"


# ─── data fetches ──────────────────────────────────────────────────────────


async def _fetch_student(student_id: str) -> dict:
    row = await db.fetchrow(
        """
        SELECT student_id, display_name, tg_id, created_at
        FROM v5.students
        WHERE student_id = $1::uuid AND deleted_at IS NULL
        """,
        student_id,
    )
    return dict(row) if row else {}


async def _fetch_profile(student_id: str) -> dict:
    row = await db.fetchrow(
        "SELECT * FROM v5.student_profile WHERE student_id = $1::uuid",
        student_id,
    )
    return dict(row) if row else {}


async def _fetch_last_session(student_id: str) -> Optional[dict]:
    row = await db.fetchrow(
        """
        SELECT session_id, started_at, last_activity_at, ended_at, end_reason,
               message_count, question_count, correct_count
        FROM v5.sessions
        WHERE student_id = $1::uuid
        ORDER BY started_at DESC
        LIMIT 1
        """,
        student_id,
    )
    return dict(row) if row else None


async def _compute_performance_summary(student_id: str) -> dict:
    """Aggregate over v5.student_question_attempts in the last 14 days.

    Slice 5 doesn't read from v5.student_skill_profile (the renamed v4
    table doesn't exist yet — see roadmap slice 5 migrations note). We
    compute the same signals fresh from attempts. Cheap; no caching here
    because the assembled brief is what gets cached upstream.
    """
    rows = await db.fetch(
        f"""
        SELECT q.subskill,
               count(*) FILTER (WHERE a.answered_at IS NOT NULL AND a.skipped = false) AS attempted,
               count(*) FILTER (WHERE a.is_correct = true) AS correct
        FROM v5.student_question_attempts a
        JOIN public.questions q ON q.question_id = a.question_id
        WHERE a.student_id = $1::uuid
          AND a.served_at > now() - interval '{RECENT_ATTEMPTS_DAYS} days'
          AND q.subskill IS NOT NULL
        GROUP BY q.subskill
        """,
        student_id,
    )
    total_attempted = sum(r["attempted"] for r in rows)
    total_correct = sum(r["correct"] for r in rows)

    by_subskill: list[dict] = []
    for r in rows:
        attempted = int(r["attempted"] or 0)
        if attempted < MIN_ATTEMPTS_FOR_SKILL_BREAKDOWN:
            continue
        correct = int(r["correct"] or 0)
        by_subskill.append({
            "subskill": r["subskill"],
            "attempted": attempted,
            "correct": correct,
            "accuracy_pct": round(100 * correct / attempted),
        })

    by_subskill.sort(key=lambda x: x["accuracy_pct"])
    weakest = by_subskill[0] if by_subskill else None
    strongest = by_subskill[-1] if by_subskill else None
    if weakest and strongest and weakest["subskill"] == strongest["subskill"]:
        strongest = None  # only one subskill — don't claim both

    accuracy_pct = (
        round(100 * total_correct / total_attempted) if total_attempted else 0
    )
    return {
        "total_attempted": total_attempted,
        "total_correct": total_correct,
        "accuracy_pct": accuracy_pct,
        "weakest_subskill": weakest,
        "strongest_subskill": strongest,
        "subskills_practiced": [r["subskill"] for r in by_subskill],
    }


# ─── template formatters (no LLM, all coherent for empty state — Bug 25) ───


def _format_facts(student_row: dict, profile_row: dict) -> str:
    name = (student_row.get("display_name") or "Student").strip() or "Student"
    target_year = profile_row.get("target_year")
    target_exam = (profile_row.get("target_exam") or "CAT").strip()
    experience = profile_row.get("experience_level")
    stage = profile_row.get("preparation_stage")
    hours = profile_row.get("hours_per_day")

    parts: list[str] = [name]
    if target_year:
        parts.append(f"target {target_exam} {target_year}")
    else:
        parts.append(f"preparing for {target_exam}")
    if experience:
        parts.append(experience.replace("_", " "))
    if hours:
        parts.append(f"{hours} hours/day")
    if stage:
        parts.append(f"{stage.replace('_', ' ')} phase")
    return ", ".join(parts) + "."


def _format_performance(perf: dict) -> str:
    total = perf["total_attempted"]
    if total == 0:
        return "Performance: just started — no skill data yet."
    if total < 5:
        return (
            f"Performance: just getting started ({total} questions tried in the "
            f"last {RECENT_ATTEMPTS_DAYS} days)."
        )
    parts = [
        f"Performance: {perf['accuracy_pct']}% accuracy on {total} attempts "
        f"in the last {RECENT_ATTEMPTS_DAYS} days"
    ]
    strong = perf.get("strongest_subskill")
    if strong:
        parts.append(
            f"strong on {_human_subskill(strong['subskill'])} ({strong['accuracy_pct']}%)"
        )
    weak = perf.get("weakest_subskill")
    if weak:
        parts.append(
            f"weakest on {_human_subskill(weak['subskill'])} ({weak['accuracy_pct']}%)"
        )
    return ", ".join(parts) + "."


def _format_notes(notes: list[dict]) -> str:
    if not notes:
        return "Notes: none yet — patterns will emerge over the next few sessions."
    lines = ["Notes:"]
    for n in notes:
        content = (n.get("content") or "").strip()
        if not content:
            continue
        lines.append(f"- {content}")
    if len(lines) == 1:
        return "Notes: none yet."
    return "\n".join(lines)


def _format_recent_activity(
    last_session: Optional[dict], profile_row: dict,
) -> str:
    if last_session:
        last_active = last_session.get("last_activity_at")
        if last_active is not None:
            now = datetime.now(tz=timezone.utc)
            delta = now - last_active
            days = delta.days
            if delta.total_seconds() < 60 * 60:
                return "Recent: in active session right now."
            if days == 0:
                return "Recent: last session earlier today."
            if days == 1:
                return "Recent: last session yesterday."
            return f"Recent: last session {days} days ago."
        return "Recent: a previous session is on record."
    onboarding_completed = profile_row.get("onboarding_completed_at")
    if onboarding_completed:
        return "Recent: first conversation since onboarding."
    return "Recent: no prior sessions yet."


def _format_minimal_activity(perf: dict) -> str:
    total = perf["total_attempted"]
    if total == 0:
        return "No practice yet."
    if total < 5:
        return f"Just started — {total} questions tried so far."
    weak = perf.get("weakest_subskill")
    if weak:
        return (
            f"Weakest: {_human_subskill(weak['subskill'])} "
            f"({weak['accuracy_pct']}% on {weak['attempted']} attempts)."
        )
    return f"{total} attempts in the last {RECENT_ATTEMPTS_DAYS} days."


# ─── Slice 2.5 / Fix 4: session stats (preserved unchanged) ────────────────

_SESSION_FALLBACK_WINDOW = "60 minutes"


async def get_session_stats(student_id: str, session_id: Optional[str] = None) -> dict:
    """Return aggregate stats for a session.

    Slice 3+: when session_id is supplied (the normal case once orchestrator
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
