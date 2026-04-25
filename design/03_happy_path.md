# Happy Paths — DHRI VARC Bot v4.1

Two end-to-end traces that demonstrate how the services actually compose.

- **Path A** is the foundational one: existing user taps `/rc`, gets a passage with 4 questions, answers them in order, session closes cleanly.
- **Path B** is the tricky one: user is mid-RC and types a free-text doubt about the current question. Two handlers need to cooperate — the doubt handler has to find the question in the active session without breaking the PJ/VA answer detection or burning through rate limit unfairly.

If you can read these two without pausing for "wait, where does X come from?", the architecture is coherent.

---

## Path A — Existing user requests RC practice

### Scenario

**Archit.** `tg_id = 12345678`, CAT 2026 target, intermediate experience.
Profile state before this interaction:
- `total_attempts = 42`, `total_correct = 27` (64% accuracy).
- `weakest_skill = 'inference'` (set because total ≥ 10).
- `most_common_trap = 'half_right_half_wrong'`.
- `current_difficulty = 'medium'`.
- 17 subskill scores updated, `inference_basic` at 0.41 (lowest).
- 12 questions already attempted (in `attempts` table).

**Action.** Archit is at the home screen and taps **Reading Comp**.

**Expected outcome.** He sees a passage from CAT 2024 Slot 1 (one of the 8 seed passages) with ≥2 unseen inference-tagged questions. He answers 4 questions, gets per-question feedback, and ends on a session summary.

---

### Step 1 — Telegram fires a callback_query

**Service.** Telegram (external).

**What happens.** Archit taps the "Reading Comp" inline button. Telegram builds a `callback_query` update with `data = "mode_rc"` and `from.id = 12345678`, POSTs it to our webhook URL `https://<railway-domain>/webhook/<WEBHOOK_SECRET>`.

**Our state.** No change yet. Just an HTTP request inbound.

---

### Step 2 — FastAPI webhook receives

**Service.** `main.py` → `@app.post(WEBHOOK_PATH)`.

**Action.** Reads the JSON body, constructs a `telegram.Update` via `Update.de_json(data, ptb_app.bot)`, calls `await route_update(update, ptb_app)`. Returns `Response(status_code=200)` regardless of handler outcome.

**Observable.** Access log line. No DB/Redis writes yet.

---

### Step 3 — Router acquires the user lock

**Service.** `bot/router.py::route_update`.

**Action.**
```
tg_id = 12345678
lock_acquired = await acquire_lock("lock:user:12345678", 5)  # → True
```

**Observable.** Redis key `lock:user:12345678 = "1"` with 5s TTL. Next update for this user within 5s is silently dropped.

**If lock fails** (rapid-fire taps): function returns immediately. No reply. User sees nothing.

---

### Step 4 — Router dispatches to callback handler

**Service.** `bot/router.py::_process_update` → `bot/callbacks.py::route_callback`.

**Action.** `update.callback_query` is truthy, so callback path. First thing: `await callback_query.answer()` to remove the loading spinner from the button. Then parses `callback_data = "mode_rc"`, matches the `mode_*` prefix.

**Observable.** Telegram removes the button spinner. Logs: `routing callback mode_rc for tg_id=12345678`.

---

### Step 5 — Callback loads profile + state

**Service.** `bot/callbacks.py` → `db/queries.py::get_or_create_user` and `memory/session.py::get_state`.

**Action.**
```sql
SELECT tg_id FROM tg_users WHERE tg_id = $1;  -- exists
UPDATE tg_users SET last_active_at = now(), username = COALESCE($2, username) WHERE tg_id = $1;
SELECT * FROM tg_users WHERE tg_id = $1;
SELECT * FROM user_profiles WHERE tg_id = $1;
```
Merges both rows into a `profile: dict`. Then `await get_state(12345678)` → returns the current IDLE state dict (home screen).

**Observable.** Two Postgres reads (~20ms), one Redis read (~30ms via HTTP).

**Profile dict now in memory:**
```python
{
  'tg_id': 12345678,
  'target_year': 2026,
  'experience': 'intermediate',
  'current_difficulty': 'medium',
  'weakest_skill': 'inference',
  'most_common_trap': 'half_right_half_wrong',
  'total_attempts': 42,
  # ... etc
}
```

