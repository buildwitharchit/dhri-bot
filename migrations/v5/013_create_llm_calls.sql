-- 013_create_llm_calls.sql
--
-- Slice 3: cost / latency / failure-rate tracking for every LLM call.
--
-- Every service that hits an LLM (VARC explanations, resume prompts, planner,
-- mentor, extractor) writes a row here. Foreign keys are nullable so failures
-- BEFORE we have a student_id / session_id / message_id are still recordable.

CREATE TABLE IF NOT EXISTS v5.llm_calls (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    student_id      UUID,
    session_id      UUID,
    message_id      UUID,
    service         VARCHAR(40) NOT NULL,
    model           VARCHAR(80) NOT NULL,
    purpose         VARCHAR(60),
    input_tokens    INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL DEFAULT 0,
    cost_usd        DOUBLE PRECISION NOT NULL DEFAULT 0,
    latency_ms      INTEGER NOT NULL DEFAULT 0,
    success         BOOLEAN NOT NULL,
    error_message   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_llm_calls_student
    ON v5.llm_calls (student_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_llm_calls_service_created
    ON v5.llm_calls (service, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_llm_calls_failures
    ON v5.llm_calls (created_at DESC)
    WHERE success = false;
