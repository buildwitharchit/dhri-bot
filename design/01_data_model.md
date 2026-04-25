# Data Model — DHRI VARC Bot v4.1

## Overview

Three tiers of state, each with a different lifetime and purpose:

1. **Durable (Postgres/Neon)** — users, questions, attempts, sessions, messages. Survives forever unless explicitly deleted.
2. **Ephemeral (Upstash Redis)** — per-user session state, request locks, rate limit counters, daily spend counter. TTL'd; can vanish without corrupting the system.
3. **Transient (in-memory Python objects)** — selector results, reranker tuples, agent contexts. Exist only for the duration of one HTTP request.

The governing principle: **Redis holds only IDs and lightweight counters, never authoritative text.** If Redis disappears, every user loses their live session but no answer history, skill score, or question content is lost. Postgres is the system of record.

A second principle: **the same 18-subskill taxonomy is referenced everywhere** — config, ingest tagger, retrieval queries, scoring, stats. The taxonomy is consistency-checked at startup in [`main.py`](../main.py) (raises `RuntimeError` on mismatch between `ALL_SUBSKILLS` and `SUBSKILL_TO_TECHNIQUE_QUERY`).

---

## Working Memory (Redis / Upstash)

Ephemeral state and coordination primitives. All keys have explicit TTLs; the system works correctly when any of them expire or are evicted.

### Key: `state:tg:{tg_id}`

**Purpose.** Holds the active session for a user — which mode, which question set, how far through they are, what they've answered so far. Uniform structure across RC, PJ, VA so the same resume/cleanup code handles every mode.

**Structure (uniform across all modes):**

```json
{
  "state": "RC_ACTIVE",
  "session_id": "f8e7d6c5-...-uuid",
  "mode": "rc",
  "passage_id": "cat_pyq_bandicoots",
  "questions_in_set": ["bandicoots_q1", "bandicoots_q2", "bandicoots_q3", "bandicoots_q4"],
  "current_question_index": 2,
  "questions_answered": {
    "bandicoots_q1": {"selected": "C", "correct": true,  "trap": "none"},
    "bandicoots_q2": {"selected": "A", "correct": false, "trap": "out_of_scope"}
  },
  "questions_remaining": ["bandicoots_q3", "bandicoots_q4"],
  "session_started_at": "2026-04-21T10:30:00+00:00"
}
```

**State values** (`state` field):
- `IDLE` — no active practice session
- `ONBOARD_YEAR`, `ONBOARD_LEVEL` — mid-onboarding
- `RC_ACTIVE`, `PJ_ACTIVE`, `VA_ACTIVE` — in a practice session

**Mode-specific field differences:**
- **RC**: `passage_id` set, `questions_in_set` has 2-4 IDs
- **PJ**: `passage_id: null`, `questions_in_set` has exactly 1 ID
- **VA**: `passage_id: null`, `questions_in_set` has exactly 1 ID, plus extra `va_type` field

**TTL.** 7200 seconds (2 hours). Reset on every user message via `set_state()`.

**Written by.** [`handlers/onboarding.py`](../handlers/onboarding.py), [`handlers/home.py`](../handlers/home.py), all three practice handlers ([`rc.py`](../handlers/practice/rc.py), [`pj.py`](../handlers/practice/pj.py), [`va.py`](../handlers/practice/va.py)), and [`handlers/session_cleanup.py`](../handlers/session_cleanup.py) (clears on stale close).

**Read by.** [`bot/router.py`](../bot/router.py) to decide which handler owns the message, [`bot/free_text.py`](../bot/free_text.py) for intent classification, every practice handler on answer processing.

**Critical invariant (FIX 7).** The current question is always
`state["questions_in_set"][state["current_question_index"]]`. There is no
`state["current_question_id"]` field. The PJ handler was specifically patched to
respect this.

**Recovery path.** If this key is missing mid-session but the session is still open in Postgres, [`db/queries.py::get_state_from_db_or_redis`](../db/queries.py) rehydrates it from `session_snapshots`.

---

### Key: `lock:user:{tg_id}`

**Purpose.** Serializes updates for a given user. Telegram can deliver multiple updates for the same user within milliseconds (rapid taps); this lock ensures only one is processed at a time. The second one is *dropped*, not queued.

