-- 014_add_session_id_to_attempts.sql
--
-- Slice 3: session lifecycle wires through. Every attempt served from now on
-- is bound to its session, which lets get_session_stats query by real session
-- and detect_session_resume_candidate find unanswered questions per session.
--
-- Slice 2/2.5 attempts already in the table will have session_id IS NULL and
-- will simply be excluded from session-scoped queries. Backfill not required.

ALTER TABLE v5.student_question_attempts
    ADD COLUMN IF NOT EXISTS session_id UUID;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_sqa_session'
    ) THEN
        ALTER TABLE v5.student_question_attempts
            ADD CONSTRAINT fk_sqa_session
            FOREIGN KEY (session_id) REFERENCES v5.sessions(session_id)
            ON DELETE SET NULL;
    END IF;
END$$;

CREATE INDEX IF NOT EXISTS idx_sqa_session
    ON v5.student_question_attempts (session_id);

CREATE INDEX IF NOT EXISTS idx_sqa_session_unanswered
    ON v5.student_question_attempts (session_id, served_at DESC)
    WHERE answered_at IS NULL AND skipped = false;
