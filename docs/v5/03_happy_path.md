# Happy Paths — DHRI v5

## Overview

This document traces specific user interactions through every service. It validates that the data model and service contracts compose into working behavior. If a trace doesn't make sense, the architecture has a gap.

**⚠️ Slice 2.5 update note (2026-04-25):**

The 5 original traces below were written before slice 2.5's architectural updates. The original traces show the bot auto-serving questions after answers (line "Step 23: VARC serves next question") — **THIS IS NO LONGER CORRECT.** Per Principle 1 (no auto-serve), the bot now ends every answer-explanation with continuation buttons and waits for the student's next message.

When reading the original traces, mentally substitute:
- "Step 22: VARC composes explanation + auto-serves next question" → "Step 22: VARC composes explanation + 4 continuation buttons. STOP. Wait for next user message."
- Any "auto-served next question" pattern → "continuation buttons + wait for tap"

Other slice 2.5 changes that affect every trace:
- Question keyboards now have a Skip button (Row 2: `[Skip / I don't know]`)
- New question serves close the previous question's keyboard via `editMessageReplyMarkup`
- Webhook idempotency check (Step 0) runs before any other processing
- LLM/DB failures show graceful fallbacks with `[Try again]` button
- Active session Redis state cleared on session boundary

**5 NEW traces are appended at the end of this document** showing:
- Trace 6: Skip flow
- Trace 7: Mid-question doubt
- Trace 8: Returning after break with resume
- Trace 9: Mid-session stats request
- Trace 10: LLM API failure with retry

These new traces fully reflect slice 2.5's architecture. When a discrepancy exists between original traces (1-5) and the contracts in `02_service_contracts.md`, **trust the contracts.**

---

## Original Traces (1-5)

Five traces are documented, in order of importance:

1. **First-time user (onboarding flow)** — most critical to get right
2. **Returning user, normal practice** — the most common flow
3. **Mid-session topic switch** — tests context-switching
4. **Wrong answer with pattern recall** — tests profile injection (the "wow" moment)
5. **Off-topic query (guardrails)** — tests the soft-redirect

Plus shorter notes on:
- Repeat-question fallback scenario
- Returning after a long break
- Rate limit hit
- Session-end pipeline (background)

---

## Trace 1: First-Time User Onboarding

### Scenario

Archit has never used dhri. He clicks the bot link from a Reddit post, opens Telegram, and taps "Start" or types `/start`. Goal: complete onboarding, take the diagnostic test, get welcomed properly. Total time: ~5-7 minutes.

---

### Step 1: First message arrives

User sends `/start` to bot.

- Service: Telegram → Bus
- Bus normalizes:
  ```
  { tg_id: 123456789, content: "/start", content_type: "text", 
    timestamp: now, source_metadata: {...} }
  ```
- Bus sends "🤔 Thinking..." to Telegram, captures thinking_message_id = 1
- Bus sends `chatAction("typing")`
- Bus calls `orchestrator.handle_message(payload, thinking_message_id=1)`

### Step 2: Orchestrator: identity resolution

- Look up tg_id = 123456789 in `students` table → not found
- Create new student:
  ```sql
  INSERT INTO students (tg_id, display_name, created_at, last_seen_at) 
  VALUES (123456789, 'Archit', now(), now())
  RETURNING student_id
  ```
- student_id = `uuid-archit-1`
- Acquire lock: `SET lock:user:123456789 1 NX EX 5` → OK

### Step 3: Persist user message

- Insert into messages:
  ```
  message_id: msg-uuid-1
  student_id: uuid-archit-1
  session_id: null (no session yet)
  role: 'user'
  content: '/start'
  content_type: 'text'
  metadata: {tg_message_id: ..., raw_telegram_payload: {...}}
  ```

### Step 4: Rate limit checks

- INCR `ratelimit:tg:123456789:2026-04-25` → 1
- 1 < 500, OK
- INCR per-minute → 1, < 5, OK
- spend cap not relevant yet (no LLM calls made)

### Step 5: Onboarding check

- Call `profile_service.ensure_profile(uuid-archit-1)`:
  - SELECT from student_profile → not found
  - INSERT default row:
    ```
    student_id: uuid-archit-1
    target_exam: 'CAT'
    target_year: null
    onboarding_complete: false
    onboarding_step: null
    ```
  - Return row
- Check onboarding_complete = false → route to `handle_onboarding_step`

### Step 6: Onboarding FSM — start_onboarding

- current_step = null
- Set onboarding_step = 'start_onboarding', onboarding_started_at = now()
- Compose response:
  ```
  Welcome to DHRI 👋
  
  I'm your AI tutor for CAT VARC. I'll help you practice 
  reading comprehension, para jumbles, and verbal ability questions — 
  the kind that show up in CAT.
  
  Quick onboarding (about 7 minutes), then we'll do a 5-question test 
  so I can understand your level. Ready?
  ```
- Inline keyboard: `[Let's start]`
- Return AgentResponse to orchestrator

### Step 7: Orchestrator: persist assistant message + commit

