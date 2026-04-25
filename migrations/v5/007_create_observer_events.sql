-- 007_create_observer_events.sql

CREATE TABLE IF NOT EXISTS v5.observer_events (
    event_id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    student_id           UUID NOT NULL
                         REFERENCES v5.students(student_id) ON DELETE CASCADE,
    session_id           UUID
                         REFERENCES v5.sessions(session_id) ON DELETE SET NULL,
    event_type           VARCHAR(50) NOT NULL,
    payload              JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at         TIMESTAMPTZ,
    processing_result    VARCHAR(30)
);

CREATE INDEX IF NOT EXISTS idx_observer_unprocessed
    ON v5.observer_events (created_at)
    WHERE processed_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_observer_student_created
    ON v5.observer_events (student_id, created_at DESC);
