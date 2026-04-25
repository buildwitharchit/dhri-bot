-- 009_migrate_v4_data.sql
--
-- Backfill v5.students from existing public.tg_users so anyone who used
-- the v4 webhook already has an identity in v5. Append-only.
--
-- Does NOT touch:
--   - public.tg_users     (v4 still reads/writes it)
--   - public.user_profiles (renamed to student_skill_profile in a later slice)
--   - public.attempts      (gains session_id column in a later slice)
--   - public.questions / passages / subskills / traps  (kept as-is)

INSERT INTO v5.students (tg_id, display_name, created_at, last_seen_at)
SELECT
    u.tg_id,
    COALESCE(NULLIF(u.first_name, ''), 'Student'),
    u.joined_at,
    u.last_active_at
FROM public.tg_users u
WHERE NOT EXISTS (
    SELECT 1
    FROM v5.students s
    WHERE s.tg_id = u.tg_id
      AND s.deleted_at IS NULL
);
