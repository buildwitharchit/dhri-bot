"""Compare live v5 schema against documented data model.

Run before starting any slice and after applying any migration.
Exits with code 1 if drift detected, 0 if clean.

Usage:
    .venv/bin/python -m scripts.check_schema_drift

Source of truth: docs/v5/01_data_model.md.
Update EXPECTED whenever the data model changes.
"""

import asyncio
import sys

# Existing pool helper exposes init_db_pool + a db facade with .fetch/.execute/etc.
# (No top-level get_pool() function in shared/db/client.py — the facade acquires
# a connection per call.)
from shared.db.client import init_db_pool, close_db_pool, db


# Source of truth: docs/v5/01_data_model.md
EXPECTED: dict[str, set[str]] = {
    "students": {
        "student_id", "tg_id", "display_name", "email",
        "preferences", "created_at", "last_seen_at", "deleted_at",
    },
    "student_profile": {
        "student_id", "target_exam", "target_year", "target_colleges",
        "experience_level", "preparation_stage", "hours_per_day",
        "why_cat", "language", "timezone",
        "onboarding_complete", "onboarding_step",
        "onboarding_started_at", "onboarding_completed_at",
        "onboarding_paused_at", "diagnostic_question_count",
        "created_at", "last_updated",
    },
    "student_notes": {
        "note_id", "student_id", "content", "category",
        "confidence", "source", "source_message_id",
        "created_at", "last_reinforced", "expires_at",
        "superseded_by", "is_active", "sensitive",
    },
    "messages": {
        "message_id", "student_id", "session_id",
        "role", "content", "content_type",
        "tg_update_id", "metadata", "embedding", "created_at",
    },
    "sessions": {
        "session_id", "student_id",
        "started_at", "last_activity_at", "ended_at", "end_reason",
        "primary_agent", "message_count", "question_count", "correct_count",
        "metadata", "created_at",
    },
    "episodic_summaries": {
        "summary_id", "session_id", "student_id",
        "summary_text", "themes", "key_moments",
        "performance_data", "domain", "embedding", "created_at",
    },
    "observer_events": {
        "event_id", "student_id", "session_id",
        "event_type", "payload",
        "processed_at", "processing_result", "created_at",
    },
    "scheduled_messages": {
        "schedule_id", "student_id",
        "send_at", "priority",
        "dedup_key", "reason",
        "content", "content_template",
        "created_at", "sent_at", "canceled_at", "canceled_reason",
    },
    "student_question_attempts": {
        "id", "student_id", "question_id", "session_id",
        "served_at", "answered_at", "is_correct", "student_answer",
        "skipped", "explanation_shown", "is_diagnostic", "fallback_tier",
    },
    "llm_calls": {
        "id", "student_id", "session_id", "message_id",
        "service", "model", "purpose",
        "input_tokens", "output_tokens", "cost_usd",
        "latency_ms", "success", "error_message", "created_at",
    },
}


async def main() -> int:
    await init_db_pool()
    drift_found = False
    try:
        for table, expected_cols in EXPECTED.items():
            rows = await db.fetch(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_schema='v5' AND table_name=$1
                """,
                table,
            )
            live_cols = {r["column_name"] for r in rows}
            if not live_cols:
                print(
                    f"[DRIFT] {table}: TABLE DOES NOT EXIST "
                    f"(expected {len(expected_cols)} columns)"
                )
                drift_found = True
                continue
            missing = expected_cols - live_cols
            extra = live_cols - expected_cols
            if missing or extra:
                drift_found = True
                print(f"[DRIFT] {table}:")
                if missing:
                    print(f"  MISSING: {sorted(missing)}")
                if extra:
                    print(f"  EXTRA:   {sorted(extra)}  (in DB but not in data model)")
            else:
                print(f"[OK]    {table}: {len(live_cols)} columns match")
    finally:
        await close_db_pool()

    if drift_found:
        print("\nSCHEMA DRIFT DETECTED. Fix migrations or update EXPECTED dict.")
        return 1
    print("\nSchema matches data model. No drift.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
