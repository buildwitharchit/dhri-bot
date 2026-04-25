-- 008_create_scheduled_messages.sql
--
-- Skeleton table; empty in v1. Defined now so v2 proactive messaging
-- doesn't require a migration later.

CREATE TABLE IF NOT EXISTS v5.scheduled_messages (
    schedule_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    student_id           UUID NOT NULL
                         REFERENCES v5.students(student_id) ON DELETE CASCADE,
    content              TEXT,
    content_template     TEXT,
    send_at              TIMESTAMPTZ NOT NULL,
    priority             INTEGER NOT NULL DEFAULT 5,
    dedup_key            VARCHAR(100),
    reason               VARCHAR(50),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at              TIMESTAMPTZ,
    canceled_at          TIMESTAMPTZ,
    canceled_reason      VARCHAR(100)
);
