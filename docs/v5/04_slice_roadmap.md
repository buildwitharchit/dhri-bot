# Slice Roadmap — DHRI v5

## Principle

dhri v5 is built in **thin vertical slices**. Each slice ships a working end-to-end capability through every relevant service. Slices grow the system by adding intelligence, not by completing layers.

This is the opposite of horizontal layering ("build all of memory service, then all of profile service..."), which is what produced v4's broken-feeling result. With slices, the system always works at every checkpoint — just less smartly than the final version.

The 8 slices below are designed to be implemented in **a single Claude Code session**. Each slice has clear scope, manual tests, and completion criteria. The session can take 1-3 days depending on how cleanly it goes.

---

## Slice Order Overview

| Slice | Goal | Time est | Key capability added |
|-------|------|----------|----------------------|
| 1 | Skeleton | 2-3h | All services exist, end-to-end flow works |
| 2 | Real retrieval | 2-3h | VARC retrieves real questions |
| 3 | Working memory + sessions | 2-3h | Conversation continuity within sessions |
| 4 | Planner + guardrails | 3-4h | Intent-driven routing, out-of-scope handling |
| 5 | Profile reads | 2-3h | Personalized responses |
| 6 | Onboarding FSM | 3-4h | Full new-user flow |
| 7 | Session-end + extraction | 3-4h | Episodic memory + profile growth |
| 8 | Mentor + observer | 2-3h | Cross-domain reactive + pattern detection |

**Total estimated time: 19-28 hours** of focused implementation.

If you're working with Claude Code and self-testing in parallel: realistic 2-3 calendar days.

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

## Slice 3: Working Memory + Sessions

### Goal

Make conversation feel continuous. Recent turns flow into the LLM context. Sessions auto-create, auto-track. Answers to questions get processed.

### What's real (added)

- **Sessions table active:** Auto-create session on first message after >2 hour gap. Update `last_activity_at` on every turn. Update `message_count`, `question_count`, `correct_count`.
- **Active session in Redis:** `state:tg:{tg_id}` populated and updated.
- **Memory Service:** `get_active_session`, `set_active_session`, `update_active_session`, `clear_active_session`.
- **Recent turns in agent prompt:** AgentContext includes last 10 turns. Agent system prompt includes them.
- **VARC handles answers:** When user taps A/B/C/D, agent identifies it as `answer_to_question`, looks up correct answer, composes explanation, records attempt.
- **Domain state:** active session tracks current question, questions_in_set, questions_answered.

### What's stubbed (still)

- Planner still hardcoded — but now hardcoded to:
  - If text matches A/B/C/D pattern → action='answer_to_question'
  - Otherwise → action='practice_request'
- Profile brief still hardcoded minimal
- Episodic still empty
- Session-end cleanup not yet implemented (sessions stay open)

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

- [ ] Sessions auto-create
- [ ] Each turn updates session metadata
- [ ] Answers processed correctly (correctness, attempt recorded)
- [ ] Recent turns visible in context (LLM responses reference them)
- [ ] Active session persists across messages
- [ ] domain_state in Redis has current question

### Estimated time: 2-3 hours

---

## Slice 4: Planner + Guardrails

### Goal

Real intent-driven routing. The orchestrator's planner LLM call replaces hardcoded routing. Out-of-scope queries get soft-redirected.

### What's real (added)

- **Planner LLM call:** Calls Gemini Flash with prompt + recent turns + active session + current message. Returns IntentClassification.
- **Conditional context fetching:** Based on `context_needs` from planner, fetch only what's needed (still mostly stubs at this point — profile brief is hardcoded, episodic is empty).
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

- [ ] Planner LLM call working (Gemini Flash)
- [ ] Intent classifications stored in messages.metadata
- [ ] Out-of-scope queries get soft-redirect
- [ ] Mentor stub invoked for `domain=mentor`
- [ ] Out-of-scope queries don't invoke agents (cost savings)
- [ ] Response guidance passes through to agents

### Estimated time: 3-4 hours

---

## Slice 5: Real Profile Service (Reads)

### Goal

Replace hardcoded profile brief with real profile data. Responses become personalized.

### What's real (added)

- **Profile Service: `get_tutor_brief`** — full implementation. Pulls from student_profile, student_skill_profile, student_notes, recent episodic summaries (still empty for now). Template-assembled string.
- **Profile Service: `get_minimal_brief`** — full implementation.
- **Profile Service: `get_active_notes`** — SQL query with confidence × recency scoring.
- **Profile brief cache:** Redis cache for tutor brief, 30-min TTL.
- **Manual seed notes for testing:** Insert ~5 fake notes for yourself manually to test injection.

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
- [ ] Notes show up in responses meaningfully
- [ ] Profile brief cache working (faster on cache hit)
- [ ] Minimal vs full brief used appropriately based on planner
- [ ] Performance stats from student_skill_profile included
- [ ] Total response time still < 5 seconds (cache helps)

### Estimated time: 2-3 hours

---

## Slice 6: Onboarding FSM

### Goal

New users go through full onboarding: profile fields + diagnostic test + mentor synthesis.

### What's real (added)