- Insert into messages: role='assistant', content=welcome
- No session created yet (onboarding doesn't create sessions until diagnostic)
- Increment ratelimit (outgoing)
- Return response to bus

### Step 8: Bus delivers

- Format response with MarkdownV2 + inline keyboard
- `bot.editMessageText(thinking_message_id=1, content, ...)` → replaces "🤔 Thinking..." with welcome
- Release lock: `DEL lock:user:123456789`

**User sees:** Welcome message with "Let's start" button. Total elapsed: ~1.5 seconds (no LLM calls in onboarding FSM, very fast).

---

### Step 9: User taps "Let's start"

- Telegram callback_query payload
- Bus normalizes: `content: "ONBOARDING_START_CONFIRMED", content_type: "button"`
- Bus sends thinking, calls orchestrator

### Step 10: Orchestrator routes to onboarding handler

- ensure_profile, check onboarding_complete = false
- Route to handle_onboarding_step
- current_step = 'start_onboarding'
- Advance to 'ask_name'
- Compose:
  ```
  What should I call you?
  
  I see "Archit" on Telegram — works for me, or pick something else.
  ```
- Inline keyboard: `[Use "Archit"]` `[Type a different name]`

### Step 11: User taps "Use 'Archit'"

- Through bus → orchestrator
- handle_onboarding_step: current_step = 'ask_name'
- save display_name = 'Archit' to students table (already set, but idempotent)
- Advance to 'ask_target_year'
- Compose:
  ```
  Which CAT year are you targeting?
  ```
- Inline keyboard: `[2026]` `[2027]` `[2028]`

### Step 12: User taps "2026"

- handle_onboarding_step: current_step = 'ask_target_year'
- UPDATE student_profile SET target_year = 2026
- Advance to 'ask_experience_level'
- Response: question + buttons `[Working professional]` `[Final-year student]` `[College student]` `[Dropper]` `[Fresher]`

### Step 13-16: Continue FSM through profile fields

- ask_experience_level → "Working professional"
- ask_preparation_stage → "Mid-prep"
- ask_hours_per_day → "2-4 hours"
- ask_target_colleges → multi-select with [IIM-A] [IIM-B] [IIM-C] [IIM-L] [IIM-K] [Others] [Skip]
  - User taps multiple, then "Done"
- ask_why_cat → "Skip" (user skips this optional one)

After each step:
- UPDATE student_profile with new field
- Advance onboarding_step
- Compose next prompt

### Step 17: Diagnostic intro

- current_step = 'ask_why_cat' completed (or skipped)
- Advance to 'diagnostic_intro'
- Compose:
  ```
  Last thing — let's do a quick 5-question diagnostic test.
  
  This helps me understand where you are right now and personalize 
  your practice. Should take about 5-7 minutes.
  
  Take it now, or skip and start fresh?
  ```
- Buttons: `[Take 5-question test]` `[Skip the test]`

### Step 18: User taps "Take 5-question test"

- handle_onboarding_step: current_step = 'diagnostic_intro'
- Advance to 'diagnostic_q1'
- **Now we delegate to VARC agent**:
  - Call `varc_agent.serve_diagnostic_question(student_id, q_index=1)`

### Step 19: VARC agent serves diagnostic Q1

- Criteria: easy inference question, unseen
- Retrieve via tier 1 (high-confidence match)
- Found: question about a passage on conservation biology, easy inference
- Compose presentation:
  ```
  **Question 1 of 5** (Easy)
  
  *Passage:* [Short passage text — 150 words]
  
  *Question:* Which of the following can be inferred from the passage?
  
  A) [Option A]
  B) [Option B]
  C) [Option C]
  D) [Option D]
  ```
- Inline keyboard: `[A]` `[B]` `[C]` `[D]`
- Return AgentResponse with:
  - content: presentation
  - keyboard: A/B/C/D
  - memory_deltas:
    - new_session: { primary_agent: 'varc', started_at: now }
    - active_context_updates: { domain_state: { passage_id, current_question_id, current_question_index: 0 } }

### Step 20: Orchestrator commits

- Persist assistant message
- Insert new session row (this is the first real session — created during diagnostic)
- Update Redis active session
- Track this session in onboarding context

### Step 21: User taps "B"

Goes through bus → orchestrator → handle_onboarding_step.

- current_step = 'diagnostic_q1'
- Delegate to `varc_agent.handle_diagnostic_answer(student_id, 'diagnostic_q1', payload)`

### Step 22: VARC agent handles answer

- Read active session domain_state: current question_id
- Look up correct answer: B
- User's answer: B → correct!
- Compose brief explanation:
  ```
  ✓ Correct!
  
  Quick explanation: [2-3 sentences on why B is right and what trap C 
  was setting up — keep it tight to maintain test momentum]
  
  Question 2 of 5 coming up...
  ```
- Record attempt:
  ```
  INSERT INTO attempts (student_id, question_id, selected_option, correct, ...)
  ```
- Update student_skill_profile with this attempt
- Update onboarding_step = 'diagnostic_q2'
- Recursively call serve_diagnostic_question for q2

OR (in cleaner implementation): Return response with explanation, then orchestrator advances onboarding_step, sets active_context for next question, AND triggers serving q2 in a follow-up message OR appended to current response.

For simplicity in v1: Return one response with both explanation AND next question presented. Single Telegram message:
```
✓ Correct!

[Brief explanation]

---

**Question 2 of 5** (Easy)

[Q2 presentation]

A) ... B) ... C) ... D) ...
```

### Steps 23-29: Repeat for Q2-Q5

Same pattern. Each turn:
- User taps option → orchestrator → varc_agent.handle_diagnostic_answer
- Agent: explanation + next question (or transition message after Q5)
- Record attempt
- Advance onboarding_step

### Step 30: After Q5 answered

- handle_diagnostic_answer for q5 returns explanation
- Advance onboarding_step = 'mentor_synthesis'
- Compose response: explanation + transition note "Let me look at your overall results..."

The next message arrival (or proactive trigger?) brings mentor in.

**Design note:** For UX continuity, treat mentor_synthesis as a follow-up message sent immediately after q5 explanation. Two options:

**Option A (cleaner):** Send q5 explanation, then send a SECOND message from mentor with synthesis.
- Bus sends explanation as the response to q5 answer
- Orchestrator triggers async task: after sending response, immediately call `mentor_agent.synthesize_diagnostic` and send result as new message via `bus.send_to_telegram` (no thinking message — direct send)

**Option B (simpler):** User has to tap a "See my results" button after q5, which triggers mentor.

Choose Option A for v1. Better UX. Worth the slight implementation complexity.

### Step 31: Mentor synthesizes diagnostic

- Triggered by orchestrator after sending q5 response
- Load 5 attempts from current session
- Identify: 4/5 correct, weakest = inference (medium difficulty wrong), strongest = main_idea
- Trap pattern: out_of_scope on the medium inference question
- Compose synthesis prompt for LLM:
  ```
  [The synthesis prompt from service contracts doc]
  ```
- Call MODEL_CHAT (Sonnet — important first impression)
- LLM returns synthesis like:
  ```
  Nice work, Archit. 4 out of 5 — solid baseline.
  
  Your strongest area looks like main idea (you got both of those right 
  with confidence). Where I'd focus our work is medium-difficulty 
  inference — you fell for an out-of-scope option, which is one of 
  the most common CAT VARC traps.
  
  Plan: I'll start serving you inference questions tuned to that pattern. 
  We'll work on it together.
  
  What sounds good?
  ```
- Buttons: `[Practice inference now]` `[Explore main idea instead]` `[Just chat first]`
- Set onboarding_complete = true, onboarding_completed_at = now(), onboarding_step = null
- Return AgentResponse

### Step 32: Bus delivers synthesis

- Sends new message (not edit) since this is a follow-up
- User sees the welcome + synthesis after q5 explanation

### Validation after onboarding

**Database state:**
```sql
-- students
SELECT * FROM students WHERE tg_id = 123456789;
-- 1 row: display_name='Archit', last_seen_at=recent

-- student_profile
SELECT * FROM student_profile WHERE student_id = 'uuid-archit-1';
-- onboarding_complete=true, all fields populated

-- student_notes
SELECT count(*) FROM student_notes WHERE student_id = 'uuid-archit-1';
-- 0-1 (depending on whether why_cat was filled)

-- sessions
SELECT * FROM sessions WHERE student_id = 'uuid-archit-1';
-- 1 row: primary_agent='varc' (or 'mixed'), ended_at=null (still active during synthesis)

-- messages
SELECT count(*) FROM messages WHERE student_id = 'uuid-archit-1';
-- ~30+ messages (10 onboarding turns + 10 diagnostic turns + synthesis)

-- attempts
SELECT count(*) FROM attempts WHERE student_id = 'uuid-archit-1';
-- 5 (the 5 diagnostic questions)

-- student_skill_profile
SELECT * FROM student_skill_profile WHERE student_id = 'uuid-archit-1';
-- weakest_subskill='inference_basic', basic accuracy stats
```

**Redis state:**
```
state:tg:123456789: { session_id, active_agent='varc', domain_state with last question }
memory:tg:123456789: list of last ~30 turns
```

**User experience:**
- Smooth onboarding (~5-7 min total)
- Clear questions, easy buttons
- Diagnostic feels like a quick test, not a chore
- Synthesis feels personal and informed

### Things that could go wrong

- User abandons mid-onboarding → state preserved, picks up where they left off when they return
- User types text instead of tapping button → re-prompt with same buttons (graceful)
- Diagnostic question retrieval fails → fall back to any easy question
- Mentor synthesis LLM call fails → simpler hardcoded welcome + suggestion to start practicing

---

## Trace 2: Returning User, Normal Practice

### Scenario

Archit has used dhri for 2 weeks. Has 50+ messages, 6 sessions, several notes. Opens Telegram, types "give me an inference question".

---

### Step 1: Message arrives

- Bus normalizes, sends thinking, calls orchestrator
- thinking_message_id = N

### Step 2: Identity, lock, persist

- Look up student → found, student_id = uuid-archit-1
- Update last_seen_at
- Acquire lock
- Insert user message into messages

### Step 3: Rate limit, spend checks

- Both pass

### Step 4: Onboarding check

- onboarding_complete = true → continue past onboarding handler

### Step 5: Load minimal context

- Read Redis: memory:tg:123456789 → 30 recent turns from last sessions
- Read Redis: state:tg:123456789
  - Last session ended yesterday → no active state in Redis (cleared by session-end pipeline)
- last_activity_at from sessions table > 2 hours ago → start new session
- Create new session: primary_agent='varc' (defaulting; will be confirmed after planner)
- session_id = sess-new-1
- Update messages.session_id for this turn
- Set Redis state:tg with new session

### Step 6: Planner LLM call

- Build planner prompt:
  ```
  Recent conversation (last 10 turns):
  user: "...what about C?"
  assistant: "C is wrong because..."
  user: "ok thanks"
  assistant: "Anything else?"
  user: "no I'm good"
  assistant: "Good session — see you next time"
  [day passes]
  user: "give me an inference question"
  
  Active session: just started, no domain_state yet
  
  Current message: "give me an inference question"
  ```
- Call Gemini Flash
- Returns:
  ```json
  {
    "intent": {
      "domain": "varc",
      "action": "practice_request",
      "continuation": "new_session",
      "emotional_tone": "neutral",
      "depth": "full_engagement",
      "references_past": "none",
      "specific_focus": "inference"
    },
    "context_needs": {
      "profile": "full",
      "episodic": {
        "needed": true,
        "domains": ["varc"],
        "topics": ["inference"],
        "limit": 2
      },
      "specific_messages": { "needed": false }
    },
    "response_guidance": {
      "tone": "warm",
      "should_acknowledge_feeling": false,
      "should_reference_pattern": true,
      "session_action": "continue"
    }
  }
  ```

### Step 7: Guardrails

- domain != 'out_of_scope' → continue

### Step 8: Conditional context fetching

In parallel:
- profile_service.get_tutor_brief(uuid-archit-1) → string (~400 tokens)
- memory_service.get_episodic_summaries(uuid-archit-1, {domains: ['varc'], topics: ['inference'], limit: 2}) → 2 summaries

### Step 9: Assemble AgentContext

```
{
  student_id: uuid-archit-1,
  display_name: 'Archit',
  recent_turns: [...10 turns from yesterday's session and earlier...],
  active_session: {session_id: sess-new-1, primary_agent: 'varc', domain_state: null},
  profile_brief: "Archit is a working professional preparing for CAT 2026. 
                  Studies 2-4 hours per day, currently in mid-prep phase.
                  Performance: 65% overall on VARC. Strong on main idea (78%),
                  weakest on inference (58%). Most common trap: out_of_scope (7 times).
                  Current streak: 4 days; longest this month: 9 days.
                  Context:
                  - Falls for out-of-scope traps on comparative passages
                  - Prefers technical/scientific passages over humanities
                  - Has mentioned work stress in last 2 weeks; sessions have been shorter
                  Recent activity: yesterday completed 8-question RC set on conservation,
                  scored 5/8, struggled with inference traps.",
  episodic_summaries: [
    "Tuesday session on inference: 4/6 correct. Caught two out-of-scope traps...",
    "Saturday session on inference vs main idea: focused on distinguishing types..."
  ],
  intent: { ... },
  response_guidance: { ... },
  current_message: { content: 'give me an inference question', message_id }
}
```

### Step 10: Route to VARC agent

- intent.domain = 'varc' → varc_agent.handle(context)

### Step 11: VARC agent processes

- intent.action = 'practice_request'
- Determine retrieval criteria:
  - subskill: inference_basic (from intent.specific_focus)
  - difficulty: medium (since student is mid-prep, default to medium)
  - profile_signals: prefers_technical_passages, struggles_with_comparative
- Call _retrieve_question_with_fallback
  - Tier 1 query: unseen, inference_basic, medium, technical_passage_bonus
  - Result: question on AI ethics passage (technical)
  - Returns (question, tier=1)

### Step 12: VARC agent composes response

Build prompt:
```
You are DHRI, a CAT VARC tutor with a warm, specific, coaching personality.
You remember students; you reference patterns; you don't lecture.

Student profile:
{profile_brief}

Recent context:
{recent_turns_summary}

Episodic context (relevant past sessions):
{episodic_summaries}

Response guidance:
- Tone: warm
- Don't acknowledge feeling
- DO reference pattern (the out-of-scope trap)
- This is a session continuation

Current request: "give me an inference question"

Retrieved question:
[Full passage and question]

Compose your response:
- Brief warm intro (1 sentence — acknowledge the request, optionally reference pattern)
- Present the passage
- Present the question with options
- Be direct, no fluff
```

Call MODEL_CHAT (Haiku):
- ~2.5s LLM call
- Returns:
  ```
  Picking up where we left off — let's keep working on inference. 
  This one's on AI ethics, which I know you like. Watch for out-of-scope 
  options — they've been catching you on comparative passages.
  
  *Passage:* [Full passage text]
  
  *Question:* Which of the following can be inferred from the author's 
  argument about algorithmic decision-making?
  
  A) [Option A]
  B) [Option B]
  C) [Option C]
  D) [Option D]
  ```

### Step 13: VARC agent returns AgentResponse

```
{
  content: "<above text>",
  content_type: "text_with_keyboard",
  keyboard: { A, B, C, D },
  memory_deltas: {
    new_assistant_turn: { content, content_type, metadata },
    active_context_updates: {
      domain_state: {
        passage_id: passage-uuid-7,
        current_question_id: q-uuid-42,
        questions_in_set: [q-uuid-42, q-uuid-43, q-uuid-44],
        questions_answered: {},
        current_question_index: 0
      }
    }
  },
  observer_events: [
    { event_type: "session_started", payload: { domain: 'varc', entry_point: 'practice_request' } }
  ],
  meta: {
    agent: 'varc',
    model_used: 'anthropic/claude-haiku-4.5',
    input_tokens: 2400,
    output_tokens: 380,
    cost_usd: 0.0035,
    generation_latency_ms: 2500,
    retrieval_used: true,
    retrieved_question_id: q-uuid-42,
    fallback_tier: 1
  }
}
```

### Step 14: Orchestrator commits

- Persist assistant message into messages table
- Update Redis active session with new domain_state
- Update sessions table: message_count++, primary_agent='varc'
- Insert observer event

### Step 15: Increment counters

- ratelimit:tg:123456789:2026-04-25 INCR
- spend:2026-04-25 INCRBYFLOAT 0.0035 + planner cost ~0.0001 = 0.0036

### Step 16: Return to bus

- bus.send_to_telegram(123456789, response, thinking_message_id)
- Edit thinking message with response + keyboard

**User sees:** Thinking emoji disappears, replaced with the response. Total elapsed: ~4-5 seconds.

### Step 17: Async post-processing

- mentor_agent.inline_observe(...) runs (lightweight, no LLM call this time)
- Mark observer event as processed
- Embedding job queued for the assistant message (it's substantial content)

### Step 18: Release lock

`DEL lock:user:123456789`

### Validation

```sql
-- New session row exists
SELECT * FROM sessions WHERE student_id = 'uuid-archit-1' ORDER BY started_at DESC LIMIT 1;
-- One row, ended_at=null

-- New messages
SELECT count(*) FROM messages WHERE session_id = 'sess-new-1';
-- 2 (user + assistant)

-- Attempt: not yet (will be inserted after user answers)

-- Redis
GET state:tg:123456789
-- Has session, domain_state with current question

LLEN memory:tg:123456789
-- 30+ items

-- Spend
GET spend:2026-04-25
-- Some value
```

**User experience:**
- Bot acknowledged the return naturally ("picking up where we left off")
- Referenced known preference (technical passages)
- Referenced known pattern (out-of-scope traps)
- Felt continuous, not transactional

This is the everyday flow. Should feel smooth.

---

## Trace 3: Mid-Session Topic Switch

### Scenario

Archit is in an active VARC session (3 questions answered). Mid-set, he asks: "wait, can you explain what 'inferred' actually means in CAT context?"

This is a doubt about the current concept, not a request for a new question. Should be handled smoothly, not as a topic switch.

---

### Steps 1-5: Same as Trace 2 (lock, persist, etc.)

Active session exists with domain_state showing they're mid-set.

### Step 6: Planner classifies

```json
{
  "intent": {
    "domain": "varc",
    "action": "concept_question",
    "continuation": "continues_current_session",
    "emotional_tone": "neutral",
    "depth": "quick_query",
    "references_past": "current_question",
    "specific_focus": "inference_concept"
  },
  "context_needs": {
    "profile": "minimal",
    "episodic": { "needed": false },
    "specific_messages": { "needed": false }
  },
  "response_guidance": {
    "tone": "warm",
    "should_acknowledge_feeling": false,
    "should_reference_pattern": false,
    "session_action": "continue"
  }
}
```

Planner correctly identifies:
- This is a concept question, not a switch
- Use minimal profile (the meta context isn't needed)
- Don't fetch episodic (not relevant)
- Tone: warm
- Session action: continue (we're going back to practice after this)

### Step 7: No guardrail trigger

### Step 8: Fetch minimal profile only

- profile_service.get_minimal_brief → "Archit, working professional, CAT 2026. Weakest: inference (58%). Common trap: out_of_scope."

### Step 9: Assemble context (lighter than Trace 2)

- recent_turns: yes
- active_session: yes (still in domain_state)
- profile_brief: minimal
- episodic_summaries: empty
- specific_past_messages: empty

### Step 10: Route to VARC

### Step 11: VARC agent processes

- intent.action = 'concept_question'
- No retrieval needed
- Compose teaching response:
  ```
  Good question — and good instinct to pause. 
  
  In CAT, "inference" means: a conclusion you can draw from the passage 
  using only what's stated, plus reasonable logic. Not what you might 
  guess or assume from outside knowledge.
  
  The trap CAT loves: options that go just slightly beyond what the 
  passage supports. Those are out-of-scope. Always ask: "did the 
  author actually say this, or am I extrapolating?"
  
  Want to try another inference question with this in mind, or finish 
  the current set first?
  ```
- Inline keyboard: `[Continue current set]` `[New question]`
- No memory_delta for new question (we didn't change current question)
- Return AgentResponse

### Step 12: Orchestrator commits

- Persist assistant turn
- No active context change (still on same question)

### Step 13: User taps "Continue current set"

- Orchestrator processes button press
- intent: action = 'continue_practice'
- VARC agent re-presents the current question (or moves to next if they were already done with current)

**User experience:**
- Asked a doubt, got a clear teaching answer
- Bot offered to continue or jump to new
- Session flow not broken
- Felt like asking a tutor a question mid-class

---

## Trace 4: Wrong Answer with Pattern Recall (the "wow" moment)

### Scenario

Archit answers a question. Picks C. Correct answer is B. C is the out-of-scope trap. He's done this 3 times before.

This is where the profile + episodic memory shine. The response should explicitly call back to past sessions.

---

### Steps 1-7: Standard flow until agent invocation

Planner classifies:
- domain: varc
- action: answer_to_question
- continuation: continues_current_session
- emotional_tone: neutral
- response_guidance:
  - tone: matter_of_fact
  - should_acknowledge_feeling: false
  - should_reference_pattern: TRUE ← key flag
  - session_action: continue

context_needs:
- profile: full ← important for the wow moment
- episodic: needed, topics=['inference', 'out_of_scope_traps'], limit=2
- specific_messages: not needed

### Step 8: Fetch full profile + relevant episodic

profile_brief includes:
> "Most common trap: out_of_scope (7 times). Falls for out-of-scope traps on comparative passages."

episodic_summaries include:
> "Tuesday: 4/6 inference, hit out-of-scope on comparative biology passage"
> "Saturday: focused on distinguishing inference from main idea, struggled with comparative argument structure"

### Step 9: Assemble context

Full profile brief, episodic summaries about the same trap pattern, recent turns showing the question and student picked C.

### Step 10: Route to VARC

### Step 11: VARC agent processes

- intent.action = 'answer_to_question'
- Read active session domain_state: current question_id = q-uuid-42
- Look up question, correct answer = B
- User picked C (from current_message content)
- Look up trap on option C: trap = "out_of_scope"
- Match pattern: this trap matches student's known pattern

### Step 12: Compose explanation prompt

System prompt to LLM (Haiku):
```
You are DHRI, a warm and specific CAT VARC tutor.

Student profile:
{full profile brief — emphasizes out_of_scope pattern}

Recent context (last 10 turns):
{recent}

Episodic context (similar past struggles):
{episodic}

Response guidance:
- Tone: matter-of-fact (not pitying, not too warm)
- Acknowledge the pattern explicitly
- This is the 4th time this exact trap has caught them
- Be specific: cite the past sessions
- Help them see the structural pattern, not just this question

The question was:
{question + options + correct}

The student picked: C
Correct answer: B
Option C is the OUT_OF_SCOPE trap.

Compose:
1. Brief, direct: "C is wrong; correct is B"
2. Explain WHY C is out of scope (cite passage)
3. Explicitly reference: this is the same trap from Tuesday and Saturday
4. Offer a teaching framing: what to ask before picking these options
5. Don't pile on. Be warm, not harsh.
```

LLM returns:
```
B is correct. C is wrong because it goes beyond what the passage 
supports — the author talks about algorithmic bias, but C jumps to 
"all algorithms are biased," which is a stronger claim than the 
passage makes.

Heads up: this is the same out-of-scope trap that caught you on 
Tuesday's biology passage and Saturday's session. Three or four times 
now. The pattern: option that *almost* fits, but extends the claim 
just a notch too far.

Next time you're choosing on inference, try this: ask "is this 
*exactly* what the passage said, or am I adding a small leap?" 
The trap options always include that small leap.

Ready for the next one, or want to revisit this passage?
```

### Step 13: Return AgentResponse

memory_deltas:
- new_assistant_turn
- active_context_updates: questions_answered[q-uuid-42] = {selected: 'C', correct: false}
- attempt_record: insert attempt row with this data

observer_events:
- { event_type: "wrong_answer", payload: {trap: "out_of_scope", consecutive_wrong: 1} }

### Step 14: Orchestrator commits, returns

### Step 15: Async observer

mentor_agent.inline_observe sees:
- wrong_answer + out_of_scope trap
- Check student_skill_profile.trap_counts: out_of_scope = 7 (now 8 with this attempt)
- This is the 4th time same trap recently
- Add note (or reinforce existing):
  - profile_service.add_note({
      content: "Continues to fall for out-of-scope traps on comparative/contrastive passages. 8 total occurrences.",
      category: "pattern",
      confidence: 0.95,
      source: "observed_behavior"
    })
- (Note: probably reinforces existing note rather than creating new)

**User experience:**
- "Whoa, the bot remembered Tuesday and Saturday"
- Explanation feels personal, not generic
- The teaching framing ("ask: did the author actually say this?") is actionable
- This is the moment that makes them feel "this bot knows me"

This is the **target conversational quality** for v5.

---

## Trace 5: Off-Topic Query (Guardrails)

### Scenario

Archit, frustrated with prep, asks: "actually can you help me write an email to my boss about taking time off"

This is out of scope. Should soft-redirect.

---

### Steps 1-5: Standard until planner

### Step 6: Planner classifies

```json
{
  "intent": {
    "domain": "out_of_scope",
    "action": "casual",
    "continuation": "new_session",
    "emotional_tone": "stressed",
    "depth": "full_engagement",
    "references_past": "none",
    "specific_focus": null
  },
  "context_needs": {
    "profile": "skip",
    "episodic": { "needed": false },
    "specific_messages": { "needed": false }
  },
  "response_guidance": {
    "tone": "warm",
    "should_acknowledge_feeling": true,
    "should_reference_pattern": false,
    "session_action": "continue"
  }
}
```

Note: planner detected stress in tone even though domain is out-of-scope. Good signal.

### Step 7: Guardrail trigger

domain == 'out_of_scope' → orchestrator handles directly, doesn't invoke agent.

Compose soft-redirect response. Logic:
- If emotional_tone is high/stressed/frustrated, acknowledge it briefly first
- Always note dhri's scope
- Always offer a path forward in scope

```python
if intent.emotional_tone in ['stressed', 'low', 'frustrated']:
    response = "I hear you — sounds like a lot going on. " + scope_note + path_forward
else:
    response = scope_note + path_forward

scope_note = "I'm focused on CAT VARC right now, so I can't help with the email. "
path_forward = "If a quick break would help, want to do 5 minutes of practice 
                to reset your head, or chat about how prep is going?"
```

Final response:
```
I hear you — sounds like a lot going on right now.

I'm focused on CAT VARC right now, so I can't help with the email itself. 
But if a quick break would help reset your head, want to do 5 minutes of 
practice, or chat about how prep is going?
```

Buttons: `[5 quick questions]` `[Talk about prep]` `[Just leave it]`

### Step 8: Persist response, observer event

- Insert assistant message
- Insert observer event: `{ event_type: 'out_of_scope_query', payload: {original_message: '...', emotional_tone: 'stressed'} }`
- This event gets processed by mentor inline observer:
  - Stress signal detected → may add note: "Mentioned work stress around taking time off"

### Step 9: Skip agent invocation, skip retrieval

Save tokens and time. Total elapsed: ~2.5s (just planner + simple compose).

### Step 10: Return to bus

**User experience:**
- Bot didn't lecture or moralize
- Acknowledged the stress
- Made the boundary clear
- Offered helpful alternatives
- Didn't break the relationship

Important: the bot still captured the emotional signal in the profile, so future sessions can reference it.

---

## Shorter Traces

### Repeat-Question Fallback

When student has done all 48 questions:
- Tier 1-4 fail (no unseen)
- Tier 5: stale repeat (not in last 7 days) succeeds
- VARC agent says: "Running low on fresh inference questions — let's revisit one from 12 days ago. Your thinking might've evolved."
- Serves question, tracks attempt
- Includes `fallback_tier: 5` in metadata for analytics

If even tier 6 fails (zero questions in DB matching anything):
- Agent: "I'm out of fresh material on this topic. Want to switch to {other subskill} instead?"

### Returning After Long Break

Student opens dhri after 5 days away.

- New session created
- Planner detects: gap > 2 hours, no current_focus
- profile + episodic loaded
- Mentor agent (or VARC with response_guidance.tone='warm') opens with:
  ```
  Welcome back. It's been about 5 days — totally fine, life happens.
  
  Quick recap of where we were: you were working on inference, 
  weakest area, dealing with out-of-scope traps. Want to pick that 
  back up, or start somewhere fresh?
  ```

The recall is what makes this feel different from generic chatbots.

### Rate Limit Hit

Student sends 80% of limit messages today.

- INCR detects 80% threshold
- Orchestrator sends warning: "Heads up — you've used 80% of today's quota. {remaining} messages left."
- Continues to handle the message normally

When 100% hit:
- INCR detects threshold
- Orchestrator sends: "You've hit today's limit (heavy testing day!). Come back tomorrow."
- Persists this assistant message but does NOT invoke agent or LLM
- Releases lock

### Session-End Pipeline (background)

Cron runs every 10 min. Finds session sess-new-1 with last_activity_at 47 minutes ago.

1. Mark ended: ended_at = now(), end_reason = 'inactivity_timeout'
2. Load all messages for session: 14 messages (7 user, 7 assistant)
3. Build prompt:
   ```
   Summarize this session AND extract new profile notes.
   {profile_brief}
   {top_10_existing_notes}
   {transcript}
   Return JSON: { summary_text, themes, key_moments, performance_summary, new_notes, reinforced_note_ids, contradicted_notes }
   ```
4. Call MODEL_SUMMARIZER (Gemini Flash)
5. Returns:
   ```json
   {
     "summary_text": "Archit completed 3 inference questions on AI ethics, scoring 2/3. Hit out-of-scope trap once again — same pattern from Tuesday/Saturday. Asked a metacognitive question about what 'inference' means in CAT context, suggesting he's reaching for understanding, not just reps. Energy good. 18-minute session.",
     "themes": ["inference", "out_of_scope_traps", "metacognition", "ai_ethics"],
     "key_moments": {
       "metacognitive_moments": [{"turn": 5, "content": "asked what 'inferred' means in CAT context"}],
       "struggles": [{"question_id": "q-uuid-42", "trap": "out_of_scope"}]
     },
     "performance_summary": {"questions": 3, "correct": 2, "accuracy": 0.67},
     "new_notes": [],
     "reinforced_note_ids": ["note-uuid-pattern-1"],
     "contradicted_notes": []
   }
   ```
6. Insert episodic_summary
7. Reinforce existing pattern note
8. Clear active session in Redis (already cleared by orchestrator on inactivity)

User wasn't online. Cost: ~$0.005. Pipeline runs in background, ~3-4 seconds.

---

## Validation Checklist (Per Trace)

After each implementation slice, verify these traces work:

### Onboarding (Trace 1):
- [ ] /start creates student row
- [ ] FSM advances correctly through all steps
- [ ] Diagnostic test serves 5 questions in order
- [ ] Mentor synthesis runs after Q5
- [ ] onboarding_complete = true at end
- [ ] Total time ~5-7 minutes

### Normal practice (Trace 2):
- [ ] Returning user gets contextual greeting
- [ ] Profile injected into responses
- [ ] Past sessions referenced when relevant
- [ ] Question retrieval honors profile signals
- [ ] Response time ~4-5 seconds

### Mid-session doubt (Trace 3):
- [ ] Concept question detected (not as topic switch)
- [ ] Minimal context loaded (saves tokens)
- [ ] Active session preserved
- [ ] User can resume practice cleanly

### Wrong answer with pattern recall (Trace 4):
- [ ] Past sessions explicitly referenced in explanation
- [ ] Trap pattern called out
- [ ] Profile note reinforced/created
- [ ] Tone is matter-of-fact, not patronizing

### Off-topic guardrail (Trace 5):
- [ ] Out-of-scope detected by planner
- [ ] Soft-redirect response (not harsh refusal)
- [ ] Emotional tone acknowledged if present
- [ ] No agent invocation (saves tokens)
- [ ] Observer event captured for profile

### Session-end pipeline:
- [ ] Cron triggers on inactivity
- [ ] Combined summary + extraction LLM call
- [ ] Episodic summary inserted
- [ ] Notes added/reinforced/superseded
- [ ] Active session cleared

---

## What Could Go Wrong (Failure Modes by Step)

For implementation: build error handling for each.

| Step | Failure mode | Mitigation |
|------|--------------|------------|
| Lock acquisition | Lock held | Return "still working on last message" |
| Persist user message | DB write fails | Fail loud, don't continue |
| Rate limit check | Redis down | Allow request through, log alert |
| Onboarding FSM | Invalid state | Reset to last known good step |
| Planner LLM | Times out / malformed | Default classification, full context |
| Planner LLM | Returns invalid domain | Treat as 'varc' |
| Profile fetch | DB error | Use empty brief, log |
| Episodic fetch | DB error | Empty list, log |
| Embedding search | pgvector error | Empty list, log |
| Agent invocation | Exception | Graceful error response, no memory commit |
| Question retrieval | All tiers fail | "No fresh material" message |
| Generation LLM | Times out | "Trying that again" + retry once |
| Generation LLM | Malformed | Hardcoded fallback message |
| Persist assistant | DB write fails | Log; user already saw response, but state inconsistent — alert |
| Memory deltas | Redis down | Log; state may be inconsistent |
| Bus delivery | Telegram API fails | Retry once; user may not see response |

The principle: **never crash the user-facing flow, even at the cost of some state inconsistency**. Better to send a response and have a state hiccup than to crash.

---

# NEW TRACES (Slice 2.5 architecture)

The five traces below reflect slice 2.5's updated architecture with continuation buttons, skip flow, mid-question doubt handling, returning-after-break, and graceful failure modes.

---

## Trace 6: Skip Flow

### Scenario

Archit gets a question. He doesn't know the answer and doesn't want to guess. He taps `[Skip / I don't know]`.

This validates Bug 8 fix from slice 2.5.

### Pre-state

- Active session: yes
- Active question: passage P, question Q1, attempt_id `att-uuid-1`, served 30 seconds ago
- domain_state.current_question_id = Q1
- last_question_attempt_id = att-uuid-1 in Redis active session

### Step 1: User taps Skip button

Telegram sends webhook with `callback_query`:
- callback_data = `v5_skip_att-uuid-1`
- message reply markup intact (button taps don't auto-clear)

### Steps 0-5: Standard processing

- Step 0 (idempotency): tg_update_id is new, proceed
- Step 1 (lock): acquired
- Step 2 (persist user message): content = "[Button tap: Skip / I don't know]", content_type = "button", tg_update_id = update_id

### Step 6.5: Deterministic detection (Bug 8)

Orchestrator sees callback_data starts with `v5_skip_`:
- Override `intent.action = "skip_request"`
- Set `context.skipped_attempt_id = att-uuid-1`
- Skip planner LLM call (deterministic override)

### Step 8-9: Context (no profile fetch needed for skip)

- AgentContext built with skipped_attempt_id, current_unanswered_attempt loaded from active session domain_state
- intent.action = "skip_request"

### Step 10: VARC handles skip_request

```python
# Pseudo-code
def handle_skip_request(context):
    attempt = context.current_unanswered_attempt  # row for att-uuid-1
    question = fetch_question(attempt.question_id)
    
    # Compose explanation prompt — slightly different from answer flow
    # Key difference: NO "you picked X" line; tone is teaching, not corrective
    prompt = f"""
    The student tapped Skip on this question. Show them what was happening with it.
    
    Question: {question.text}
    Options: {question.options}
    Correct answer: {question.correct_letter}
    
    Compose:
    1. Brief acknowledgment ("No worries — here's what was happening with that one:")
    2. Explanation of correct answer + why each wrong option is wrong
    3. Brief 'what next' transition line ("Want to try another, or shift gears?")
    
    Do NOT include a new question or A/B/C/D options.
    """
    
    response_text = MODEL_VARC_TUTOR.complete(prompt)
    
    return AgentResponse(
        content=response_text,
        keyboard_buttons=[
            [{"text": "Next question", "callback_data": "v5_continue_next"},
             {"text": "Different subskill", "callback_data": "v5_continue_switch_subskill"}],
            [{"text": "Show my stats", "callback_data": "v5_continue_stats"},
             {"text": "I have a doubt", "callback_data": "v5_continue_doubt"},
             {"text": "I'm done", "callback_data": "v5_continue_done"}]
        ],
        response_type="skip_explanation",
        requires_keyboard_close=False,  # original question's keyboard stays (student already saw it)
        memory_deltas={
            "attempt_record": {
                "operation": "update",
                "data": {
                    "id": "att-uuid-1",
                    "answered_at": now(),
                    "is_correct": None,         # null because skipped
                    "student_answer": None,     # null because skipped
                    "skipped": True,
                    "explanation_shown": True
                }
            },
            "active_context_updates": {
                "domain_state.current_question_id": None,  # this question is closed for the student
                "last_question_attempt_id": None
            }
        },
        meta={...}
    )
```

### Step 13: Orchestrator commits memory deltas

- UPDATE v5.student_question_attempts WHERE id = 'att-uuid-1':
  - answered_at = now()
  - is_correct = NULL (skipped doesn't count as wrong)
  - student_answer = NULL
  - skipped = TRUE
  - explanation_shown = TRUE
- Active session Redis updated: last_question_attempt_id cleared (no open question)

### User sees

```
No worries — here's what was happening with that one:

Correct answer: B
Why: ...
[teaching content]

Want to try another, or shift gears?

[Next question]  [Different subskill]
[Show my stats]  [I have a doubt]  [I'm done]
```

### Validation

```sql
-- The attempt row reflects the skip
SELECT skipped, answered_at, is_correct, student_answer, explanation_shown
FROM v5.student_question_attempts WHERE id = 'att-uuid-1';
-- skipped=TRUE, answered_at=<recent>, is_correct=NULL, student_answer=NULL, explanation_shown=TRUE

-- The retrieval ladder will now consider this question "seen"
-- (so it won't be re-served via tier 1-4; only tier 5/6 fallback)
```

### Why this is correct

- Skip is "I saw it but didn't want to guess" — student deserves the explanation, but no correctness counted
- The retrieval ladder respects skips ("seen" but not "answered") so the question won't be force-served again
- Continuation buttons let the student decide what's next (Principle 1)

---

## Trace 7: Mid-Question Doubt

### Scenario

Archit is shown a question. Before answering, he types: "what does 'incommensurable' mean in option B?"

He has an open question, but his message isn't an answer (no A/B/C/D regex match). It's a free text question while a question is active.

This validates Bug 1 fix from slice 2.5.

### Pre-state

- Active session: yes
- Open attempt: att-uuid-2, question Q2, served 45 seconds ago
- Question Q2's keyboard still active in chat history

### Steps 0-2: Standard

- update_id new, lock acquired, message persisted with content_type='text'

### Step 6.5: Deterministic detection (Bug 1)

Orchestrator runs the mid-question doubt check:
- Active session has `last_question_attempt_id = att-uuid-2` (an unanswered attempt)
- content_type == "text" (not button callback)
- content does NOT match answer regex (`^[ABCDabcd1234]\s*$`, etc)
- → Override `intent.action = "doubt_about_current"`
- → Set `context.mid_question_doubt = true`
- → Set `context.current_unanswered_attempt = <att-uuid-2 row>`
- Skip planner LLM (deterministic override)

### Step 10: VARC handles doubt_about_current (mid-question case)

In slice 2.5, this is hardcoded — no LLM call:

```python
def handle_mid_question_doubt(context):
    attempt = context.current_unanswered_attempt
    
    return AgentResponse(
        content="Got it — I'll come back to that. First, let's finish the current question or skip it. What works?",
        keyboard_buttons=[
            [{"text": "Back to the question", "callback_data": f"v5_show_question_{attempt.id}"}],
            [{"text": "Skip this question", "callback_data": f"v5_skip_{attempt.id}"},
             {"text": "I have a different question", "callback_data": "v5_continue_doubt"}]
        ],
        response_type="mid_question_doubt_ack",
        requires_keyboard_close=False,  # original question's keyboard stays valid
        memory_deltas={
            "active_context_updates": {}  # NO updates to attempt row — still unanswered intentionally
        },
        meta={"agent": "varc", "model_used": None, "cost_usd": 0.0, ...}
    )
```

### Step 13: Memory deltas

- NO update to v5.student_question_attempts (the attempt is intentionally still open)
- Active session Redis state unchanged (still has last_question_attempt_id = att-uuid-2)

### User sees

```
[Original question still visible above with A/B/C/D buttons]
[New message:]

Got it — I'll come back to that. First, let's finish the current question or skip it. What works?

[Back to the question]
[Skip this question]  [I have a different question]
```

### Step 19: User taps [Back to the question]

- callback_data = `v5_show_question_att-uuid-2`
- Step 6.5 detects this:
  - Look up the attempt and its question
  - Re-render the question presentation (same passage + question + A/B/C/D + Skip keyboard)
  - response_type = "question_serve" (re-served)
  - requires_keyboard_close = false (we're showing the same question again)
  - DO NOT insert a new attempts row — the existing one is still valid

User sees the question again. Original question above (with intact A/B/C/D buttons) is still tappable. The student can answer either copy.

### Alternative: User taps [I have a different question]

- callback_data = `v5_continue_doubt`
- Orchestrator composes: "Got it, what's your question?" + 1 button `[Back to current question]`
- response_type = "mid_question_doubt_ack"
- Free text from student after this is handled by future slice 4 planner; in slice 2.5, just routes as practice_request

### Validation

```sql
-- The original attempt is still open (no leak from doubt handling)
SELECT answered_at, skipped FROM v5.student_question_attempts WHERE id = 'att-uuid-2';
-- answered_at=NULL, skipped=FALSE

-- Three messages exist for this turn:
-- 1. user message: "what does 'incommensurable' mean in option B?"
-- 2. assistant message: the doubt-ack response
-- (3. assistant message after [Back to question] tap: re-render of question)
```

### Why this is correct

- Student's autonomy preserved: they get to ask doubts mid-question without losing the question
- Original question's keyboard stays valid (`requires_keyboard_close=false`)
- The choice is explicit: come back, skip, or pursue the doubt
- No domain_state corruption: the open attempt remains open
- Slice 4 will replace the hardcoded ack with an LLM-driven response that actually attempts to address the doubt; slice 2.5 just acknowledges and offers the buttons

---

## Trace 8: Returning After Break (with Resume)

### Scenario

Archit had a session yesterday. He was on inference question Q42, didn't answer, life got busy. Today he comes back and types "hi".

This validates Bug 2 fix.

### Pre-state

- Last session: ended 18 hours ago (`ended_at = '2026-04-24T16:00:00Z'`, `end_reason = 'inactivity_timeout'`)
- Last session had unanswered attempt: att-uuid-old, question Q42, subskill='inference_basic', `answered_at IS NULL AND skipped = FALSE`
- Episodic summary exists from session-end pipeline: "Worked on inference, scored 4/6, struggled with out-of-scope traps."
- No active session in Redis

### Step 1-2: Standard

- Lock acquired
- User message "hi" persisted

### Step 5: Session boundary detection

- Read `state:tg:{tg_id}` from Redis → empty (was cleared at session close)
- Check Postgres for most recent session → found, ended_at set 18 hours ago → no active session
- This is a session boundary (in fact, the start of a new session after a break)
- Create new session row in v5.sessions
- Set new state:tg:{tg_id} in Redis (Principle 3: previous state was already cleared on session close)

**Returning-after-break detection (Bug 2):**

```python
candidate = memory_service.detect_session_resume_candidate(student_id)

# detect_session_resume_candidate runs:
# SELECT most recent ended session (yesterday's)
# Check for unanswered, non-skipped attempts → finds att-uuid-old with Q42
# Check days_since_break <= 14 → 18 hours = 0.75 days, OK
# Returns:
candidate = {
    "previous_session_id": "sess-uuid-old",
    "previous_session_ended_at": "2026-04-24T16:00:00Z",
    "days_since_break": 0,  # rounded down from 0.75
    "last_question_id": "Q42",
    "last_question_subskill": "inference_basic",
    "last_session_summary": "Worked on inference, scored 4/6, struggled with out-of-scope traps."
}

context.session_resume_candidate = candidate
```

### Step 6: Planner classifies

```json
{
  "intent": {
    "domain": "varc",
    "action": "casual",  // or "small_talk" — "hi" is ambiguous
    "continuation": "new_session",
    "emotional_tone": "neutral",
    "depth": "quick_query",
    "references_past": "none"
  }
}
```

### Step 10: VARC handles, but special-cased on session_resume_candidate

In VARC's handle:

```python
if context.session_resume_candidate and intent.action in ("casual", "small_talk", "practice_request", ...):
    # The returning-after-break override
    candidate = context.session_resume_candidate
    
    prompt = f"""
    The student is returning after a {candidate.days_since_break} day break. 
    Last session: {candidate.last_session_summary}
    Specifically, they had an unanswered {candidate.last_question_subskill} question still open.
    
    Compose a warm welcome (2 sentences) that:
    - Acknowledges the return
    - Offers to either pick up the unfinished question or start fresh
    
    End with a transition line. Buttons added by system.
    """
    
    response_text = MODEL_VARC_TUTOR.complete(prompt)  # or Sonnet for warmth
    
    return AgentResponse(
        content=response_text,
        keyboard_buttons=[
            [{"text": "Resume that question", "callback_data": f"v5_resume_{candidate.last_question_id}"},
             {"text": "Start fresh", "callback_data": "v5_continue_next"}],
            [{"text": "Just chat first", "callback_data": "v5_continue_doubt"}]
        ],
        response_type="session_resume_prompt",
        requires_keyboard_close=False,
        memory_deltas={"new_assistant_turn": {...}, "active_context_updates": {}},
        meta={...}
    )
```

### User sees

```
Welcome back. Last time we were on an inference question — you were working through it 
when you stepped away. Want to pick that one up, or start fresh?

[Resume that question]  [Start fresh]
[Just chat first]
```

### Step 19: User taps [Resume that question]

- callback_data = `v5_resume_Q42`
- Step 6.5 detects: route to VARC with intent.action = "practice_request" + context override `resume_question_id = Q42`
- VARC re-serves Q42:
  - Insert NEW attempt row for the new session (att-uuid-new) with question_id = Q42
  - Mark fallback_tier = 1 (specific resume, not a fallback fetch)
  - Note: the OLD attempt (att-uuid-old) stays open in the closed session forever, never answered. Acceptable; just analytics noise.
- Continue normally

### Alternative: User taps [Start fresh]

- callback_data = `v5_continue_next`
- Standard practice_request flow → VARC retrieves next via 6-tier ladder
- Insert observer event "resume_declined" for analytics

### Why this is correct

- Bot remembers what was being worked on across sessions (wow moment 2)
- Student has choice — not forced to resume
- Old unanswered attempt is preserved (analytics) but not held open in current session
- New session starts cleanly per Principle 3

---

## Trace 9: Mid-Session Stats Request

### Scenario

Archit has answered 4 questions in this session. After getting an explanation, he taps `[Show my stats]` to see how he's doing.

This validates Bug 12 fix.

### Pre-state

- Active session: yes, message_count = 9, ~4 questions answered
- v5.student_question_attempts has 4 rows for this session: 3 correct, 1 wrong, 0 skipped
- 2 subskills practiced: inference_basic (3 attempts, 2 correct), specific_detail (1 attempt, 1 correct)

### Step 1: User taps Show my stats

- callback_data = `v5_continue_stats`

### Step 6.5: Deterministic detection

- callback prefix `v5_continue_` matched
- Action = `stats` → `intent.action = "stats_request"`

### Step 9: Context loaded (minimal)

- `context.session_stats = profile_service.get_session_stats(student_id, session_id)`
- Pure SQL, no LLM, ~30ms

### Step 10: Orchestrator handles directly (NO agent invocation, Bug 12)

```python
if intent.action == "stats_request":
    stats = context.session_stats
    
    response_text = (
        f"**This session so far:**\n"
        f"- Attempted: {stats['attempted']}, Correct: {stats['correct']}, "
        f"Skipped: {stats['skipped']}\n"
        f"- Accuracy: {stats['accuracy_pct']}%\n"
    )
    
    if stats['subskill_breakdown']:
        response_text += "- Subskills:\n"
        for subskill, sub_stats in stats['subskill_breakdown'].items():
            response_text += f"  - {subskill}: {sub_stats['correct']}/{sub_stats['attempted']} ({sub_stats['accuracy']}%)\n"
    
    if stats.get('trap_pattern'):
        response_text += f"- Most common trap: {stats['trap_pattern']}\n"
    
    response_text += f"- Time: {stats['duration_seconds'] // 60} min"
    
    return AgentResponse(
        content=response_text,
        keyboard_buttons=[
            [{"text": "Next question", "callback_data": "v5_continue_next"},
             {"text": "Different subskill", "callback_data": "v5_continue_switch_subskill"}],
            [{"text": "I have a doubt", "callback_data": "v5_continue_doubt"},
             {"text": "I'm done", "callback_data": "v5_continue_done"}]
        ],
        response_type="session_stats",
        requires_keyboard_close=False,
        memory_deltas={"new_assistant_turn": {...}, "active_context_updates": {}},
        meta={"agent": "orchestrator", "model_used": None, "cost_usd": 0.0, ...}
    )
```

### User sees

```
This session so far:
- Attempted: 4, Correct: 3, Skipped: 0
- Accuracy: 75%
- Subskills:
  - inference_basic: 2/3 (67%)
  - specific_detail: 1/1 (100%)
- Time: 12 min

[Next question]  [Different subskill]
[I have a doubt]  [I'm done]
```

### Validation

```sql
-- The stats response is persisted as assistant message
SELECT content, metadata->>'response_type' FROM v5.messages 
WHERE student_id = 'your-uuid' ORDER BY created_at DESC LIMIT 1;
-- content has the stats text, response_type = 'session_stats'

-- No LLM call recorded (orchestrator-composed, no LLM)
SELECT count(*) FROM v5.llm_calls 
WHERE student_id = 'your-uuid' AND created_at > now() - interval '1 minute';
-- 0 (or 1 if planner ran; but stats_request was detected deterministically before planner in this case)
```

### Why this is correct

- Cheap (pure SQL, no LLM) — slice 5 will introduce LLM calls only where they add value
- Latency excellent (~50ms total)
- Continuation buttons keep flow going (Principle 1)
- The stats text is templated for predictability; LLM-driven would be inconsistent

---

## Trace 10: LLM API Failure with Retry

### Scenario

Archit asks for a question. OpenRouter is having issues — VARC's LLM call fails.

This validates Bug 18 fix from slice 2.5 + Principle 5.

### Pre-state

- Active session: yes
- OpenRouter is down (or rate-limited, or returning 500s)

### Steps 0-9: Standard until VARC is invoked

### Step 10: VARC handle, attempts LLM call

```python
async def handle_practice_request(context):
    question, tier = await retrieve_question(...)  # this works (no LLM)
    
    prompt = build_presentation_prompt(question)
    
    try:
        response_text = await openrouter.chat(model=MODEL_VARC_TUTOR, prompt=prompt)
    except (TimeoutError, APIError) as e:
        log.error(f"LLM call failed: {e}")
        
        # Retry once
        try:
            await asyncio.sleep(0.5)
            response_text = await openrouter.chat(model=MODEL_VARC_TUTOR, prompt=prompt)
        except (TimeoutError, APIError) as e2:
            log.error(f"LLM call failed after retry: {e2}")
            
            # Graceful fallback (Principle 5)
            return AgentResponse(
                content="Hmm, having trouble thinking right now. Try again in a moment?",
                keyboard_buttons=[
                    [{"text": "Try again", "callback_data": "v5_retry"}]
                ],
                response_type="error_fallback",
                requires_keyboard_close=False,  # the previously-served question's keyboard (if any) stays valid
                memory_deltas={
                    "new_assistant_turn": {...},
                    "active_context_updates": {}  # NO updates — state stays consistent for clean retry
                },
                observer_events=[
                    {"event_type": "llm_failure", "payload": {
                        "service": "varc",
                        "model": MODEL_VARC_TUTOR,
                        "error": str(e2),
                        "retry_attempted": True
                    }}
                ],
                meta={
                    "agent": "varc",
                    "model_used": None,  # null because no successful call
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cost_usd": 0.0,
                    "generation_latency_ms": ...,  # how long retries took
                    "response_type": "error_fallback"
                }
            )
```

### Step 13: Memory deltas applied

- new_assistant_turn persisted (the canned error message)
- active_context_updates is empty — domain_state stays as-is, attempt row not modified
- observer_event 'llm_failure' inserted (for monitoring / dashboards)

### User sees

```
Hmm, having trouble thinking right now. Try again in a moment?

[Try again]
```

### Step 19: User taps Try again

- callback_data = `v5_retry`
- Step 6.5 detects: 
  - Load the previous user message (the one that triggered the failed flow)
  - Use its `metadata.intent_classification` to retrieve the original intent
  - Set `context.retry_context = {"retrying": true, "original_intent": <recovered_intent>}`
- Re-runs the same flow with same intent
- If OpenRouter is back up: VARC composes successfully, user sees their question
- If still down: another error_fallback response (acceptable; user can keep trying or come back later)

### Validation

```sql
-- The error fallback was persisted as an assistant message
SELECT content, metadata->>'response_type', metadata->>'cost_usd'
FROM v5.messages 
WHERE student_id = 'your-uuid' ORDER BY created_at DESC LIMIT 1;
-- content has the error message, response_type='error_fallback', cost_usd=0.0

-- An observer event was logged
SELECT * FROM v5.observer_events 
WHERE student_id = 'your-uuid' AND event_type = 'llm_failure' 
ORDER BY created_at DESC LIMIT 1;
-- 1 row, payload has model + error details

-- llm_calls has the failed call(s) logged with success=false (slice 3+)
SELECT model, success, error_message FROM v5.llm_calls
WHERE student_id = 'your-uuid' AND success = FALSE
ORDER BY created_at DESC LIMIT 5;
-- 2 rows (initial + retry), success=false on both
```

### Why this is correct

- User experience is preserved: a clear fallback message, not a crash or silence
- State is clean: no half-modified domain_state, no half-recorded attempts
- Retry mechanism is explicit: button click, not automatic
- Observability: failures are logged to v5.llm_calls + observer_events for monitoring
- The retry uses the original intent (not re-classified), so a transient outage doesn't accidentally change what the user gets

This pattern applies to any LLM-bearing service (planner, VARC, Mentor, extractor). Each service implements the same retry-once-then-fallback pattern.

---

## Summary

These traces represent the conversational quality target for v5. If the implementation produces these flows in 4-5 seconds with the right tone and context awareness, the architecture is working.

The "wow moments" are:
1. Onboarding feels personal, not formy
2. Wrong-answer explanations reference specific past mistakes (Trace 4)
3. Returning after a break, the bot recalls context (handled in load + planner)
4. Topic switches are smooth (Trace 3)
5. Off-topic queries get warm, scope-clear redirects (Trace 5)

These five qualities are what make dhri feel like Claude/ChatGPT for VARC — and they emerge from the architecture, not from any single magic prompt.
