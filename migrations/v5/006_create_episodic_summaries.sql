-- 006_create_episodic_summaries.sql

CREATE TABLE IF NOT EXISTS v5.episodic_summaries (
    summary_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id           UUID NOT NULL UNIQUE
                         REFERENCES v5.sessions(session_id) ON DELETE CASCADE,
    student_id           UUID NOT NULL
                         REFERENCES v5.students(student_id) ON DELETE CASCADE,
    domain               VARCHAR(20),
    summary_text         TEXT NOT NULL,
    themes               TEXT[],
    key_moments          JSONB NOT NULL DEFAULT '{}'::jsonb,
    performance_data     JSONB NOT NULL DEFAULT '{}'::jsonb,
    embedding            vector(1536),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_episodic_student_created
    ON v5.episodic_summaries (student_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_episodic_domain
    ON v5.episodic_summaries (domain);

CREATE INDEX IF NOT EXISTS idx_episodic_themes
    ON v5.episodic_summaries USING GIN (themes);
-- HNSW on embedding deferred until populated (v2+).