**Value.** The literal string `"1"`.

**TTL.** 5 seconds. Set atomically via `SET NX EX 5`. Released in `finally` block by [`bot/router.py`](../bot/router.py).

**Written by.** [`bot/router.py::route_update`](../bot/router.py) — `acquire_lock(f"lock:user:{tg_id}", 5)`.

**Read by.** Nobody. Existence-checked only.

**Failure mode.** If a handler crashes without releasing the lock, the user is frozen out for up to 5 seconds. Not blocking — the `finally` block in `route_update` ensures the lock is released even on exception.

---

### Key: `lock:practice:{tg_id}`

**Purpose.** Longer lock held across a multi-step practice interaction (e.g. a full RC passage load). Distinct from `lock:user:{tg_id}` to allow separate tuning.

**Value.** `"1"`.

**TTL.** 30 seconds.

**Currently used by.** Reserved for practice-handler-level coordination; defined in the Redis schema (Section 6 of spec) but not yet acquired at any call site. Practice flows use only the user-level lock today.

---

### Key: `rl:msg:{tg_id}:{date_ist}`

**Purpose.** Daily counter for free-text LLM-triggering messages. Gate at 50 per user per calendar day (IST). Commands, inline button taps, and static responses do NOT count.

**Value.** An integer incremented via Redis `INCR`.

**TTL.** Seconds until next midnight IST (calculated by [`bot/utils.py::get_seconds_until_ist_midnight`](../bot/utils.py)). Applied only on the first `INCR` of the day.

**Example key.** `rl:msg:12345678:2026-04-21`

**Written by.** [`db/queries.py::check_and_increment_rate_limit`](../db/queries.py).

**Read by.** Same function. Returns `(allowed: bool, count: int)`.

**Behavior when `count >= 50`.** The function returns `(False, count)` without incrementing. Caller sends the rate-limit message. On cap, commands and buttons still work because neither debits this counter.

---

### Key: `profile:{tg_id}`

**Purpose.** Cached `user_profiles` row (trap_counts, most_common_trap, current_streak, etc.) to avoid hitting Postgres on every turn.

**Value.** JSON-encoded user_profiles row.

**TTL.** 1800 seconds (30 minutes). Invalidated (deleted) on every write to `user_profiles` — see [`memory/profile.py::update_skill_score`](../memory/profile.py) and `update_trap_counts`.

**Status.** Defined in spec Section 6. Current v4.1 profile reads go directly through [`db/queries.py::get_or_create_user`](../db/queries.py); this cache is reserved for a later optimization pass and is actively invalidated but not actively read yet.

---

### Key: `spend:{date_iso}`

**Purpose.** Running USD total of LLM costs today. Checked before every chat completion and embedding call; if adding the estimated cost would exceed `DAILY_LLM_SPEND_CAP_USD`, the call raises `SpendCapExceededError`.

**Value.** A float encoded as a decimal string. Incremented by each call's estimated cost (pre-flight) or actual cost (from `usage.total_tokens` in the response).

**TTL.** 3,024,000 seconds (35 days). Long TTL so we can run a monthly report retroactively.

**Example.** `spend:2026-04-21 = "0.2847"`

**Written by.** [`agent/llm.py::_add_spend`](../agent/llm.py).

**Read by.** [`agent/llm.py::_get_spend_today`](../agent/llm.py) and `_check_spend_cap`.

**Units.** Always USD. `DAILY_LLM_SPEND_CAP_USD` default is `0.50`.

**Accuracy.** Approximate. The cost estimate uses a conservative $/Mtoken table and `len(text)//4` as a token estimate; it's reconciled with `resp.usage.total_tokens` when the API returns it. Not accounting-grade.

---

## Durable Data (Postgres / Neon)

Row-oriented storage of users, content, attempts, and conversations. All timestamps are `TIMESTAMP` (naive UTC) — write as `datetime.now(timezone.utc).replace(tzinfo=None)` when binding explicitly. `TIMESTAMP DEFAULT now()` columns are handled by the DB.

### Table: `tg_users`

**Purpose.** One row per Telegram user. Identity + onboarding answers + ban state. This table's primary key (`tg_id`) is the foreign key target for nearly everything else.

