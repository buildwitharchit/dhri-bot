# Slice Roadmap — DHRI v5

## Principle

dhri v5 is built in **thin vertical slices**. Each slice ships a working end-to-end capability through every relevant service. Slices grow the system by adding intelligence, not by completing layers.

This is the opposite of horizontal layering ("build all of memory service, then all of profile service..."), which is what produced v4's broken-feeling result. With slices, the system always works at every checkpoint — just less smartly than the final version.

The 9 slices below (8 + a retrofit slice) are designed to be implemented across a few Claude Code sessions. Each slice has clear scope, manual tests, and completion criteria.

**Slice 2.5** was added after slice 2 verification, when an architectural review surfaced 25 issues. Seven of those needed retroactive code fixes to slice 2; eleven were distributed into slices 3-8 prompts; seven were deferred to v1.5+. See `DECISIONS.md` for the catalog and rationale.

---

## Schema Drift Discipline (binds every slice)

Schema drift is the most common silent bug source. After it bit slice 2 (the `fallback_tier` column was specified in `01_data_model.md` but never created), we instituted a discipline that every slice now follows:

1. **Each slice's "Migrations" subsection lists files + columns explicitly.** Not "create the schema" but "create THIS schema with THESE columns." Claude Code reads the explicit list when planning the migration; missing a column is now visible drift, not an oversight.

2. **CREATE TABLE migrations include ALL columns documented in `01_data_model.md` for that table** — even columns this slice doesn't use. Future-slice columns get sensible defaults (NULL, FALSE, 0). This prevents needing follow-up migrations for columns we already know about.

3. **Run `python -m scripts.check_schema_drift` after every slice's migrations apply.** Compare exit code and output to the slice's expected drift state. STOP and fix before declaring slice done if unexpected drift surfaces.

4. **Migration numbering is sequential and never reused.** Each migration file gets the next number. Even no-op migrations (where `IF NOT EXISTS` makes them idempotent on re-runs) take a number. Gaps in numbering make provenance harder when rebuilding.

5. **Mid-cycle migrations (added between slices) get the next available number.** Migration 015 was added during slice 3 verification to fix the `fallback_tier` drift caught from slice 2. It ships standalone, doesn't disrupt slice 3's planned migrations.

This discipline catches drift in seconds (script call) instead of hours (production debugging).

---

## Slice Order Overview

| Slice | Goal | Time est | Key capability added | Status |
|-------|------|----------|----------------------|--------|
| 1 | Skeleton | 2-3h | All services exist, end-to-end flow works | ✅ shipped |
| 2 | Real retrieval | 2-3h | VARC retrieves real questions, scoring works | ✅ shipped |
| **2.5** | **Retrofit fixes** | **3-5h** | **Skip button, mid-question doubts, close keyboards, stats, error fallbacks, idempotency** | ✅ shipped |
| 3 | Working memory + sessions | 3-4h | LLM explanations, session lifecycle, returning-after-break | ✅ shipped (incl. verification fixes) |
| 4 | Planner + guardrails | 3-4h | Intent-driven routing, small_talk distinction, out-of-scope, [Different subskill] picker | ✅ shipped (incl. 5 verification fixes) |
| 5 | Profile reads | 2-3h | Personalized responses, default difficulty, empty-state fallbacks | ⏳ next |
| 6 | Onboarding FSM | 3-4h | New-user flow with pause option + diagnostic + synthesis | — |
| 7 | Session-end + extraction | 3-4h | Episodic memory + profile note growth | — |
| 8 | Mentor + observer | 3-4h | Strategic responses + real-time pattern detection | — |

**Total estimated time: 24-33 hours** of focused implementation. (Slice 3 took longer than estimated due to the markdown render bug + Redis cleanup bug surfacing during verification — both real bugs caught and fixed cleanly.)

If you're working with Claude Code and self-testing in parallel: realistic 4-6 calendar days.

**Six architectural principles bind every slice** (documented in detail in `02_service_contracts.md`):

1. Bot NEVER auto-serves a question after answer (diagnostic exception)
2. Old keyboards must be closed when new question served
3. Active session state cleared on session boundary (any code path closing Postgres session clears Redis state)
4. Webhook idempotency via tg_update_id
5. UX never breaks on infrastructure failure (graceful fallbacks)
6. Profile cache invalidation mandatory on writes

When implementing any slice, verify the change preserves all six.

---

## Slice 1: Skeleton — End-to-End Thin Thread

### Goal

Prove the architecture composes. A user sends any message, every service is touched, a response comes back. No intelligence yet — just plumbing.

### What's real

- **Database schema:** All v5 tables created (students, student_profile, student_notes, messages, sessions, episodic_summaries, observer_events, scheduled_messages). Migrations ready.
- **Message Bus:** Receives Telegram webhook, normalizes, sends thinking message, calls orchestrator, edits with response.
- **Orchestrator:** Lock acquisition, identity resolution (creates students row if missing), persist user message, hardcoded routing to VARC agent, persist assistant message, release lock.
- **Memory Service:** `append_turn` (Redis only), `get_recent_turns` (Redis only).
- **Profile Service:** `ensure_profile` (creates default row), `get_minimal_brief` returns hardcoded "Archit, CAT 2026 aspirant".
- **VARC Agent:** Receives context, returns hardcoded response with random question from existing v4 questions table.
- **Mentor Agent:** Stub. Returns hardcoded "Hello, I'm DHRI" if invoked.

### What's stubbed

- Planner LLM call → hardcoded `intent.domain = 'varc'`, `action = 'practice_request'`
- Real retrieval (VARC just picks random question)
- Episodic summaries → empty
- Profile brief → hardcoded
- Session lifecycle → no auto-create, no auto-close
- Rate limiting → check exists but limits set very high
- Spend caps → not enforced
- Onboarding FSM → not implemented (skip onboarding check entirely)
- Guardrails → not implemented
- Observer events → table exists, no inserts
- Async post-processing → none

### Migration from v4

Run migrations to create new tables. Don't drop v4 data yet. Existing tg_users table copied to students.

```sql
-- Create students from tg_users
INSERT INTO students (tg_id, display_name, created_at, last_seen_at)
SELECT tg_id, COALESCE(first_name, 'Student'), created_at, last_seen_at
FROM tg_users;
```

### Migrations (slice 1)

Each migration must include ALL columns specified in `01_data_model.md` for its table — even columns this slice doesn't use yet. Use `IF NOT EXISTS` everywhere for idempotent re-runs.

- `migrations/v5/001_create_v5_schema.sql` — `CREATE SCHEMA IF NOT EXISTS v5;`

- `migrations/v5/002_create_students.sql` — `CREATE TABLE v5.students` with: `student_id`, `tg_id`, `display_name`, `email`, `preferences`, `created_at`, `last_seen_at`, `deleted_at`.

- `migrations/v5/003_create_student_profile.sql` — `CREATE TABLE v5.student_profile` with ALL of: `student_id`, `target_exam`, `target_year`, `target_colleges`, `experience_level`, `preparation_stage`, `hours_per_day`, `why_cat`, `language`, `timezone`, `onboarding_complete`, `onboarding_step`, `onboarding_started_at`, `onboarding_completed_at`, `onboarding_paused_at`, `diagnostic_question_count`, `created_at`, `last_updated`.

- `migrations/v5/004_create_student_notes.sql` — `CREATE TABLE v5.student_notes` per data model.

- `migrations/v5/005_create_messages.sql` — `CREATE TABLE v5.messages` with ALL of: `message_id`, `student_id`, `session_id`, `role`, `content`, `content_type`, `tg_update_id`, `metadata`, `embedding`, `created_at`. Plus the UNIQUE partial index on `tg_update_id WHERE tg_update_id IS NOT NULL`.

- `migrations/v5/006_create_sessions.sql` — `CREATE TABLE v5.sessions` per data model.

- `migrations/v5/007_create_episodic_summaries.sql` — `CREATE TABLE v5.episodic_summaries` per data model.

- `migrations/v5/008_create_observer_events.sql` — `CREATE TABLE v5.observer_events` per data model.

- `migrations/v5/009_create_scheduled_messages.sql` — `CREATE TABLE v5.scheduled_messages` per data model (skeleton table for v2).

