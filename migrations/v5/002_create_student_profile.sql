-- 002_create_student_profile.sql

CREATE TABLE IF NOT EXISTS v5.student_profile (
    student_id              UUID PRIMARY KEY
                            REFERENCES v5.students(student_id) ON DELETE CASCADE,
    target_exam             VARCHAR(20) NOT NULL DEFAULT 'CAT',
    target_year             SMALLINT,
    target_colleges         TEXT[],
    experience_level        VARCHAR(50),
    preparation_stage       VARCHAR(50),
    hours_per_day           VARCHAR(20),
    why_cat                 TEXT,
    language                VARCHAR(10) NOT NULL DEFAULT 'en',
    timezone                VARCHAR(50) NOT NULL DEFAULT 'Asia/Kolkata',
    onboarding_complete     BOOLEAN NOT NULL DEFAULT false,
    onboarding_step         VARCHAR(30),
    onboarding_started_at   TIMESTAMPTZ,
    onboarding_completed_at TIMESTAMPTZ,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_updated            TIMESTAMPTZ NOT NULL DEFAULT now()
);
