# Slice Roadmap — DHRI v5

## Principle

dhri v5 is built in **thin vertical slices**. Each slice ships a working end-to-end capability through every relevant service. Slices grow the system by adding intelligence, not by completing layers.

This is the opposite of horizontal layering ("build all of memory service, then all of profile service..."), which is what produced v4's broken-feeling result. With slices, the system always works at every checkpoint — just less smartly than the final version.

The 9 slices below (8 + a retrofit slice) are designed to be implemented across a few Claude Code sessions. Each slice has clear scope, manual tests, and completion criteria.

**Slice 2.5** was added after slice 2 verification, when an architectural review surfaced 25 issues. Seven of those needed retroactive code fixes to slice 2; eleven were distributed into slices 3-8 prompts; seven were deferred to v1.5+. See `DECISIONS.md` for the catalog and rationale.

---

## Slice Order Overview

| Slice | Goal | Time est | Key capability added |
|-------|------|----------|----------------------|
| 1 | Skeleton | 2-3h | All services exist, end-to-end flow works |
| 2 | Real retrieval | 2-3h | VARC retrieves real questions, scoring works |
| **2.5** | **Retrofit fixes** | **3-5h** | **Skip button, mid-question doubts, close keyboards, stats, error fallbacks, idempotency** |
| 3 | Working memory + sessions | 3-4h | LLM explanations, session lifecycle, returning-after-break |
| 4 | Planner + guardrails | 3-4h | Intent-driven routing, small_talk distinction, out-of-scope |
| 5 | Profile reads | 2-3h | Personalized responses, default difficulty, empty-state fallbacks |
| 6 | Onboarding FSM | 3-4h | New-user flow with pause option + diagnostic + synthesis |
| 7 | Session-end + extraction | 3-4h | Episodic memory + profile note growth |
| 8 | Mentor + observer | 3-4h | Strategic responses + real-time pattern detection |

**Total estimated time: 24-33 hours** of focused implementation (slice 2.5 added ~5 hours).

If you're working with Claude Code and self-testing in parallel: realistic 3-5 calendar days.

**Six architectural principles bind every slice** (documented in detail in `02_service_contracts.md`):

1. Bot NEVER auto-serves a question after answer (diagnostic exception)
2. Old keyboards must be closed when new question served
3. Active session state cleared on session boundary
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
- [ ] Fallback tier logged in metadata
- [ ] Question presentation includes passage when applicable
- [ ] Inline keyboard with A/B/C/D works (taps registered, even if not yet processed)
- [ ] Response time < 5 seconds

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

### Migrations

- `011_add_skipped_to_attempts.sql`: ADD COLUMN skipped BOOLEAN DEFAULT FALSE
- `012_add_tg_update_id_to_messages.sql`: ADD COLUMN tg_update_id BIGINT + UNIQUE partial index

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

- **Sessions auto-management:** Session created on first message after 30-min gap. Updated on every turn (`last_activity_at`, `message_count`). Closed by `/admin/cleanup` cron after 30 min inactivity.
- **Memory service real:** `append_turn` (Postgres source of truth + Redis LIST cache, 24h TTL, 50 items max). `get_recent_turns` (Redis-first with Postgres fallback, repopulates cache). `get_active_session`, `set_active_session`, `update_active_session`, `clear_active_session`.
- **Session boundary state clearing (Principle 3, Bug 13):** When a new session starts, orchestrator DELs `state:tg:{tg_id}` BEFORE creating new state. domain_state from closed session never leaks.
- **Returning-after-break feature (Bug 2):** New `memory_service.detect_session_resume_candidate(student_id)`. When a closed session has unanswered work and student returns within 14 days, VARC composes a resume prompt: "Welcome back. Last time we were on a {subskill} question — want to pick that up, or start fresh?" Buttons: `[Resume that question]` `[Start fresh]` `[Just chat first]`.
- **VARC explanations LLM-generated:** Replaces slice 2's hardcoded explanation strings. MODEL_VARC_TUTOR (Haiku 4.5). System prompt includes Principle 1 enforcement ("never include a new question in your response").
- **Cost/latency tracking:** New `v5.llm_calls` table logs every LLM call (service, model, tokens, cost, latency, success). Migration 013.