**Schema drift verification at end of slice 1:**
```bash
.venv/bin/python -m scripts.check_schema_drift
```

Expected output: `student_question_attempts` and `llm_calls` show as missing (those tables land in slices 2 and 3). All other tables `[OK]`.

### Manual test

1. Restart all services (Railway redeploy or local restart)
2. Open Telegram, message the bot: "hi"
3. Expected: "🤔 Thinking..." appears, then within 2-3 seconds gets edited to a response with a random VARC question
4. Send "another one"
5. Expected: another random question
6. Check Postgres:
   ```sql
   SELECT count(*) FROM students;        -- should be 1+
   SELECT count(*) FROM messages;        -- should be 4 (2 user, 2 assistant)
   SELECT count(*) FROM sessions;        -- should be 0 (slice 1 doesn't create sessions)
   ```
7. Check Redis:
   ```
   GET memory:tg:{your_tg_id}            -- should have list of turns
   GET state:tg:{your_tg_id}             -- should be null (no active session in slice 1)
   ```

### Completion criteria

- [ ] All new tables exist with correct schema
- [ ] Two messages can be sent, both get responses
- [ ] Both messages logged in messages table
- [ ] Redis memory list has both turns
- [ ] No errors in logs
- [ ] Lock acquired and released cleanly
- [ ] Response time < 4 seconds

### Estimated time: 2-3 hours

---

## Slice 2: Real VARC Retrieval

### Goal

Replace random question selection with the existing v4 retrieval pipeline (pgvector + reranking). Questions actually match criteria.

### What's real (added on top of slice 1)

- **VARC Agent retrieval:** Imports v4's retrieval functions (`retrieve_for_subskill` etc.). Hardcoded subskill = 'inference_basic', difficulty = 'medium'. Implements the 6-tier fallback ladder.
- Retrieved question is presented properly (with passage if applicable, options A/B/C/D, inline keyboard).
- Generation LLM call to compose the presentation message (Haiku model).

### What's stubbed (still)

- Planner still hardcoded
- Profile brief still hardcoded
- Episodic still empty
- Session lifecycle still not implemented

### Migrations (slice 2)

- `migrations/v5/010_create_student_question_attempts.sql` — `CREATE TABLE v5.student_question_attempts` with ALL of: `id`, `student_id`, `question_id`, `session_id`, `served_at`, `answered_at`, `is_correct`, `student_answer`, `skipped`, `explanation_shown`, `is_diagnostic`, `fallback_tier`. Indexes per data model: `(student_id, served_at DESC)`, `(student_id, answered_at) WHERE answered_at IS NULL`, `(student_id, question_id)`, partial on `is_diagnostic = true`, partial on `fallback_tier IS NOT NULL`, partial on `skipped = true`.

**Note (slice 3 retrospective):** the original slice 2 migration shipped without `fallback_tier` and `is_diagnostic` columns despite the data model spec'ing them. This was caught during slice 3 verification and patched mid-cycle as `migrations/v5/015_add_fallback_tier_to_attempts.sql`. The remaining gaps (`is_diagnostic` plus two `student_profile` columns) get fixed by `migrations/v5/016_onboarding_columns.sql` in slice 6. **Going forward**, every migration's column list MUST be checked against the data model.

**Schema drift verification at end of slice 2:**
```bash
.venv/bin/python -m scripts.check_schema_drift
```

Expected output: `llm_calls` shows as missing (lands in slice 3). All other tables `[OK]`. **If drift surfaces because of missing columns on tables that exist: STOP and add the missing columns via a follow-up migration before declaring slice 2 done.**

### Manual test

1. Restart, send "give me a question"
2. Expected: An inference question with passage and options appears
3. Tap "B" or any option
4. Expected: For now, just gets another question (we don't process answers yet — that's slice 3)
5. Repeat 5 times
6. Expected: 5 different inference questions (no repeats — fallback ladder respects seen_ids)
7. Check Postgres:
   ```sql
   SELECT m.metadata->>'retrieved_question_id' FROM messages m 
   WHERE role='assistant' AND student_id = ? ORDER BY created_at DESC LIMIT 5;
   -- Should show 5 distinct question IDs
   
   SELECT m.metadata->>'fallback_tier' FROM messages m 
   WHERE role='assistant' ORDER BY created_at DESC LIMIT 5;
   -- Should mostly be tier=1 or tier=2
   ```

### Completion criteria

- [ ] Real questions retrieved, not random
- [ ] No repeat questions (within seen set)
- [ ] Fallback tier logged in `messages.metadata` AND `v5.student_question_attempts.fallback_tier` (the column landed in migration 015 mid-cycle)
- [ ] Question presentation includes passage when applicable
- [ ] Inline keyboard with A/B/C/D works (taps registered, even if not yet processed)
- [ ] Response time < 5 seconds
- [ ] Schema drift checker passes per the expected drift state above

### Estimated time: 2-3 hours

---

## Slice 2.5: Architectural Retrofit

### Goal

Retrofit slice 2's verified base with seven fixes that emerged from architectural review. These should have been in slice 2 from the start, but emerged only after testing the base behavior. Fixing now is cheaper than fixing after slice 8.

See `slice_2_5_retrofit.md` for the full Claude Code prompt and detailed manual test plan.

### What's real (added on top of slice 2)

- **Skip button:** New row 2 on question keyboards: `[Skip / I don't know]`. Inserts an attempt with `skipped=true`, shows explanation without "you picked X", advances to continuation buttons.
- **Mid-question doubt detection:** When student types free text mid-question (with an open unanswered attempt), orchestrator routes to a hardcoded ack response with `[Back to the question]`, `[Skip this question]`, `[I have a different question]` buttons. Slice 2.5 uses hardcoded ack; slice 4's planner makes it LLM-driven.
- **Close old keyboards on new question serve:** When VARC serves a new question, orchestrator removes the inline keyboard from the previous question via Telegram's `editMessageReplyMarkup`. Active session Redis state tracks `last_question_message_id` for this.
- **`[Show my stats]` button:** New continuation row option. Tapping calls `profile_service.get_session_stats(student_id, session_id)` (orchestrator-composed response, no LLM). Shows attempted/correct/skipped/accuracy + subskill breakdown + duration.
- **LLM API failure user-facing fallback:** When generation LLM fails (timeout, rate limit, etc.), retry once. If still failing, return canned "Hmm, having trouble thinking right now. Try again in a moment?" with `[Try again]` button.
- **DB write failure handling:** Step 2 (user msg persist) and step 12 (assistant msg persist) wrapped in try/except with canned error before delivery. Step 13 (memory deltas) wrapped with log+continue (response already delivered).
- **Webhook idempotency via `tg_update_id`:** Step 0 of `handle_message` checks if Telegram's update_id already exists in `v5.messages`. If yes, short-circuit (it's a Telegram retry). UNIQUE partial index on `messages.tg_update_id`.

### What's stubbed (still)

- Planner still hardcoded (slice 4)
- Profile brief still hardcoded minimal (slice 5)
- Episodic still empty (slice 7)
- Session lifecycle still manual (slice 3)
- Onboarding still skipped (slice 6)
- Mentor still stub (slice 8)
- Mid-question doubt response is hardcoded ack (slice 4 makes it LLM-driven)

### Migrations (slice 2.5)