---

### Step 6 — Dispatch to `start_rc_session`

**Service.** `handlers/practice/rc.py::start_rc_session(update, profile, bot)`.

**Action.** Instantiates `PracticeSelector()` and calls `await selector.get_rc_passage(profile)`.

---

### Step 7 — Selector: fetch seen IDs

**Service.** `retrieval/selector.py::get_seen_question_ids`.

**Action.**
```sql
SELECT DISTINCT question_id FROM attempts WHERE tg_id = $1;
```
Returns 12 question IDs.

**Observable.** One Postgres read. Uses `idx_attempts_tg_question`.

---

### Step 8 — Selector: pick weakest RC subskill

**Service.** `memory/profile.py::get_weakest_subskill_in_group(tg_id, RC_SUBSKILLS)`.

**Action.**
```sql
SELECT subskill FROM user_skill_scores
WHERE tg_id = $1 AND subskill = ANY($2)  -- 8 RC subskills
ORDER BY score ASC LIMIT 1;
```
Returns `'inference_basic'` (score 0.41).

**Observable.** One Postgres read via `idx_skill_scores_tg`.

---

### Step 9 — Selector: embed the technique query

**Service.** `retrieval/selector.py::_fetch_rc` → `agent/llm.py::embed`.

**Action.**
1. `query_text = SUBSKILL_TO_TECHNIQUE_QUERY['inference_basic']` — the 4-line cognitive anchor.
2. Spend-cap check: `_check_spend_cap(est_usd ≈ 0.000002)`. Passes.
3. OpenRouter POST `/embeddings` with `model='openai/text-embedding-3-small'`. Returns 1536-dim vector.
4. `_add_spend(actual_usd)` — increments `spend:2026-04-21` in Redis by ~$0.000002.

**Observable.** 1 HTTP call to OpenRouter (~200-400ms). Redis `spend:<date>` counter nudges up. `httpx` INFO log: `HTTP Request: POST https://openrouter.ai/api/v1/embeddings "HTTP/1.1 200 OK"`.

---

### Step 10 — Selector: pgvector search

**Service.** `retrieval/selector.py::_fetch_rc`.

**Action.**
```sql
SELECT q.question_id, q.passage_id,
       1 - (q.technique_embedding <=> $1::vector) as similarity,
       q.subskill, q.traps_present, q.difficulty
FROM questions q
WHERE q.type = 'rc_question'
  AND q.subskill = 'inference_basic'
  AND q.difficulty = 'medium'
  AND q.is_active = true
  AND q.needs_review = false        -- FIX 2
  AND q.question_id != ALL($seen)   -- exclude Archit's 12
  AND q.passage_id IS NOT NULL
ORDER BY q.technique_embedding <=> $1
LIMIT 20;
```

Candidates come back ranked by cosine similarity. Uses `idx_questions_embedding` (HNSW).

**Observable.** One Postgres read, ~30-80ms. Log: `selector fetched 11 RC candidates for inference_basic/medium`.

---

### Step 11 — Selector: rerank

**Service.** `retrieval/reranker.py::rerank(candidates, 'half_right_half_wrong')`.

**Action.** Pure Python, no I/O. For each candidate:
```
composite = 0.62 * similarity + 0.38 * (1.0 if 'half_right_half_wrong' in traps_present else 0.0)
```
Questions carrying Archit's dominant trap get a +0.38 bonus, floating to the top. Returns sorted list.

**Observable.** Microseconds. No logs.

---

### Step 12 — Selector: pick best passage

**Service.** `retrieval/selector.py::_fetch_rc` (continuation).

**Action.** Counts how many reranked candidates share each `passage_id`. Picks the passage with the most unseen questions. Requires ≥ 2 — otherwise returns `None` and the fallback chain tries the next config.

Suppose the top passage is `cat_pyq_bandicoots` with 3 unseen questions (`_q1`, `_q3`, `_q4` — `_q2` was already attempted).