**Columns.**

| Column           | Type           | Meaning                                           |
|------------------|----------------|---------------------------------------------------|
| `tg_id`          | `BIGINT` PK    | Telegram numeric user ID (permanent, non-reusable)|
| `username`       | `VARCHAR(64)`  | @handle at time of last contact, nullable         |
| `first_name`     | `VARCHAR(64)`  | As set by user in Telegram, nullable              |
| `target_year`    | `SMALLINT`     | 2025/2026/2027 — set during onboarding            |
| `experience`     | `VARCHAR(20)`  | `beginner` / `intermediate` / `advanced`          |
| `is_banned`      | `BOOLEAN`      | Default `false`; toggled via `/ban` admin command |
| `ban_reason`     | `TEXT`         | Free-text note                                    |
| `joined_at`      | `TIMESTAMP`    | First `/start`                                    |
| `last_active_at` | `TIMESTAMP`    | Refreshed on every `get_or_create_user` call      |

**Example row.**

```
tg_id:          12345678
username:       archit_s
first_name:     Archit
target_year:    2026
experience:     intermediate
is_banned:      false
joined_at:      2026-04-20 10:15:00
last_active_at: 2026-04-21 09:22:33
```

**Lifecycle.** Created on first `/start`. Never deleted. Onboarding columns filled over two inline-keyboard taps after `/start`.

---

### Table: `user_skill_scores`

**Purpose.** The adaptive engine. One row per (user × subskill) holding an EWMA score between 0.0 and 1.0. Scores are tracked at the **18-subskill level** (internal retrieval granularity), not at the 7 student-facing skills (those are aggregated at query time).

**Columns.**

| Column          | Type          | Meaning                                   |
|-----------------|---------------|-------------------------------------------|
| `tg_id`         | `BIGINT` FK   | References `tg_users`                     |
| `subskill`      | `VARCHAR(40)` | One of 18 values in `ALL_SUBSKILLS`       |
| `score`         | `FLOAT`       | EWMA of correctness, seeded 0.5           |
| `attempts_count`| `INTEGER`     | How many attempts rolled into this score  |
| `updated_at`    | `TIMESTAMP`   | Last attempt that moved this score        |
| **PK**          | `(tg_id, subskill)` | Composite                           |

**EWMA formula.** On each attempt (`memory/profile.py::update_skill_score`):

```
new_score = old_score * (1 - ALPHA) + (1.0 if correct else 0.0) * ALPHA
         where ALPHA = 0.15
```

Half-life ≈ 4.3 attempts — recent performance moves the needle quickly but ancient history still matters.

**Example row.**

```
tg_id:           12345678
subskill:        inference_basic
score:           0.4821
attempts_count:  17
updated_at:      2026-04-21 09:20:10
```

**Seeding.** On first `/start`, [`db/queries.py::initialize_skill_scores`](../db/queries.py) inserts 18 rows at `score = 0.5`, one per subskill.

**Indexes.** `idx_skill_scores_tg` on `(tg_id, score)` — supports the "weakest subskill in group" query, which is the hottest retrieval-path query.

---

### Table: `user_profiles`

**Purpose.** One row per user. Holds everything that's a *property of the student* rather than a property of their (user × skill) pair: trap counts, streaks, difficulty level, total counts, and the aggregated weakest student-facing skill.

**Columns.**

| Column               | Type           | Meaning                                                       |
|----------------------|----------------|---------------------------------------------------------------|
| `tg_id`              | `BIGINT` PK FK | One row per user                                              |
| `trap_counts`        | `JSONB`        | `{"half_right_half_wrong": 8, "out_of_scope": 3, ...}`        |
| `most_common_trap`   | `VARCHAR(40)`  | argmax of `trap_counts`, default `none`                       |
| `current_difficulty` | `VARCHAR(10)`  | `easy` \| `medium` \| `hard`, default `medium`                |
| `current_streak`     | `INTEGER`      | Consecutive days with at least one attempt                    |
| `longest_streak`     | `INTEGER`      |                                                               |
| `last_practice_date` | `DATE`         |                                                               |
| `total_attempts`     | `INTEGER`      |                                                               |
| `total_correct`      | `INTEGER`      |                                                               |
| `total_sessions`     | `INTEGER`      |                                                               |
| `weakest_skill`      | `VARCHAR(40)`  | One of 7 `STUDENT_SKILLS`, NULL until 10 total attempts       |
| `updated_at`         | `TIMESTAMP`    |                                                               |