### What's stubbed (still)

- Planner still hardcoded with simple regex routing (slice 4)
- Profile brief still hardcoded minimal (slice 5)
- Episodic summaries still stubbed; cleanup just closes sessions, doesn't generate summaries (slice 7)
- Mentor still stub (slice 8)
- Onboarding still skipped (slice 6)

### Manual test

1. Send "give me an inference question"
2. Expected: Question presented, session created in DB
3. Tap a button (any option)
4. Expected: Explanation of correct/wrong, attempt recorded, next question presented
5. Answer 3 questions
6. Send "what was that last question about?"
7. Expected: Bot uses recent turns context to reference the question (not perfectly without profile, but recognizes the topic)
8. Check Postgres:
   ```sql
   SELECT * FROM sessions WHERE student_id = ?; 
   -- 1 active session
   
   SELECT count(*) FROM attempts WHERE student_id = ? AND attempted_at > now() - interval '10 min';
   -- 3 attempts
   
   SELECT message_count, question_count, correct_count FROM sessions WHERE ended_at IS NULL;
   -- ~7-8 messages, 3 questions, however many correct
   ```
9. Check Redis:
   ```
   GET state:tg:{tg_id}
   -- Should show active session with current_question, questions_answered
   ```

### Completion criteria

- [ ] Sessions auto-create on 30-min gap
- [ ] Each turn updates session metadata
- [ ] Answers + skips processed correctly (preserved from slice 2.5)
- [ ] Recent turns visible in context (LLM responses reference them naturally)
- [ ] Active session persists across messages
- [ ] domain_state in Redis has current question, last_question_message_id
- [ ] **Session boundary clears Redis state (Bug 13):** old domain_state gone after new session starts
- [ ] **Returning-after-break works (Bug 2):** unanswered question + 30+ min gap + new message → resume prompt with [Resume] [Start fresh] [Just chat]
- [ ] **No regression:** all slice 2.5 fixes still work (skip, mid-question doubt, close keyboards, stats button, error fallback, idempotency)
- [ ] LLM call costs logged in `v5.llm_calls`
- [ ] /admin/cleanup cron runs, closes inactive sessions

### Estimated time: 3-4 hours

---

## Slice 4: Planner + Guardrails

### Goal

Real intent-driven routing. Single planner LLM call (Gemini Flash) replaces hardcoded routing. Out-of-scope queries soft-redirected. Critical: planner correctly distinguishes `small_talk` (no question served) from `practice_request` (question served) — Bug 15.

### What's real (added on top of slice 3)

- **Planner LLM call:** `services/orchestrator/planner.py` with `classify(message, recent_turns, active_session_summary)` returning IntentClassification. MODEL_PLANNER (Gemini Flash by default). Cost: ~$0.0001-0.0003 per call. Latency: ~1.5s added.
- **Robust JSON parsing with safe defaults:** On planner failure, default to `intent.action = "small_talk"` (NOT practice_request — safer). The bot will ask what student wants rather than auto-serving.
- **Granular subskill enum (Bug 22):** Planner returns one of 12 specific subskills (`inference_basic`, `inference_advanced`, `main_idea_full_passage`, `specific_detail`, etc.). VARC falls back to `inference_basic` and logs misclassification if planner returns out-of-enum.
- **Mixed-intent secondary signal (Bug 15):** Planner returns `intent.secondary_signal` for messages with both action and emotional undertone (e.g., "I'm stressed, give me an easy one" → action=practice_request, difficulty=easy, secondary_signal={type:"emotional_undertone", value:"mild_stress"}).
- **Small_talk vs practice_request distinction (Bug 15):** Planner prompt explicitly distinguishes brief acknowledgments (small_talk) from forward-intent (practice_request). When in doubt: small_talk. Bot responds with warm ack + continuation buttons, no question.
- **Conditional context fetching:** Orchestrator fetches profile/episodic/specific_messages per `context_needs` from planner.
- **Out-of-scope guardrails:** Quant/LRDI/general off-topic → templated soft-redirect (no agent invocation, no LLM call). Buttons: `[VARC question]` `[Strategy chat]`.
- **Intent-driven routing:** orchestrator routes to varc / mentor (stub) / out_of_scope handler based on intent.domain + action. Step 6.5 deterministic detection (slice 2.5) still runs first, overriding planner where appropriate (skip callbacks, continuation callbacks, mid-question doubts, answer regex).

