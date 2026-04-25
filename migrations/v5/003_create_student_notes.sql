-- 003_create_student_notes.sql

CREATE TABLE IF NOT EXISTS v5.student_notes (
    note_id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    student_id           UUID NOT NULL
                         REFERENCES v5.students(student_id) ON DELETE CASCADE,
    content              TEXT NOT NULL,
    category             VARCHAR(30),
    confidence           FLOAT NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    source               VARCHAR(30),
    source_message_id    UUID,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_reinforced      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at           TIMESTAMPTZ,
    superseded_by        UUID REFERENCES v5.student_notes(note_id),
    is_active            BOOLEAN NOT NULL DEFAULT true,
    sensitive            BOOLEAN NOT NULL DEFAULT false
);

CREATE INDEX IF NOT EXISTS idx_student_notes_student
    ON v5.student_notes (student_id);

CREATE INDEX IF NOT EXISTS idx_student_notes_active
    ON v5.student_notes (student_id, is_active, last_reinforced DESC);

CREATE INDEX IF NOT EXISTS idx_student_notes_category
    ON v5.student_notes (category);

CREATE INDEX IF NOT EXISTS idx_student_notes_expires
    ON v5.student_notes (expires_at)
    WHERE expires_at IS NOT NULL;