**Example row.**

```
tg_id:              12345678
trap_counts:        {"half_right_half_wrong": 8, "out_of_scope": 3, "too_extreme": 2}
most_common_trap:   half_right_half_wrong
current_difficulty: medium
current_streak:     4
longest_streak:     11
last_practice_date: 2026-04-21
total_attempts:     42
total_correct:      27
total_sessions:     9
weakest_skill:      inference
updated_at:         2026-04-21 09:20:10
```

**Weakest-skill logic.** Set to NULL until `total_attempts >= MIN_ATTEMPTS_FOR_WEAKEST_SKILL (=10)`. After that, [`memory/profile.py::get_weakest_student_skill`](../memory/profile.py) groups the 18 subskill scores into 7 student-facing skills (via `SUBSKILL_TO_SKILL`), averages within each bucket, and returns the bucket with the lowest average. Used by the home screen to show a "Work on [X] ⚡" chip.

**Lifecycle.** Created by `get_or_create_user` on first `/start`. Updated on every attempt and on trap-fall events.

---

### Table: `passages`

**Purpose.** One row per RC passage. Referenced by `questions.passage_id` for `rc_question` rows. Not used for PJ or VA (those store inline sentences or source_text).

**Columns.**

| Column       | Type          | Meaning                                                     |
|--------------|---------------|-------------------------------------------------------------|
| `passage_id` | `VARCHAR(60)` PK | e.g. `cat_pyq_bandicoots`                                |
| `full_text`  | `TEXT`        | Complete passage, typically 400–600 words                   |
| `word_count` | `INTEGER`     | Computed at ingest                                          |
| `topic`      | `VARCHAR(60)` | `conservation_biology`, `economics_methodology`, etc.       |
| `tone`       | `VARCHAR(30)` | From the restricted tone vocabulary (see system prompt)     |
| `source`     | `VARCHAR(20)` | `cat_official` \| `mock` \| `custom` \| `agent_generated`   |
| `year`       | `SMALLINT`    | Source year, e.g. 2024                                      |
| `difficulty` | `VARCHAR(10)` | `easy` \| `medium` \| `hard`                                 |
| `is_active`  | `BOOLEAN`     | Soft delete flag                                             |
| `created_at` | `TIMESTAMP`   |                                                              |

**Seed count.** 8 passages from CAT 2024 Slot 1 and CAT 2023 Slot 1.

---

### Table: `questions`

**Purpose.** One row per question. The richest table — 28 columns because it has to accommodate 9 question types, store trap taxonomy, and hold a 1536-dim pgvector embedding for retrieval.

**Columns.**