- **Guardrails:** When `intent.domain == 'out_of_scope'`, orchestrator handles directly with soft-redirect, no agent invocation.
- **Response guidance:** Passed to agents in AgentContext. Agents are aware of `tone` and `should_acknowledge_feeling` (even if profile is still simple).

### What's stubbed (still)

- Profile brief: hardcoded minimal (no LLM-based personalization yet)
- Episodic summaries: empty
- Onboarding: not yet (assume all users post-onboarding)
- Mentor: still stub, but orchestrator can route to it for `domain='mentor'`

### Manual test

1. Send "give me an inference question" → expected: practice flow as before
2. Send "actually how is my progress overall?" → expected: planner classifies as `domain=mentor, action=review_progress`. Mentor stub returns hardcoded "Let me look at your progress". Real synthesis comes in slice 5+.
3. Send "what's a good recipe for pasta?" → expected: planner classifies as `domain=out_of_scope`. Bot replies with soft-redirect: "I'm focused on CAT VARC..." plus offers to continue practice.
4. Send "I'm so frustrated with this" → expected: planner classifies as `domain=mentor, action=vent, emotional_tone=frustrated`. Mentor stub returns simple acknowledgment. (Real warmth in slice 8.)
5. Check Postgres:
   ```sql
   SELECT metadata->'intent_classification'->>'domain' FROM messages 
   WHERE role='user' ORDER BY created_at DESC LIMIT 5;
   -- Should show mix: 'varc', 'mentor', 'out_of_scope', etc.
   
   SELECT count(*) FROM observer_events WHERE event_type = 'out_of_scope_query';
   -- Should be 1
   ```

### Completion criteria

- [ ] Planner LLM call working (Gemini Flash via MODEL_PLANNER)
- [ ] Intent classifications stored in messages.metadata
- [ ] **Small_talk vs practice_request correctly distinguished (Bug 15):** "thanks" / "ok" / "got it" → small_talk + continuation buttons (NO new question); "another" / "next" → practice_request + new question
- [ ] **Mixed-intent secondary signal works (Bug 15):** "I'm stressed, give me an easy one" → action=practice_request, secondary_signal captured, VARC tone adjusted
- [ ] **Granular subskill from planner (Bug 22):** "main idea question" → planner returns `main_idea_full_passage` exactly
- [ ] Out-of-scope queries get soft-redirect with `[VARC question]` `[Strategy chat]` buttons
- [ ] Mentor stub invoked for `domain=mentor`
- [ ] Out-of-scope queries don't invoke agents (cost savings)
- [ ] Response guidance passes through to agents
- [ ] **No regression:** all slice 1-3 + 2.5 behavior preserved
- [ ] Default to `small_talk` on planner failure (NOT practice_request) — verify by temporarily breaking planner key

### Estimated time: 3-4 hours

---

## Slice 5: Real Profile Service (Reads)

### Goal

Replace hardcoded profile brief with real profile data. Responses become personalized. Default difficulty derived from profile (Bug 23). Empty-state fallbacks for new students (Bug 25).