- `migrations/v5/011_add_skipped_to_attempts.sql` — `ADD COLUMN IF NOT EXISTS skipped BOOLEAN DEFAULT FALSE` to `v5.student_question_attempts`. Plus partial index on `skipped = true`. (Slice 2's CREATE TABLE should have included this column inline; if it did, migration 011 is a no-op via `IF NOT EXISTS`. Either way it ships and gets recorded.)

- `migrations/v5/012_add_tg_update_id_to_messages.sql` — `ADD COLUMN IF NOT EXISTS tg_update_id BIGINT` to `v5.messages`. Plus UNIQUE partial index `WHERE tg_update_id IS NOT NULL`. Same `IF NOT EXISTS` story.

**Schema drift verification at end of slice 2.5:**
```bash
.venv/bin/python -m scripts.check_schema_drift
```

Expected output: `llm_calls` shows as missing (lands in slice 3). All other tables `[OK]`.

### Six architectural principles enshrined

This slice is when the 6 principles become operational rules across services. See `docs/v5/02_service_contracts.md` for the full text.

### Manual test

See `slice_2_5_retrofit.md` for detailed test plan covering all 7 fixes.

Summary:
1. Tap [Skip / I don't know] on a fresh question. Verify skipped=true row, explanation without "you picked X", continuation buttons.
2. Get a question, don't answer, type "what does X mean?" — verify mid-question doubt ack with 3 buttons. Tap [Back to the question]; verify re-render. Tap [Skip] from doubt buttons; verify skip flow.
3. Get a question. Answer. Get next question. Scroll up to old question — verify A/B/C/D buttons gone (keyboard cleared).
4. After 2-3 questions, tap [Show my stats] — verify stats response with continuation buttons.
5. Break OPENROUTER_API_KEY. Send a message. Verify "Try again" fallback. Restore key, tap [Try again], verify original intent re-runs.
6. Code review: try/except around user-msg-persist, assistant-msg-persist, memory-deltas.
7. Curl the same Telegram update payload twice in quick succession; verify second is short-circuited (no duplicate user message in v5.messages).

### Completion criteria

- [ ] All 7 fixes implemented
- [ ] All manual tests pass
- [ ] Migrations 011 + 012 applied
- [ ] Preflight 9/9 PASS
- [ ] No regression in slice 1 or slice 2 manual tests
- [ ] DECISIONS.md updated with the 6 architectural principles + slice 2.5 entries

### Estimated time: 3-5 hours

---

## Slice 3: Working Memory + Sessions

### Goal

Make conversation feel continuous. Recent turns flow into LLM context. Sessions auto-create on 30-min gap, auto-close on inactivity. Memory service is real (Postgres source of truth + Redis cache). Returning-after-break feature: bot offers to resume unfinished work.

### What's real (added on top of slice 2.5)

- **Sessions auto-management:** Session created on first message after 30-min gap. Updated on every turn (`last_activity_at`, `message_count`). Closed by `/admin/v5/cleanup-sessions` endpoint, hit by external cron every 10 min.
- **Memory service real:** `append_turn` (Postgres source of truth + Redis LIST cache, 24h TTL, 50 items max). `get_recent_turns` (Redis-first with Postgres fallback, repopulates cache). `get_active_session`, `set_active_session`, `update_active_session`, `clear_active_session`. New `resolve_session(student_id, tg_id)` is the slice 3 entry point — returns `(session_id, resume_candidate, prior_question_message_id)`.
- **Session boundary state clearing (Principle 3, Bug 13):** When a new session starts, orchestrator DELs `state:tg:{tg_id}` BEFORE creating new state. domain_state from closed session never leaks. Layered: `cleanup_inactive_sessions` clears Redis proactively for each session it closes; `resolve_session` does defensive verification on every turn.
- **Returning-after-break feature (Bug 2):** New `memory_service.detect_session_resume_candidate(student_id)`. When a closed session has unanswered work and student returns within 14 days, VARC composes a resume prompt with Sonnet (warmth): "Welcome back. Last time we were on a {subskill} question — want to pick that up, or start fresh?" Buttons (deterministic): `[Resume that question]` `[Start fresh]` `[Just chat first]`.
- **VARC explanations LLM-generated:** Replaces slice 2's hardcoded explanation strings. MODEL_VARC_TUTOR (Haiku 4.5). System prompt includes Principle 1 enforcement ("never include a new question in your response").
- **Cost/latency tracking via `v5.llm_calls` table:** Every LLM call logs one row. New shared modules: `chat_with_metadata` (the canonical LLM interface) and `record_llm_call` (best-effort observability).
- **Old-session keyboard close on new question serve:** When a session boundary fires + a new question is served, orchestrator closes the OLD session's last question's keyboard (via `prior_question_message_id` carried forward by `resolve_session`).

### Slice 3 verification fixes (landed mid-cycle)

Three issues surfaced during Phase A/B verification testing:

- **Markdown rendering broken** — orchestrator-composed responses (stats, etc.) used legacy Markdown parse mode. Subskill names (`inference_basic`) contain underscores; legacy Markdown parser interprets `_` as italic markers and rejected the message. Fixed by switching the bus to HTML parse mode with bus-side fallback to plain text on parse error. LLM-generated content now goes through `html.escape()` before delivery.

- **`v5.sessions.message_count` never incremented** — orchestrator's per-turn UPDATE bumped `last_activity_at` but not `message_count`. Fixed by combining into a single UPDATE; both fields now move together.

- **`cleanup_inactive_sessions` didn't clear Redis** — closed Postgres sessions but left `state:tg:{tg_id}` stale until next user message arrived. Fixed by making `close_session` (called by cleanup) the single source of truth: Postgres update + Redis DEL in one operation. Also added a defensive staleness check in `resolve_session` for layered protection.

### What's stubbed (still)

- Planner still hardcoded with simple regex routing (slice 4)
- Profile brief still hardcoded minimal (slice 5)
- Episodic summaries still stubbed; `process_session_end` is a no-op (slice 7 wires it)
- Mentor still stub (slice 8)
- Onboarding still skipped (slice 6)
- `v5fail` test trigger still in VARC (slice 4 removes it once real LLM error handling is wired around planner)

### Migrations (slice 3)

- `migrations/v5/013_create_llm_calls.sql` — `CREATE TABLE v5.llm_calls` with ALL of: `id`, `student_id`, `session_id`, `message_id`, `service`, `model`, `purpose`, `input_tokens`, `output_tokens`, `cost_usd`, `latency_ms`, `success`, `error_message`, `created_at`. Indexes: `(student_id, created_at DESC)`, `(service, model)`, partial on `success = false`.

- `migrations/v5/014_add_session_id_to_attempts.sql` — `ADD COLUMN IF NOT EXISTS session_id UUID` (FK to `v5.sessions`, nullable, ON DELETE SET NULL) to `v5.student_question_attempts`. Plus partial index for session-scoped unanswered queries.

- `migrations/v5/015_add_fallback_tier_to_attempts.sql` — **Mid-cycle correction.** Adds `fallback_tier SMALLINT` to `v5.student_question_attempts` after schema audit caught the gap from slice 2's incomplete CREATE TABLE. The retrieval ladder was already computing the tier and surfacing it in `messages.metadata`; this migration also lands it on the attempts row for analytics. See DECISIONS.md and `slice_2_5_retrofit.md`-era notes for context.

**Schema drift verification at end of slice 3:**
```bash
.venv/bin/python -m scripts.check_schema_drift
```

Expected output: 3 columns missing on `student_profile` (`onboarding_paused_at`, `diagnostic_question_count`) and `student_question_attempts` (`is_diagnostic`). These are slice 6's territory; will land with migration 016. Exit code 1 (drift exists, but expected). **If the script reports any OTHER drift, STOP and fix before declaring slice 3 done.**

### Manual test

1. Send "give me an inference question"
2. Expected: Question presented, session created in DB
3. Tap a button (any option)
4. Expected: LLM-generated explanation of correct/wrong, attempt recorded, **5-button continuation row** (no auto-served next question).
5. Answer 3 questions
6. Send "what was that last question about?"
7. Expected: Bot uses recent turns context to reference the question
8. Check Postgres:
   ```sql
   SELECT * FROM v5.sessions WHERE student_id = ? ORDER BY started_at DESC LIMIT 1;
   -- 1 active session, message_count > 0
   
   SELECT count(*) FROM v5.student_question_attempts WHERE student_id = ? AND served_at > now() - interval '10 min';
   -- attempts present, all with session_id populated, all with fallback_tier set
   
   SELECT service, purpose, count(*) FROM v5.llm_calls WHERE student_id = ? GROUP BY service, purpose;
   -- should see varc/answer_explanation, varc/skip_explanation rows
   ```
9. Check Redis: `GET state:tg:{tg_id}` shows active session with `last_question_message_id`, `last_question_attempt_id`.
10. Trigger boundary: age the session, hit `/admin/v5/cleanup-sessions`. Verify session closed in Postgres AND Redis state cleared.
11. Send "hi" → resume prompt appears (Bug 2). Tap [Resume] → original question re-served in new session.

### Completion criteria

- [ ] Sessions auto-create on 30-min gap
- [ ] Each turn updates session metadata (`last_activity_at` AND `message_count` in same UPDATE)
- [ ] Answers + skips processed correctly with LLM-generated explanations (preserved from slice 2.5 + new in slice 3)
- [ ] Recent turns visible in context (LLM responses reference them naturally)
- [ ] Active session persists across messages
- [ ] domain_state in Redis has current question, last_question_message_id, last_question_attempt_id
- [ ] **Session boundary clears Redis state (Bug 13):** `cleanup_inactive_sessions` AND `resolve_session` both enforce this; layered defense
- [ ] **Returning-after-break works (Bug 2):** unanswered question + 30+ min gap + new message → resume prompt with [Resume] [Start fresh] [Just chat]
- [ ] **No regression:** all slice 2.5 fixes still work (skip, mid-question doubt, close keyboards, stats button, error fallback, idempotency)
- [ ] **HTML parse mode rendering correct:** stats response shows bold header, no raw asterisks (slice 3 verification fix)
- [ ] LLM call costs logged in `v5.llm_calls` for every call
- [ ] External cron (Railway, cron-job.org, etc.) hits `/admin/v5/cleanup-sessions` every 10 min, closes inactive sessions, clears Redis
- [ ] Schema drift checker passes per the expected drift state above

### Estimated time: 3-4 hours (actual was longer due to 2 verification bugs)

---

## Slice 4: Planner + Guardrails

### Goal

Real intent-driven routing. Single planner LLM call (Gemini Flash) replaces hardcoded routing. Out-of-scope queries soft-redirected. Critical: planner correctly distinguishes `small_talk` (no question served) from `practice_request` (question served) — Bug 15.

### What's real (added on top of slice 3)

- **Planner LLM call:** `services/orchestrator/planner.py` with `classify(message, recent_turns, active_session_summary)` returning IntentClassification. MODEL_PLANNER (Gemini Flash by default). Cost: ~$0.0001-0.0003 per call. Latency: ~500-1500ms added.
- **Robust JSON parsing with safe defaults:** On planner failure, default to `intent.action = "small_talk"` (NOT practice_request — safer). The bot will ask what student wants rather than auto-serving.
- **Granular subskill enum (Bug 22):** Planner returns one of 12 specific subskills (`inference_basic`, `inference_advanced`, `main_idea_full_passage`, `specific_detail`, etc.). VARC falls back to `inference_basic` and logs misclassification if planner returns out-of-enum.
- **Mixed-intent secondary signal (Bug 15):** Planner returns `intent.secondary_signal` for messages with both action and emotional undertone (e.g., "I'm stressed, give me an easy one" → action=practice_request, difficulty=easy, secondary_signal={type:"emotional_undertone", value:"mild_stress"}).
- **Small_talk vs practice_request distinction (Bug 15):** Planner prompt explicitly distinguishes brief acknowledgments (small_talk) from forward-intent (practice_request). When in doubt: small_talk. Bot responds with warm ack + continuation buttons, no question.
- **Conditional context fetching:** Orchestrator fetches profile/episodic/specific_messages per `context_needs` from planner.
- **Out-of-scope guardrails:** Quant/LRDI/general off-topic → templated soft-redirect (no agent invocation, no LLM call). Buttons: `[VARC question]` `[Strategy chat]`.
- **Strategy chat callback:** `[Strategy chat]` tap → "what would you like to talk through?" open response (no buttons; next free-text message flows through planner like any other).
- **Intent-driven routing:** orchestrator routes to varc / mentor (stub) / out_of_scope handler / orchestrator-direct (small_talk, stats, strategy_chat, subskill_picker) based on intent.domain + action. Step 6.5 deterministic detection (slice 2.5) still runs first, overriding planner where appropriate (skip callbacks, continuation callbacks, answer regex, retry, picker).
- **v5fail test trigger removed:** real LLM error handling now wraps the openrouter calls via slice 3's `chat_with_metadata` retry-once + `_error_fallback_response` canned UI.
- **[Different subskill] picker:** real implementation (slice 4 prompt called for it; placeholder shipped initially; verification fix completed it). 2-row 4-button keyboard. Top 3 weakest subskills (≥5 answered attempts, sorted by accuracy ASC) padded with defaults to always reach 4. Cold-start defaults: `[Inference (basic)]` `[Main idea]` `[Specific detail]` `[Inference (advanced)]`. Each button has callback_data `v5_continue_subskill_<subskill_name>`. Tap routes as practice_request with `intent.subskill` set.
- **Keyboard reconstruction on idempotency retry:** `keyboard_json` persisted in `messages.metadata` at Step 12; Step 0's retry path reconstructs keyboard from metadata so duplicate-update_id deliveries return the full inline keyboard, not text-only.
- **Response guidance:** Passed to agents in AgentContext. Agents are aware of `tone` and `should_acknowledge_feeling` (even if profile is still simple).

### Slice 4 verification fixes (landed mid-cycle, all bundled into slice 4 commit)

Five issues surfaced during verification testing. All shipped as part of slice 4:

- **observer_events silent drop (slice 1 design gap, surfaced in slice 4):** AgentResponse carries `observer_events` as a delta. Earlier docs said `commit_deltas` would iterate and persist. `commit_deltas` was never built; the field was silently dropped across slices 1-4. Slice 4 was the first slice to populate it with non-empty content (out_of_scope_query in orchestrator, llm_failure in VARC). Fixed by adding `services/memory/main.py:persist_observer_event` helper. Each emission site calls the helper directly. The response's `observer_events` field is retained as metadata-only.

- **`_error_fallback_response` refactor:** changed from sync to async, takes full `context` (not just `intent`) so it can populate the observer_event payload with student_id and session_id.

- **Mid-question doubt detection over-eager (Bug 15 broken):** Step 6.5's deterministic mid-question doubt detector intercepted ANY free text typed during an open question, including small_talk like "thanks" / "ok" / "another." Fixed by adding `_looks_like_doubt` heuristic: messages ≤ 2 words without question marks or question words fall through to the planner. Real doubts ("what does premise mean?") still route deterministically. Practice requests during open questions correctly close the abandoned question's keyboard before serving the new one (Principle 2 path reused).

- **[Different subskill] picker padding fix:** initial implementation returned `weakest[:4]`, which surfaced a 1-button picker for students with ≥5 attempts on only one subskill. Fixed by always padding with defaults to reach exactly 4 buttons.

- **`close_session` atomicity (audit pass finding):** earlier `close_session` only did the Postgres UPDATE; callers (`cleanup_inactive_sessions`, future paths) were responsible for calling `clear_active_session` separately. Audit revealed the contract documented atomic behavior that didn't exist. Fixed by moving the Redis DEL into `close_session` itself: lookup `tg_id`, UPDATE Postgres (with COALESCE for double-close idempotency), DEL Redis. Every caller now inherits the cleanup automatically. `cleanup_inactive_sessions` simplified — JOIN to `v5.students` removed, redundant `clear_active_session` removed. `resolve_session`'s defensive staleness check kept for defense-in-depth.

### What's stubbed (still)

- Profile brief: hardcoded minimal (no LLM-based personalization yet) — slice 5
- Episodic summaries: empty (`process_session_end` is a no-op stub) — slice 7
- Onboarding: not yet (assume all users post-onboarding) — slice 6
- Mentor: still stub; orchestrator routes to it for `domain='mentor'` but mentor returns a placeholder — slice 8
- `get_default_difficulty`: not yet built; VARC uses hardcoded `DEFAULT_DIFFICULTY = "medium"` when `intent.difficulty` is null. Works today (everyone is cold-start) but breaks silently after slice 6 if students with `preparation_stage='revision'` exist. **Slice 5 must wire this.**

### Manual test

Phase A: regression — verify slice 1-3 + 2.5 still work after the orchestrator refactor.
Phase B: new behavior — test all 11+ scenarios per the slice 4 verification plan.

Both phases plus the 5 verification fixes were exercised before slice 4 was committed. Send "give me an inference question" → practice flow. Type "thanks" during an open question → small_talk_ack (NOT mid_question_doubt_ack). Type "what does premise mean?" → mid_question_doubt_ack (planner bypassed). Send "solve 2x+3=7" → out_of_scope soft redirect with observer_event row in `v5.observer_events`. Tap `[Different subskill]` → 4-button picker. Curl-twice idempotency test → keyboard reconstructed.

### Migrations (slice 4)

None new. Slice 4 is pure code (planner LLM + routing + observer_events inline persistence + close_session atomic + Step 6.5 heuristic + subskill picker + keyboard reconstruction). No schema changes.

**Schema drift verification at end of slice 4:**
```bash
.venv/bin/python -m scripts.check_schema_drift
```

Expected: same 3 missing columns as end-of-slice-3 (slice 6 territory: `onboarding_paused_at`, `diagnostic_question_count`, `is_diagnostic`). No NEW drift introduced by slice 4. Exit code 1 (drift exists, but expected).

### Completion criteria

- [x] Planner LLM call working (Gemini Flash via MODEL_PLANNER)
- [x] Intent classifications stored in messages.metadata
- [x] Small_talk vs practice_request correctly distinguished (Bug 15) — including during open questions
- [x] Mixed-intent secondary signal works (Bug 15)
- [x] Granular subskill from planner (Bug 22)
- [x] Out-of-scope queries get soft-redirect with `[VARC question]` `[Strategy chat]` buttons
- [x] Out-of-scope queries persist `observer_events` row (slice 4 verification fix — `persist_observer_event` helper)
- [x] LLM failures persist `llm_failure` observer_event for slice 8 observer to track
- [x] [Different subskill] picker shows exactly 4 buttons regardless of attempt history
- [x] [Strategy chat] callback works
- [x] Keyboard reconstruction on idempotency retry redelivers full inline keyboard
- [x] `close_session` atomically clears Redis state (audit finding fix)
- [x] Mentor stub invoked for `domain=mentor`
- [x] Out-of-scope queries don't invoke agents (cost savings)
- [x] Response guidance passes through to agents
- [x] No regression: all slice 1-3 + 2.5 behavior preserved
- [x] Default to `small_talk` on planner failure (NOT practice_request)
- [x] `v5fail` test trigger removed from VARC
- [x] Schema drift checker output matches expected

### Estimated time: 3-4 hours (actual: longer due to 5 verification fixes; closer to 6-8 hours)

---

## Slice 5: Real Profile Service (Reads)

### Goal

Replace hardcoded profile brief with real profile data. Responses become personalized. Default difficulty derived from profile (Bug 23 — slice 4 audit confirmed this is currently a hardcoded fallback in VARC; slice 5 fixes it). Empty-state fallbacks for new students (Bug 25).

### Carry-over from slice 4

The slice 4 audit pass surfaced that `get_default_difficulty` is documented in `02_service_contracts.md` but doesn't exist in code. VARC currently uses a hardcoded `DEFAULT_DIFFICULTY = "medium"` constant when `intent.difficulty` is null. This works today because every student is effectively cold-start (no `preparation_stage` populated until slice 6 onboarding lands). But after slice 6 ships, students with `preparation_stage='revision'` will silently get medium questions instead of hard.

**Slice 5 must wire `get_default_difficulty` and thread it through AgentContext.** The function reads `student_profile.preparation_stage` and maps to "easy" | "medium" | "hard" per the contract. AgentContext gets a new `default_difficulty` field. VARC's `_handle_practice_request` reads from AgentContext.default_difficulty instead of the hardcoded constant.

This is documented in `02_service_contracts.md` under `get_default_difficulty` with a "Status: function not built yet" note. Remove that status note when slice 5 ships.

### What's real (added on top of slice 4)

- **Profile Service `get_tutor_brief`** — full implementation. Pulls from `v5.student_profile`, `v5.student_skill_profile`, `v5.student_notes`, recent episodic summaries (still empty until slice 7). Template-assembled string. NO LLM call.
- **Empty-state fallbacks (Bug 25):** Students with <5 questions practiced get coherent friendly text, not "N/A" placeholders. Students with no recent sessions get "first time back since onboarding {N} days ago" or similar. Never empty sections.
- **`get_minimal_brief`** — short version (~50-100 tokens) for low-context queries.
- **`get_default_difficulty(student_id)` (Bug 23):** Derives from preparation_stage. `just_starting`→easy, `mid_prep`→medium, `final_3_months`→medium, `revision`→hard. Threaded through AgentContext as a new `default_difficulty` field. VARC's `_handle_practice_request` uses it when planner doesn't supply difficulty (replaces slice 4's hardcoded `DEFAULT_DIFFICULTY = "medium"` constant).
- **`get_active_notes(student_id, filter)`:** SQL with `confidence × exp(-Δt / 30 days)` scoring (30-day half-life), top 20 ordered by score.
- **Profile brief Redis cache:** `profile:brief:{student_id}` with 30-min TTL. Read in `get_tutor_brief`. Invalidated (DEL) on any write to student_notes/student_profile/student_skill_profile (Principle 6 — Bug 14 hardening — operational starting slice 5 per the scope note in service contracts).
- **Skill signals computed from `v5.student_question_attempts`:** top/bottom subskill (with min 5 attempts), recent accuracy, trap counts. Cached briefly in Redis (1-hour TTL).
- **`/admin/notes/add` endpoint:** For manual note testing before slice 7 builds auto-extraction.

### What's stubbed (still)

- Notes extraction (writing): not yet — slice 7
- Episodic memory: still empty (slice 7 generates them)
- Onboarding FSM: not yet

### Manual seeding for testing

Insert into student_notes manually for your test user:

```sql
INSERT INTO student_notes (student_id, content, category, confidence, source, last_reinforced)
VALUES 
  ('your-uuid', 'Falls for out-of-scope traps on inference questions', 'pattern', 0.9, 'observed_behavior', now()),
  ('your-uuid', 'Prefers technical/scientific passages over humanities', 'preference', 0.85, 'observed_behavior', now()),
  ('your-uuid', 'Working professional, has 2-4 hours per day for prep', 'goal', 0.95, 'explicit_statement', now()),
  ('your-uuid', 'Currently in mid-prep phase, targeting CAT 2026', 'goal', 1.0, 'explicit_statement', now()),
  ('your-uuid', 'Mentioned work stress in last 2 weeks', 'emotional', 0.7, 'observed_behavior', now() - interval '5 days');
```

Also fill student_profile manually:
```sql
UPDATE student_profile 
SET target_year = 2026, experience_level = 'working_professional', 
    preparation_stage = 'mid_prep', hours_per_day = '2-4', onboarding_complete = true
WHERE student_id = 'your-uuid';
```

### Manual test

1. Send "give me an inference question"
2. Expected: VARC agent's response includes references like "watch for out-of-scope traps" or "this one's technical, which I know you like"
3. Send "I had a rough day, can we do something easy?"
4. Expected: Planner detects emotional_tone='low', VARC gets full profile brief, response is calibrated warm + serves easy question
5. Check the assistant message metadata:
   ```sql
   SELECT metadata FROM messages WHERE role='assistant' ORDER BY created_at DESC LIMIT 1;
   -- Look at context_loaded.profile = 'full' or 'minimal'
   ```
6. Verify cache: send same query twice within 30 min, check no extra DB queries the second time

### Completion criteria

- [ ] Tutor brief assembled from real data
- [ ] Notes show up in responses meaningfully (LLM weaves them naturally)
- [ ] **Profile brief cache invalidation works (Principle 6, Bug 14):** every write to student_notes/student_profile DELs the cache; verify GET returns null after write
- [ ] **Empty-state fallbacks work (Bug 25):** fresh student with 0 attempts gets coherent text, not "N/A"
- [ ] **Default difficulty derives from preparation_stage (Bug 23):** verify by setting preparation_stage manually and observing VARC's question difficulty
- [ ] Minimal vs full brief used appropriately based on planner's context_needs
- [ ] Performance stats from `v5.student_question_attempts` included
- [ ] **No regression:** all slice 1-4 + 2.5 behavior preserved
- [ ] Total response time still < 5 seconds (cache helps)
- [ ] Schema drift checker output matches expected

### Migrations (slice 5)

Slice 5 may need to create `v5.student_skill_profile` if not already present. Verify first:

```sql
SELECT count(*) FROM information_schema.tables
WHERE table_schema='v5' AND table_name='student_skill_profile';
```

If 0 (table doesn't exist):
- `migrations/v5/0XX_create_student_skill_profile.sql` — Create `v5.student_skill_profile` per data model. **Include all columns documented** in `01_data_model.md`. Use the next available migration number after slice 4's last migration.

If table already exists, no migrations.

After applying, update `scripts/check_schema_drift.py`'s EXPECTED dict to include `student_skill_profile`.

**Schema drift verification at end of slice 5:**
```bash
.venv/bin/python -m scripts.check_schema_drift
```

Expected: same 3 missing columns as end-of-slice-3/4 (slice 6 territory). If slice 5 added `student_skill_profile`, that table should show `[OK]`.

### Estimated time: 2-3 hours

---

## Slice 6: Onboarding FSM

### Goal

New users go through full onboarding: profile fields + optional 5-question diagnostic test + mentor synthesis. Includes pause option (Bug 6) and post-onboarding session boundary (Bug 24).

### What's real (added on top of slice 5)

- **Orchestrator `handle_onboarding_step`:** Full FSM implementation. States: `not_started` → `awaiting_name` → `awaiting_target_year` → `awaiting_experience_level` → `awaiting_preparation_stage` → `awaiting_hours_per_day` → `awaiting_target_colleges_optional` → `awaiting_why_cat_optional` → `diagnostic_offered` → `diagnostic_active` → `mentor_synthesis` → `completed`.
- **Inline keyboards** for each onboarding step (no LLM calls during data collection).
- **Onboarding pause option (Bug 6):** Every state (except `diagnostic_active`) includes `[Do this later]` button. On tap: set `onboarding_paused_at = now`, send "Sure, take your time. Just send a message when you're ready." Resume from same state on next message.
- **VARC `serve_diagnostic_question(student_id, q_index)`:** Q1 easy inference_basic, Q2 easy main_idea_full_passage, Q3 medium inference_basic, Q4 medium specific_detail, Q5 hard inference_basic. Specific picks, NOT 6-tier ladder.
- **VARC diagnostic mode (auto-continue exception to Principle 1):** During Q1-Q4, after answer/skip, scoring + brief explanation + AUTO-SERVE next diagnostic question. NO continuation buttons during diagnostic. Q5: scoring + transition message + trigger mentor_synthesis. Skip button still works during diagnostic.
- **Mentor `synthesize_diagnostic`:** Sonnet 4.5. Inputs: profile + 5 diagnostic attempts. Output: warm 3-4 paragraph synthesis ending with deterministic 4-button continuation row: `[Practice my weakest]` `[Explore my strongest]` `[Ask DHRI a question]` `[Just chat first]`.
- **Mentor `handle_skip_diagnostic`:** For students who skip diagnostic. Shorter synthesis. Buttons: `[Start with easy inference]` `[Pick my own focus]` `[Just chat]`.
- **Post-onboarding session boundary (Bug 24):** When mentor synthesis completes, the onboarding session is closed (`ended_at = now`, `end_reason = 'onboarding_complete'`). The `close_session` call DELs Redis state automatically (Principle 3). Next message starts a fresh session, normal practice flow. Diagnostic attempts (`is_diagnostic = true`) DO count toward stats but are flagged for analytics.

### What's stubbed (still)

- Notes extraction at session end: slice 7
- Mentor's reactive responses for "review_progress" etc.: slice 8
- Mentor observer mode: slice 8

### Migrations (slice 6)

- `migrations/v5/016_onboarding_columns.sql` — adds the 3 columns the onboarding FSM needs that weren't created in earlier slices' CREATE TABLEs:
  - `student_profile.onboarding_paused_at TIMESTAMP WITH TIME ZONE` (Bug 6 — pause option)
  - `student_profile.diagnostic_question_count SMALLINT DEFAULT 0` (counter for diagnostic Q1-Q5)
  - `student_question_attempts.is_diagnostic BOOLEAN DEFAULT FALSE` (analytics flag for diagnostic attempts)
  - Plus partial index on `is_diagnostic = true`
  
  All `ADD COLUMN IF NOT EXISTS`. Existing rows: `paused_at NULL`, `count 0`, `is_diagnostic FALSE`. No backfill needed.

If by the time slice 6 runs, slice 1's migration 003 had ALREADY included `onboarding_paused_at` and `diagnostic_question_count` (per the updated data model), migration 016 only adds `is_diagnostic` — the other two are no-ops thanks to `IF NOT EXISTS`. Either way the migration ships and is recorded.

**Schema drift verification at end of slice 6:**
```bash
.venv/bin/python -m scripts.check_schema_drift
```

Expected output: ALL tables `[OK]`. Exit code 0. **If any drift remains, STOP and fix before declaring slice 6 done.** This is the slice that brings the schema fully aligned with the data model.

### Manual test

1. **Reset your test user** to mimic new student:
   ```sql
   UPDATE v5.student_profile SET onboarding_complete = false, onboarding_step = null,
       target_year = null, experience_level = null, /* ... reset all */ 
   WHERE student_id = 'your-uuid';
   
   DELETE FROM v5.student_notes WHERE student_id = 'your-uuid';
   DELETE FROM v5.messages WHERE student_id = 'your-uuid';
   DELETE FROM v5.sessions WHERE student_id = 'your-uuid';
   DELETE FROM v5.student_question_attempts WHERE student_id = 'your-uuid';
   ```
2. Restart services
3. Send `/start`
4. Expected: Welcome message + "Let's start" button
5. Click through each onboarding step:
   - Confirm name
   - Pick year (2026)
   - Pick experience (working professional)
   - Pick stage (mid-prep)
   - Pick hours (2-4)
   - Pick colleges (or skip)
   - Skip "why CAT"
   - Click "Take 5-question test"
6. Answer 5 diagnostic questions (mix of correct/wrong intentionally)
7. Expected: After Q5, immediately receive a synthesis message from mentor
8. Check Postgres:
   ```sql
   SELECT * FROM v5.student_profile WHERE student_id = 'your-uuid';
   -- onboarding_complete=true, all fields populated
   
   SELECT count(*) FROM attempts WHERE student_id = 'your-uuid';
   -- 5
   
   SELECT * FROM student_skill_profile WHERE student_id = 'your-uuid';
   -- Has weakest_subskill set
   ```
9. Click "Practice my weakest" from mentor synthesis
10. Expected: Normal practice flow begins, questions on weakest subskill

### Completion criteria

- [ ] FSM advances through all states
- [ ] Each FSM transition saves data correctly
- [ ] Diagnostic test serves 5 specific questions in correct order (Q1 easy inference → Q5 hard inference)
- [ ] Mentor synthesis uses Sonnet, references test results
- [ ] Skip path works (mentor synthesis without test data)
- [ ] **Onboarding pause works (Bug 6):** [Do this later] sets onboarding_paused_at, resumes from same step on next message
- [ ] **Post-onboarding session boundary (Bug 24):** onboarding session closed after synthesis; new session for next message
- [ ] **Existing students unaffected:** any students predating slice 6 have onboarding_complete=true (set via migration)
- [ ] **No regression:** all slice 1-5 + 2.5 behavior preserved
- [ ] After onboarding, normal flow works (continuation buttons after answers, etc.)

### Estimated time: 3-4 hours

---

## Slice 7: Session-End Pipeline + Profile Extraction

### Goal

When sessions end, generate episodic summaries and extract profile notes via single combined LLM call (cost optimization). Profile grows organically. Returning-after-break (slice 3) now has rich summaries to draw from.

### Carry-over from slice 4 audit pass (silent-drop pattern)

The slice 4 audit pass surfaced a class of bug: deltas documented on AgentResponse that aren't iterated by the orchestrator. `observer_events` was the first; the audit confirmed `notes_proposed` is the next likely tripwire if slice 7 isn't careful.

**Slice 7 implementation rule:** the extractor's notes do NOT flow back through `AgentResponse.notes_proposed`. The extractor runs in `process_session_end` (an async background pipeline, not in the request flow) and writes notes directly via `profile_service.add_note(...)`. There is no orchestrator iteration of any "notes" delta field.

If you find yourself emitting `response.notes_proposed = [...]` from any handler during slice 7, STOP — that's the silent-drop pattern again. Persist via `add_note` directly at the emission site instead.

This is documented in `02_service_contracts.md` "Cross-Service Contract: Persistence Pattern."

### What's real (added on top of slice 6)

- **Memory Service `process_session_end(session_id)`:** Full pipeline. Single LLM call (MODEL_EXTRACTOR = Gemini Flash via `chat_with_metadata` + `record_llm_call` for observability) returns JSON with: summary, topics, notes_to_add (with category, content, confidence, expires_after_days), skill_signals. Notes are persisted directly via `profile_service.add_note` — NOT via a delta on the response.
- **Memory Service `cleanup_inactive_sessions`:** Cron, runs every 10 min. Closes sessions inactive > 30 min via `close_session` (atomic Postgres + Redis DEL since slice 4 audit fix). Triggers `process_session_end` for each closed session.
- **Profile Service extraction integration:** Adds notes from extraction LLM via `add_note` (which DELs cache per Principle 6). Reinforces matches against existing notes (substring overlap > 50%) instead of duplicating.
- **Profile Service conflict resolution:** Basic rules — latest wins for category=context with expires_after_days; never override category=goal without explicit student statement.
- **Episodic summaries used:** Planner can request them via `context_needs.episodic.needed`. Profile brief includes "recent activity" section drawing from latest summary. Slice 3's returning-after-break logic now references real episodic content (not just "we were on a {subskill} question").
- **Note expiration:** category=context defaults to 14-day expiration. category=anxiety_pattern/skill_gap/goal: no expiration.

### What's stubbed (still)

- Mentor reactive mode: slice 8
- Mentor observer mode: slice 8 (raw signal source for slice 7's extractor — slice 7 ships extractor; slice 8 ships the observer that produces signals into observer_events)

### Manual test

1. Have an active session, do 4-5 questions (mix correct/wrong/skip).
2. Manually trigger cleanup or wait > 30 min.
3. Verify session closed in DB: `SELECT ended_at, end_reason FROM v5.sessions WHERE student_id = 'your-uuid' ORDER BY ended_at DESC LIMIT 1;`
4. Verify episodic summary: `SELECT * FROM v5.episodic_summaries WHERE student_id = 'your-uuid' ORDER BY created_at DESC LIMIT 1;` — 1 row with sensible summary, topics, key_moments, performance_data.
5. Verify notes: `SELECT * FROM v5.student_notes WHERE student_id = 'your-uuid' ORDER BY created_at DESC LIMIT 5;` — 1-3 new notes with reasonable categories.
6. Verify cache invalidation: `GET profile:brief:{student_id}` in Redis returns nil after extraction.
7. Send a new message (starts new session). Verify response references "last session" naturally.
8. Run a second similar session covering same ground. Verify duplicate notes are reinforced (last_reinforced updated, confidence bumped) NOT duplicated.
9. Verify v5.llm_calls has the extraction call (~$0.005).

### Completion criteria

- [ ] Inactive sessions auto-close
- [ ] Episodic summaries generated correctly with sensible content
- [ ] Profile notes extracted from sessions
- [ ] **Profile brief cache invalidated on extraction (Principle 6, Bug 14):** every add_note DELs cache
- [ ] Existing notes get reinforced when relevant (not duplicated)
- [ ] Sensitive notes flagged appropriately
- [ ] LLM call cost ~$0.005 per session (Gemini Flash, single combined call)
- [ ] Returning-after-break logic (slice 3) works with new episodic data
- [ ] **No regression:** all slice 1-6 + 2.5 behavior preserved
- [ ] Schema drift checker passes — all `[OK]`

### Migrations (slice 7)

None new. Slice 7 is pure code (extractor + episodic summary writes use existing `v5.episodic_summaries` and `v5.student_notes` tables created in slice 1).

**Schema drift verification at end of slice 7:**
```bash
.venv/bin/python -m scripts.check_schema_drift
```

Expected: ALL tables `[OK]`. No drift (slice 6 already eliminated remaining drift).

### Estimated time: 3-4 hours

---

## Slice 8: Mentor Reactive + Observer

### Goal

Mentor agent fully functional. Strategic queries get real responses. Observer detects patterns inline. Real-time pattern signal flows into AgentContext (Bug 4). The 3 priority "wow moments" verified end-to-end.

### What's real (added on top of slice 7)


- **Mentor Agent `handle(context)`:** Full implementation. Single LLM call (Sonnet 4.5 = MODEL_MENTOR) using full profile + episodic + notes context. Handles:
  - `action=review_progress` ("how am I doing?")
  - `action=vent` (emotional support)
  - `action=meta` (questions about dhri itself)
  - `action=casual` (warm acknowledgments)
- **Contextual continuation buttons (deterministic, in orchestrator):** After mentor returns, orchestrator picks one of 4 button-set patterns based on intent.action + emotional_tone:
  - Anxiety/frustration: `[Try one easy one]` `[Different subskill]` `[Talk it out more]` `[Take a break]`
  - Strategy/review_progress: `[Practice my weak areas]` `[Show my stats]` `[Ask another question]` `[I'm done]`
  - Motivation/casual: `[One easy win]` `[Just chat]` `[Show my stats]` `[I'm done]`
  - Meta queries: `[OK, let's practice]` `[Different subskill]` `[Ask another meta question]` `[I'm done]`
- **System prompt rule for Mentor LLM:** "End with a brief 'what's next' question. Do NOT include a question with passage and A/B/C/D options. The button options are added by the system."
- **Mentor `inline_observe(student_id, session_id, intent, agent_response, recent_turns)`:** Runs async via `asyncio.create_task` after response sent. Gemini Flash scans recent turns for patterns. Returns 0-3 lightweight events to insert into `v5.observer_events`:
  - Consecutive wrongs (3+) → `pattern_name='consistent_struggle'`
  - Same trap multiple times → reinforce pattern note
  - Metacognitive questions → `pattern_name='breakthrough'`
  - Self-corrections → growth signal
  - Frustrated language → `pattern_name='frustration'`
- **Real-time pattern signal in AgentContext (Bug 4):** Before agent invocation, orchestrator queries `v5.observer_events` for high-confidence patterns within current session. Sets `context.realtime_pattern` if relevant. VARC's LLM system prompt includes this signal: "Note: this is the {N}th time the student has hit this trap in recent sessions. Reference this pattern explicitly in your explanation." This makes wow-moment-1 work within a session, not just across sessions.
- **Slice 7's extractor (already built) consumes observer_events:** Events with confidence > 0.7 are considered note candidates by the extractor.

### What's stubbed (still)

- Initiator mode (proactive messages): deferred to v1.5
- Scheduler service: deferred to v1.5
- Cross-session goal tracking: deferred to v1.5

### Manual test

**Reactive mentor:**
1. Send "I'm feeling really demotivated, my accuracy isn't improving" → Mentor responds (not VARC); response references specific past struggles if notes exist; ends with anxiety-set continuation buttons.
2. Send "I have 3 months left, what should I focus on?" → Mentor responds with strategic advice grounded in profile + notes; ends with strategy-set continuation buttons.
3. Send "what is dhri?" → Mentor handles meta question with meta-set continuation buttons.

**Observer mode:**
4. Get 3 inference questions wrong with same trap → check `v5.observer_events`: expect rows with pattern_name='consistent_struggle' and confidence > 0.7.
5. Send "ugh, this is frustrating" after wrong answer → expect pattern_name='frustration' event.
6. Verify response shown to user is unaffected by observer events (response delivered before observer runs).

**Real-time signal (Bug 4):**
7. Within a single session: get a question wrong via out_of_scope trap. Then get another wrong via same trap. Verify VARC's explanation explicitly references the trap pattern (not generic). Check `messages.metadata.context_loaded.realtime_pattern` is populated.

**Wow moment 1 (cross-session pattern recall):**
8. Have a session with 2-3 wrong answers via out_of_scope trap. Trigger cleanup. Start new session. Get an inference question wrong via out_of_scope. Verify VARC's explanation references the past pattern explicitly ("this is the kind of thing we've talked about — last time you hit out_of_scope on inference too...").

**Wow moment 2 (returning-after-break):**
9. Have a session, leave a question unanswered, wait 30+ min (or trigger cleanup). Send "hi". Verify resume prompt with specific reference to the unanswered question. Tap [Resume] → verify question re-served.

**Wow moment 3 (topic switch):**
10. Mid-VARC session (just got a question), abruptly type "actually how do I manage my time better?" → Verify Mentor takes over (planner classifies as mentor + review_progress); response acknowledges the switch ("happy to come back to that question anytime"); ends with strategy-set buttons.

### Completion criteria

- [ ] Mentor handles strategic queries with real data (notes + episodic + profile referenced)
- [ ] Mentor handles emotional venting with specificity (references past patterns)
- [ ] **Continuation buttons after every Mentor response (Principle 1):** different button sets for anxiety vs strategy vs motivation vs meta
- [ ] Inline observer processes events without blocking response
- [ ] Pattern detection triggers note creation/reinforcement
- [ ] **Real-time pattern signal flows into AgentContext (Bug 4):** within-session pattern detection works
- [ ] **Wow moment 1 verified:** wrong answer references past mistake (cross-session)
- [ ] **Wow moment 2 verified:** returning-after-break, bot remembers
- [ ] **Wow moment 3 verified:** topic switches handled smoothly
- [ ] Mentor uses MODEL_MENTOR (Sonnet 4.5)
- [ ] Async post-processing doesn't add user-visible latency
- [ ] **No regression:** all slice 1-7 + 2.5 behavior preserved
- [ ] Schema drift checker passes — all `[OK]`

### Migrations (slice 8)

None new. Slice 8 is pure code (mentor agent + observer use existing `v5.observer_events` table created in slice 1).

**Schema drift verification at end of slice 8 (the v1 ship gate):**
```bash
.venv/bin/python -m scripts.check_schema_drift
```

Expected: ALL `[OK]`. No drift. **At v1 ship: schema must match data model exactly. No deferred drift remaining.**

### Estimated time: 3-4 hours

---

## After All Slices: Quality Pass (1 day)

Once slice 8 is verified, before shipping to first 5-10 users, do a focused quality pass:

### Day 1: Self-use as a real student

Use dhri yourself for 2-3 hours of actual VARC prep. Don't test individual features — use it AS the student. Note every place that feels:
- Slow (>5s response)
- Confusing (had to re-read or scroll up)
- Robotic (response felt templated, not warm)
- Broken (didn't do what you expected)
- Missing (wanted a button/option that wasn't there)

Most issues will be prompt-engineering tweaks, not architectural. Iterate the system prompts in:
- `services/varc/prompts.py` — explanation tone, length, warmth
- `services/mentor/prompts.py` — empathy, specificity, transitions
- `services/orchestrator/planner.py` — small_talk vs practice_request edge cases

### Day 2: Friend test (optional)

If you have a friend prepping for CAT, give them access for an hour. Watch (with permission) what they do. Their first 10 minutes will reveal more than your own testing — they'll do things you didn't predict.

### Quality bar before shipping:

- [ ] You'd send dhri's link to a friend without caveats
- [ ] Average response feels like a tutor, not a chatbot
- [ ] No more than 1 in 20 responses feels robotic or off
- [ ] No crashes or stuck states observed in 2+ hours of personal use
- [ ] Cost per active student per day < ₹15 (~$0.18)

If quality bar isn't hit: more prompt iteration. If structural issues remain: don't ship; address first.

### Shipping protocol:

1. Tag git: `git tag v1.0 && git push --tags`
2. Final preflight: 9/9 PASS on production
3. Post to your network: 5-10 close friends/colleagues prepping for CAT
4. Watch logs (Sentry, v5.llm_calls cost rollup) for first 48 hours
5. Daily check-in: read 5-10 conversations end-to-end, note recurring issues
6. Iterate prompts daily for first week; bigger structural changes go on a backlog

---

## Out of Scope for v1

Explicitly NOT building in this slice plan:

### Deferred to v1.5 / v2:

- **Scheduler service:** No proactive outreach. No absence check-ins. No reminders.
- **Mentor initiator mode:** No bot-initiated messages.
- **Multi-agent (quant, LR, DI, GDPI):** VARC + Mentor only.
- **Web UI:** Telegram only.
- **Monetization:** No payments, no premium tiers.
- **Analytics dashboard (full):** Basic Streamlit only after v1 ships.
- **Eval pipeline:** Build separately after v1 ships, in `dhri-evals/` folder.
- **Vector search on episodic:** Skip; SQL filtering enough.
- **Vector search on notes:** Skip; SQL filtering enough.
- **Cross-agent communication:** Not needed with one specialist.
- **User-facing memory inspection:** Future feature.

### Things explicitly not added (architectural decisions):

- LLM in profile read path (template only)
- Mentor calling VARC agent directly (always through orchestrator)
- Multiple planner LLMs (one combined call only)
- Real-time streaming of responses (batch response only)

---

## Risk and Contingency

### High-risk slices (where things often break):

**Slice 4 (Planner):**
- Planner LLM may return malformed JSON. Build robust fallback to default classification.
- Planner may misclassify subtle cases. Iterate on prompt with examples after slice ships.
- Performance: planner adds ~1.5s. If unacceptable, simplify the prompt or use cheaper model.

**Slice 6 (Onboarding):**
- FSM has many states; bugs likely. Test each transition carefully.
- Diagnostic test depends on having questions of all difficulty/subskill combinations. Verify your 48 questions cover this; seed more if needed.
- Mentor synthesis quality depends heavily on the LLM call and prompt. Iterate.

**Slice 7 (Session-end):**
- Combined LLM call may produce malformed JSON. Robust parsing required.
- Profile extraction may be too aggressive (creating duplicate notes) or too conservative (missing real signals). Iterate on extraction prompt.

### Skipping slices

If you run out of time, skip slices in this order of priority:

1. **Skip slice 8 (Mentor):** Bot still works without sophisticated mentor. Ship with VARC-only.
2. **Skip slice 7 (Extraction):** Bot works without auto-extraction. Manual notes still possible.
3. **Skip slice 6 (Onboarding):** New users get a basic "set your preferences" command instead. Less polished.

Don't skip slices 1-5. They're the minimum viable.

---

## Slice Completion Self-Check

After each slice, before moving on:

1. **Manual test passes** — all checklist items verified
2. **No new errors in logs** — clean Sentry/console output
3. **Database is consistent** — spot-check rows, no orphaned records
4. **Latency is acceptable** — < 5s for normal turns
5. **You can use it yourself** — try a 5-message exchange, feels OK

If any of these fail, fix before proceeding. Slices accumulate problems otherwise.

---

## After All Slices: Quality Pass

Once slice 8 ships, do a 1-day quality pass before declaring v1 ready:

- [ ] Self-test for 1 hour as a new user (full onboarding + 30 minutes of practice)
- [ ] Self-test as returning user (after 1+ day gap)
- [ ] Test all 5 happy paths from `03_happy_path.md`
- [ ] Verify all rate limits and spend caps work
- [ ] Verify guardrails block correctly
- [ ] Check costs: total spend / total user messages should be < $0.01/message

Then ship to first 5-10 users.

---

## Documentation Discipline

After each slice, update `DECISIONS.md` on main branch with:
- What was implemented
- What was deferred and why
- Any architectural pivots from the original design
- Cost / latency observations

This DECISIONS.md becomes a major portfolio asset for Sarvam application.

---

## Final Notes on This Plan

This roadmap is designed to be **executed by Claude Code in a single session**, with you reviewing each slice before approving the next. The slices are independent enough that if Claude Code makes a mistake on slice N, it doesn't corrupt slice N-1.

Key principles to enforce during implementation:

1. **One slice at a time.** Don't let Claude Code start slice 3 before slice 2 is verified.
2. **One file at a time within a slice.** Approve each file before moving to the next.
3. **Test after every slice.** Don't accumulate untested changes.
4. **No premature polishing.** Slice 8 is when polish begins.
5. **Stub aggressively.** Stubs are documentation that this isn't done yet.

If a slice takes 2x estimated time, that's a signal to pause and reassess. Don't push through. Either the design has a gap (fix the design), or the implementation is going off-plan (refocus).

Good luck. Ship slice 1 by end of day 1.