Then:
```sql
SELECT * FROM questions
WHERE question_id = ANY(ARRAY['cat_pyq_bandicoots_q1','cat_pyq_bandicoots_q3','cat_pyq_bandicoots_q4'])
  AND needs_review = false
ORDER BY question_order;

SELECT * FROM passages WHERE passage_id = 'cat_pyq_bandicoots';
```

Returns `{"passage": {...}, "questions": [q1, q3, q4]}`.

**Observable.** Two Postgres reads, ~30ms.

---

### Step 13 — RC handler: create session

**Service.** `handlers/practice/rc.py::start_rc_session` → `db/queries.py::create_session`.

**Action.**
```sql
INSERT INTO sessions (tg_id, mode) VALUES ($1, 'rc') RETURNING session_id;
```
Returns `session_id = 'a7b8...uuid'`.

**Observable.** Row in `sessions` with `ended_at = NULL`, `started_at = now()`, `mode = 'rc'`.

---

### Step 14 — RC handler: write state to Redis

**Service.** `memory/session.py::set_state`.

**Action.** Serializes the state dict and writes:
```
SET state:tg:12345678 '<json>' EX 7200
```

State contents:
```json
{
  "state": "RC_ACTIVE",
  "session_id": "a7b8...uuid",
  "mode": "rc",
  "passage_id": "cat_pyq_bandicoots",
  "questions_in_set": ["cat_pyq_bandicoots_q1","cat_pyq_bandicoots_q3","cat_pyq_bandicoots_q4"],
  "current_question_index": 0,
  "questions_answered": {},
  "questions_remaining": ["cat_pyq_bandicoots_q1","cat_pyq_bandicoots_q3","cat_pyq_bandicoots_q4"],
  "session_started_at": "2026-04-21T10:30:00+00:00"
}
```

**Observable.** Redis key populated, 7200s TTL.

---

### Step 15 — Send passage to Telegram

**Service.** `handlers/practice/rc.py::start_rc_session` → `bot/utils.py::send_long_message`.

**Action.** Passage is ~478 words, ~2800 characters. Under the 4000-char Telegram cap, so one message. Sent as HTML with `<b>📖 Reading Comprehension</b>` heading.

**Observable.** Telegram API call. User sees the full passage.

---

### Step 16 — Send question 1 to Telegram

**Service.** Same handler → `_send_question`.

**Action.** Renders question 1 text + options with HTML escaping, attaches the 4-button answer keyboard:
```
Question 1 of 3
Which one of the following statements provides a gist of this passage?
A) The onslaught of animals...
B) Marsupials are going extinct...
C) A type of bandicoots was nearly wiped out...
D) The negligent attitude of the British colonists...

[A] [B] [C] [D]
```

**Observable.** Second Telegram message.

---

### Step 17 — Write a system message row

**Service.** `db/queries.py::write_message`.

**Action.**
```sql
INSERT INTO messages (session_id, tg_id, role, content, message_type, question_id) VALUES (...);
```
Content: `"RC session started: passage=cat_pyq_bandicoots, questions=[q1, q3, q4]"`, role `system`.

**Observable.** Row in `messages`.

---

### Step 18 — Router releases lock

**Service.** `bot/router.py::route_update` (finally block).

**Action.** `await release_lock("lock:user:12345678")`.

**Observable.** Redis key gone. Total elapsed from step 1: ~800ms-1200ms (dominated by the embedding call).

---

### [User reads passage and thinks]

Minutes pass. State TTL keeps refreshing on each user message, so 2 hours is generous.

---

### Step 19 — Archit taps answer **A** for question 1

Another callback, `data = "answer_A"`. Steps 1-5 repeat (new lock, new profile load, new state load).

### Step 20 — Callback routes to RC answer handler

**Service.** `bot/callbacks.py::route_callback`. Sees `answer_*` prefix, checks `state.state == "RC_ACTIVE"`, routes to `handlers.practice.rc.handle_rc_answer(update, profile, state, 'A', bot)`.

---

### Step 21 — Load current question

**Service.** `handlers/practice/rc.py::handle_rc_answer`.

