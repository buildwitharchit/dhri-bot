-- 010_create_student_question_attempts.sql
--
-- Tracks every (student, question) serve and the resulting answer (if any).
-- Drives the 6-tier retrieval ladder (defines "seen") and answer scoring.
--
-- v4's public.attempts is keyed on tg_id and remains untouched; v5 needs its
-- own student_id-scoped attempts table because retrieval queries join it
-- against v5.students and v5 question lifecycle differs (separate row per
-- serve, even repeats).

CREATE TABLE IF NOT EXISTS v5.student_question_attempts (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    student_id          UUID NOT NULL
                        REFERENCES v5.students(student_id) ON DELETE CASCADE,
    question_id         VARCHAR(60) NOT NULL
                        REFERENCES public.questions(question_id),
    served_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    answered_at         TIMESTAMPTZ,
    is_correct          BOOLEAN,
    student_answer      TEXT,
    explanation_shown   BOOLEAN NOT NULL DEFAULT false
);

CREATE INDEX IF NOT EXISTS idx_sqa_student_question
    ON v5.student_question_attempts (student_id, question_id);

CREATE INDEX IF NOT EXISTS idx_sqa_student_served
    ON v5.student_question_attempts (student_id, served_at DESC);

CREATE INDEX IF NOT EXISTS idx_sqa_unanswered
    ON v5.student_question_attempts (student_id, served_at DESC)
    WHERE answered_at IS NULL;