| Column                | Type             | Meaning                                                        |
|-----------------------|------------------|----------------------------------------------------------------|
| `question_id`         | `VARCHAR(60)` PK | Globally unique, human-readable                                |
| `type`                | `VARCHAR(30)`    | One of 9: `rc_question`, `pj`, `va_grammar`, `va_vocab`, etc.  |
| `passage_id`          | `VARCHAR(60)` FK | Set only for `rc_question`                                     |
| `source_text`         | `TEXT`           | Paragraph for `va_summary` and `va_sentence_insertion` only    |
| `question_text`       | `TEXT`           | The actual stem                                                |
| `options`             | `JSONB`          | `{"A": "...", "B": "...", ...}` — null for PJ                  |
| `correct_option`      | `VARCHAR(1)`     | Letter — null for PJ                                           |
| `correct_order`       | `VARCHAR(10)`    | `"4,1,2,3"` — set for PJ, nullable otherwise                   |
| `explanation`         | `TEXT`           |                                                                |
| `rc_question_type`    | `VARCHAR(30)`    | RC-specific: `main_idea`, `inference`, etc.                    |
| `sentences`           | `JSONB`          | For PJ and `va_wrong_one_out`: `{"1": "...", "2": "..."}`      |
| `connector_type`      | `VARCHAR(30)`    | PJ: e.g. `contrast_then_clarification`                         |
| `opening_clue`        | `TEXT`           | PJ: free-text hint                                             |
| `pj_connector_map`    | `JSONB`          | PJ: per-sentence connector analysis                            |
| `skill`               | `VARCHAR(40)`    | Student-facing, one of 7                                       |
| `subskill`            | `VARCHAR(40)`    | Internal, one of 18                                            |
| `traps_present`       | `TEXT[]`         | Subset of 8 trap values                                        |
| `option_traps`        | `JSONB`          | `{"A": "too_extreme", "B": null, ...}` — key trap signal       |
| `one_line_technique`  | `TEXT`           | Single-sentence cognitive anchor, feeds the embedding          |
| `taxonomy_version`    | `SMALLINT`       | Currently `1`                                                  |
| `tagged_at`           | `TIMESTAMP`      | Naive UTC — set to `datetime.now(timezone.utc).replace(tzinfo=None)` |
| `tagger_model`        | `VARCHAR(80)`    | `manual_pyq_v4` for seed; model slug otherwise                 |
| `technique_embedding` | `vector(1536)`   | text-embedding-3-small output, indexed via HNSW                |
| `difficulty`          | `VARCHAR(10)`    |                                                                |
| `source`              | `VARCHAR(20)`    |                                                                |
| `year`                | `SMALLINT`       |                                                                |
| `question_order`      | `SMALLINT`       | RC: 1..4 within a passage; null otherwise                      |
| `is_active`           | `BOOLEAN`        | Soft delete                                                    |
| `needs_review`        | `BOOLEAN`        | **CRITICAL** — excluded from retrieval while `true`            |
| `manually_tagged`     | `BOOLEAN`        | `true` for seed PYQs; `false` for tagger output                |
| `created_at`          | `TIMESTAMP`      |                                                                |

**Example row (abbreviated).**

```
question_id:         cat_pyq_bandicoots_q1
type:                rc_question
passage_id:          cat_pyq_bandicoots
question_text:       Which one of the following statements provides a gist of this passage?
options:             {"A": "The onslaught...", "B": "Marsupials...", "C": "...", "D": "..."}
correct_option:      C
skill:               main_idea
subskill:            main_idea_full_passage
traps_present:       {too_extreme, out_of_scope, half_right_half_wrong}
option_traps:        {"A": "too_extreme", "B": "out_of_scope", "C": null, "D": "half_right_half_wrong"}
one_line_technique:  Main idea must cover both halves of the passage — the problem AND the response...
technique_embedding: [-0.0234, 0.0198, ..., -0.0056]  -- 1536 dims
difficulty:          medium
needs_review:        false
```

**The embedding anchor.** Built by `ingest/embedder.py::build_embed_text` as the 3-line string:

```
<one_line_technique>
Skill: <subskill>
Trap: <primary trap or 'none'>
```

Only this is embedded; the question text itself is not.

**`needs_review` gate.** Three questions in the 48-PYQ seed are flagged (`cat_pyq_crafts_q4`, `cat2023s1_indian_ocean_q3`, `cat2023s1_geography_q1`). Every retrieval SQL in [`retrieval/selector.py`](../retrieval/selector.py) includes `AND q.needs_review = false`. This is **FIX 2** — without it, flagged questions reach students.

**Indexes.**
- `idx_questions_type_difficulty (type, difficulty, is_active)`
- `idx_questions_skill (skill, difficulty) WHERE is_active`
- `idx_questions_subskill (subskill, difficulty) WHERE is_active`
- `idx_questions_review (needs_review) WHERE needs_review` — partial, supports admin queue
- `idx_questions_embedding` — HNSW on `technique_embedding vector_cosine_ops`

---

### Table: `sessions`

**Purpose.** One row per practice session (RC / PJ / VA) or free-text conversation. Created at session start, closed either by the user completing the set or by the cron cleanup.

**Columns.**