**Action (FIX 7).**
```python
qids = state["questions_in_set"]
idx = state["current_question_index"]  # 0
qid = qids[idx]  # 'cat_pyq_bandicoots_q1'
```
Then:
```sql
SELECT * FROM questions WHERE question_id = 'cat_pyq_bandicoots_q1';
SELECT * FROM passages WHERE passage_id = 'cat_pyq_bandicoots';
```

---

### Step 22 — Compare + resolve trap

**Service.** Same handler + `handlers/practice/common.py::resolve_trap_for_selection`.

**Action.**
```
correct_option = 'C'
selected = 'A'
is_correct = False

option_traps = {"A": "too_extreme", "B": "out_of_scope", "C": null, "D": "half_right_half_wrong"}
trap = option_traps['A'] = 'too_extreme'
```

---

### Step 23 — Record attempt (the critical write)

**Service.** `handlers/practice/common.py::record_attempt`.

**Action.** Four writes atomically (not in an explicit transaction, but each is its own):

**23a. Insert attempt:**
```sql
INSERT INTO attempts (tg_id, session_id, question_id, selected_option, correct_option,
                     is_correct, trap_fallen_for)
VALUES (12345678, 'a7b8...', 'cat_pyq_bandicoots_q1', 'A', 'C', false, 'too_extreme');
```

**23b. Update skill score (EWMA):**
`memory/profile.py::update_skill_score(12345678, 'main_idea_full_passage', False)`.
- Read current score for `main_idea_full_passage`: let's say 0.58.
- New score = `0.58 * 0.85 + 0.0 * 0.15 = 0.493`.
- UPSERT `user_skill_scores`.
- Increment `user_profiles.total_attempts` (42 → 43), `total_correct` unchanged (27).
- `total_attempts >= 10` → recompute `weakest_skill`. Still `'inference'`.
- `DEL profile:12345678` (cache bust, for the currently-unused cache).

**23c. Update trap counts:**
`memory/profile.py::update_trap_counts(12345678, 'too_extreme')`.
- Read current trap_counts: `{"half_right_half_wrong": 8, "out_of_scope": 3, "too_extreme": 2}`.
- New: `{"half_right_half_wrong": 8, "out_of_scope": 3, "too_extreme": 3}`.
- `most_common_trap` still `half_right_half_wrong` (8 > 3).
- UPDATE `user_profiles`.

**23d. Update session counters:**
```sql
UPDATE sessions SET
  questions_attempted = questions_attempted + 1,
  questions_correct = questions_correct + 0,
  last_active_at = now(),
  skills_practiced = array_append(...,'main_idea')
WHERE session_id = 'a7b8...';
```

**Observable.** Four DB writes, one Redis DEL. ~100-150ms total.

---

### Step 24 — Send feedback

**Service.** `handlers/practice/rc.py::handle_rc_answer` (continuation) → `bot/utils.py::send_long_message`.

**Action.** Builds the feedback message:
```
❌ Not quite
Correct answer: C
Trap you fell for: too extreme

C captures both halves of the passage — the near-wipeout by invasive species
AND the present-day revival effort using Shark Bay survivors. A overstates
extinction (survivors persisted on islands). B widens the claim to all
marsupials. D misattributes cause to colonists' attitudes rather than
invasive species.
```

Sent to Archit.

---

### Step 25 — Advance state, send next question

**Service.** Same handler.

**Action.**
```python
state["questions_answered"]["cat_pyq_bandicoots_q1"] = {
    "selected": "A", "correct": False, "trap": "too_extreme"
}
state["questions_remaining"].remove("cat_pyq_bandicoots_q1")
state["current_question_index"] = 1
await set_state(12345678, state)
```

Next question fetch:
```sql
SELECT * FROM questions WHERE question_id = 'cat_pyq_bandicoots_q3';
```

Send with answer keyboard.

**Observable.** Redis updated, question 2 sent.

---

### Steps 26-29 — Archit answers q3 correctly, then q4 correctly

Same flow as 19-25 but on correct answers:
- q3 correct → `trap_fallen_for = 'none'`, skill score nudges up via EWMA, `total_correct` increments.
- q4 correct → same.

After q4, `current_question_index` becomes 3, which equals `len(questions_in_set)`. Handler detects session completion.

