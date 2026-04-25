-- 004_create_messages.sql
--
-- v5.messages is independent from public.messages (which v4 still writes to).

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS v5.messages (
    message_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    student_id      UUID NOT NULL
                    REFERENCES v5.students(student_id) ON DELETE CASCADE,
    session_id      UUID,
    role            VARCHAR(20) NOT NULL
                    CHECK (role IN ('user','assistant','system')),
    content         TEXT NOT NULL,
    content_type    VARCHAR(20) NOT NULL DEFAULT 'text',
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    embedding       vector(1536),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_v5_messages_student_created
    ON v5.messages (student_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_v5_messages_session
    ON v5.messages (session_id);

CREATE INDEX IF NOT EXISTS idx_v5_messages_created
    ON v5.messages (created_at);
-- HNSW index on embedding deferred until embeddings are populated (later slice).
