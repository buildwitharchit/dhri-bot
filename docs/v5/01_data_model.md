# Data Model — DHRI v5

## Overview

dhri v5 is structured around four storage layers, each with a distinct purpose:

1. **Postgres (durable):** identity, conversation history, profile, episodic summaries, content
2. **Redis (ephemeral):** active session state, working memory cache, locks, counters
3. **pgvector (Postgres extension):** embedding-based retrieval for questions and (eventually) messages
4. **Existing v4 tables (kept):** questions, passages, attempts, subskills, traps — domain content

The fundamental principle: **Postgres is the source of truth. Redis is a performance cache.** If Redis goes down or evicts keys, the system rehydrates from Postgres. No data loss.

This document covers all data shapes. Relationships, examples, lifecycles. Read this before reading service contracts — it's the foundation.

---

## Postgres Schema (New Tables)

### `students`

Master identity table. Every other table FKs to this.

```
students
────────
student_id           UUID PRIMARY KEY DEFAULT gen_random_uuid()
tg_id                BIGINT UNIQUE  (nullable, set when Telegram-linked)
display_name         VARCHAR(100)
email                VARCHAR(255)   (nullable, for future web login)
created_at           TIMESTAMP DEFAULT now()
last_seen_at         TIMESTAMP DEFAULT now()
preferences          JSONB DEFAULT '{}'
deleted_at           TIMESTAMP      (nullable, for soft-delete / GDPR)
```

**Example row:**
```json
{
  "student_id": "550e8400-e29b-41d4-a716-446655440000",
  "tg_id": 123456789,
  "display_name": "Archit",
  "email": null,
  "created_at": "2026-04-25T10:00:00Z",
  "last_seen_at": "2026-04-25T15:30:00Z",
  "preferences": {
    "language": "en",
    "notification_quiet_hours": [22, 8],
    "timezone": "Asia/Kolkata"
  },
  "deleted_at": null
}
```

**Indexes:**
- `tg_id` (unique, partial where deleted_at IS NULL)
- `email` (unique, partial where email IS NOT NULL AND deleted_at IS NULL)
- `created_at`