---

### Step 30 — Wrap session

**Service.** `handlers/practice/rc.py::_wrap_rc_session`.

**Action.**

**30a. Generate summary** via `memory/summarizer.py::generate_session_summary(session_id, tg_id)`:
- Reads session header + ordered attempts.
- Composes: mode=rc, 3 attempts, 2 correct, 67% accuracy, trap=too_extreme, subskills=`{main_idea_full_passage, specific_detail}`.
- Calls `MODEL_SUMMARIZER` (Gemini Flash) with a rigid 3-sentence prompt.
- Returns: *"You practised reading comprehension with a mix of main-idea and specific-detail questions. You got 2 of 3 right (67%), with your one miss falling for the 'too extreme' trap on a gist question. Next time, slow down on gist options — reject any that overstate the passage's scope, like 'all marsupials' when only 'bandicoots' are discussed."*

**30b. Close session:**
```sql
UPDATE sessions SET
  ended_at = now(),
  was_completed = true,
  summary = '<the 3-sentence string>',
  duration_mins = EXTRACT(EPOCH FROM (now() - started_at)) / 60
WHERE session_id = 'a7b8...' AND ended_at IS NULL;
```

**30c. Reset state:**
```python
await set_state(12345678, {"state": "IDLE"})
```

**30d. Send wrap message** with the summary and the home keyboard.

**Observable.**
- Session row has `ended_at`, `was_completed=true`, `summary`, `duration_mins`.
- Redis state reset to `{"state": "IDLE"}`.
- User sees the home screen.

Total elapsed from start to wrap: whatever Archit took (minutes), but per-interaction latency is ~1.5s for the selector call, ~400-800ms per answer.

---

### Validation checklist

After the full flow, the following are observable:

**Postgres.**
- `sessions`: one new row with `ended_at`, `was_completed=true`, `questions_attempted=3`, `questions_correct=2`, `summary` populated.
- `messages`: 1 system row (session start) + 0 user/assistant rows (no free text).
- `attempts`: 3 new rows with correct `is_correct` + `trap_fallen_for` populated.
- `user_skill_scores`: 2 rows updated (`main_idea_full_passage`, `specific_detail`).
- `user_profiles`: `total_attempts = 45`, `total_correct = 29`, `trap_counts['too_extreme'] = 3`, `weakest_skill = 'inference'` (unchanged).

**Redis.**
- `state:tg:12345678 = {"state": "IDLE"}`.
- `spend:<today>` incremented by ~$0.000004 (one embedding + one summary call).

**User experience.**
- 4 Telegram messages: passage + Q1 / Q1-feedback + Q2 / Q2-feedback + Q3 / Q3-feedback + wrap.
- Last message has a summary + home keyboard.

---

### What could go wrong

**Step 9 — embed call fails.** Retry 3x with backoff. If still fails: raises `RuntimeError`, bubbles to router, user sees generic error. Session *was not created* yet, so no orphan rows. Safe to retry.

**Step 9 — spend cap exceeded.** Raises `SpendCapExceededError`. Router catches, sends "I'm out of LLM budget for today" message. Lock released. User can retry after midnight IST.

**Step 12 — no passage has ≥ 2 unseen questions.** The fallback chain tries adjacent difficulty, then the second-weakest subskill. If all four configs fail, returns `None`. Handler sends "No fresh RC passage matches your profile right now. Try /pj or /va" and doesn't create a session.

**Step 21 — question vanished between serve and answer.** Shouldn't happen (questions aren't deleted, only soft-flagged). Handler logs error and silently returns. User sees nothing, state stays dirty until 2h idle cleanup kicks in.

**Step 23a — attempt insert fails (FK violation).** Same question-vanished scenario. Handler logs and returns. No partial state — no skill score is updated if the insert fails.

**Step 30a — summariser fails.** Caught in `_wrap_rc_session`. Summary falls back to a templated string. Session still closes cleanly.

**Step 30b — session close UPDATE races with cron cleanup.** Guard clause `AND ended_at IS NULL` ensures double-close is a no-op.

---

## Path B — Mid-RC doubt

### Scenario

