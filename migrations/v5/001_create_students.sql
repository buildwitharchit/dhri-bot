-- 001_create_students.sql
--
-- Bootstrap the v5 schema. All v5 tables live under schema "v5" so they
-- coexist with v4's public.sessions / public.messages without colliding.
--
-- Idempotent: safe to re-run.

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE SCHEMA IF NOT EXISTS v5;

CREATE TABLE IF NOT EXISTS v5.students (
    student_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tg_id          BIGINT,
    display_name   VARCHAR(100),
    email          VARCHAR(255),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    preferences    JSONB NOT NULL DEFAULT '{}'::jsonb,
    deleted_at     TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_students_tg_id_active
    ON v5.students (tg_id)
    WHERE deleted_at IS NULL AND tg_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_students_email_active
    ON v5.students (email)
    WHERE deleted_at IS NULL AND email IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_students_created_at
    ON v5.students (created_at);