**Lifecycle:**
- Created on first Telegram message (or first web signup later)
- Updated on every interaction (last_seen_at)
- Soft-deleted on user request (sets deleted_at, doesn't actually remove)

**Written by:** message_bus (creates row), orchestrator (updates last_seen_at), profile service (updates preferences)
**Read by:** all services (resolve student_id from tg_id)

---

### `student_profile`

Structured facts about each student. One row per student.

```
student_profile
───────────────
student_id              UUID PRIMARY KEY  (FK to students)
target_exam             VARCHAR(20) DEFAULT 'CAT'
target_year             SMALLINT          (2026, 2027, 2028)
target_colleges         TEXT[]            (e.g., ['IIM-A', 'IIM-B'])
experience_level        VARCHAR(50)       ('working_professional' | 'final_year_student' | 'college_student' | 'dropper' | 'fresher')
preparation_stage       VARCHAR(50)       ('just_starting' | 'mid_prep' | 'final_3_months' | 'revision')
hours_per_day           VARCHAR(20)       ('1-2' | '2-4' | '4-6' | '6+')
why_cat                 TEXT              (nullable, captured during onboarding)
language                VARCHAR(10) DEFAULT 'en'
timezone                VARCHAR(50) DEFAULT 'Asia/Kolkata'
onboarding_complete     BOOLEAN DEFAULT false
onboarding_step         VARCHAR(30)       (FSM state during onboarding; null when complete)
onboarding_started_at   TIMESTAMP         (nullable)
onboarding_completed_at TIMESTAMP         (nullable)
created_at              TIMESTAMP DEFAULT now()
last_updated            TIMESTAMP DEFAULT now()
```

**Example row (mid-onboarding):**
```json
{
  "student_id": "550e8400-...",
  "target_exam": "CAT",
  "target_year": 2026,
  "target_colleges": null,
  "experience_level": "working_professional",
  "preparation_stage": null,
  "hours_per_day": null,
  "why_cat": null,
  "language": "en",
  "timezone": "Asia/Kolkata",
  "onboarding_complete": false,
  "onboarding_step": "ask_preparation_stage",
  "onboarding_started_at": "2026-04-25T10:00:00Z",
  "onboarding_completed_at": null
}
```

**Example row (post-onboarding):**
```json
{
  "student_id": "550e8400-...",
  "target_exam": "CAT",
  "target_year": 2026,
  "target_colleges": ["IIM-A", "IIM-B", "IIM-C"],
  "experience_level": "working_professional",
  "preparation_stage": "mid_prep",
  "hours_per_day": "2-4",
  "why_cat": "Want to transition into product management at a tier-1 firm",
  "language": "en",
  "timezone": "Asia/Kolkata",
  "onboarding_complete": true,
  "onboarding_step": null,
  "onboarding_started_at": "2026-04-25T10:00:00Z",
  "onboarding_completed_at": "2026-04-25T10:25:00Z"
}
```

**Indexes:**
- Primary key on student_id (also FK)

**Lifecycle:**
- Row created by orchestrator on first message (with all fields null except student_id)
- Updated by onboarding FSM step-by-step
- Updated occasionally by profile service when student explicitly states facts (e.g., "I'm now targeting 2027")

**Written by:** orchestrator (FSM updates during onboarding), profile service (post-onboarding updates)
**Read by:** profile service (assembling tutor brief), orchestrator (checking onboarding status)

---

### `student_notes`

Narrative memory about each student. Free-form facts with attribution and lifecycle.

```
student_notes
─────────────
note_id              UUID PRIMARY KEY DEFAULT gen_random_uuid()
student_id           UUID NOT NULL    (FK to students, indexed)
content              TEXT NOT NULL
category             VARCHAR(30)      ('preference' | 'personality' | 'life_event' | 'pattern' | 'goal' | 'emotional')
confidence           FLOAT NOT NULL CHECK (confidence >= 0 AND confidence <= 1)
source               VARCHAR(30)      ('explicit_statement' | 'observed_behavior' | 'inferred')
source_message_id    UUID             (nullable, FK to messages)
created_at           TIMESTAMP DEFAULT now()
last_reinforced      TIMESTAMP DEFAULT now()
expires_at           TIMESTAMP        (nullable; for time-bound notes)
superseded_by        UUID             (nullable, FK to student_notes; points to newer note that replaced this)
is_active            BOOLEAN DEFAULT true
sensitive            BOOLEAN DEFAULT false  (medical, mental health, family conflict, etc.)
```

**Example notes for one student:**

```json
[
  {
    "note_id": "abc123...",
    "student_id": "550e8400-...",
    "content": "Prefers technical/scientific RC passages over humanities",
    "category": "preference",
    "confidence": 0.85,
    "source": "observed_behavior",
    "source_message_id": null,
    "created_at": "2026-04-22T14:00:00Z",
    "last_reinforced": "2026-04-25T09:30:00Z",
    "expires_at": null,
    "superseded_by": null,
    "is_active": true,
    "sensitive": false
  },
  {
    "note_id": "def456...",
    "student_id": "550e8400-...",
    "content": "Mentioned wedding in December — likely unavailable mid-Dec to early-Jan",
    "category": "life_event",
    "confidence": 0.95,
    "source": "explicit_statement",
    "source_message_id": "msg-uuid-123",
    "created_at": "2026-04-23T11:00:00Z",
    "last_reinforced": "2026-04-23T11:00:00Z",
    "expires_at": "2026-01-15T00:00:00Z",
    "superseded_by": null,
    "is_active": true,
    "sensitive": false
  },
  {
    "note_id": "ghi789...",
    "student_id": "550e8400-...",
    "content": "Falls for out-of-scope traps on inference questions when passage is comparative",
    "category": "pattern",
    "confidence": 0.92,
    "source": "observed_behavior",
    "source_message_id": null,
    "created_at": "2026-04-20T16:00:00Z",
    "last_reinforced": "2026-04-25T10:15:00Z",
    "expires_at": null,
    "superseded_by": null,
    "is_active": true,
    "sensitive": false
  }
]
```

**Indexes:**
- `student_id` (for filtering by student)
- `(student_id, is_active, last_reinforced DESC)` (composite, for ordered retrieval)
- `category` (for category-filtered queries)
- `expires_at` (for nightly expiration job)

**Lifecycle:**
- Created by profile service during extraction (typically session-end)
- Updated when reinforced (last_reinforced bumped)
- Superseded when newer contradictory note created (superseded_by set, is_active = false)
- Auto-deactivated when expires_at < now() (nightly cron)
- Confidence decays based on category (life_events stable; emotional fast decay; preferences slow decay)

**Written by:** profile service exclusively
**Read by:** profile service (assembling tutor brief), mentor agent (in observer mode)

---

### `messages`

Durable conversation history. Every turn persisted here.

```
messages
────────
message_id           UUID PRIMARY KEY DEFAULT gen_random_uuid()
student_id           UUID NOT NULL    (FK to students, indexed)
session_id           UUID             (FK to sessions, indexed, nullable for between-session messages)
role                 VARCHAR(20) NOT NULL  ('user' | 'assistant' | 'system')
content              TEXT NOT NULL
content_type         VARCHAR(20) DEFAULT 'text'  ('text' | 'button' | 'voice' | 'image')
metadata             JSONB DEFAULT '{}'
embedding            vector(1536)     (nullable, populated async for important messages)
created_at           TIMESTAMP DEFAULT now()
```

**Metadata field structure (varies by role):**

For `role='user'`:
```json
{
  "tg_message_id": 12345,
  "raw_telegram_payload": { ... },
  "intent_classification": { ... },  // populated after planner runs
  "planner_latency_ms": 1450,
  "planner_cost_usd": 0.00018
}
```

For `role='assistant'`:
```json
{
  "agent_used": "varc",
  "model_used": "anthropic/claude-haiku-4.5",
  "input_tokens": 2345,
  "output_tokens": 412,
  "total_tokens": 2757,
  "cost_usd": 0.0034,
  "generation_latency_ms": 2890,
  "context_loaded": {
    "profile": "full",
    "episodic_summaries": 2,
    "specific_messages": 0
  },
  "retrieval_used": true,
  "retrieved_question_id": "q-uuid-789",
  "fallback_tier": 1
}
```

For `role='system'` (rare; for system events like "session started"):
```json
{
  "event_type": "session_started",
  "trigger": "first_message_after_inactivity"
}
```

**Example user message:**
```json
{
  "message_id": "msg-uuid-abc",
  "student_id": "550e8400-...",
  "session_id": "sess-uuid-xyz",
  "role": "user",
  "content": "give me an inference question",
  "content_type": "text",
  "metadata": {
    "tg_message_id": 12345,
    "intent_classification": {
      "domain": "varc",
      "action": "practice_request",
      "specific_focus": "inference"
    },
    "planner_latency_ms": 1340
  },
  "embedding": null,
  "created_at": "2026-04-25T10:00:00Z"
}
```

**Indexes:**
- `(student_id, created_at DESC)` (composite, primary access pattern)
- `session_id`
- `created_at` (for time-range queries)
- HNSW index on `embedding` (for similarity search; created when embedding column becomes populated)

**Lifecycle:**
- User messages: inserted synchronously by orchestrator at start of request (BEFORE agent runs)
- Assistant messages: inserted synchronously by orchestrator after agent returns
- System messages: rare, inserted on specific events
- Embeddings: populated asynchronously after response sent. Not all messages get embedded. Selection criteria:
  - Long content (>200 chars) → embed
  - Marked emotionally significant in metadata → embed
  - Contains a question → embed
  - Otherwise skip (most "thanks" / "ok" / button presses are skipped)

**Written by:** orchestrator
**Read by:** orchestrator (loading recent turns), memory service (embedding search), eval service (replay)

---

### `sessions`

Logical conversation units. Invisible to user but tracked for analytics and episodic memory.

```
sessions
────────
session_id            UUID PRIMARY KEY DEFAULT gen_random_uuid()
student_id            UUID NOT NULL    (FK to students, indexed)
primary_agent         VARCHAR(20)      ('varc' | 'mentor' | 'mixed')
started_at            TIMESTAMP DEFAULT now()
last_activity_at      TIMESTAMP DEFAULT now()
ended_at              TIMESTAMP        (nullable while active)
end_reason            VARCHAR(30)      ('inactivity_timeout' | 'explicit_end' | 'session_switch' | 'error' | null)
message_count         INTEGER DEFAULT 0
question_count        INTEGER DEFAULT 0
correct_count         INTEGER DEFAULT 0
metadata              JSONB DEFAULT '{}'  (passage_ids covered, traps encountered, etc.)
created_at            TIMESTAMP DEFAULT now()
```

**Example active session:**
```json
{
  "session_id": "sess-uuid-xyz",
  "student_id": "550e8400-...",
  "primary_agent": "varc",
  "started_at": "2026-04-25T10:00:00Z",
  "last_activity_at": "2026-04-25T10:23:00Z",
  "ended_at": null,
  "end_reason": null,
  "message_count": 12,
  "question_count": 3,
  "correct_count": 2,
  "metadata": {
    "passages_covered": ["passage-uuid-1"],
    "subskills_practiced": ["inference_basic", "main_idea"],
    "traps_encountered": ["out_of_scope"]
  }
}
```

**Indexes:**
- `(student_id, started_at DESC)` (composite, for "recent sessions" queries)
- `(ended_at, last_activity_at)` partial where ended_at IS NULL (for inactivity cleanup)

**Lifecycle:**
- Created when first message arrives after a 2-hour gap (or first ever message)
- Updated on every turn (last_activity_at, message_count)
- Updated when question answered (question_count, correct_count)
- Closed when:
  - Inactivity > 45 minutes (cron-driven)
  - Explicit end intent ("I'm done for today")
  - Session switch (mentor → varc → mentor counts as same session if quick; full switch creates new session)
  - Error during processing

**Written by:** orchestrator (create, update), session cleanup cron (close on inactivity)
**Read by:** orchestrator (loading active session), memory service (closing session, generating summary), profile service (extraction context)

---

### `episodic_summaries`

Generated summaries of completed sessions. Searchable memory of the past.

```
episodic_summaries
──────────────────
summary_id           UUID PRIMARY KEY DEFAULT gen_random_uuid()
session_id           UUID NOT NULL    (FK to sessions, unique)
student_id           UUID NOT NULL    (FK to students, indexed)
domain               VARCHAR(20)      ('varc' | 'mentor' | 'mixed' | 'onboarding')
summary_text         TEXT NOT NULL
themes               TEXT[]           (e.g., ['inference', 'out_of_scope_traps', 'fatigue'])
key_moments          JSONB DEFAULT '{}'
performance_data     JSONB DEFAULT '{}'
embedding            vector(1536)     (nullable, populated async)
created_at           TIMESTAMP DEFAULT now()
```

**Example episodic summary:**
```json
{
  "summary_id": "es-uuid-1",
  "session_id": "sess-uuid-xyz",
  "student_id": "550e8400-...",
  "domain": "varc",
  "summary_text": "Archit completed an 8-question RC set on conservation biology, scoring 5/8 (62%). He struggled with two inference questions where he picked out-of-scope options — same trap as last Tuesday. Showed self-awareness, asking 'why do I keep doing this?' midway through. Energy was steady throughout the 23-minute session. Good engagement, slight fatigue toward the end.",
  "themes": ["inference", "out_of_scope_traps", "self_awareness", "consistency"],
  "key_moments": {
    "breakthroughs": [],
    "struggles": [
      {"question_id": "q-uuid-3", "trap": "out_of_scope", "context": "comparative passage"},
      {"question_id": "q-uuid-7", "trap": "out_of_scope", "context": "evaluative inference"}
    ],
    "metacognitive_moments": [
      {"turn": 14, "content": "asked why they fall for out-of-scope traps"}
    ]
  },
  "performance_data": {
    "questions_attempted": 8,
    "correct": 5,
    "accuracy": 0.625,
    "subskill_breakdown": {
      "inference_basic": {"attempted": 4, "correct": 2},
      "main_idea": {"attempted": 2, "correct": 2},
      "specific_detail": {"attempted": 2, "correct": 1}
    },
    "duration_seconds": 1380,
    "avg_time_per_question_seconds": 172
  },
  "embedding": null,
  "created_at": "2026-04-25T10:48:00Z"
}
```

**Indexes:**
- `(student_id, created_at DESC)` (primary access pattern)
- `domain`
- GIN index on `themes` (for tag-based filtering)
- HNSW index on `embedding` (for v2+ semantic search)

**Lifecycle:**
- Created by memory service at session-end via combined LLM call (summary + extraction)
- Embeddings populated asynchronously
- Never updated; new sessions create new summaries
- Old summaries (90+ days) may eventually be consolidated into weekly rollups (v2+)

**Written by:** memory service (session-end pipeline)
**Read by:** profile service (assembling tutor brief), orchestrator (when planner requests episodic context)

---

### `observer_events`

Event queue for the mentor's observer mode. Lightweight signals about what's happening in conversations.

```
observer_events
───────────────
event_id             UUID PRIMARY KEY DEFAULT gen_random_uuid()
student_id           UUID NOT NULL    (FK to students, indexed)
session_id           UUID             (FK to sessions, nullable)
event_type           VARCHAR(50) NOT NULL
payload              JSONB DEFAULT '{}'
created_at           TIMESTAMP DEFAULT now()
processed_at         TIMESTAMP        (nullable; null = not yet processed)
processing_result    VARCHAR(30)      (nullable; 'profile_updated' | 'no_action' | 'error')
```

**Event types:**

```
session_started
session_ended
correct_answer
wrong_answer
consecutive_correct           (3+ in a row)
consecutive_wrong             (3+ in a row)
emotional_signal_detected     (low energy, frustration, etc.)
metacognitive_question        (student asks about their own patterns)
trap_pattern_detected         (same trap multiple times)
break_detected                (long gap between messages)
explicit_preference_stated    ("I prefer technical passages")
explicit_goal_stated          ("I want to be at 90% by month-end")
out_of_scope_query            (for guardrail tracking)
```

**Example event:**
```json
{
  "event_id": "ev-uuid-1",
  "student_id": "550e8400-...",
  "session_id": "sess-uuid-xyz",
  "event_type": "wrong_answer",
  "payload": {
    "question_id": "q-uuid-3",
    "subskill": "inference_basic",
    "trap": "out_of_scope",
    "selected_option": "C",
    "correct_option": "B",
    "is_consecutive_wrong": false
  },
  "created_at": "2026-04-25T10:15:00Z",
  "processed_at": "2026-04-25T10:15:00.500Z",
  "processing_result": "no_action"
}
```

**Indexes:**
- `(processed_at, created_at)` partial where processed_at IS NULL (for queue queries)
- `(student_id, created_at DESC)` (for analytics)

**Lifecycle:**
- Created by orchestrator (or VARC agent) inline during request handling
- Processed by mentor observer (inline, after response sent) OR by background job (v2+)
- Never deleted; functions as audit log

**Written by:** orchestrator, VARC agent (via memory_deltas)
**Read by:** mentor observer (consumes events, marks processed), analytics dashboard

---

### `scheduled_messages` (skeleton table for v2; structure now, no use yet)

Placeholder table. Empty in v1 but defined so we don't need migration later.

```
scheduled_messages
──────────────────
schedule_id          UUID PRIMARY KEY DEFAULT gen_random_uuid()
student_id           UUID NOT NULL    (FK to students)
content              TEXT
content_template     TEXT             (alternative: template to compose at send time)
send_at              TIMESTAMP NOT NULL
priority             INTEGER DEFAULT 5  (1 = highest)
dedup_key            VARCHAR(100)     (prevent duplicate scheduling)
reason               VARCHAR(50)
created_at           TIMESTAMP DEFAULT now()
sent_at              TIMESTAMP        (nullable)
canceled_at          TIMESTAMP        (nullable)
canceled_reason      VARCHAR(100)     (nullable)
```

Not used in v1. Defined for v2 proactive messaging.

---

## Existing v4 Tables (Kept, Some Modifications)

### `tg_users` — DEPRECATED in v5

The `tg_users` table from v4 is replaced by `students.tg_id`. During migration:
1. For each row in `tg_users`, create a corresponding `students` row with the same tg_id
2. Drop `tg_users` table after migration verified

### `questions` — KEPT, no changes needed

Existing schema with technique fingerprints, traps, subskills.

### `passages` — KEPT, no changes needed

### `attempts` — KEPT, minor addition

Add column:
```
attempts (existing) + new column:
session_id           UUID             (FK to sessions, nullable for backward compat)
```

This links attempts to the new sessions table for analytics.

### `user_skill_scores` — KEPT, no changes needed

### `user_profiles` — RENAMED to `student_skill_profile` in v5

This v4 table has trap_counts, weakest_skill, streak data. Rename for clarity (avoids confusion with new `student_profile` table).

```
student_skill_profile (renamed from v4 user_profiles)
─────────────────────
student_id            UUID PRIMARY KEY  (was tg_id; FK to students)
trap_counts           JSONB
weakest_skill         VARCHAR(50)
weakest_subskill      VARCHAR(50)
strongest_skill       VARCHAR(50)
current_streak        INTEGER
longest_streak        INTEGER
last_active_date      DATE
total_questions       INTEGER
total_correct         INTEGER
last_updated          TIMESTAMP
```

---

## Redis Schema (Working State)

All Redis keys use the pattern `{namespace}:{identifier}` for clarity.

### Active Session Context

```
Key:    state:tg:{tg_id}
Type:   String (JSON-serialized)
TTL:    7200 seconds (2 hours), reset on every interaction
```

**Value structure:**
```json
{
  "session_id": "sess-uuid-xyz",
  "student_id": "550e8400-...",
  "active_agent": "varc",
  "started_at": "2026-04-25T10:00:00Z",
  "last_activity_at": "2026-04-25T10:23:00Z",
  "message_count_in_session": 12,
  "domain_state": {
    "passage_id": "passage-uuid-1",
    "current_question_id": "q-uuid-3",
    "questions_in_set": ["q-uuid-1", "q-uuid-2", "q-uuid-3", "q-uuid-4"],
    "questions_answered": {
      "q-uuid-1": {
        "selected": "B",
        "correct": true,
        "attempted_at": "2026-04-25T10:05:00Z"
      },
      "q-uuid-2": {
        "selected": "A",
        "correct": false,
        "attempted_at": "2026-04-25T10:11:00Z"
      }
    },
    "current_question_index": 2
  }
}
```

**Notes:**
- If key doesn't exist, no active session — first message starts one
- TTL refreshed on every write
- Lost on Redis eviction; rehydrated from sessions table + last assistant message metadata if needed

**Written by:** orchestrator (create, update), session cleanup cron (delete)
**Read by:** orchestrator (every turn)

---

### Working Memory Cache

```
Key:    memory:tg:{tg_id}
Type:   List (Redis LIST, LPUSH/LRANGE)
TTL:    86400 seconds (24 hours)
Max:    50 items (LPUSH + LTRIM 0 49)
```

**Each item is a JSON-serialized turn:**
```json
{
  "role": "user",
  "content": "give me an inference question",
  "content_type": "text",
  "timestamp": "2026-04-25T10:00:00Z",
  "message_id": "msg-uuid-abc",
  "metadata": {
    "agent_used": "varc",
    "model_used": "anthropic/claude-haiku-4.5"
  }
}
```

**Notes:**
- This is a CACHE of recent rows from the messages table
- If empty (Redis evicted), rehydrate from messages: `SELECT * FROM messages WHERE student_id = ? ORDER BY created_at DESC LIMIT 30`
- LPUSH on every turn, LTRIM to keep last 50

**Written by:** orchestrator (after persisting to messages table)
**Read by:** orchestrator (loading context for planner)

---

### Per-User Lock

```
Key:    lock:user:{tg_id}
Type:   String (just "1")
TTL:    5 seconds
SET NX: yes (only set if not exists)
```

**Purpose:** Prevent concurrent processing of the same user's messages.

**Acquisition:**
```
SET lock:user:{tg_id} 1 NX EX 5
```
If returns OK → lock acquired
If returns null → lock held, return "still processing your last message" to user

**Release:**
```
DEL lock:user:{tg_id}
```

**Notes:**
- Auto-released by TTL even if process crashes
- 5-second TTL handles >99% of legitimate processing times

**Written by:** orchestrator (acquire/release)
**Read by:** orchestrator only

---

### Rate Limit Counters

```
Key:    ratelimit:tg:{tg_id}:{date}
Type:   String (integer)
TTL:    25 hours (covers timezone variance)

Operations:
INCR    ratelimit:tg:{tg_id}:{date}
EXPIRE  ratelimit:tg:{tg_id}:{date} 90000  (only on first SET)
```

**Limits (configurable via env vars):**
- v1 testing: 500 messages/day per user
- Beta: 50-100/day per user
- Per-minute: 5 messages

**Per-minute limit:**
```
Key:    ratelimit:minute:tg:{tg_id}:{epoch_minute}
Type:   String (integer)
TTL:    120 seconds
Limit:  5 per minute
```

**Notes:**
- Soft warn at 80% of daily limit ("you've used 80% of today's quota")
- Hard block at 100% ("you've hit today's limit, come back tomorrow")
- Per-minute throttle returns "you're sending too fast, slow down a bit"

**Written by:** orchestrator (incr on every message)
**Read by:** orchestrator (check before processing)

---

### Daily Spend Counter

```
Key:    spend:{date}                        (global, all users)
Type:   String (float as string)
TTL:    25 hours
```

**Operations:**
```
INCRBYFLOAT spend:{date} 0.0034
```

**Limit:** $2.00/day in v1 (configurable via DAILY_LLM_SPEND_CAP_USD env var)

**Behavior:** When cap exceeded, orchestrator short-circuits with "DHRI is taking a quick break. Come back in a bit." Logged for investigation.

**Written by:** every service that makes an LLM call
**Read by:** orchestrator (check before LLM calls)

---

### Tutor Brief Cache (optional optimization)

```
Key:    profile:brief:{student_id}
Type:   String (the assembled tutor brief)
TTL:    1800 seconds (30 minutes)
```

**Purpose:** Avoid re-assembling tutor brief on every turn during an active session.

**Invalidation:**
- TTL expires naturally
- Profile service explicitly DELs this key when notes/profile change

**Written by:** profile service (set after assembly), profile service (delete on writes)
**Read by:** profile service (check cache first in get_tutor_brief)

---

## Transient Objects (In-Memory During Request Processing)

These objects don't persist; they exist only during the lifecycle of a single request.

### `IntentClassification`

Output of the planner LLM call. Drives routing and context fetching.

```
IntentClassification {
  intent: {
    domain: "varc" | "mentor" | "meta" | "out_of_scope" | "onboarding"
    action: "practice_request" | "answer_to_question" | "doubt_about_current" 
          | "concept_question" | "review_progress" | "vent" | "casual" 
          | "switch_topic" | "explicit_end" | "onboarding_response"
    continuation: "continues_current_session" | "switches_topic" | "new_session"
    emotional_tone: "neutral" | "low" | "high" | "stressed" | "confident" | "frustrated"
    depth: "quick_query" | "full_engagement"
    references_past: "current_question" | "earlier_in_session" | "past_session" | "none"
    specific_focus: string (nullable)  // e.g., "inference", "main_idea"
  }
  context_needs: {
    profile: "minimal" | "full" | "skip"
    episodic: {
      needed: boolean
      domains: [string] (nullable)
      topics: [string] (nullable)
      limit: integer (default 3)
    }
    specific_messages: {
      needed: boolean
      query: string (nullable)  // for embedding search
      limit: integer (default 5)
    }
  }
  response_guidance: {
    tone: "warm" | "encouraging" | "matter_of_fact" | "celebratory" | "supportive" | "firm"
    should_acknowledge_feeling: boolean
    should_reference_pattern: boolean
    session_action: "continue" | "transition" | "wrap_up" | "pause"
  }
  meta: {
    planner_model: string
    planner_latency_ms: integer
    planner_cost_usd: float
    planner_tokens_in: integer
    planner_tokens_out: integer
  }
}
```

**Lifecycle:** created in step 5 of orchestrator flow, used in steps 6-10, persisted to messages.metadata

---

### `AgentContext`

Bundle passed to agents containing everything they need to respond.

```
AgentContext {
  student_id: UUID
  tg_id: integer
  display_name: string
  
  recent_turns: [
    {
      role: string
      content: string
      timestamp: datetime
      metadata: object
    }
  ]
  
  active_session: {
    session_id: UUID (nullable)
    primary_agent: string (nullable)
    domain_state: object (nullable)
    started_at: datetime (nullable)
    message_count: integer
  }
  
  profile_brief: string (nullable)        // assembled if context_needs.profile != "skip"
  
  episodic_summaries: [                    // populated if context_needs.episodic.needed
    {
      summary_text: string
      themes: [string]
      created_at: datetime
      domain: string
    }
  ]
  
  specific_past_messages: [                // populated if context_needs.specific_messages.needed
    {
      role: string
      content: string
      created_at: datetime
      similarity_score: float
    }
  ]
  
  intent: IntentClassification.intent
  response_guidance: IntentClassification.response_guidance
  
  current_message: {
    content: string
    content_type: string
    message_id: UUID
    timestamp: datetime
  }
}
```

**Lifecycle:** assembled in step 7 of orchestrator flow, passed to agent in step 8, discarded after response

---

### `AgentResponse`

What agents return to the orchestrator.

```
AgentResponse {
  content: string                          // the actual response text
  content_type: string                     // 'text' | 'text_with_keyboard'
  keyboard: object (nullable)              // inline keyboard structure for buttons
  
  memory_deltas: {
    new_assistant_turn: object             // turn data to append
    active_context_updates: object         // partial updates to active session state
    new_session: object (nullable)         // new session row if creating
    close_session: object (nullable)       // session_id + end_reason if closing
    attempt_record: object (nullable)      // attempt to record (if answering question)
  }
  
  observer_events: [                       // events to insert
    {
      event_type: string
      payload: object
    }
  ]
  
  meta: {
    agent: string
    model_used: string
    input_tokens: integer
    output_tokens: integer
    cost_usd: float
    generation_latency_ms: integer
    retrieval_used: boolean
    retrieved_question_id: UUID (nullable)
    fallback_tier: integer (1-6)
  }
}
```

**Lifecycle:** returned by agent in step 9, processed in steps 10-13 of orchestrator flow

---

## Onboarding FSM States

The onboarding finite state machine. Tracked in `student_profile.onboarding_step`.

```
States (in order):
  null (initial)
    → start_onboarding
  
  start_onboarding
    → ask_name
  
  ask_name
    → ask_target_year
  
  ask_target_year
    → ask_experience_level
  
  ask_experience_level
    → ask_preparation_stage
  
  ask_preparation_stage
    → ask_hours_per_day
  
  ask_hours_per_day
    → ask_target_colleges (optional)
  
  ask_target_colleges
    → ask_why_cat (optional)
  
  ask_why_cat
    → diagnostic_intro
  
  diagnostic_intro
    → diagnostic_q1
  
  diagnostic_q1
    → diagnostic_q2
  
  diagnostic_q2
    → diagnostic_q3
  
  diagnostic_q3
    → diagnostic_q4
  
  diagnostic_q4
    → diagnostic_q5
  
  diagnostic_q5
    → mentor_synthesis
  
  mentor_synthesis
    → null + onboarding_complete = true
```

**Transition rules:**
- Most steps advance on user response (button press or text)
- "ask_target_colleges" and "ask_why_cat" can be skipped (button: "Skip")
- "diagnostic_intro" can be skipped (button: "Skip the test")
  - If skipped, jumps directly to mentor_synthesis with no test data
- During diagnostic_q1 through diagnostic_q5, VARC agent serves questions; FSM tracks current index
- mentor_synthesis triggers Mentor agent to review test results and propose next steps

**Diagnostic question selection:**
- Q1: easy inference
- Q2: easy main_idea or specific_detail (variety)
- Q3: medium inference
- Q4: medium specific_detail or purpose
- Q5: hard inference

This pattern surfaces both subskill performance and trap-pattern signals.

---

## VARC Retrieval Fallback Ladder

When VARC agent retrieves a question, it tries tiers in order. Each tier weakens constraints.

```
Tier 1: best match
  - Unseen by student
  - Matches subskill from intent
  - Matches difficulty (or profile-implied difficulty)
  - Bonus: matches profile signals (e.g., student's preferred passage type)

Tier 2: drop profile bonus
  - Unseen
  - Matches subskill
  - Matches difficulty

Tier 3: drop difficulty
  - Unseen
  - Matches subskill

Tier 4: drop subskill
  - Unseen
  - Any subskill (still VARC question)

Tier 5: allow stale repeats
  - Seen, but not in last 7 days
  - Matches subskill if possible

Tier 6: any seen question (last resort)
  - Any question, oldest seen first
  - Agent acknowledges: "We've done this one before — let's see if your thinking has evolved"
```

The agent records `fallback_tier` in `messages.metadata` for analytics.

If even Tier 6 returns empty (zero questions in DB matching criteria — unlikely given 48 seeded), agent gracefully responds: "I'm out of fresh material on this topic. Want to try a different VARC subskill?"

---

## Data Flow Summary

```
Student message arrives
  ↓
Bus (no DB writes)
  ↓
Orchestrator:
  1. Acquire lock (Redis)
  2. Insert user message → messages table
  3. Update last_seen_at → students
  4. Load recent turns (Redis cache, fallback Postgres)
  5. Load active session (Redis)
  6. Planner LLM call → IntentClassification
  7. Conditional fetch:
     - Profile service: get_tutor_brief OR get_minimal_brief
     - Memory service: get_episodic_summaries (filtered)
     - Memory service: embedding_search_messages
  8. Assemble AgentContext
  9. Route to agent (VARC or Mentor)
  10. Agent retrieves question (if needed) from questions table via pgvector
  11. Agent makes generation LLM call
  12. Agent returns AgentResponse
  13. Insert assistant message → messages table
  14. Apply memory_deltas:
      - Update Redis active session
      - Update sessions table (message_count, etc.)
      - Insert observer_events → observer_events table
      - Insert attempt → attempts table (if answering question)
  15. Increment ratelimit, spend counters
  16. Return response → bus
  17. Release lock
  18. (Async) Run inline mentor observer
  19. (Async) Embedding job for messages

Session-end cron (every 10 min):
  - Find sessions where last_activity_at > 45 min ago AND ended_at IS NULL
  - For each: combined LLM call generates summary + extracts notes
  - Insert episodic_summary, upsert student_notes, mark session ended
```

---

## Migration from v4

### Tables to add (new):
- students
- student_profile
- student_notes
- messages
- sessions
- episodic_summaries
- observer_events
- scheduled_messages (empty placeholder)

### Tables to modify:
- attempts: add `session_id` column
- user_profiles: rename to `student_skill_profile`, change PK from tg_id to student_id

### Tables to keep unchanged:
- questions
- passages
- subskills
- traps

### Tables to drop (after migration):
- tg_users (replaced by students.tg_id)

### Migration script outline:
1. Create new tables
2. Migrate tg_users → students
3. Add student_id to existing rows in attempts, user_profiles
4. Rename user_profiles → student_skill_profile
5. Drop tg_users
6. Verify foreign keys
7. Add indexes

---

## Open Questions / TBD

These are intentionally deferred:

- **Embedding strategy for messages:** Which messages to embed and when. Initial heuristic in code; refine based on retrieval quality.
- **Note conflict resolution:** Specific rules for when two notes contradict. Start simple (latest wins), refine as needed.
- **Episodic summary embeddings:** Skip in v1. Add when student has 50+ summaries.
- **Cross-domain semantic memory:** When mentor agent expands, may need notes scoped to domains. Defer.
- **GDPR / deletion:** soft-delete via deleted_at on students. Hard delete pipeline TBD.

---

## Appendix: Naming Conventions

- **student_id** (UUID): internal identifier
- **tg_id** (BIGINT): Telegram user ID, unique per Telegram account
- **session_id** (UUID): logical conversation unit
- **message_id** (UUID): single turn in conversation
- **summary_id** (UUID): episodic summary
- **note_id** (UUID): student note
- **event_id** (UUID): observer event

All timestamps are UTC, stored as TIMESTAMP WITH TIME ZONE.
All UUIDs use `gen_random_uuid()` for default.
All JSONB defaults are `'{}'` unless explicitly listed.