Same user, Archit, **mid-session in RC**. He just read question 2 of the bandicoots passage (q3 = 'exclosures' vocab question). He's unsure — instead of picking an option, he types:

> **"what does exclosures actually mean here?"**

Expected outcome: the bot detects this as a doubt (not a PJ answer, not a concept query), attaches the current question as context, runs a Socratic-style LLM call per the system prompt, responds with a tutoring message that ends with a follow-up question. Archit's session state is preserved — he can still tap A/B/C/D to answer when ready.

**State before.**
```json
{
  "state": "RC_ACTIVE",
  "session_id": "a7b8...",
  "mode": "rc",
  "passage_id": "cat_pyq_bandicoots",
  "questions_in_set": ["cat_pyq_bandicoots_q1","cat_pyq_bandicoots_q3","cat_pyq_bandicoots_q4"],
  "current_question_index": 1,
  "questions_answered": {
    "cat_pyq_bandicoots_q1": {"selected": "A", "correct": false, "trap": "too_extreme"}
  },
  "questions_remaining": ["cat_pyq_bandicoots_q3","cat_pyq_bandicoots_q4"],
  ...
}
```

---

### Step 1-5 — Webhook, router, lock, dispatch to free-text

Same as Path A steps 1-3, then in step 4 the router sees `update.message` (not `callback_query`) and dispatches to `bot/free_text.py::handle_free_text`.

---

### Step 6 — Load profile + state

**Service.** `bot/free_text.py::handle_free_text`.

**Action.** Same as Path A step 5: `get_or_create_user` + `get_state`. State comes back showing `RC_ACTIVE` mid-session.

---

### Step 7 — Classify intent (deterministic, no LLM)

**Service.** `agent/classifier.py::classify_free_text`.

**Action.**
```python
in_active_practice = state["state"] in ("RC_ACTIVE", "PJ_ACTIVE", "VA_ACTIVE")  # True
text = "what does exclosures actually mean here?"

# is_pj_answer(text) — regex doesn't match, not 4 distinct digits. → False
# concept prefix check: startswith("what") — no, startswith("what is") → no, but "what does" isn't a prefix.
#   Actually "what is"/"what are" match; "what does" does not. So no concept match.
# Falls through to default → 'doubt'
```

Returns `"doubt"`.

**Observable.** Log: `classified free-text as doubt for tg_id=12345678`. No LLM call, microseconds.

---

### Step 8 — Debit the rate limit

**Service.** `db/queries.py::check_and_increment_rate_limit`.

**Action.**
```
key = "rl:msg:12345678:2026-04-21"
current = await redis.get(key)  # e.g. "3"
count = 3

# 3 < 50 → allowed
new_count = await redis.incr(key)  # 4
# new_count != 1, so no expire reset
return (True, 4)
```

**Observable.** Redis `rl:msg:12345678:2026-04-21` now `"4"`.

**If cap hit**: returns `(False, 50)`. Handler sends the cap message. No DB writes, no LLM call. Lock still released normally.

---

### Step 9 — Route to doubt handler

**Service.** `bot/free_text.py` → `handlers/doubt.py::handle_doubt(update, profile, state, text, bot)`.

---

### Step 10 — Attach current question context

**Service.** `handlers/doubt.py::handle_doubt`.

**Action.**
```python
session_id = state["session_id"]  # 'a7b8...'
# mode is 'rc', so attach question context
qids = state["questions_in_set"]
idx = state["current_question_index"]  # 1
qid = qids[idx]  # 'cat_pyq_bandicoots_q3'

q_row = await db.fetchrow(
    "SELECT question_text, explanation, options FROM questions WHERE question_id = $1",
    'cat_pyq_bandicoots_q3'
)
context_snippet = f"Current question: {q_row['question_text']}"
# "The text uses the word 'exclosures' because Wild Deserts has adopted a measure of"
```

**Observable.** One Postgres read.

---

### Step 11 — Fetch last session summaries + message history

**Service.** Same handler + `db/queries.py::get_session_messages`.

**Action.**

**11a. Last summaries:**
```sql
SELECT summary FROM sessions
WHERE tg_id = $1 AND summary IS NOT NULL
ORDER BY started_at DESC LIMIT 2;
```
Returns up to 2 previous session summaries (if any).

