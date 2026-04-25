-- 005_create_sessions.sql
--
-- v5.sessions is independent from public.sessions (v4 still uses public).

CREATE TABLE IF NOT EXISTS v5.sessions (
    session_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    student_id            UUID NOT NULL
                          REFERENCES v5.students(student_id) ON DELETE CASCADE,
    primary_agent         VARCHAR(20),
    started_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_activity_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at              TIMESTAMPTZ,
    end_reason            VARCHAR(30),
    message_count         INTEGER NOT NULL DEFAULT 0,
    question_count        INTEGER NOT NULL DEFAULT 0,
    correct_count         INTEGER NOT NULL DEFAULT 0,
    metadata              JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_v5_sessions_student_started
    ON v5.sessions (student_id, started_at DESC);

CREATE INDEX IF NOT EXISTS idx_v5_sessions_open
    ON v5.sessions (last_activity_at)
    WHERE ended_at IS NULL;

-- Now that v5.sessions exists, add the deferred FK from v5.messages.session_id.
-- Wrapped in a DO block so re-runs don't fail with duplicate-constraint errors.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_v5_messages_session'
    ) THEN
        ALTER TABLE v5.messages
            ADD CONSTRAINT fk_v5_messages_session
            FOREIGN KEY (session_id)
            REFERENCES v5.sessions(session_id)
            ON DELETE SET NULL;
    END IF;
END$$;