### What's real (added on top of slice 4)

- **Profile Service `get_tutor_brief`** — full implementation. Pulls from `v5.student_profile`, `v5.student_skill_profile`, `v5.student_notes`, recent episodic summaries (still empty until slice 7). Template-assembled string. NO LLM call.
- **Empty-state fallbacks (Bug 25):** Students with <5 questions practiced get coherent friendly text, not "N/A" placeholders. Students with no recent sessions get "first time back since onboarding {N} days ago" or similar. Never empty sections.
- **`get_minimal_brief`** — short version (~50-100 tokens) for low-context queries.
- **`get_default_difficulty(student_id)` (Bug 23):** Derives from preparation_stage. `just_starting`→easy, `mid_prep`→medium, `final_3_months`→medium, `revision`→hard. Used by VARC when planner doesn't supply difficulty (replaces slice 4's "fall back to medium" heuristic with profile-aware derivation).
- **`get_active_notes(student_id, filter)`:** SQL with `confidence × exp(-Δt / 30 days)` scoring (30-day half-life), top 20 ordered by score.
- **Profile brief Redis cache:** `profile:brief:{student_id}` with 30-min TTL. Read in `get_tutor_brief`. Invalidated (DEL) on any write to student_notes/student_profile/student_skill_profile (Principle 6 — Bug 14 hardening).
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
- **Post-onboarding session boundary (Bug 24):** When mentor synthesis completes, the onboarding session is closed (`ended_at = now`, `end_reason = 'onboarding_complete'`). DEL `state:tg:{tg_id}` (Principle 3). Next message starts a fresh session, normal practice flow. Diagnostic attempts (`is_diagnostic = true`) DO count toward stats but are flagged for analytics.
- **Migration 014_onboarding_columns.sql:** Adds `onboarding_paused_at`, `diagnostic_question_count` to `v5.student_profile` (if not already present from slice 1).


### What's stubbed (still)

- Notes extraction at session end: slice 7
- Mentor's reactive responses for "review_progress" etc.: slice 8
- Mentor observer mode: slice 8

### Manual test

1. **Reset your test user** to mimic new student:
   ```sql
   UPDATE student_profile SET onboarding_complete = false, onboarding_step = null,
       target_year = null, experience_level = null, /* ... reset all */ 
   WHERE student_id = 'your-uuid';
   
   DELETE FROM student_notes WHERE student_id = 'your-uuid';
   DELETE FROM messages WHERE student_id = 'your-uuid';
   DELETE FROM sessions WHERE student_id = 'your-uuid';
   DELETE FROM attempts WHERE student_id = 'your-uuid';
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
   SELECT * FROM student_profile WHERE student_id = 'your-uuid';
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

### What's real (added on top of slice 6)

- **Memory Service `process_session_end(session_id)`:** Full pipeline. Single LLM call (MODEL_EXTRACTOR = Gemini Flash) returns JSON with: summary, topics, notes_to_add (with category, content, confidence, expires_after_days), skill_signals.
- **Memory Service `cleanup_inactive_sessions`:** Cron, runs every 10 min. Closes sessions inactive > 30 min, triggers process_session_end for each.
- **Profile Service extraction integration:** Adds notes from extraction LLM via `add_note` (which DELs cache per Principle 6). Reinforces matches against existing notes (substring overlap > 50%) instead of duplicating.
- **Profile Service conflict resolution:** Basic rules — latest wins for category=context with expires_after_days; never override category=goal without explicit student statement.
- **Episodic summaries used:** Planner can request them via `context_needs.episodic.needed`. Profile brief includes "recent activity" section drawing from latest summary.
- **Note expiration:** category=context defaults to 14-day expiration. category=anxiety_pattern/skill_gap/goal: no expiration.

### What's stubbed (still)

- Mentor reactive mode: slice 8
- Mentor observer mode: slice 8 (raw signal source for slice 7's extractor)

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