**11b. Message history within current session:**
```sql
SELECT role, content, message_type, question_id, created_at
FROM messages WHERE session_id = 'a7b8...'::uuid
ORDER BY created_at ASC LIMIT 10;
```
Returns the system row from session start + any prior doubt turns this session. For Archit right now: just the system row.

---

### Step 12 — Compose the LLM call via `explain()`

**Service.** `agent/explainer.py::explain(profile, user_text, history, last_summaries, model=MODEL_COMPLEX)`.

**Action.**

**12a. Build context string:**
```
tg_id=12345678
Weakest skill: Inference
Most common trap: half_right_half_wrong
Current streak: 1 day(s)
Total attempts: 43

Recent sessions:
- <Archit's previous session summary, if any>
```

**12b. Format system prompt** with this context into `SYSTEM_PROMPT_TEMPLATE`:
```
You are a CAT VARC expert tutor — direct, specific, practical.

TEACHING RULES:
1. Never give the answer before engaging with the student's reasoning...
   [8 rules, Section 22 verbatim]

Student context:
<context block>
```

**12c. Build messages array.** `build_messages_for_llm(history, user_text)`:
```python
messages = []
# Loop through history, keep only role='user' or role='assistant'
# The system row is dropped (role='system').
# So messages is [] here.

# Append the new user turn. user_text is the composed doubt:
messages.append({
  "role": "user",
  "content": (
    "Current question: The text uses the word 'exclosures' because Wild Deserts "
    "has adopted a measure of\n\n"
    "Student message: what does exclosures actually mean here?"
  )
})
```

**12d. Call LLM:**
```python
response = await llm_call_with_retry_messages(
    system=system_prompt,
    messages=messages,
    model='anthropic/claude-haiku-4-5'
)
```

Internally:
- Spend cap check: est tokens ~1800 (system) + 150 (user) + 400 margin = 2350. Est USD ~$0.0024. Passes.
- OpenRouter POST `/chat/completions`. ~2-3s latency.
- Retries up to 3x on `RateLimitError` / any Exception.
- On success: `_add_spend(actual_usd)` from `resp.usage.total_tokens`.

**Observable.** HTTP log for the completion. Response is a plain text string, HTML-formatted.

**Example response** (what Claude Haiku might return, abbreviated):
> <b>Before we get to meaning</b>, let's look at where 'exclosures' sits in the passage. It appears where Wild Deserts describes their sanctuaries — and the surrounding sentences talk about <i>fenced areas cleared of rabbits and cats</i>.
>
> So what's being fenced OUT? Think about the morphology: ex- (out) + closure. What does that suggest the fencing is for?
>
> If you focus on option C, 'barring the entry of invasive species', does that match the fencing's job? What about B's mention of islands — does the passage say these exclosures are on islands, or on the mainland?

Note the adherence to system prompt rule 1 (doesn't give the answer), rule 2 (doesn't name a trap since no wrong answer was picked yet), rule 6 (HTML formatting), rule 7 (ends with follow-up question).

---

### Step 13 — Log both turns

**Service.** `db/queries.py::write_message`.

**Action.** Two inserts:
```sql
INSERT INTO messages (session_id, tg_id, role, content) VALUES (..., 'user', 'what does exclosures actually mean here?');
INSERT INTO messages (session_id, tg_id, role, content) VALUES (..., 'assistant', '<the tutoring reply>');
```

**Observable.** 2 new rows in `messages`.

---

### Step 14 — Send reply to Telegram

**Service.** `bot/utils.py::send_long_message`.

**Action.** Splits on paragraph boundaries if > 4000 chars (unlikely for a 150-word response). Sends with HTML parse mode.

---

### Step 15 — State is unchanged

**Critical.** The doubt handler **does not touch `set_state`**. `state.state` stays `RC_ACTIVE`, `current_question_index` stays `1`, `questions_remaining` stays `['cat_pyq_bandicoots_q3', 'cat_pyq_bandicoots_q4']`. Archit can now:
- Tap A/B/C/D and the answer handler (step 20 of Path A) processes it normally.
- Type another doubt, which repeats this flow.
- Go idle — session cleanup will eventually close it.

