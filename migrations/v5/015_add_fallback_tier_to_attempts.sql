-- 015_add_fallback_tier_to_attempts.sql
--
-- Slice 3 audit follow-up: docs/v5/01_data_model.md specifies fallback_tier
-- SMALLINT on student_question_attempts. Slice 2's migration 010 omitted it.
-- The retrieval ladder already computes the tier (1-6) and surfaces it in
-- response.meta.fallback_tier and v5.messages.metadata.fallback_tier — this
-- migration lets us also persist it on the attempt row so per-attempt tier
-- analysis (e.g. "what % of attempts were tier 4 fallbacks?") becomes possible
-- without joining through v5.messages.
--
-- No backfill: rows inserted by slice 2 / 2.5 / early-slice-3 stay NULL.
-- Code change in services/varc/main.py writes the tier on every new INSERT.

ALTER TABLE v5.student_question_attempts
    ADD COLUMN IF NOT EXISTS fallback_tier SMALLINT;

CREATE INDEX IF NOT EXISTS idx_attempts_fallback_tier
    ON v5.student_question_attempts (fallback_tier)
    WHERE fallback_tier IS NOT NULL;