| Column                | Type        | Meaning                                          |
|-----------------------|-------------|--------------------------------------------------|
| `session_id`          | `UUID` PK   | Generated by `uuid_generate_v4()`                |
| `tg_id`               | `BIGINT` FK |                                                  |
| `mode`                | `VARCHAR(20)` | `rc` \| `pj` \| `va` \| `doubt` \| `concept`  |
| `started_at`          | `TIMESTAMP` |                                                  |
| `ended_at`            | `TIMESTAMP` | NULL while open                                  |
| `last_active_at`      | `TIMESTAMP` | Refreshed on every attempt                       |
| `duration_mins`       | `INTEGER`   | Computed on close                                |
| `was_completed`       | `BOOLEAN`   | `true` if user finished the set; `false` if timed out |
| `questions_attempted` | `INTEGER`   |                                                  |
| `questions_correct`   | `INTEGER`   |                                                  |
| `skills_practiced`    | `TEXT[]`    | Set of student-facing skill names                |
| `summary`             | `TEXT`      | 3-sentence AI summary, see `memory/summarizer.py`|
| `created_at`          | `TIMESTAMP` |                                                  |

**Lifecycle.**
1. Created by any practice-start handler via [`db/queries.py::create_session`](../db/queries.py).
2. Updated on every attempt via [`handlers/practice/common.py::record_attempt`](../handlers/practice/common.py) — which increments `questions_attempted`, `questions_correct`, and appends to `skills_practiced`.
3. Closed by either the last question's handler (natural end) or the 10-minute cron [`handlers/session_cleanup.py::cleanup_stale_sessions`](../handlers/session_cleanup.py) after 2 hours of inactivity.

**Indexes.**
- `idx_sessions_tg_id (tg_id, started_at DESC)` — history lookups
- `idx_sessions_open (tg_id, last_active_at) WHERE ended_at IS NULL` — cleanup scan

---

### Table: `session_snapshots`

**Purpose.** Persists the Redis state of resumable sessions (`rc`, `pj`, `va`) at cleanup time, so `/resume` can restore them even after Redis expires. Only written for modes in `RESUMABLE_MODES`.

**Columns.**

| Column                | Type          | Meaning                                       |
|-----------------------|---------------|-----------------------------------------------|
| `session_id`          | `UUID` PK FK  | One snapshot per session                      |
| `tg_id`               | `BIGINT` FK   |                                               |
| `current_mode`        | `VARCHAR(20)` | Mirror of state.mode                          |
| `current_question_id` | `VARCHAR(60)` | Derived: `questions_in_set[current_index]`    |
| `passage_id`          | `VARCHAR(60)` |                                               |
| `questions_in_set`    | `TEXT[]`      |                                               |
| `questions_answered`  | `JSONB`       |                                               |
| `questions_remaining` | `TEXT[]`      |                                               |
| `snapped_at`          | `TIMESTAMP`   |                                               |

**Lifecycle.** Written by [`db/queries.py::write_session_snapshot`](../db/queries.py) as part of the session cleanup flow. Read by `/resume` to reconstruct the live `state` dict. Upserted — a single session has at most one snapshot.

---

### Table: `messages`

**Purpose.** Chat transcript — every user turn, every assistant turn, every system note tied to a session. Supports the doubt/concept LLM flows (which need recent history) and future analytics.

**Columns.**

| Column          | Type          | Meaning                                   |
|-----------------|---------------|-------------------------------------------|
| `id`            | `UUID` PK     |                                           |
| `session_id`    | `UUID` FK     |                                           |
| `tg_id`         | `BIGINT` FK   |                                           |
| `tg_message_id` | `BIGINT`      | Telegram's message ID (for future edits)  |
| `role`          | `VARCHAR(10)` | `user` \| `assistant` \| `system`         |
| `content`       | `TEXT`        |                                           |
| `message_type`  | `VARCHAR(20)` | `text`, `passage`, `explanation`, ...     |
| `question_id`   | `VARCHAR(60)` | Set when the message is about a question  |
| `created_at`    | `TIMESTAMP`   |                                           |

**Example row.**

```
id:           uuid
session_id:   <rc session uuid>
tg_id:        12345678
role:         assistant
content:      "✅ Correct. The answer is C. Both halves of the passage..."
message_type: explanation
question_id:  cat_pyq_bandicoots_q1
created_at:   2026-04-21 10:33:01
```

