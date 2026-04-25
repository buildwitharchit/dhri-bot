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
  metadata: { tg_message_id, raw_telegram_payload }
  session_id: TBD (set after session lookup in step 4)
  ```
- Capture `message_id` for downstream use

#### Step 3: Rate limit and spend cap checks

- INCR daily message counter; check against `MAX_MESSAGES_PER_USER_PER_DAY`
- INCR per-minute counter; check against `MAX_MESSAGES_PER_USER_PER_MINUTE`
- Read current daily spend; check against `DAILY_LLM_SPEND_CAP_USD`

If any limit hit:
- Return canned response (e.g., "You've sent a lot of messages today — let's pick this up tomorrow")
- Persist the canned response as assistant message
- Release lock and exit

#### Step 4: Onboarding check

- Read `student_profile` for this student
- If `onboarding_complete = false`:
  - Route to onboarding handler (see `handle_onboarding_step` below)
  - Return early
- If `onboarding_complete = true`, continue to step 5

#### Step 5: Load minimal context for planning

- Read recent turns from Redis cache (`memory:tg:{tg_id}`)
- If empty, fall back: `SELECT * FROM messages WHERE student_id = ? ORDER BY created_at DESC LIMIT 10`
- Read active session from Redis (`state:tg:{tg_id}`)
- If active session exists:
  - Update `last_activity_at`
  - Update `messages.session_id` for the user message just inserted
- If no active session AND last activity > 2 hours ago:
  - Create new session in `sessions` table
  - Update `state:tg:{tg_id}` in Redis with new session_id
  - Update `messages.session_id` for user message

#### Step 6: Planner LLM call

- Call `planner.classify(message, recent_turns, active_session_summary)`
- Returns IntentClassification object

#### Step 7: Guardrails check

- If `intent.domain == "out_of_scope"`:
  - Compose soft-redirect response
  - Save observer event: `out_of_scope_query`
  - Return early (skip context fetching, skip agent invocation)
  - Persist as assistant message
  - Release lock

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
- intent (from step 6)
- response_guidance (from step 6)
- current_message (the just-inserted user message)

#### Step 10: Agent invocation

Based on `intent.domain`:
- "varc" → `varc_agent.handle(context)`
- "mentor" → `mentor_agent.handle(context)`
- "meta" → `mentor_agent.handle(context)` (mentor handles meta queries)
- (out_of_scope already handled in step 7)
- (onboarding already handled in step 4)

Returns AgentResponse.

#### Step 11: Validate and post-process response

- Check response.content is non-empty and under reasonable length (<4000 chars to fit Telegram)
- If invalid, fall back to graceful error message
- Append any orchestrator-level additions (rare; mostly empty)

#### Step 12: Persist assistant message

- Insert into `messages` table:
  ```
  role: 'assistant'
  content: response.content
  metadata: response.meta + intent_classification
  session_id: from active session
  ```

#### Step 13: Apply memory deltas

From `response.memory_deltas`:
- Update Redis active session (LPUSH new turn, update domain_state)
- Update `sessions` table (message_count++, question_count if applicable)
- Insert observer events from `response.observer_events`
- Insert attempt record if applicable (for answered questions)

If `response.memory_deltas.close_session` is set:
- Mark session as ended
- Trigger session-end pipeline (async)

#### Step 14: Increment counters

- Increment ratelimit counters (already done in step 3 actually; this is the second increment for outgoing message tracking — clarify in implementation)
- Increment spend counter by `response.meta.cost_usd + planner_cost`

#### Step 15: Return response to bus

Return AgentResponse to bus, which edits the thinking message.

#### Step 16: Async post-processing

After response sent (Python: use `asyncio.create_task`):
- Run mentor inline observer
- Queue embedding job for the new messages (if they meet "important" criteria)

#### Step 17: Release lock

`DEL lock:user:{tg_id}`

**Error handling:**
- Any uncaught exception → release lock, return graceful error response, log full traceback
- Planner LLM failure → fall back to default intent (varc + practice_request) and full context fetch
- Agent failure → return graceful error, do NOT commit memory deltas (state remains consistent)
- DB write failure on message persistence → fail loud, return error

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
   - Render into template:
     ```
     {display_name} is a {experience_level} preparing for {target_exam} {target_year}.
     {target_colleges if any}.
     Studies {hours_per_day} hours per day, currently in {preparation_stage} phase.
     
     Performance: {accuracy} overall on VARC. Strong on {top_subskill} ({pct}),
     weakest on {bottom_subskill} ({pct}). Most common trap: {trap_name} ({count} times).
     Current streak: {streak} days; longest this month: {longest}.
     
     Context:
     - {note_1.content}
     - {note_2.content}
     - {note_3.content}
     ...
     
     Recent activity: {last_session_summary if recent}.
     ```
4. Cache result: SET `profile:brief:{student_id}` value EX 1800
5. Return string

**Notes:** No LLM call. Pure template assembly. ~50ms typical.

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
4. Invalidate tutor brief cache: DEL `profile:brief:{student_id}`
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

**Output:** AgentResponse

**Internal flow:**

1. Read `context.intent.action` to decide flow:
   - `practice_request` → retrieve question + serve
   - `answer_to_question` → process answer + explain
   - `doubt_about_current` → explain without retrieving
   - `concept_question` → teach concept without retrieving
   - `switch_topic` → graceful transition
   - `explicit_end` → wrap up session

2. **For practice_request:**
   - Determine retrieval criteria from intent + profile
   - Call `_retrieve_question_with_fallback(student_id, criteria)`
   - Compose presentation prompt
   - Call MODEL_CHAT (Haiku) with prompt
   - Build response with question + inline keyboard A/B/C/D
   - Return AgentResponse with memory_deltas including new question state

3. **For answer_to_question:**
   - Read active session domain_state to get current question
   - Determine if answer is correct
   - Determine if it matched a trap
   - Compose explanation prompt with: question, options, correct answer, student's answer, trap if hit, profile pattern reference if applicable
   - Call MODEL_CHAT (Haiku for normal explanations, Sonnet for complex/important explanations)
   - Build response: explanation + next question OR offer next steps
   - Record attempt
   - Return AgentResponse

4. **For doubt_about_current:**
   - Read active question + recent turns
   - Compose response (no retrieval)
   - Single LLM call with full context
   - Return AgentResponse

5. **For concept_question:**
   - Compose teaching response (no retrieval)
   - Single LLM call
   - Return AgentResponse

6. **For switch_topic:**
   - Recognize the switch
   - Save current state for potential resume
   - Compose acknowledgment + offer next direction
   - Return AgentResponse with active_context update (paused state)

7. **For explicit_end:**
   - Compose wrap-up response
   - Set memory_deltas.close_session
   - Return AgentResponse

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

**Output:** AgentResponse

**Internal flow:**
- Read intent.action to determine sub-flow:
  - `review_progress` → strategic answer using profile + episodic
  - `vent` → emotional support response
  - `casual` → warm acknowledgment
  - `meta` → answers about dhri, capabilities, etc.

- For all of these, single LLM call (Haiku for simple, Sonnet for nuanced)
- No retrieval from question bank
- Use full profile_brief and episodic_summaries from context

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
   Return JSON matching this schema: { ... }
   
   Guidance:
   - "out_of_scope" domain: anything unrelated to CAT/VARC/student prep journey
   - Quant/LR/DI questions → out_of_scope (DHRI is VARC-only for now)
   - General productivity/study tips → in scope
   - Personal/emotional venting (related to prep) → mentor domain
   - Specific past sessions referenced → set context_needs.specific_messages
   - "How am I doing?" → mentor + review_progress + full profile
   - Answer to current question (A/B/C/D or "I think B") → action=answer_to_question
   ...
   ```
2. Call MODEL_PLANNER (Gemini Flash by default)
3. Parse JSON output
4. Validate structure (fall back to safe defaults if invalid)
5. Return IntentClassification

**Failure modes:**
- LLM returns malformed JSON → use default classification (varc, practice_request, full context)
- LLM returns invalid domain → log, treat as varc
- LLM call times out (>5s) → use default

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

# Orchestrator
handle_message(normalized_payload, thinking_message_id) → AgentResponse
handle_onboarding_step(student_id, normalized_payload) → AgentResponse

# Memory Service
get_recent_turns(student_id, tg_id, limit=20) → [Turn]
append_turn(student_id, tg_id, turn_data, message_id) → bool
get_active_session(tg_id) → Session | None
set_active_session(tg_id, session_data) → bool
update_active_session(tg_id, partial_update) → bool
clear_active_session(tg_id) → bool
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
add_note(student_id, note_data) → note_id
reinforce_note(note_id) → bool
supersede_note(old_note_id, new_note_data) → new_note_id
get_active_notes(student_id, filter) → [Note]
decay_confidences() → bool  # cron

# VARC Agent
handle(context) → AgentResponse
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