**Observable.** `state:tg:12345678` TTL was refreshed by the `get_state` call? **No** — `get_state` only reads. TTL keeps counting down from whenever it was last `set`. If we want TTL refresh on doubt, a `set_state(tg_id, state)` at the end would do it. **Current behaviour: doubt does not refresh state TTL.** This is a known limitation; see "What could go wrong" below.

---

### Step 16 — Router releases lock

Same as Path A step 18. Total elapsed: ~3-5s, dominated by the `MODEL_COMPLEX` call.

---

### Validation checklist

After this flow:

**Postgres.**
- `messages`: 2 new rows in session `a7b8...` (user + assistant).
- `sessions`: unchanged (no attempt, so no counter updates).
- `attempts`: unchanged.
- `user_skill_scores`, `user_profiles`: unchanged.

**Redis.**
- `state:tg:12345678`: unchanged contents. TTL decremented (not refreshed).
- `rl:msg:12345678:2026-04-21`: incremented by 1.
- `spend:2026-04-21`: incremented by ~$0.0025 (one Haiku call).

**User experience.**
- 1 Telegram message: the tutoring reply.
- Buttons from question 2 are still clickable (Telegram doesn't auto-expire inline keyboards).

---

### What could go wrong

**Step 7 — classifier mis-routes.** E.g. Archit types `"How do I solve this?"` — would match the `how do i` prefix and route to *concept* instead of *doubt*. Not ideal mid-session. Mitigation (future): check `in_active_practice` first and prefer doubt. Today: not mitigated — concept handler would fire.

**Step 7 — classifier misses a PJ-looking answer.** E.g. `"2,4,1,3"` while in RC would be `is_pj_answer=True` but mode is RC, so it would still route to PJ answer handler, which would try to look up the current question and find an RC question. The handler would reject — `normalise_pj_answer` returns `"2413"` but RC `correct_order` is NULL. Edge case: rare in practice. Future fix: condition `pj_answer` routing on `mode == 'pj'`, not just "in active practice".

**Step 8 — rate limit denied.** User sees the cap message. Doubt burns no tokens. Session state unchanged. User can still answer the question via button tap.

**Step 10 — question not found.** DB read returns None. Handler continues with empty `context_snippet`. LLM still runs, just without the question attached. Not fatal.

**Step 12 — LLM fails 3x.** Bubbles up as `RuntimeError`. Router catches, sends generic error. Rate limit was already debited — unfair but acceptable at this scale.

**Step 12 — spend cap exceeded.** `SpendCapExceededError`. Router sends the spend-cap message. Rate limit was already debited.

**Step 15 — state TTL expires mid-session (no refresh on doubt).** If Archit spent 110 minutes typing doubts without answering, state TTL (set 2h ago at session start) would expire, and next doubt or answer would find state=None. Handler would say "session already complete". Mitigation: add `set_state(tg_id, state)` at end of doubt handler to refresh TTL on activity. Tracked as future work, not a v4.1 blocker.

---

## Cross-path observations

Reading both traces, some patterns emerge:

1. **Lock → load profile → load state → dispatch is the same prelude in every path.** That's 4 Redis ops + 2 Postgres reads on every incoming message, ~100ms overhead before any handler runs. Cacheable later (the `profile:{tg_id}` Redis cache is invalidated correctly but never read — easy win once needed).

2. **Only `record_attempt` does 4+ DB writes in a single request.** Everything else is 1-2 writes. Schema could colocate some of these into one round trip if latency ever matters.

3. **The reranker is the cheapest step in retrieval.** Pure Python. The embedding call dominates retrieval latency (~400ms vs ~30ms for the pgvector search).

4. **State mutation is always the last thing a handler does before sending to Telegram.** This is intentional: if Telegram send fails, state reflects the user's actual position (they didn't see the next question), and a retry won't double-advance.

5. **Rate limit is debited once per free-text message, never on commands or callbacks.** Button-based practice is effectively rate-limit-free. Free-text doubt/concept is the only way to burn the 50/day quota.