**Indexes.**
- `idx_messages_session (session_id, created_at ASC)` — for history reconstruction
- `idx_messages_tg_id (tg_id, created_at DESC)` — user-level scans

---

### Table: `attempts`

**Purpose.** One row per question attempt. Primary source of truth for scoring, "already seen" retrieval exclusion, trap tracking, and weekly stats.

**Columns.**

| Column            | Type          | Meaning                                       |
|-------------------|---------------|-----------------------------------------------|
| `id`              | `UUID` PK     |                                               |
| `tg_id`           | `BIGINT` FK   |                                               |
| `session_id`      | `UUID` FK     |                                               |
| `question_id`     | `VARCHAR(60)` FK |                                            |
| `selected_option` | `VARCHAR(10)` | Letter for MCQ, digit-string for PJ           |
| `correct_option`  | `VARCHAR(10)` | Denormalized — what was correct at attempt time |
| `is_correct`      | `BOOLEAN`     |                                               |
| `trap_fallen_for` | `VARCHAR(40)` | Derived from `option_traps[selected]`         |
| `pj_mistake_type` | `VARCHAR(40)` | PJ only: `wrong_opener`, `wrong_closer`, `middle_order`, etc. |
| `is_reattempt`    | `BOOLEAN`     | Reserved for retry mode                       |
| `time_taken_secs` | `INTEGER`     | Optional                                      |
| `attempted_at`    | `TIMESTAMP`   |                                               |

**Example row (wrong answer to an RC question).**

```
id:              uuid
tg_id:           12345678
session_id:      <rc session uuid>
question_id:     cat_pyq_bandicoots_q1
selected_option: A
correct_option:  C
is_correct:      false
trap_fallen_for: too_extreme
pj_mistake_type: null
is_reattempt:    false
attempted_at:    2026-04-21 10:32:45
```

**Indexes.**
- `idx_attempts_tg_id (tg_id, attempted_at DESC)` — history & week stats
- `idx_attempts_session (session_id)` — session summary
- `idx_attempts_tg_question (tg_id, question_id)` — seen-exclusion lookup

**Used by retrieval.** [`retrieval/selector.py::get_seen_question_ids`](../retrieval/selector.py) does `SELECT DISTINCT question_id FROM attempts WHERE tg_id = $1` and excludes those from pgvector candidates via `question_id != ALL($seen)`.

---

### Table: `feedback`

**Purpose.** User-submitted bug reports or complaints about specific questions. Review queue for the admin.

**Columns.**

| Column        | Type           | Meaning                         |
|---------------|----------------|---------------------------------|
| `id`          | `UUID` PK      |                                 |
| `tg_id`       | `BIGINT` FK    |                                 |
| `question_id` | `VARCHAR(60)` FK | Nullable if not question-specific |
| `session_id`  | `UUID` FK      |                                 |
| `message`     | `TEXT`         |                                 |
| `is_resolved` | `BOOLEAN`      |                                 |
| `created_at`  | `TIMESTAMP`    |                                 |

**Lifecycle.** Written by `/feedback` command handler. Reviewed in admin Streamlit panel.

---

## Transient Objects (in-process, request-scoped)

Not persisted anywhere. Exist only for the lifetime of one HTTP request. Documented here because they're passed between functions and have implicit shapes.

### Object: `profile` (dict)

Produced by [`db/queries.py::get_or_create_user`](../db/queries.py). A dict merging `tg_users` + `user_profiles` rows. Shape:

```python
{
  "tg_id": int,
  "username": str | None,
  "first_name": str | None,
  "target_year": int | None,
  "experience": str | None,
  "is_banned": bool,
  "trap_counts": dict,
  "most_common_trap": str,          # 'none' for new users
  "current_difficulty": str,        # 'easy' | 'medium' | 'hard'
  "current_streak": int,
  "weakest_skill": str | None,      # None until 10+ attempts
  "total_attempts": int,
  "total_correct": int,
  # ... other user_profiles columns
}
```

Passed into every handler. Treated as read-mostly — writes go through `memory/profile.py` helpers, which invalidate the optional profile cache.

---

### Object: `state` (dict)

The deserialized `state:tg:{tg_id}` Redis value. See "Working Memory — state:tg:{tg_id}" above for the full structure.

