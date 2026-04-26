# Service Contracts — DHRI v5

## Overview

dhri v5 has six services in production (the scheduler is deferred to v2). This document defines the API surface of each service: what each function accepts, what it returns, what side effects it has, and importantly, what each service does NOT do.

The fundamental principle: **services own data; they don't share it.** When service A needs data owned by service B, it calls B's API. No service reaches directly into another service's tables.

The exception is the orchestrator, which orchestrates flow and is allowed to read across multiple services to assemble context. But even the orchestrator doesn't write to other services' tables — it calls their APIs.

---

## Service Boundary Summary

| Service | Owns | Reads from |
|---------|------|------------|
| Message Bus | Nothing (stateless) | Nothing |
| Orchestrator | sessions, observer_events, messages | All services (via API) |
| Memory Service | messages cache (Redis), episodic_summaries | sessions, messages |
| Profile Service | student_profile, student_notes | student_skill_profile (read-only) |
| VARC Agent | Nothing | questions, passages (RAG) |
| Mentor Agent | Nothing | All services (via API in observer mode) |

**Note:** "Owns" means the service is the only one that writes to those tables/keys. Other services may read via the owning service's API.

---

## Architectural Principles (cross-service invariants)

These principles bind every service. They are not optional. When implementing or modifying any service, check that the change preserves all of these.

### Principle 1: The bot NEVER auto-serves a question after an answer

After the student answers (or skips) a question, the response includes scoring + explanation + a continuation prompt with buttons. The next question is served ONLY when the student explicitly opts in: tapping `[Next question]`, typing a practice request, or in the bounded diagnostic-mode exception.

**Exception (the only one):** During the 5-question onboarding diagnostic test, auto-continuation is allowed because the student opted into a known sequence. After Q5 + mentor synthesis, the no-auto-serve rule resumes.

**Why:** Auto-looping makes the bot feel transactional and removes student autonomy. The student must be able to pause, ask a doubt, switch topic, or stop without fighting the bot.

**Enforcement:**
- VARC agent's `handle` function ends with continuation buttons after every answer / skip / explanation
- Mentor agent's `handle` function ends with contextual continuation buttons
- Orchestrator does NOT chain "answer → next question" automatically; it requires an explicit follow-up message from the student
- Planner classifies brief acknowledgments ("ok", "thanks", "got it") as `small_talk`, which does NOT trigger question retrieval — orchestrator responds with a warm acknowledgment + continuation buttons

### Principle 2: Old keyboards must be closed when a new question is served

Telegram inline keyboards persist forever in chat history. If the student scrolls up and taps a button on a 3-day-old question, that creates ambiguity: should the bot score it against today's question, or against the old one?

**Rule:** When VARC agent serves a new question, orchestrator removes the inline keyboard from the previous question's message (via `editMessageReplyMarkup` with empty markup). The previous question becomes visually closed.

**Enforcement:**
- Active session Redis state stores `last_question_message_id`
- Orchestrator's response-delivery step removes the previous keyboard before sending new question keyboard
- AgentResponse includes `requires_keyboard_close: boolean` and `previous_question_message_id` to coordinate this
- If keyboard close fails (e.g., message too old to edit), log and continue — not a hard failure

### Principle 3: Active session state is per-session and cleared on session boundary

When a new session starts (after 30+ minute gap, explicit end, or session switch), the orchestrator MUST delete the active session Redis key (`state:tg:{tg_id}`) before recreating it for the new session. This prevents `domain_state` from the closed session leaking into the new session.

**Enforcement:**
- Orchestrator's session-creation flow: detect session boundary → DEL `state:tg:{tg_id}` → INSERT new session row → SET new `state:tg:{tg_id}`
- Memory service's `clear_active_session(tg_id)` is called at session boundaries
- Working memory cache (`memory:tg:{tg_id}`) is NOT cleared at session boundary — it's a sliding window of recent turns, useful across sessions

### Principle 4: Webhook idempotency via Telegram update_id

Telegram retries webhooks if it doesn't get a 200 OK fast enough. Without idempotency, the same message gets processed multiple times.

**Rule:** Orchestrator's first step is to check if the incoming update's `update_id` already exists in `messages.tg_update_id` for this student. If yes, the request is a Telegram retry — short-circuit by returning the cached prior response (or just 200 OK with no action).

**Enforcement:**
- `messages.tg_update_id` has a UNIQUE partial index
- Orchestrator's handle_message flow checks this before any other work
- If duplicate detected, log and return early without processing

### Principle 5: User experience never breaks on infrastructure failure

When LLM APIs, databases, or external services fail, the user must see a graceful response — never a crash, never a silent failure.

**Failure-mode matrix:**
- Planner LLM fails → use safe default classification (`small_talk` + minimal context), continue
- Generation LLM fails → retry once; if both fail, send canned response: "Hmm, having trouble thinking right now. Try again in a moment?" + button `[Try again]`
- Database write fails AFTER response delivered → log loud (Sentry), proceed; user sees the response, state is slightly inconsistent but functional
- Database write fails BEFORE response delivered → fail loud, send canned error to user
- Redis unavailable → fall back to Postgres for reads; for writes (rate limits, lock), allow request through and log alert

**Principle:** Better to send a response and have a state hiccup than to crash the user-facing flow.

### Principle 6: Profile cache invalidation is mandatory on writes

The tutor brief Redis cache (`profile:brief:{student_id}`) gets stale fast. Notes get added (slice 8 observer), profile gets updated (onboarding, settings).

**Rule:** ANY service that writes to `student_notes`, `student_profile`, or `student_skill_profile` MUST invalidate `profile:brief:{student_id}` in Redis immediately, even if the write is async.

**Enforcement:**
- Profile service's `add_note`, `reinforce_note`, `supersede_note`, `update_profile` all DEL the cache key as their last step
- Memory service's session-end pipeline (which calls profile service to add notes) inherits this guarantee
- Mentor observer (which calls profile service) inherits this guarantee
- VARC and Mentor agents do NOT write profile data directly; they always call profile service

---

## Service 1: Message Bus

The thinnest service. Pure platform translation. No business logic, no state, no DB writes.

### `receive_telegram_update(raw_update)`

**Trigger:** Telegram POSTs to `/webhook/{secret}`

**Input:** Raw Telegram Update object (JSON)

**Output:** None (asynchronous; returns 200 OK to Telegram immediately, then forwards to orchestrator)

**Side effects:**
1. Parse the Telegram update
2. Extract `tg_id` from `update.message.from.id` (or `update.callback_query.from.id`)
3. Determine content_type:
   - `update.message.text` → "text"
   - `update.callback_query.data` → "button"
   - `update.message.voice` → "voice" (rejected gracefully in v1)
   - `update.message.photo` → "image" (rejected gracefully in v1)
4. Build normalized payload:
   ```
   {
     tg_id: int,
     content: string,
     content_type: string,
     timestamp: datetime,
     source_metadata: { tg_message_id, tg_chat_id, raw_update }
   }
   ```
5. Send "🤔 Thinking..." reply via Telegram, capture the returned message_id
6. Send `chatAction("typing")` to Telegram
7. Hand normalized payload + thinking_message_id to `orchestrator.handle_message`
8. Receive response from orchestrator
9. Edit thinking message with final response (replaces "🤔 Thinking...")

**Error handling:**
- Invalid update → log and return 200 OK (don't retry; spammy)
- Webhook secret mismatch → return 403
- Voice/image content → reply gracefully: "I can only handle text right now. What's on your mind about CAT VARC?"

**Notes:**
- Stateless: no Redis writes, no DB writes, nothing cached
- The "thinking message" pattern is bus's responsibility (it knows about Telegram's edit_message API)
- Refresh `chatAction("typing")` every 4 seconds while waiting for orchestrator response (typing animation lasts 5 seconds)

### `send_to_telegram(tg_id, response, thinking_message_id)`

**Trigger:** Called by orchestrator at end of request handling, or by mentor/scheduler for proactive messages (v2+)

**Input:**
- tg_id: int
- response: AgentResponse content + keyboard
- thinking_message_id: int (the "thinking" message to edit)

**Output:** Success boolean

**Side effects:**
1. Format response for Telegram:
   - Convert markdown to Telegram MarkdownV2
   - Build inline keyboard if response.keyboard provided
   - Chunk if >4096 chars (rare)
