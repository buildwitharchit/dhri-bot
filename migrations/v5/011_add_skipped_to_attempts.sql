-- 011_add_skipped_to_attempts.sql
--
-- Slice 2.5 / Fix 1 (Skip button on questions).
--
-- Skip is recorded as: answered_at = now(), skipped = true, is_correct = NULL,
-- student_answer = NULL, explanation_shown = true. The retrieval ladder's
-- "seen" check (NOT EXISTS in v5.student_question_attempts) covers skips
-- naturally; no change to retrieval SQL needed.

ALTER TABLE v5.student_question_attempts
    ADD COLUMN IF NOT EXISTS skipped BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_attempts_skipped
    ON v5.student_question_attempts (student_id, skipped)
    WHERE skipped = true;