Passed into handlers alongside `profile`. Mutated by handlers (`state["current_question_index"] += 1`, etc.) and written back via `set_state(tg_id, state)`.

---

### Object: `PracticeSelector` result

Return value of [`retrieval/selector.py`](../retrieval/selector.py) methods. Two shapes depending on mode.

**For `get_rc_passage`:**

```python
{
  "passage": {                  # row from passages, as dict
    "passage_id": "cat_pyq_bandicoots",
    "full_text": "...",
    "topic": "conservation_biology",
    # ... etc
  },
  "questions": [                # list of question dicts, up to 4
    {"question_id": "...", "type": "rc_question", ...},
    ...
  ]
}
```

**For `get_pj` and `get_va`:** a single question dict (row from `questions`, as dict).

In both cases, `None` is returned if no matching content exists after running the full fallback chain.

---

### Object: reranker candidate tuple

Internal to [`retrieval/reranker.py::rerank`](../retrieval/reranker.py). A list of `(composite_score: float, question_dict)` tuples sorted descending. Not exposed — only `[q for _, q in scored]` leaks out.

---

### Object: `intent` (Literal string)

Output of [`agent/classifier.py::classify_free_text`](../agent/classifier.py):

- `"pj_answer"` — 4 distinct digits from {1-4} detected while in `PJ_ACTIVE`
- `"concept"` — message starts with `how do i`, `what is`, `explain`, etc.
- `"doubt"` — default

This is a deterministic classifier, no LLM. Kept cheap so it can run on every free-text message before rate-limit debit.

---

### Object: agent response / LLM reply (string)

Plain text returned by [`agent/llm.py`](../agent/llm.py) call paths:
- `llm_call_with_retry(system, user, model) -> str`
- `llm_call_with_retry_messages(system, messages, model) -> str`
- `embed(text) -> list[float]`

No structured envelope. Text is HTML-formatted per [`agent/prompts.py::SYSTEM_PROMPT_TEMPLATE`](../agent/prompts.py) teaching rule #6 (`<b>` and `<i>` tags only).

---

## Data-flow invariants

Properties that must hold across the whole system. Violations indicate bugs.

1. **Every `question_id` in `attempts.question_id` exists in `questions.question_id`.** Enforced by FK.
2. **Every `passage_id` in `questions.passage_id` (when not null) exists in `passages.passage_id`.** Enforced by FK.
3. **`user_skill_scores` has exactly `len(ALL_SUBSKILLS) == 18` rows per user after onboarding.** Enforced by `initialize_skill_scores` + the ON CONFLICT DO NOTHING.
4. **`state.current_question_index` is always in `[0, len(questions_in_set))`** unless the session is done, in which case it equals `len(questions_in_set)`.
5. **`state.questions_remaining ⊆ state.questions_in_set` and disjoint from `state.questions_answered.keys()`.**
6. **Retrieval SQL always filters `needs_review = false`.** Any new retrieval path must add this.
7. **Every call to `embed`, `llm_call_with_retry*`, or any `async def` function is awaited.** FIX 6 — grep-auditable via `scripts/preflight.py` Check 9.
8. **Subskill validity:** `(questions.type, questions.subskill)` must be a legal pair per `SKILL_TYPE_MATRIX`. Ingest validation flips `needs_review = true` on violation (FIX 8).
9. **Taxonomy consistency:** `set(ALL_SUBSKILLS) == set(SUBSKILL_TO_TECHNIQUE_QUERY.keys())`. Checked at startup in `main.py`.

---

## Open questions (known TBDs)

- **Profile cache (`profile:{tg_id}`)** — defined in spec, actively invalidated on writes, but not actively read yet. Deferred to a later optimization pass when the profile hit count justifies it.
- **`lock:practice:{tg_id}`** — reserved in the Redis schema but no call site acquires it today. Kept in case future flows need longer coordination than the 5s user-level lock.
- **`sessions.duration_mins`** — computed at close, but the interval conversion assumes Postgres local TZ matches UTC. Works on Neon (UTC by default) but worth verifying before enabling non-UTC deployments.
- **`attempts.time_taken_secs`** — column exists, never populated. Reserved for a future client-side timer.