2. Call `bot.editMessageText(thinking_message_id, response.content, parse_mode='MarkdownV2', reply_markup=keyboard)`
3. If edit fails (message too old, etc.), fall back to `bot.sendMessage`

**Error handling:**
- Telegram API errors → retry once with exponential backoff
- Persistent failure → log and return false (orchestrator already committed memory; user will see no response, but state is consistent)

### What the Message Bus does NOT do

- Database writes (no inserts, no updates)
- Redis writes
- Business logic (no decisions about what to do with messages)
- Authentication (just verifies webhook secret)
- Identity resolution (orchestrator handles tg_id → student_id mapping)
- Rate limiting (orchestrator's job)
- Any LLM calls

---

## Service 2: Orchestrator

The conductor. Every message flows through here. The orchestrator is the only service that knows about conversation flow, intent, and routing.

### `handle_message(normalized_payload, thinking_message_id)`

**Trigger:** Called by message bus after normalization

**Input:**
- normalized_payload: { tg_id, content, content_type, timestamp, source_metadata }
- thinking_message_id: int

**Output:** AgentResponse

**Internal flow (high-level):**

#### Step 0: Webhook idempotency check (Bug 20 mitigation)

- Extract `tg_update_id` from `normalized_payload.source_metadata.raw_update.update_id`
- Query: `SELECT message_id FROM messages WHERE tg_update_id = $1 LIMIT 1`
- If a row exists, this is a Telegram webhook retry. Three possible actions:
  - If we have a paired assistant message (same student, immediately after this user message), return that response again — Telegram will treat it as a no-op since the assistant message was already delivered
  - If no paired assistant message exists yet (the original processing is still in flight), short-circuit with a 200 OK and no further work; the in-flight original will deliver
  - In all cases, do NOT proceed with normal processing
- Log the duplicate detection at INFO level for monitoring

#### Step 1: Identity resolution and lock

- Look up student in `students` table by tg_id
- If not exists, create new row with display_name from Telegram (orchestrator's job, not bus's)
- Update `last_seen_at`
- Acquire lock: `SET lock:user:{tg_id} 1 NX EX 5`
- If lock not acquired, return canned response: "Still working on your last message — hang on a sec"

#### Step 2: Persist user message

- Insert into `messages` table:
  ```
  role: 'user'
  content: normalized_payload.content
  content_type: normalized_payload.content_type
  tg_update_id: <from step 0>
  metadata: { tg_message_id, raw_telegram_payload }
  session_id: TBD (set after session lookup in step 4)
  ```
- Capture `message_id` for downstream use
- The UNIQUE index on `tg_update_id` provides a second line of defense against race conditions in step 0

#### Step 3: Rate limit and spend cap checks

- INCR daily message counter; check against `MAX_MESSAGES_PER_USER_PER_DAY`
  - Soft warn at 80%: prepend "Heads up — you've used 80% of today's quota." to the response
  - Hard block at 100%: return canned response, persist as assistant, release lock, exit
- INCR per-minute counter; check against `MAX_MESSAGES_PER_USER_PER_MINUTE` (default 5/min)
- Read current daily spend; check against `DAILY_LLM_SPEND_CAP_USD`

If any hard limit hit:
- Return canned response (e.g., "You've hit today's limit — come back tomorrow")
- Persist the canned response as assistant message
- Release lock and exit

#### Step 4: Onboarding check

- Read `student_profile` for this student
- If `onboarding_complete = false`:
  - Route to onboarding handler (see `handle_onboarding_step` below)
  - Return early
- If `onboarding_complete = true`, continue to step 5

#### Step 5: Load context, manage session boundary

- Read recent turns from Redis cache (`memory:tg:{tg_id}`)
- If empty, fall back: `SELECT * FROM messages WHERE student_id = ? ORDER BY created_at DESC LIMIT 10`
- Read active session from Redis (`state:tg:{tg_id}`)

**Session boundary detection (Principle 3 enforcement):**

- If active session exists AND its `last_activity_at` is < 30 minutes ago:
  - This is a session continuation
  - Update `last_activity_at` and `message_count_in_session`
  - Update `messages.session_id` for the user message just inserted
  
- If active session exists BUT `last_activity_at` is > 30 minutes ago:
  - This is a session boundary
  - Mark the old session as ended in Postgres: `UPDATE sessions SET ended_at = now(), end_reason = 'inactivity_timeout' WHERE session_id = ?`
  - Trigger async session-end pipeline for the old session (memory_service.process_session_end)
  - **DEL `state:tg:{tg_id}`** in Redis (clears domain_state, last_question_message_id, etc.)
  - Create new session row in Postgres
  - Set new `state:tg:{tg_id}` in Redis
  - Update `messages.session_id` for user message to NEW session
  - **Returning-after-break detection (Bug 2):** Check if the previous session has any unanswered attempts. If yes, set `context.session_resume_candidate = { last_question_id, last_session_summary }` for downstream use in step 9.
  
- If no active session exists at all (Redis evicted or first ever message):
  - Check Postgres `sessions` table for the most recent session for this student
  - If found and `ended_at IS NULL` and `last_activity_at` < 30 min ago: rehydrate Redis state, treat as continuation
  - Else: create new session, no Redis state to clear
  - Same returning-after-break check applies

#### Step 6: Planner LLM call

- Call `planner.classify(message, recent_turns, active_session_summary)`
- Returns IntentClassification object
- **On planner LLM failure** (timeout, malformed JSON, network error):
  - Log error
  - Use safe default classification:
    ```
    intent.domain = "varc"
    intent.action = "small_talk"  // safest — bot will ask what student wants
    context_needs.profile = "minimal"
    context_needs.episodic.needed = false
    response_guidance.tone = "warm"
    ```
  - Continue to step 7 with this default

#### Step 6.5: Detect quick patterns BEFORE invoking agents (slice 2.5 additions)

After planner returns (or default), the orchestrator runs deterministic detection for special cases:

- **Skip detection:** If `normalized_payload.content_type == "button"` AND callback_data matches `v5_skip_<attempt_id>`:
  - Override intent.action = "skip_request"
  - Set context.skipped_attempt_id = attempt_id
- **Continuation button detection:** If callback_data matches `v5_continue_<action>`:
  - Override intent.action accordingly: `next` → practice_request, `done` → explicit_end, `doubt` → doubt_about_current, `switch_subskill` → switch_topic, `stats` → stats_request
- **Mid-question doubt detection (Bug 1):** If active session has `last_question_attempt_id` (an unanswered attempt) AND content is text (not a button) AND content does NOT match an answer regex (A/B/C/D, 1-4):
  - Override intent.action = "doubt_about_current"
  - Set context.mid_question_doubt = true
  - Set context.current_unanswered_attempt = <the attempt row>
- **Answer detection:** If active session has `last_question_attempt_id` AND content matches an answer regex OR callback_data matches `v5_answer_<qid>_<letter>`:
  - Override intent.action = "answer_to_question"

These deterministic overrides supersede planner classifications because deterministic signals (button taps, regex matches against active state) are more reliable than LLM inference.

#### Step 7: Guardrails check

- If `intent.domain == "out_of_scope"`:
  - Compose soft-redirect response with continuation buttons appropriate to context:
    - For quant/LRDI: "I focus on VARC for now — quant and LRDI are coming later." + buttons `[VARC question]` `[Strategy chat]`
    - For general off-topic: "Let's get back to VARC." + same buttons
  - Save observer event: `out_of_scope_query`
  - Skip context fetching, skip agent invocation
  - Persist as assistant message (with `response_type: "off_topic_redirect"`)
  - Release lock and return

#### Step 8: Conditional context fetching

Based on `intent_classification.context_needs`:

- If `context_needs.profile == "full"`:
  - Call `profile_service.get_tutor_brief(student_id)` → string
- If `context_needs.profile == "minimal"`:
  - Call `profile_service.get_minimal_brief(student_id)` → string
- If `context_needs.profile == "skip"`:
  - Set profile_brief to null

- If `context_needs.episodic.needed`:
  - Call `memory_service.get_episodic_summaries(student_id, filter)` → list
- Else: empty list

- If `context_needs.specific_messages.needed`:
  - Call `memory_service.embedding_search_messages(student_id, query, limit)` → list
- Else: empty list

These calls run in parallel where possible.

#### Step 9: Assemble AgentContext

Compose the AgentContext object from:
- student data (student_id, tg_id, display_name)
- recent_turns (from step 5)
- active_session (from step 5)
- profile_brief (from step 8)
- episodic_summaries (from step 8)
- specific_past_messages (from step 8)
- intent (from step 6, possibly overridden in step 6.5)
- response_guidance (from step 6)
- current_message (the just-inserted user message)
- **mid_question_doubt** (from step 6.5; true if student typed text mid-question)
- **current_unanswered_attempt** (from step 6.5; the attempt row, if applicable)
- **is_diagnostic_mode** (true if onboarding_step is in diagnostic_q1..q5)
- **session_resume_candidate** (from step 5; populated if returning after break with unfinished work)
- **skipped_attempt_id** (from step 6.5; if student tapped Skip)
- **default_difficulty** (call `profile_service.get_default_difficulty(student_id)` — Bug 23)
- **session_stats** (only if intent.action == "stats_request" — call profile_service for current session counts)

#### Step 10: Agent invocation

Based on `intent.domain` and `intent.action`:

- `intent.domain == "varc"`:
  - `intent.action == "answer_to_question"` or `"skip_request"` → `varc_agent.handle(context)` for scoring/skip flow
  - `intent.action == "doubt_about_current"` (mid-question) → `varc_agent.handle(context)` with mid-question response shape
  - `intent.action == "practice_request"` → `varc_agent.handle(context)` for question retrieval
  - `intent.action == "small_talk"` → orchestrator composes warm acknowledgment + continuation buttons; NO agent invocation, NO question retrieval
  - `intent.action == "stats_request"` → orchestrator composes session stats response + continuation buttons; NO agent invocation
  - `intent.action == "concept_question"` → `varc_agent.handle(context)` for teaching response
- `intent.domain == "mentor"` → `mentor_agent.handle(context)`
- `intent.domain == "meta"` → `mentor_agent.handle(context)` (mentor handles meta queries)
- `intent.domain == "out_of_scope"` → already handled in step 7
- `intent.domain == "onboarding"` → already handled in step 4

Returns AgentResponse.

**On agent failure** (timeout, exception, malformed response):
- Retry once with same context
- If still failing, compose canned error response:
  - content: "Hmm, having trouble thinking right now. Try again in a moment?"
  - keyboard: `[[Try again]]` (callback_data: `v5_retry`)
  - response_type: "error_fallback"
- DO NOT commit memory deltas (state remains consistent — student can retry cleanly)
- Persist as assistant message
- Continue to remaining steps

#### Step 11: Validate and post-process response

- Check response.content is non-empty
- **Length handling (Bug 9):** If response.content > 3500 chars (approaching Telegram's 4096 limit):
  - Split: send passage as one message first (no keyboard), then question + options + keyboard as second message
  - For non-question responses, truncate gracefully with "[continued]" marker
- If invalid (empty, malformed), use canned fallback
- Append any orchestrator-level additions (rare; mostly empty)

#### Step 11.5: Close previous keyboard if needed (Bug 11 / Principle 2)

- If `response.requires_keyboard_close == true` (set when serving a new question):
  - Read `last_question_message_id` from active session Redis state
  - If non-null, call Telegram API: `editMessageReplyMarkup(chat_id=tg_id, message_id=last_question_message_id, reply_markup=null)` to remove the inline keyboard from the previous question
  - On success or failure, log; do NOT block the response delivery on this
  - Update `state:tg:{tg_id}` to clear `last_question_message_id` (will be set to new question's message_id after delivery in step 13)

#### Step 12: Persist assistant message

- Insert into `messages` table:
  ```
  role: 'assistant'
  content: response.content
  metadata: response.meta + intent_classification + response_type + tg_message_id (after delivery)
  session_id: from active session
  ```

**On DB write failure (Bug 19):**
- Log loud (Sentry alert with full context)
- The user has NOT yet seen the response (we haven't delivered to bus yet)
- Send canned error to user: "Hmm, something went wrong saving that. Try once more?"
- Release lock and exit

#### Step 13: Apply memory deltas

From `response.memory_deltas`:
- Update Redis active session (LPUSH new turn, update domain_state)
- **If response served a new question:** update `state:tg:{tg_id}.last_question_message_id` and `last_question_attempt_id` after Telegram delivers
- Update `sessions` table (message_count++, question_count if applicable)
- Insert observer events from `response.observer_events`
- Insert/update student_question_attempts row if applicable:
  - On answer: UPDATE existing row (set answered_at, is_correct, student_answer, explanation_shown)
  - On skip: UPDATE existing row (set answered_at, skipped=true, explanation_shown)
  - On new question serve: INSERT new row (student_id, question_id, served_at, fallback_tier)

If `response.memory_deltas.close_session` is set:
- Mark session as ended
- Trigger session-end pipeline (async)

**On memory delta write failure:**
- Log loud
- The user has already seen the response — DO NOT crash
- State will be slightly inconsistent (next session might miss this turn's context); acceptable per Principle 5

#### Step 14: Increment counters

- Increment spend counter by `response.meta.cost_usd + planner_cost`
- Note: rate limit counters already incremented in step 3

#### Step 15: Return response to bus

Return AgentResponse to bus, which edits the thinking message.

#### Step 16: Async post-processing

After response sent (Python: use `asyncio.create_task`):
- Run mentor inline observer (slice 8) — non-blocking
- Queue embedding job for the new messages (if they meet "important" criteria)

#### Step 17: Release lock

`DEL lock:user:{tg_id}` — must run in a finally block to handle exceptions

**Error handling summary (per Principle 5):**
- Any uncaught exception → release lock, return graceful error response, log full traceback
- Planner LLM failure → safe default classification, continue (handled in step 6)
- Agent failure → retry once, then canned fallback (handled in step 10)
- Generation LLM timeout → handled inside agent; surfaces as agent failure
- DB write failure on user message persistence (step 2) → fail loud, return error to user
- DB write failure on assistant message persistence (step 12) → fail loud before delivery
- DB write failure on memory deltas (step 13) → log loud, continue (response already delivered)
- Redis unavailable → fall back to Postgres for reads; log alert; allow writes through where possible
- Lock acquisition failure → return canned "still working" message
- Telegram API failure on send → retry once, then log; user may see no response but state stays consistent

### `handle_onboarding_step(student_id, normalized_payload)`

**Trigger:** Called from main flow when `onboarding_complete == false`

**Input:**
- student_id: UUID
- normalized_payload: { content, content_type, ... }

**Output:** AgentResponse

**Internal flow:**

The onboarding FSM. State stored in `student_profile.onboarding_step`.

```
Pseudo-code:
current_step = student_profile.onboarding_step

if current_step is null:
  # First message ever — start onboarding
  set onboarding_step = 'start_onboarding'
  set onboarding_started_at = now()
  return welcome message + button "Let's start"

elif current_step == 'start_onboarding':
  # User clicked "Let's start"
  set onboarding_step = 'ask_name'
  return "What should I call you? (using {telegram_first_name} by default)" + buttons

elif current_step == 'ask_name':
  # User entered name or accepted default
  save display_name to students table
  set onboarding_step = 'ask_target_year'
  return question + inline keyboard with [2026, 2027, 2028]

elif current_step == 'ask_target_year':
  save target_year to student_profile
  set onboarding_step = 'ask_experience_level'
  return question + keyboard

elif current_step == 'ask_experience_level':
  save experience_level
  set onboarding_step = 'ask_preparation_stage'
  return question + keyboard

elif current_step == 'ask_preparation_stage':
  save preparation_stage
  set onboarding_step = 'ask_hours_per_day'
  return question + keyboard

elif current_step == 'ask_hours_per_day':
  save hours_per_day
  set onboarding_step = 'ask_target_colleges'
  return question + multi-select keyboard + "Skip" button

elif current_step == 'ask_target_colleges':
  save target_colleges (or null if skipped)
  set onboarding_step = 'ask_why_cat'
  return question + "Skip" button

elif current_step == 'ask_why_cat':
  save why_cat (or null if skipped) as a note in student_notes (category=goal)
  set onboarding_step = 'diagnostic_intro'
  return diagnostic intro message + buttons ["Take 5-question test", "Skip the test"]

elif current_step == 'diagnostic_intro':
  if user clicked "Skip the test":
    set onboarding_step = 'mentor_synthesis'
    delegate to mentor agent for synthesis (no test data)
    handle response from mentor (sets onboarding_complete = true)
  else:  # Take the test
    set onboarding_step = 'diagnostic_q1'
    delegate to varc_agent.serve_diagnostic_question(student_id, q_index=1)
    return varc response

elif current_step in ['diagnostic_q1', 'diagnostic_q2', 'diagnostic_q3', 'diagnostic_q4']:
  # User answered a diagnostic question
  delegate to varc_agent.handle_diagnostic_answer(student_id, current_step, normalized_payload)
  varc agent returns: explanation + records attempt + advances FSM
  set onboarding_step = next diagnostic step
  if next step is 'mentor_synthesis':
    delegate to mentor synthesis (after returning q5 explanation)
  return varc response

elif current_step == 'diagnostic_q5':
  # Last diagnostic question
  delegate to varc_agent.handle_diagnostic_answer
  varc returns explanation
  set onboarding_step = 'mentor_synthesis'
  
  # Trigger mentor synthesis as follow-up message (separate response)
  schedule mentor_agent.synthesize_diagnostic to run after returning current response
  return varc response with note: "Let me look at your overall results..."

elif current_step == 'mentor_synthesis':
  # Mentor processes test results and welcomes properly
  call mentor_agent.synthesize_diagnostic(student_id)
  set onboarding_complete = true
  set onboarding_completed_at = now()
  set onboarding_step = null
  return mentor response (welcome, summary of weak areas, suggested next steps)
```

**Side effects:**
- Updates `student_profile` row at each step
- May insert into `student_notes` (e.g., for why_cat)
- Calls `varc_agent.serve_diagnostic_question` and `varc_agent.handle_diagnostic_answer`
- Calls `mentor_agent.synthesize_diagnostic`

**Error handling:**
- Invalid response (e.g., text when button expected) → re-prompt with same step + buttons
- DB write failure → return error, FSM state unchanged

### What the Orchestrator does NOT do

- Generate response content (delegates to agents)
- Domain knowledge (no VARC rules, no question selection logic)
- Direct DB writes to profile, notes, episodic_summaries (delegates to services)
- Direct LLM calls for content (only the planner classification call)
- Direct retrieval from question bank (delegates to VARC agent)

---

## Service 3: Memory Service

Owns: working memory cache (Redis), episodic_summaries.
Reads: messages, sessions.

### `get_recent_turns(student_id, tg_id, limit=20)`

**Input:**
- student_id: UUID
- tg_id: int (used for Redis key)
- limit: int

**Output:** List of turn objects (most recent first)

**Internal flow:**
1. LRANGE `memory:tg:{tg_id}` 0 {limit-1}
2. If list is shorter than limit OR doesn't exist, fall back to Postgres:
   `SELECT * FROM messages WHERE student_id = ? ORDER BY created_at DESC LIMIT ?`
3. If Postgres returned data and Redis was empty, repopulate Redis cache (LPUSH each, then LTRIM 0 49)
4. Return turns

### `append_turn(student_id, tg_id, turn_data, message_id)`

**Input:** student_id, tg_id, turn_data (role, content, etc.), message_id

**Output:** Success boolean

**Internal flow:**
1. Build cache item: `{role, content, content_type, timestamp, message_id, metadata}`
2. LPUSH `memory:tg:{tg_id}` with serialized item
3. LTRIM `memory:tg:{tg_id}` 0 49 (keep last 50)
4. EXPIRE `memory:tg:{tg_id}` 86400

**Notes:** Does NOT insert into messages table — orchestrator does that synchronously before calling this. This function only updates the Redis cache.

### `get_active_session(tg_id)`

**Input:** tg_id

**Output:** Active session object OR null

**Internal flow:**
1. GET `state:tg:{tg_id}`
2. If exists, parse JSON and return
3. If null, return null (no active session)

### `set_active_session(tg_id, session_data)`

**Input:** tg_id, session_data (full active context object)

**Output:** Success

**Internal flow:**
1. SET `state:tg:{tg_id}` with serialized JSON
2. EXPIRE `state:tg:{tg_id}` 7200 (2 hours)

### `update_active_session(tg_id, partial_update)`

**Input:** tg_id, partial dict to merge

**Output:** Success

**Internal flow:**
1. GET current state
2. Deep-merge partial_update
3. SET back

### `clear_active_session(tg_id)`

**Input:** tg_id

**Output:** Success

**Internal flow:** DEL `state:tg:{tg_id}`

**When called (Principle 3 enforcement):**
- By orchestrator on session boundary detection (gap > 30 min, explicit end, session switch)
- By orchestrator on explicit_end intent
- By session cleanup cron when closing inactive sessions

**Important:** This function ONLY clears the active session state (`state:tg:{tg_id}`). It does NOT clear the working memory cache (`memory:tg:{tg_id}`) — recent turns persist across sessions for cross-session context recall.

### `detect_session_resume_candidate(student_id)` — Bug 2 support

**Input:** student_id: UUID

**Output:** dict | null

**Internal flow:**

Called by orchestrator at session boundary detection (step 5 of handle_message). Returns information about unfinished work from the most recently closed session, if any.

```sql
-- Find most recently ended session for this student
SELECT s.session_id, s.ended_at, s.last_activity_at, es.summary_text, es.themes
FROM sessions s
LEFT JOIN episodic_summaries es ON es.session_id = s.session_id
WHERE s.student_id = $1
  AND s.ended_at IS NOT NULL
ORDER BY s.ended_at DESC
LIMIT 1;

-- If found, check for unanswered attempts in that session
SELECT q.question_id, q.subskill, q.passage_id
FROM student_question_attempts a
JOIN public.questions q ON q.question_id = a.question_id
WHERE a.session_id = $found_session_id
  AND a.answered_at IS NULL
  AND a.skipped = false
ORDER BY a.served_at DESC
LIMIT 1;
```

**Output shape (when match found):**
```json
{
  "previous_session_id": "sess-uuid-old",
  "previous_session_ended_at": "2026-04-24T15:30:00Z",
  "days_since_break": 1,
  "last_question_id": "q-uuid-42",
  "last_question_subskill": "inference_basic",
  "last_session_summary": "Worked on inference, scored 4/6, struggled with out-of-scope traps."
}
```

**Output:** null if no recently closed session, or no unfinished work in the closed session, or `days_since_break > 14` (too stale to suggest resume).

**Notes:** Pure SQL, no LLM call. ~20ms typical. Used by orchestrator to populate `context.session_resume_candidate`, which VARC agent uses to compose the "want to pick up where we left off?" response.

### `get_episodic_summaries(student_id, filter)`

**Input:**
- student_id: UUID
- filter: { domains?: [string], topics?: [string], limit: int (default 3), days_back: int (default 30) }

**Output:** List of episodic_summary objects

**Internal flow:**
SQL query:
```sql
SELECT * FROM episodic_summaries
WHERE student_id = ?
  AND created_at > now() - interval '{days_back} days'
  AND ($domains IS NULL OR domain = ANY($domains))
  AND ($topics IS NULL OR themes && $topics)
ORDER BY created_at DESC
LIMIT $limit
```

### `embedding_search_messages(student_id, query, limit=5)`

**Input:**
- student_id: UUID
- query: string (will be embedded)
- limit: int

**Output:** List of message objects with similarity scores

**Internal flow:**
1. Embed the query using OpenRouter's embedding model (text-embedding-3-small)
2. SQL query with pgvector:
   ```sql
   SELECT message_id, content, role, created_at,
          1 - (embedding <=> $query_embedding) AS similarity
   FROM messages
   WHERE student_id = ?
     AND embedding IS NOT NULL
   ORDER BY embedding <=> $query_embedding
   LIMIT $limit
   ```
3. Filter results by similarity threshold (e.g., > 0.7)

**Notes:**
- Only operates on messages with populated embeddings (~30% of all messages typically)
- v1: this function may return empty results often if embeddings aren't yet populated. That's fine — it's a v2+ feature mostly.

### `commit_deltas(student_id, tg_id, deltas)`

**Input:** student_id, tg_id, MemoryDeltas object (from agent response)

**Output:** Success

**Internal flow:**
- If `deltas.new_assistant_turn`: append_turn (Redis only; orchestrator already wrote to Postgres)
- If `deltas.active_context_updates`: update_active_session
- If `deltas.new_session`: insert into sessions table, set_active_session
- If `deltas.close_session`: update sessions table (ended_at, end_reason), trigger async session-end pipeline
- If `deltas.attempt_record`: insert into attempts table

### `close_session(session_id, end_reason)`

**Input:** session_id, end_reason

**Output:** Success

**Internal flow:**
1. UPDATE sessions SET ended_at = now(), end_reason = ? WHERE session_id = ?
2. Trigger session-end pipeline (async — see below)

### Session-End Pipeline (`process_session_end(session_id)`)

**Trigger:** Called by close_session OR by session cleanup cron

**Internal flow:**
1. Load all messages for session: `SELECT * FROM messages WHERE session_id = ? ORDER BY created_at ASC`
2. Load session metadata: `SELECT * FROM sessions WHERE session_id = ?`
3. Build prompt for combined LLM call:
   ```
   "Given this session transcript, produce:
    1. A concise summary (3-5 sentences)
    2. Key themes (3-7 short tags)
    3. Notable moments (struggles, breakthroughs, metacognitive questions)
    4. New profile notes about the student (if any new information surfaced)
    5. Reinforcement of existing notes (if any were validated by behavior)
    
    Existing student profile:
    {tutor_brief}
    
    Existing notes:
    {top_10_notes}
    
    Session transcript:
    {transcript}
    
    Return JSON: { summary_text, themes, key_moments, performance_summary, 
                   new_notes: [...], reinforced_note_ids: [...], 
                   contradicted_notes: [...] }"
   ```
4. Call MODEL_SUMMARIZER (Gemini Flash)
5. Insert episodic_summary
6. For each new_note in result: call profile_service.add_note
7. For each reinforced_note_id: call profile_service.reinforce_note
8. For each contradiction: call profile_service.supersede_note
9. Clear active session in Redis if still set

**Notes:**
- Combined LLM call (~$0.005 per session)
- Async — doesn't block user
- Idempotent — safe to retry on failure

### Session Cleanup Cron (`cleanup_inactive_sessions`)

**Trigger:** Cron every 10 minutes

**Internal flow:**
1. SQL:
   ```sql
   SELECT session_id FROM sessions
   WHERE ended_at IS NULL
     AND last_activity_at < now() - interval '45 minutes'
   ```
2. For each session_id:
   - Call `close_session(session_id, 'inactivity_timeout')`

### What Memory Service does NOT do

- Generate response content
- Make routing decisions
- Update profile (notes, structured profile fields)
- Read directly from `student_notes` (profile service's territory)

---

## Service 4: Profile Service

Owns: student_profile, student_notes.
Reads: student_skill_profile (the renamed v4 table), messages (for source attribution).

### `ensure_profile(student_id)`

**Input:** student_id

**Output:** student_profile row

**Internal flow:**
1. SELECT from student_profile
2. If not exists, INSERT a row with defaults (target_exam='CAT', onboarding_complete=false)
3. Return row

### `update_profile(student_id, updates)`

**Input:** student_id, updates dict (e.g., { target_year: 2027, hours_per_day: '4-6' })

**Output:** Updated row

**Internal flow:**
1. UPDATE student_profile SET (each field), last_updated = now() WHERE student_id = ?
2. Invalidate tutor brief cache: DEL `profile:brief:{student_id}`
3. Return updated row

### `get_tutor_brief(student_id)`

**Input:** student_id

**Output:** String (assembled tutor brief, ~300-500 tokens)

**Internal flow:**

1. Check Redis cache: GET `profile:brief:{student_id}`
2. If hit, return cached value
3. Else assemble:
   - Call `_get_structured_facts(student_id)` → row from student_profile
   - Call `_get_performance_summary(student_id)` → row from student_skill_profile + recent stats
   - Call `_get_top_notes(student_id, limit=8)` → top notes by confidence × recency
   - Call `_get_last_session_summary(student_id)` → most recent episodic_summary, or null
   - Render into template (with graceful empty-state fallbacks per Bug 25):
     ```
     {display_name} is a {experience_level} preparing for {target_exam} {target_year}.
     {target_colleges if any, else ""}.
     Studies {hours_per_day} hours per day, currently in {preparation_stage} phase.
     
     {if total_questions >= 5:}
       Performance: {accuracy}% overall on VARC. Strong on {top_subskill} ({pct}%),
       weakest on {bottom_subskill} ({pct}%). Most common trap: {trap_name} ({count} times).
       Current streak: {streak} days; longest this month: {longest}.
     {else:}
       Performance: Just getting started — practiced {total_questions} questions so far. 
       Patterns will emerge over the next few sessions.
     {end if}
     
     {if notes:}
       Context:
       - {note_1.content}
       - {note_2.content}
       ...
     {end if}
     
     {if last_session_summary AND days_since_last < 14:}
       Recent activity: {N days ago} — {last_session_summary.summary_text}
     {elif onboarding_completed_at AND days_since_onboarding > 0 AND no sessions yet:}
       First time back since onboarding {N days ago}.
     {else if no episodic data:}
       (No recent session history yet.)
     {end if}
     ```
4. Cache result: SET `profile:brief:{student_id}` value EX 1800
5. Return string

**Notes:** No LLM call. Pure template assembly. ~50ms typical. Cache invalidation on any write to student_notes / student_profile / student_skill_profile is mandatory (Principle 6).

**Empty-state philosophy:** New students with zero practice data should still get a coherent brief — never empty sections, never "N/A" placeholders. Use friendly fallback phrases that match the student's actual state.

### `get_minimal_brief(student_id)`

**Input:** student_id

**Output:** String (~50-100 tokens)

**Internal flow:**
- SQL queries for name, target, weakest skill
- Render minimal template:
  ```
  {display_name}, working professional, CAT 2026 aspirant. 
  Weakest: {weakest_subskill} ({accuracy}%). Common trap: {trap}.
  ```
- Return

**Notes:** Used when planner says profile context is "minimal". Saves tokens.

### `get_default_difficulty(student_id)` — Bug 23

**Input:** student_id

**Output:** "easy" | "medium" | "hard"

**Internal flow:**
1. Read `student_profile.preparation_stage` for the student
2. Map to default difficulty:
   - `just_starting` → "easy"
   - `mid_prep` → "medium"
   - `final_3_months` → "medium" (hard mixed in occasionally — implementation can use medium as base; VARC's retrieval ladder handles mixed difficulty natively)
   - `revision` → "hard"
3. If preparation_stage is null (uncommon, e.g., onboarding incomplete edge case), default to "medium"
4. Return string

**Notes:** Called by orchestrator when constructing AgentContext. Used by VARC agent when `intent.difficulty` from planner is null. ~5ms typical (single SELECT, can be cached briefly with student profile).

### `get_session_stats(student_id, session_id)` — Bug 12

**Input:** student_id, session_id

**Output:** dict with current session stats

**Internal flow:**
SQL:
```sql
SELECT 
  count(*) FILTER (WHERE answered_at IS NOT NULL AND skipped = false) AS attempted,
  count(*) FILTER (WHERE is_correct = true) AS correct,
  count(*) FILTER (WHERE skipped = true) AS skipped,
  count(*) FILTER (WHERE answered_at IS NULL AND skipped = false) AS open
FROM student_question_attempts
WHERE student_id = $1 AND session_id = $2;
```

Plus aggregation by subskill (which subskills appeared in this session, accuracy per subskill).

**Output shape:**
```json
{
  "attempted": 6,
  "correct": 4,
  "skipped": 1,
  "open": 0,
  "accuracy_pct": 67,
  "subskill_breakdown": {
    "inference_basic": {"attempted": 4, "correct": 3, "accuracy": 75},
    "main_idea": {"attempted": 2, "correct": 1, "accuracy": 50}
  },
  "duration_seconds": 1380,
  "trap_pattern": "out_of_scope (2 times)"
}
```

**Notes:** Used when student taps `[Show my session stats]` continuation button. Pure SQL, no LLM. ~30ms typical.

### `add_note(student_id, note_data)`

**Input:**
- student_id: UUID
- note_data: { content, category, confidence, source, source_message_id?, expires_at?, sensitive? }

**Output:** note_id

**Internal flow:**
1. Check for duplicate notes:
   - SELECT existing notes WHERE student_id = ? AND category = ? AND is_active = true
   - For each, compare content via simple similarity (lowercase string match or substring)
   - If high similarity found, treat as reinforcement instead of new note
2. If reinforcement: call `reinforce_note(existing_note_id)` and return
3. Else: INSERT new row into student_notes
4. **Invalidate tutor brief cache: DEL `profile:brief:{student_id}`** (Principle 6 — mandatory)
5. Return note_id

### `reinforce_note(note_id)`

**Input:** note_id

**Output:** Success

**Internal flow:**
1. UPDATE student_notes 
   SET last_reinforced = now(), 
       confidence = LEAST(confidence + 0.05, 1.0)
   WHERE note_id = ?
2. Invalidate tutor brief cache

### `supersede_note(old_note_id, new_note_data)`

**Input:** old_note_id, new_note_data

**Output:** new note_id

**Internal flow:**
1. Insert new note
2. Update old note: SET superseded_by = new_note_id, is_active = false
3. Invalidate tutor brief cache
4. Return new note_id

### `get_active_notes(student_id, filter)`

**Input:**
- student_id: UUID
- filter: { categories?: [string], limit: int, exclude_sensitive?: bool }

**Output:** List of note objects

**Internal flow:**
SQL:
```sql
SELECT * FROM student_notes
WHERE student_id = ?
  AND is_active = true
  AND (expires_at IS NULL OR expires_at > now())
  AND ($categories IS NULL OR category = ANY($categories))
  AND ($exclude_sensitive IS FALSE OR sensitive = false)
ORDER BY (confidence * exp(-EXTRACT(EPOCH FROM (now() - last_reinforced))/2592000)) DESC
LIMIT ?
```

(The exp() expression decays scores over a 30-day half-life.)

### `decay_confidences()` (background cron)

**Trigger:** Cron nightly (3 AM UTC)

**Internal flow:**
1. UPDATE student_notes SET confidence = confidence * 0.95 
   WHERE category = 'emotional' AND last_reinforced < now() - interval '7 days'
2. UPDATE student_notes SET is_active = false
   WHERE expires_at IS NOT NULL AND expires_at < now()
3. UPDATE student_notes SET is_active = false
   WHERE confidence < 0.2 AND is_active = true

### What Profile Service does NOT do

- LLM extraction (memory service triggers extraction at session-end and calls add_note)
- Direct read of conversation messages
- Routing decisions
- Generate response content

---

## Service 5: VARC Agent

Owns: nothing (stateless agent).
Reads: questions, passages (via existing RAG pipeline).

### `handle(context)`

**Input:** AgentContext

**Output:** AgentResponse — ALWAYS ends with continuation prompt + buttons (per Principle 1), except in diagnostic mode.

**Internal flow:**

1. Read `context.intent.action` to decide flow:
   - `practice_request` → retrieve question + serve
   - `answer_to_question` → process answer + explain + continuation buttons
   - `skip_request` → record skip + show explanation + continuation buttons
   - `doubt_about_current` (mid-question) → acknowledge doubt + offer back/skip/different-doubt buttons
   - `concept_question` (no active question) → teach concept + continuation buttons
   - `switch_topic` → graceful transition + continuation buttons
   - `explicit_end` → wrap up session, no buttons

2. **For practice_request:**
   - Determine retrieval criteria:
     - subskill: from `intent.subskill` (slice 4+); falls back to `inference_basic` in slice 2
     - difficulty: from `intent.difficulty` (slice 4+); falls back to `context.default_difficulty` (Bug 23); if both null, defaults to `medium`
     - profile_signals: from context.profile_brief (slice 5+)
   - Call `_retrieve_question_with_fallback(student_id, criteria)`
   - INSERT a new row into `student_question_attempts` (served_at=now, fallback_tier=N)
   - Compose presentation prompt
   - Call MODEL_VARC_TUTOR (Haiku 4.5) with prompt
   - For tier 5 / tier 6 retrievals, prepend acknowledgment string:
     - Tier 5: "We've seen this passage before — let's try it with fresh eyes."
     - Tier 6: "I'm running low on new questions in this category — let me serve one we did a while back to see how your thinking has changed."
   - Build response:
     - content: passage + question + options text
     - keyboard_buttons: row 1 `[A] [B] [C] [D]`, row 2 `[Skip / I don't know]` (callback `v5_skip_<attempt_id>`)
     - response_type: "question_serve"
     - **requires_keyboard_close: true** (so orchestrator removes previous question's keyboard — Principle 2)
   - memory_deltas: update domain_state with new question, new attempt_id

3. **For answer_to_question:**
   - Read `context.current_unanswered_attempt` for the question being answered
   - Determine if answer is correct
   - Determine if it matched a trap
   - Compose explanation prompt with: question, options, correct answer, student's answer, trap if hit, profile pattern reference if applicable
   - Call MODEL_VARC_TUTOR (Haiku for normal, Sonnet for nuanced)
   - **System prompt rule for the LLM:** "You produce only the scoring acknowledgment + explanation + a brief 'what next' transition line. You NEVER include a new question, new options, or new A/B/C/D in your response. The continuation buttons will be added by the system."
   - Build response:
     - content: scoring + explanation + brief transition line ("Want another, or something else?")
     - keyboard_buttons: row 1 `[Next question] [Different subskill]`, row 2 `[I have a doubt] [I'm done]`
     - response_type: "answer_explanation"
     - requires_keyboard_close: false (the answered question's keyboard stays — student already answered it)
   - memory_deltas: UPDATE attempt row (answered_at, is_correct, student_answer, explanation_shown)

4. **For skip_request:**
   - Read `context.skipped_attempt_id` (passed from orchestrator)
   - Look up the question
   - Compose explanation prompt (similar to answer flow but no "you picked X" line)
   - Call MODEL_VARC_TUTOR with same no-auto-question rule
   - Build response:
     - content: "No worries — here's what was happening with that one:" + explanation + transition line
     - keyboard_buttons: same 4 continuation buttons as answer flow
     - response_type: "skip_explanation"
   - memory_deltas: UPDATE attempt row (answered_at=now, skipped=true, explanation_shown=true)

5. **For doubt_about_current (mid-question, Bug 1):**
   - Read `context.current_unanswered_attempt` (the still-open question)
   - The student typed text mid-question. They have an unanswered attempt and the message isn't an answer.
   - In slice 2.5: hardcoded acknowledgment, no LLM call needed:
     - content: "Got it — I'll come back to that. First, let's finish the current question or skip it. What works?"
     - keyboard_buttons: row 1 `[Back to the question]` (callback `v5_show_question_<attempt_id>`), row 2 `[Skip this question]` (callback `v5_skip_<attempt_id>`) `[I have a different question]` (callback `v5_continue_doubt`)
     - response_type: "mid_question_doubt_ack"
     - requires_keyboard_close: false (current question's keyboard still valid)
   - In slice 4+ (with planner): real LLM call to attempt to address the doubt while preserving question state. Buttons same.
   - memory_deltas: NO updates to attempt row (still unanswered, intentionally)

6. **For concept_question (no active question, Bug doesn't apply):**
   - Compose teaching response (no retrieval)
   - System prompt includes the no-auto-question rule
   - Single LLM call
   - Build response with continuation buttons:
     - row 1 `[Practice this concept]` `[Practice something else]`
     - row 2 `[Ask another question]` `[I'm done]`
   - response_type: "concept_explanation"

7. **For switch_topic:**
   - Recognize the switch
   - Compose acknowledgment that respects the previous context:
     - "Got it, switching to {new_subskill}. We were on {old_subskill} — happy to come back to that anytime."
   - Build response with continuation buttons appropriate to new subskill
   - response_type: "topic_switch_ack"
   - In v1, the previous context is NOT preserved in domain_state for resume (slice 2.5 deferred). The acknowledgment is shallow.

8. **For explicit_end:**
   - Compose wrap-up response (warm, brief)
   - "Good work today. Come back anytime — I'll remember where we left off."
   - response_type: "session_wrap"
   - keyboard_buttons: empty (no continuation; the student said they're done)
   - memory_deltas: close_session = { end_reason: 'explicit_end' }

**Returning-after-break special case (Bug 2):**

If `context.session_resume_candidate` is set AND `intent.action` is "practice_request" or "small_talk" or "casual" or generic greeting:
- Compose response that acknowledges the gap and offers to resume:
  - "Welcome back. Last time we were on a {subskill} question about {topic}. Want to pick up that one, or start fresh?"
- keyboard_buttons: row 1 `[Resume that question]` `[Start fresh]`, row 2 `[Just chat first]`
- response_type: "session_resume_prompt"
- This takes priority over normal practice_request flow. If the student taps `[Start fresh]`, the next message routes to normal practice_request flow.

### `_retrieve_question_with_fallback(student_id, criteria)`

**Input:**
- student_id: UUID
- criteria: { subskill?, difficulty?, profile_signals? }

**Output:** Question object + tier (1-6)

**Internal flow:** Implements the 6-tier fallback ladder from data model document.

```
seen_ids = SELECT question_id FROM attempts WHERE student_id = ?

# Tier 1: best match
results = pgvector_query(
  exclude=seen_ids,
  subskill=criteria.subskill,
  difficulty=criteria.difficulty,
  bonus_filter=criteria.profile_signals
)
if results: return (results[0], tier=1)

# Tier 2: drop bonus
results = pgvector_query(exclude=seen_ids, subskill=criteria.subskill, difficulty=criteria.difficulty)
if results: return (results[0], tier=2)

# Tier 3: drop difficulty
results = pgvector_query(exclude=seen_ids, subskill=criteria.subskill)
if results: return (results[0], tier=3)

# Tier 4: drop subskill (any VARC question)
results = pgvector_query(exclude=seen_ids)
if results: return (results[0], tier=4)

# Tier 5: stale repeats (seen but not in last 7 days)
seven_days_ago = now() - 7 days
recent_seen_ids = SELECT question_id FROM attempts WHERE attempted_at > seven_days_ago
results = SELECT * FROM questions WHERE id NOT IN recent_seen_ids AND subskill = criteria.subskill
if results: return (random_from(results), tier=5)

# Tier 6: any seen, oldest first
results = SELECT * FROM questions q 
          JOIN attempts a ON q.id = a.question_id
          WHERE a.student_id = ?
          ORDER BY a.attempted_at ASC LIMIT 1
if results: return (results[0], tier=6)

# True empty (very rare)
return (None, tier=0)
```

### `serve_diagnostic_question(student_id, q_index)`

**Input:** student_id, q_index (1-5)

**Output:** AgentResponse

**Internal flow:**
- Question selection logic per onboarding spec:
  - q1: easy inference
  - q2: easy main_idea or specific_detail
  - q3: medium inference
  - q4: medium specific_detail or purpose
  - q5: hard inference
- Retrieve question matching criteria (no fallback needed if seeded properly; can use tier 1-3)
- Compose presentation (briefer than normal practice; "Question 3 of 5:")
- Return AgentResponse with question + keyboard

### `handle_diagnostic_answer(student_id, current_step, normalized_payload)`

**Input:** student_id, current step name, payload

**Output:** AgentResponse

**Internal flow:**
- Get current question from session domain_state
- Determine correctness, trap if any
- Brief explanation (less detailed than normal — preserve diagnostic momentum)
- Record attempt
- Update student_skill_profile
- Update onboarding_step to next (q2, q3, ... or 'mentor_synthesis')
- Return response

### What VARC Agent does NOT do

- Quant, LR, DI, GDPI handling (different domains)
- Cross-session memory (uses what's in context)
- Profile updates (returns observer events; orchestrator commits)
- Direct DB writes outside its domain

---

## Service 6: Mentor Agent

Owns: nothing.
Reads: profile (via service), episodic memory (via service), all messages (via service).

### `handle(context)` — reactive mode

**Input:** AgentContext

**Output:** AgentResponse — ALWAYS ends with contextual continuation buttons (per Principle 1).

**Internal flow:**
- Read intent.action to determine sub-flow:
  - `review_progress` → strategic answer using profile + episodic
  - `vent` → emotional support response
  - `casual` → warm acknowledgment
  - `meta` → answers about dhri, capabilities, etc.

- For all of these, single LLM call (Sonnet for nuanced, Haiku for simple)
- No retrieval from question bank
- Use full profile_brief and episodic_summaries from context

**System prompt rule for the Mentor LLM:**
"End your response with a brief 'what's next' question (e.g., 'want to try one with this in mind, or shift gears?'). Do NOT include a question (with passage and A/B/C/D options) in your response. Do NOT serve practice questions. The actual button options will be added by the system based on the conversation context. The student decides what comes next."

**Contextual button selection (orchestrator appends after mentor returns):**

Based on `intent.action` and `intent.emotional_tone`, orchestrator picks the right button set:

- For anxiety / frustration / "vent" with strong negative emotion:
  - row 1: `[Try one easy one]` `[Different subskill]`
  - row 2: `[Talk it out more]` `[Take a break]`
  
- For strategy / planning queries (`review_progress`, neutral tone):
  - row 1: `[Practice my weak areas]` `[Show my stats]`
  - row 2: `[Ask another question]` `[I'm done]`
  
- For motivation / "casual" / mild stress:
  - row 1: `[One easy win]` `[Just chat]`
  - row 2: `[Show my stats]` `[I'm done]`
  
- For `meta` queries (about dhri itself):
  - row 1: `[OK, let's practice]` `[Different subskill]`
  - row 2: `[Ask another meta question]` `[I'm done]`

The button-set selection is deterministic in orchestrator code, not LLM-driven, to ensure consistency.

- response_type: "mentor_strategic_response"
- requires_keyboard_close: false (mentor responses don't replace question keyboards)

- Return AgentResponse

### `synthesize_diagnostic(student_id)`

**Input:** student_id

**Output:** AgentResponse

**Internal flow:**
1. Load diagnostic test results (5 attempts from this session)
2. Identify weak subskills, trap patterns
3. Compose synthesis prompt:
   ```
   "You're DHRI, the warm CAT VARC tutor. The student just completed a 5-question 
   diagnostic test. Synthesize their results and welcome them properly.
   
   Test results:
   - Q1 (easy inference): {correct/wrong, time}
   - Q2 (easy main idea): ...
   - Q3 (medium inference): ...
   - Q4 (medium specific detail): ...
   - Q5 (hard inference): ...
   
   Patterns observed:
   - Strongest: {top_subskill}
   - Weakest: {bottom_subskill}
   - Trap pattern: {trap if any}
   
   Profile:
   - Target: CAT 2026
   - Experience: working professional
   - Hours: 2-4 per day
   
   Compose a warm welcome (3-4 sentences) that:
   - Notes their strongest area honestly
   - Identifies their weakest area gently
   - Suggests starting with [weakest area] and offers practice
   - Asks what they'd like to do next
   
   End with inline buttons: [Practice my weakest], [Explore my strongest], [Ask DHRI a question]"
   ```
4. Call MODEL_CHAT (Sonnet for this important first impression)
5. Build response with synthesized message + buttons
6. Return AgentResponse

### `handle_skip_diagnostic(student_id)`

**Input:** student_id

**Output:** AgentResponse

**Internal flow:**
- Compose welcome without test data
- Acknowledge skip
- Suggest a default starting point (probably easy inference)
- Buttons: [Start with easy inference], [Pick my own focus]
- Return

### `inline_observe(student_id, session_id, recent_turn, agent_response)` — observer mode

**Trigger:** Called by orchestrator after every successful turn (async)

**Input:**
- student_id: UUID
- session_id: UUID
- recent_turn: the user message + assistant response
- agent_response: AgentResponse including observer_events

**Output:** None (side effects only)

**Internal flow:**
1. Process observer_events from agent_response:
   - For `wrong_answer` events with same trap as recent: increment counter, may add note
   - For `consecutive_wrong` (3+): generate emotional_signal event, may add note "Student frustrated mid-session"
   - For `metacognitive_question`: high-value signal, add note
   - For other events: lightweight rule-based handling
2. Pattern detection (cheap, rule-based mostly):
   - 5+ wrong on same subskill → may add note
   - Self-corrections detected → may add note "Student showing growth"
   - Long pauses (>30s between turns) → emotional_signal possible
3. For each detection that warrants a note:
   - Call `profile_service.add_note(...)` with confidence based on signal strength
4. Mark events as processed in observer_events table

**Notes:**
- Mostly rule-based; rare LLM calls for fuzzy detections
- Runs async, doesn't block user
- Idempotent — safe to retry on failure

### What Mentor Agent does NOT do

- Question retrieval or VARC-specific logic
- Direct DB writes outside calling other services
- Initiator mode (deferred to v2)

---

## Service 7: Planner (Sub-component of Orchestrator)

Not a separate service in v1, but a clearly defined sub-component.

### `classify(message, recent_turns, active_session_summary)`

**Input:**
- message: current user message text
- recent_turns: last 10 turns (role + content only)
- active_session_summary: brief description of active session if any

**Output:** IntentClassification object

**Internal flow:**
1. Build prompt:
   ```
   You are an intent classifier for DHRI, a CAT VARC AI tutor.
   
   Recent conversation (last 10 turns):
   {recent_turns_formatted}
   
   Active session: {active_session_summary or 'none'}
   
   Current message: "{message}"
   
   Classify intent and decide what context the agent will need.
   Return JSON matching this schema: { ... full schema from 01_data_model.md ... }
   
   GUIDANCE:
   
   Domain classification:
   - "out_of_scope" domain: anything unrelated to CAT/VARC/student prep journey
   - Quant/LR/DI math questions → out_of_scope (DHRI is VARC-only for now)
   - General productivity/study tips related to CAT prep → in scope (mentor domain)
   - Personal/emotional venting (related to prep) → mentor domain
   - Specific past sessions referenced → set context_needs.specific_messages.needed = true
   - "How am I doing?" → mentor + review_progress + full profile
   - Answer to current question (A/B/C/D or "I think B") → action=answer_to_question
   
   CRITICAL — small_talk vs practice_request distinction (Bug 15):
   
   After a recent question + answer in the conversation, brief acknowledgments must be 
   classified as small_talk, NOT practice_request. The bot's response to small_talk 
   is a warm acknowledgment + re-show of continuation buttons — NOT a new question.
   
   Examples that should be small_talk:
   - "ok"
   - "got it"
   - "thanks"
   - "i see"
   - "alright"
   - "hmm"
   - "interesting"
   - "okay continue" (ambiguous → still small_talk; orchestrator asks "what next?")
   - "makes sense"
   
   Examples that should be practice_request:
   - "another"
   - "next"
   - "next question"
   - "give me one more"
   - "more"
   - "let's continue"
   - "another inference one" (→ subskill="inference_basic")
   - "give me an easy one" (→ difficulty="easy")
   
   When in doubt, classify as small_talk — the bot will ask the student what they want, 
   never auto-serving a question.
   
   Subskill enum (must match question bank EXACTLY — Bug 22):
   inference_basic | inference_advanced | main_idea_full_passage | specific_detail | 
   passage_summary | sentence_insertion | sentence_odd_one_out | strengthen_weaken | 
   purpose_of_example | vocab_in_context | author_tone | para_jumble
   
   If the student says "inference" generically, use inference_basic.
   If they say "main idea" or "summary", use main_idea_full_passage.
   If unclear, leave subskill: null and let VARC's default apply.
   
   Mixed-intent / secondary signals (Bug 15):
   When a message has BOTH a strong action AND an emotional undertone, classify by 
   the action and capture the emotion in secondary_signal. Example:
     "I'm stressed, give me an easy one" 
     → domain=varc, action=practice_request, difficulty=easy
     → secondary_signal: { type: "emotional_undertone", value: "mild_stress" }
   The agent will use the secondary_signal to adjust tone in its response.
   ```
2. Call MODEL_PLANNER (Gemini Flash by default)
3. Parse JSON output
4. Validate structure (fall back to safe defaults if invalid)
5. Return IntentClassification

**Failure modes (Principle 5):**
- LLM returns malformed JSON → use default classification:
  - intent.domain = "varc"
  - intent.action = "small_talk"  ← safest default; bot will ask what student wants (NOT practice_request which would auto-serve)
  - intent.subskill = null
  - intent.difficulty = null
  - context_needs.profile = "minimal"
  - context_needs.episodic.needed = false
  - response_guidance.tone = "warm"
- LLM returns invalid domain → log, treat as varc + small_talk
- LLM call times out (>5s) → use default
- Always log raw response on parse failure for debugging

**Cost:** ~$0.0001-0.0003 per call.

---

## Cross-Service Contracts: Memory Deltas

When agents return AgentResponse, the `memory_deltas` field is the contract for state changes. Orchestrator processes these in order:

```
memory_deltas {
  new_assistant_turn: {
    content, content_type, metadata
  }
  
  active_context_updates: {
    domain_state: {...},  // partial update
    message_count_in_session: int  // increment by N
  }
  
  new_session: {  // if creating new session
    primary_agent, started_at
  }
  
  close_session: {  // if closing
    session_id, end_reason
  }
  
  attempt_record: {  // if answering a question
    question_id, selected_option, correct, time_taken_seconds
  }
}
```

Orchestrator commits these atomically where possible. If any step fails, log loud and continue (don't lose user-facing response over a memory write hiccup).

---

## Error Handling Conventions

Consistent across services:

- **Logging:** Every error logged with traceback, student_id, request_id
- **User-facing fallbacks:** Always graceful, never expose internals. Examples:
  - Planner failed → use defaults, response continues
  - Agent failed → "Hmm, let me try that again" + retry once
  - DB write failed (non-critical) → log, continue
  - DB write failed (critical, e.g., user message persistence) → fail loud, return error to user
- **Lock leaks:** Always release lock in finally block
- **Idempotency:** Where possible, operations are safe to retry

---

## Open Questions / Future Work

- **Planner caching:** For exact duplicate messages within 30 sec, could cache classification. Skip for v1.
- **Speculative context fetching:** Could start fetching context BEFORE planner returns based on heuristics. Skip; complexity not justified.
- **Fan-out for parallel agent invocations:** When planner says "this needs both VARC and mentor," currently we route to one. Skip for v1.
- **Memory deltas as event sourcing:** Could log all deltas separately for replay/debugging. Skip for v1.

---

## Appendix: Function Signature Summary

```
# Message Bus
receive_telegram_update(raw_update) → 200 OK
send_to_telegram(tg_id, response, thinking_message_id) → bool
edit_telegram_keyboard(tg_id, message_id, new_keyboard | null) → bool  # for closing old keyboards (Bug 11)

# Orchestrator
handle_message(normalized_payload, thinking_message_id) → AgentResponse
handle_onboarding_step(student_id, normalized_payload) → AgentResponse
check_idempotency(tg_update_id) → bool  # Bug 20

# Memory Service
get_recent_turns(student_id, tg_id, limit=20) → [Turn]
append_turn(student_id, tg_id, turn_data, message_id) → bool
get_active_session(tg_id) → Session | None
set_active_session(tg_id, session_data) → bool
update_active_session(tg_id, partial_update) → bool
clear_active_session(tg_id) → bool
detect_session_resume_candidate(student_id) → ResumeCandidate | None  # Bug 2
get_episodic_summaries(student_id, filter) → [EpisodicSummary]
embedding_search_messages(student_id, query, limit=5) → [Message]
commit_deltas(student_id, tg_id, deltas) → bool
close_session(session_id, end_reason) → bool
process_session_end(session_id) → bool  # async pipeline
cleanup_inactive_sessions() → bool       # cron

# Profile Service
ensure_profile(student_id) → ProfileRow
update_profile(student_id, updates) → ProfileRow
get_tutor_brief(student_id) → string
get_minimal_brief(student_id) → string
get_default_difficulty(student_id) → "easy" | "medium" | "hard"  # Bug 23
get_session_stats(student_id, session_id) → SessionStats  # Bug 12
add_note(student_id, note_data) → note_id  # invalidates profile:brief cache
reinforce_note(note_id) → bool  # invalidates profile:brief cache
supersede_note(old_note_id, new_note_data) → new_note_id  # invalidates profile:brief cache
get_active_notes(student_id, filter) → [Note]
decay_confidences() → bool  # cron

# VARC Agent
handle(context) → AgentResponse  # all flows: practice/answer/skip/doubt/concept/switch/end/resume
serve_diagnostic_question(student_id, q_index) → AgentResponse
handle_diagnostic_answer(student_id, current_step, payload) → AgentResponse

# Mentor Agent
handle(context) → AgentResponse
synthesize_diagnostic(student_id) → AgentResponse
handle_skip_diagnostic(student_id) → AgentResponse
inline_observe(student_id, session_id, recent_turn, agent_response) → None  # async

# Planner (sub-component)
classify(message, recent_turns, active_session_summary) → IntentClassification
```
