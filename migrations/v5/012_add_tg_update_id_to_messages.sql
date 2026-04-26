-- 012_add_tg_update_id_to_messages.sql
--
-- Slice 2.5 / Fix 7 (Webhook idempotency).
--
-- Telegram retries webhooks if it doesn't get a 200 OK fast enough. Persisting
-- the update_id on the user message gives the orchestrator's Step 0 idempotency
-- check a fast lookup (UNIQUE partial index) and a hard guard against duplicate
-- inserts even under tight races.

ALTER TABLE v5.messages
    ADD COLUMN IF NOT EXISTS tg_update_id BIGINT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_tg_update_id
    ON v5.messages (tg_update_id)
    WHERE tg_update_id IS NOT NULL;