- **Orchestrator: `handle_onboarding_step`** — full FSM implementation.
- **Inline keyboards** for each step (not just A/B/C/D).
- **VARC: `serve_diagnostic_question`** — selects question based on q_index (1=easy inference, 2=easy main_idea, 3=medium inference, 4=medium specific_detail, 5=hard inference).
- **VARC: `handle_diagnostic_answer`** — brief explanation + advance.
- **Mentor: `synthesize_diagnostic`** — full implementation. LLM call (Sonnet) for the welcome.
- **Mentor: `handle_skip_diagnostic`** — for users who skip the test.

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
- [ ] Diagnostic test serves 5 specific questions
- [ ] Mentor synthesis uses Sonnet, references test results
- [ ] Skip path works (mentor synthesis without test data)
- [ ] After onboarding, normal flow works

### Estimated time: 3-4 hours

---

## Slice 7: Session-End Pipeline + Profile Extraction

### Goal

When sessions end, generate episodic summaries and extract profile notes. Profile grows organically.

### What's real (added)

- **Memory Service: `process_session_end`** — full pipeline. Combined LLM call for summary + extraction.
- **Memory Service: `cleanup_inactive_sessions`** — cron job, runs every 10 min.
- **Profile Service: extraction integration** — adds notes returned from extraction LLM call.
- **Profile Service: conflict resolution** — basic rules (latest wins for life_event, sensitivity flag handling).
- **Episodic summaries used:** Now that they exist, planner can request them, profile brief can include "recent activity" section.

### What's stubbed (still)

- Mentor reactive mode: slice 8
- Mentor observer mode: slice 8

### Manual test

1. Have an active session (do 4-5 questions, with 1-2 wrong on inference)
2. Don't send messages for 45+ minutes
3. Wait or manually trigger cleanup: `curl -X POST -H "X-Admin-Secret: ..." https://your-railway-url/admin/cleanup-sessions`
4. Expected: cron processes the session
5. Check Postgres:
   ```sql
   SELECT * FROM sessions WHERE student_id = 'your-uuid' ORDER BY ended_at DESC LIMIT 1;
   -- ended_at is set, end_reason='inactivity_timeout'
   
   SELECT * FROM episodic_summaries WHERE student_id = 'your-uuid' ORDER BY created_at DESC LIMIT 1;
   -- 1 row, with summary_text, themes, key_moments, performance_data
   
   SELECT * FROM student_notes WHERE student_id = 'your-uuid' ORDER BY created_at DESC LIMIT 5;
   -- 1-3 new notes from this session
   ```
6. Send a new message (starts new session)
7. Expected: Bot's response includes reference to "yesterday" or "last session" if profile loaded
8. Check planner output:
   ```sql
   SELECT metadata->'intent_classification'->>'context_needs' FROM messages 
   WHERE role='user' AND created_at > now() - interval '5 min';
   -- May show episodic.needed=true
   ```

### Completion criteria

- [ ] Inactive sessions auto-close
- [ ] Episodic summaries generated correctly
- [ ] Profile notes extracted from sessions
- [ ] Existing notes get reinforced when relevant
- [ ] New notes don't duplicate existing ones
- [ ] Sensitive notes flagged appropriately
- [ ] LLM call cost ~$0.005 per session

### Estimated time: 3-4 hours

---

## Slice 8: Mentor Reactive + Observer

### Goal

Mentor agent fully functional. Strategic queries get real responses. Observer detects patterns inline.

### What's real (added)

- **Mentor Agent: `handle`** — full implementation. Single LLM call (Sonnet for nuanced) using full profile + episodic context. Handles:
  - `action=review_progress` ("how am I doing?")
  - `action=vent` (emotional support)
  - `action=meta` (questions about dhri itself)
  - `action=casual` (warm acknowledgments)
- **Mentor Agent: `inline_observe`** — runs after every successful turn (async). Processes observer events. Detects:
  - Consecutive wrongs (3+) → emotional_signal note
  - Same trap multiple times → reinforce pattern note
  - Metacognitive questions → high-value note
  - Long pauses → emotional_signal possible
  - Self-corrections → growth note
- **Async post-processing:** Orchestrator triggers `mentor.inline_observe` via async task after sending response.

### What's stubbed (still)

- Initiator mode (proactive messages): deferred to v2
- Scheduler service: deferred to v2

### Manual test

1. Send "how am I doing in VARC?"
2. Expected: Mentor responds with specific data (accuracy, weakest area, pattern, recent progress) — not generic
3. Send "I'm so frustrated, I keep messing up inference"
4. Expected: Mentor responds with empathy + specific reference to your inference pattern + suggestion
5. Get 3 wrong answers in a row on practice
6. Check observer events:
   ```sql
   SELECT * FROM observer_events WHERE event_type IN ('wrong_answer', 'consecutive_wrong')
   ORDER BY created_at DESC LIMIT 5;
   -- Should see consecutive_wrong event after 3rd wrong
   ```
7. Check student_notes for new emotional/pattern notes:
   ```sql
   SELECT * FROM student_notes WHERE student_id = 'your-uuid' AND created_at > now() - interval '10 min';
   -- May have new note about frustration or trap pattern
   ```
8. Send "what is dhri?"
9. Expected: Mentor handles meta question, explains itself

### Completion criteria

- [ ] Mentor handles strategic queries with real data
- [ ] Mentor handles emotional venting with specificity
- [ ] Inline observer processes events without blocking response
- [ ] Pattern detection triggers note creation/reinforcement
- [ ] Mentor uses appropriate model (Sonnet for nuance)
- [ ] Async post-processing doesn't add user-visible latency

### Estimated time: 2-3 hours

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
