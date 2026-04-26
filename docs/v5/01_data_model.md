# Data Model — DHRI v5

## ⚠️ Schema source-of-truth notice

**This document is the source of truth for v5 schema. Every CREATE TABLE migration must match the column list documented here EXACTLY. Every ALTER TABLE migration referenced in the slice roadmap must match the columns specified there.**

**When a slice adds a new table:** the migration's CREATE TABLE statement includes ALL columns documented for that table here, even if some columns aren't used by the current slice. This prevents schema drift.

**When this document changes:** also update `scripts/check_schema_drift.py`'s EXPECTED dict and any affected slice's roadmap migrations subsection.

**Schema drift is checked by:** `python -m scripts.check_schema_drift`. Run before starting any slice and after applying any migration. Exit code 0 = clean; 1 = drift exists.

---

## Overview

dhri v5 is structured around four storage layers, each with a distinct purpose:

1. **Postgres (durable):** identity, conversation history, profile, episodic summaries, content, LLM call observability
2. **Redis (ephemeral):** active session state, working memory cache, locks, counters
3. **pgvector (Postgres extension):** embedding-based retrieval for questions and (eventually) messages
4. **Existing v4 tables (kept):** questions, passages, attempts, subskills, traps — domain content

**Postgres schema separation:** All v5 tables live in a dedicated `v5` Postgres schema. v4 tables continue to live in `public`. v5 services qualify all table names (`v5.students`, `v5.messages`, etc.). This keeps v5 truly append-only and lets v4 stay 100% functional during the v5 build, supporting the strangler-fig migration discipline. Schema names are omitted in this document for clarity but should be applied in actual SQL (`v5.students`, `public.questions`, etc.).

The fundamental principle: **Postgres is the source of truth. Redis is a performance cache.** If Redis goes down or evicts keys, the system rehydrates from Postgres. No data loss.

This is enforced architecturally: any code path that closes a Postgres session MUST also clear the corresponding Redis state (`state:tg:{tg_id}`). This invariant is the structural fix for Bug 13 (Principle 3 violations). See `02_service_contracts.md` Architectural Principles section for the full rule.

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
onboarding_paused_at    TIMESTAMP         (nullable; set when student taps "Pause onboarding"; cleared when they resume)
diagnostic_question_count SMALLINT DEFAULT 0  (incremented during onboarding diagnostic; reaches 5 then triggers mentor synthesis)
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
tg_update_id         BIGINT           (nullable, only set on user messages; Telegram's update_id for webhook idempotency)
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
  "fallback_tier": 1,
  "response_type": "question_serve" | "answer_explanation" | "skip_explanation" | "mid_question_doubt_ack" | "continuation_prompt" | "session_resume_prompt" | "off_topic_redirect" | "session_stats" | "error_fallback",
  "tg_message_id": 56789,
  "keyboard_active": true,
  "previous_question_message_id": 56788
}
```

The `response_type` field is critical for analytics and for downstream services to know what kind of response was just sent. The `previous_question_message_id` (when present) is the tg_message_id of the question whose keyboard we should close on the next question serve (Bug 11 mitigation).

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
- `tg_update_id` (UNIQUE partial index where tg_update_id IS NOT NULL; for webhook idempotency — if Telegram retries an update, we detect the duplicate before reprocessing)
- HNSW index on `embedding` (for similarity search; created when embedding column becomes populated)

**Lifecycle:**
- User messages: inserted synchronously by orchestrator at start of request (BEFORE agent runs). The orchestrator first checks if `tg_update_id` already exists; if so, the request is a Telegram webhook retry and is short-circuited with the cached prior response.
- Assistant messages: inserted synchronously by orchestrator after agent returns
- System messages: rare, inserted on specific events
- Embeddings: populated asynchronously after response sent. Not all messages get embedded. Selection criteria:
  - Long content (>200 chars) → embed
  - Marked emotionally significant in metadata → embed
  - Contains a question → embed
  - Otherwise skip (most "thanks" / "ok" / button presses are skipped)

**Written by:** orchestrator
**Read by:** orchestrator (loading recent turns, idempotency check), memory service (embedding search), eval service (replay)

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
- Created when first message arrives after a **30-minute gap** (or first ever message). 30-min threshold chosen empirically for CAT prep behavior — see DECISIONS.md "Slice 3 verified: 30-minute session boundary".
- Updated on every turn: single UPDATE that bumps both `last_activity_at = now()` and `message_count = message_count + 1`. The two MUST move together — drift between them would indicate a bug.
- Updated when question answered (question_count, correct_count)
- Closed by either:
  - `cleanup_inactive_sessions` cron (Postgres `last_activity_at < now() - interval '30 minutes'`)
  - Explicit end intent ("I'm done for today")
  - Session switch (mentor → varc → mentor counts as same session if quick; full switch creates new session) — slice 4+
  - Error during processing

**`message_count` semantic:** counts ORCHESTRATOR TURNS, not individual messages. One turn = one user message + one assistant response = `+1`. If we later want a user-only count, that's a different column. (See DECISIONS.md "Slice 3 verification: message_count + Redis cleanup fixes".)

**Critical invariant — Redis cleanup on close:** Every code path that closes a Postgres session MUST also clear the corresponding Redis state (`state:tg:{tg_id}`). This includes `cleanup_inactive_sessions`, explicit_end handlers, and any future session-close path. Failure to clear Redis leaves stale state that resolve_session has to detect on the next message — extra latency and a subtle Bug 13 surface. The invariant lives in code: `close_session` and its callers ALL DEL Redis state. (See Architectural Principles in `02_service_contracts.md`.)

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

### `student_question_attempts`

Tracks every question served to a student, plus their answer (if given). Introduced in slice 2; supersedes the v4 `attempts` table for v5 traffic.

```
student_question_attempts
─────────────────────────
id                   UUID PRIMARY KEY DEFAULT gen_random_uuid()
student_id           UUID NOT NULL    (FK to students, indexed)
question_id          UUID NOT NULL    (FK to public.questions, indexed)
session_id           UUID             (FK to sessions, nullable for backward compat with slice 2 which predates session auto-create)
served_at            TIMESTAMP DEFAULT now()
answered_at          TIMESTAMP        (nullable; null means student hasn't answered yet)
is_correct           BOOLEAN          (nullable; null when unanswered or skipped)
student_answer       VARCHAR(10)      (nullable; null when unanswered or skipped; 'A'|'B'|'C'|'D' otherwise)
skipped              BOOLEAN DEFAULT FALSE  (true when student tapped "Skip / I don't know"; explanation still shown but no correctness recorded)
explanation_shown    BOOLEAN DEFAULT FALSE
is_diagnostic        BOOLEAN DEFAULT FALSE  (true for the 5 onboarding diagnostic questions; analytics flag)
fallback_tier        SMALLINT         (1-6, which tier of the retrieval ladder served this question)
```

**Example rows:**

```json
[
  {
    "id": "att-uuid-1",
    "student_id": "550e8400-...",
    "question_id": "q-uuid-42",
    "session_id": "sess-uuid-xyz",
    "served_at": "2026-04-25T10:05:00Z",
    "answered_at": "2026-04-25T10:06:30Z",
    "is_correct": true,
    "student_answer": "B",
    "skipped": false,
    "explanation_shown": true,
    "is_diagnostic": false,
    "fallback_tier": 2
  },
  {
    "id": "att-uuid-2",
    "student_id": "550e8400-...",
    "question_id": "q-uuid-43",
    "session_id": "sess-uuid-xyz",
    "served_at": "2026-04-25T10:08:00Z",
    "answered_at": "2026-04-25T10:08:45Z",
    "is_correct": null,
    "student_answer": null,
    "skipped": true,
    "explanation_shown": true,
    "is_diagnostic": false,
    "fallback_tier": 3
  },
  {
    "id": "att-uuid-3",
    "student_id": "550e8400-...",
    "question_id": "q-uuid-44",
    "session_id": "sess-uuid-xyz",
    "served_at": "2026-04-25T10:10:00Z",
    "answered_at": null,
    "is_correct": null,
    "student_answer": null,
    "skipped": false,
    "explanation_shown": false,
    "is_diagnostic": false,
    "fallback_tier": 1
  }
]
```

**Indexes:**
- `(student_id, served_at DESC)` — primary access (recent activity)
- `(student_id, answered_at)` partial WHERE `answered_at IS NULL` — fast "last unanswered" lookup
- `(student_id, question_id)` — duplicate-detection / "have they seen this question?" queries
- `is_diagnostic` — analytics

**Lifecycle:**
- Inserted on every question serve (one row per serve, even repeats — repeats are allowed in tier 5/6 fallback)
- Updated when student answers OR skips (answered_at, is_correct, student_answer, skipped, explanation_shown set)
- An attempt with `answered_at IS NULL` and `skipped = FALSE` represents an "open question" the student hasn't engaged with yet. There can be at most one open question per student at a time (enforced by application logic, not DB constraint).
- Skipped attempts (`skipped = TRUE`) are still considered "seen" by the retrieval ladder — the student saw the question even if they didn't answer it.

**Written by:** VARC agent (insert on serve, update on answer/skip)
**Read by:** VARC agent (retrieval ladder seen-set query, last-unanswered lookup), profile service (skill signal calculation), session-end pipeline (extraction context)

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

### `llm_calls`

Observability table for every LLM call made by any v5 service. Introduced in slice 3.

```
llm_calls
─────────
id                   UUID PRIMARY KEY DEFAULT gen_random_uuid()
student_id           UUID             (FK to students; nullable for system-level calls)
session_id           UUID             (FK to sessions; nullable when call happens outside a session)
message_id           UUID             (FK to messages; nullable; the assistant message this call produced, if any)
service              VARCHAR(30) NOT NULL  ('orchestrator' | 'varc' | 'mentor' | 'memory')
model                VARCHAR(60) NOT NULL  (e.g., 'anthropic/claude-haiku-4-5', 'anthropic/claude-sonnet-4-5')
purpose              VARCHAR(40) NOT NULL  (call-site label; see enum below)
input_tokens         INTEGER          (nullable; populated when OpenRouter response includes usage data)
output_tokens        INTEGER          (nullable; same)
cost_usd             DOUBLE PRECISION (nullable; computed from token counts × model pricing)
latency_ms           INTEGER          (nullable; wall-clock time of the call)
success              BOOLEAN NOT NULL DEFAULT true
error_message        TEXT             (nullable; populated on failure)
created_at           TIMESTAMP DEFAULT now()
```

**`purpose` enum** (extend as new call sites land):
- `intent_classification` — planner LLM call (slice 4+)
- `answer_explanation` — VARC answer scoring + explanation (slice 3+)
- `skip_explanation` — VARC explanation after student skipped (slice 3+)
- `concept_explanation` — VARC teaching response (slice 4+)
- `resume_prompt` — VARC welcome-back when returning after break (slice 3+)
- `mentor_strategic_response` — Mentor reactive response (slice 8+)
- `mentor_diagnostic_synthesis` — Mentor onboarding synthesis (slice 6+)
- `mentor_inline_observe` — Mentor observer mode (slice 8+)
- `session_end_extraction` — Memory service's session-end summary + notes extraction (slice 7+)

**Indexes:**
- `(student_id, created_at DESC)` (primary access for per-user analytics)
- `(service, model)` (for cost rollups by service/model)
- partial index `WHERE success = false` (fast lookup of failed calls)

**Lifecycle:**
- Inserted by `shared/observability/llm_log.py:record_llm_call` after every LLM call (success OR failure)
- Best-effort insertion: if Postgres write fails, the user-facing response is NOT impacted. The call is logged to stdout instead, and the metric is lost. (Per Principle 5.)
- Never updated. Never deleted. Pure append-only.

**Written by:** every service that makes LLM calls (orchestrator, VARC, mentor, memory). Always via `record_llm_call`, never via direct INSERT.
**Read by:** cost-monitoring dashboards, eval pipeline, manual analytics queries.

**Cost example:**
```sql
-- Daily cost by service
SELECT 
  service, 
  count(*) AS calls,
  round(sum(cost_usd)::numeric, 4) AS total_cost_usd,
  round(avg(latency_ms)) AS avg_latency_ms
FROM v5.llm_calls
WHERE created_at > now() - interval '1 day'
  AND success = true
GROUP BY service
ORDER BY total_cost_usd DESC;
```

---

## Existing v4 Tables (Kept, Some Modifications)

### `tg_users` — DEPRECATED in v5

The `tg_users` table from v4 is replaced by `students.tg_id`. During migration:
1. For each row in `tg_users`, create a corresponding `students` row with the same tg_id
2. Drop `tg_users` table after migration verified

### `questions` — KEPT, no changes needed

Existing schema with technique fingerprints, traps, subskills.

### `passages` — KEPT, no changes needed

### `attempts` — KEPT (v4 historical, read-only in v5)

The v4 `attempts` table holds historical attempt data from v4 traffic. It is NOT written to by v5 services. v5 introduces a fresh `student_question_attempts` table (documented above) that is the source of truth for v5 traffic.

We do NOT add a `session_id` column to v4's `attempts` (earlier plan rescinded). v4 attempts and v5 attempts are kept in parallel, both readable by analytics if needed but written only by their respective version. After v4 is fully retired (post-slice-8 quality pass), v4 `attempts` may be archived.

This split avoids destructive schema changes to v4 tables during the v5 build, supporting the strangler-fig migration discipline.

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

## Content Rendering and Parse Mode

All assistant messages delivered to Telegram use **HTML parse mode** (`parse_mode=HTML`). Implementation lives in `services/message_bus/main.py:_safe_edit_text` and `_safe_send_text`.

**Why HTML over Markdown:** Subskill names (e.g., `inference_basic`, `main_idea_full_passage`) contain underscores. Telegram's legacy Markdown parser interprets unmatched `_` as the start of an italic marker and rejects the entire message. HTML parse mode only requires escaping `<`, `>`, `&` — none of which appear in normal English text or subskill names. Far more permissive for the kind of content the bot generates. (See DECISIONS.md "Markdown rendering: switch from Markdown to HTML parse mode" for rationale.)

**Two content sources, two handling rules:**

1. **Orchestrator-composed templates** (stats, error fallback, soft-redirect, mid-question doubt ack, session resume prompt, continuation acknowledgments): may contain HTML tags `<b>`, `<i>`, `<u>`, `<s>`, `<a href>`, `<code>`, `<pre>` — these are the tags Telegram's HTML parser supports. NOT escaped before delivery. Templates are author-controlled; tags are intentional.

2. **LLM-generated content** (VARC explanations, Mentor responses, resume prompt body): HTML escape pass via `html.escape()` before delivery. The LLM's system prompt explicitly instructs it to output plain text — no markdown, no HTML — so escaping only handles edge cases where the LLM disobeys.

**Bus-side fallback:** `_safe_edit_text` and `_safe_send_text` retry without `parse_mode` if Telegram returns a parse error (BadRequest with "can't parse entities"). This ensures every message delivers, even if escape passes miss something.

**Templates that contain HTML tags should be marked in code** with a clear convention (e.g., `_html_template = ...`) so future maintainers don't confuse them with plain-text content that would need escaping.

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
  "last_question_message_id": 56789,
  "last_question_attempt_id": "att-uuid-3",
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

**Field notes:**
- `last_question_message_id` — Telegram message_id of the most recently served question. Used to remove its inline keyboard via `editMessageReplyMarkup` when a new question is served (Bug 11 mitigation: prevents stale keyboards from accumulating).
- `last_question_attempt_id` — Database ID of the most recent unanswered attempt row. Used to disambiguate stale button taps (a tap on an old question always routes to the most recent unanswered attempt).
- `domain_state` — agent-specific state. For VARC, includes current question-set context.

**Notes:**
- If key doesn't exist, no active session — first message starts one
- TTL refreshed on every write
- Lost on Redis eviction; rehydrated from sessions table + last assistant message metadata if needed
- **Cleared on session boundary:** When a new session starts (after 30+ minute gap, explicit end, or session switch), this key is DELETED before being recreated for the new session. domain_state from the closed session does NOT leak into the new session. (Bug 13 mitigation.)

**Written by:** orchestrator (create, update, clear-on-boundary), session cleanup cron (delete on inactivity timeout)
**Read by:** orchestrator (every turn), VARC agent (via context.active_session in AgentContext)

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
          | "small_talk"  // brief acknowledgments: "ok", "thanks", "got it" — does NOT trigger question serve
          | "skip_request"  // student wants to skip current question
          | "stats_request"  // mid-session "how am I doing"
          | "navigation"  // back, edit a previous answer (rare)
    continuation: "continues_current_session" | "switches_topic" | "new_session" | "stays_on_question"
    emotional_tone: "neutral" | "low" | "high" | "stressed" | "confident" | "frustrated"
    depth: "quick_query" | "full_engagement"
    references_past: "current_question" | "earlier_in_session" | "past_session" | "none"
    specific_focus: string (nullable)  // see subskill enum below
    subskill: string (nullable)  // EXACT match against question bank's subskill column; see enum below
    difficulty: "easy" | "medium" | "hard" (nullable)  // if null, profile-derived default applies
    secondary_signal: {  // (Bug 15) for mixed-intent messages like "I'm stressed, give me an easy one"
      type: "emotional_undertone" | "side_request" | null
      value: string (nullable)  // e.g., "mild_stress", "fatigue", "wants_easy"
    }
    confidence: float (0.0-1.0)
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

**Subskill enum (must match question bank exactly — Bug 22):**
- `inference_basic`
- `inference_advanced`
- `main_idea_full_passage`
- `specific_detail`
- `passage_summary`
- `sentence_insertion`
- `sentence_odd_one_out`
- `strengthen_weaken`
- `purpose_of_example`
- `vocab_in_context`
- `author_tone`
- `para_jumble` (PJ — has its own answer-input mode; defer routing to dedicated handler)

If the planner returns a subskill not in this enum, the VARC agent falls back to `inference_basic` (default) and logs the misclassification for prompt iteration.

**The small_talk vs practice_request distinction (Bug 15 critical guidance):**

After a recent question + answer turn, brief acknowledgments must be classified as `small_talk`, not `practice_request`. The bot's response to small_talk is a warm acknowledgment + re-show of continuation buttons — NOT a new question.

- `practice_request` requires explicit forward intent: "another", "next", "more", "give me", "let's continue"
- `small_talk` covers: "ok", "thanks", "got it", "i see", "alright", "hmm", "interesting" (after recent context)
- When in doubt, prefer `small_talk` — the bot will then ask the student what they want next, never auto-serving.

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
    last_question_message_id: integer (nullable)  // tg_message_id of most recent question for keyboard close
    last_question_attempt_id: UUID (nullable)     // attempt row of most recent unanswered question
  }
  
  profile_brief: string (nullable)        // assembled if context_needs.profile != "skip"
  default_difficulty: string              // "easy" | "medium" | "hard"; from profile_service.get_default_difficulty (Bug 23)
  
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
  
  // Slice 2.5+ additions for new flows:
  
  mid_question_doubt: boolean              // true if student typed text mid-question (Bug 1)
  current_unanswered_attempt: object | null  // the open attempt row, if any (Bug 1, Bug 8)
  is_diagnostic_mode: boolean              // true during onboarding diagnostic Q1-Q5 (auto-continue allowed)
  skipped_attempt_id: UUID (nullable)      // set when student tapped skip button (Bug 8)
  session_resume_candidate: object | null  // set when returning after break with unfinished work (Bug 2):
                                           //   { last_question_id, last_question_subskill, last_session_summary, days_since_break }
  session_stats: object | null             // populated only if intent.action == "stats_request" (Bug 12)
  retry_context: object | null             // set when student tapped [Try again] after error fallback (Bug 18)
}
```

**Lifecycle:** assembled in step 9 of orchestrator flow, passed to agent in step 10, discarded after response.

**Notes on slice progression:**
- Slice 1: only basic fields populated; new fields default to null/false
- Slice 2: adds current_unanswered_attempt, skipped_attempt_id (for skip + answer flow)
- Slice 2.5: adds mid_question_doubt, retry_context
- Slice 3: adds session_resume_candidate (returning-after-break)
- Slice 4+: full population with planner-driven intent

---

### `AgentResponse`

What agents return to the orchestrator.

```
AgentResponse {
  content: string                          // the actual response text
  content_type: string                     // 'text' | 'text_with_keyboard'
  
  keyboard_buttons: [                      // structured button definitions; orchestrator builds Telegram InlineKeyboard from this
    [                                      // each inner array is a row of buttons
      { text: "Next question", callback_data: "v5_continue_next" },
      { text: "Different subskill", callback_data: "v5_continue_switch_subskill" }
    ],
    [
      { text: "I have a doubt", callback_data: "v5_continue_doubt" },
      { text: "I'm done", callback_data: "v5_continue_done" }
    ]
  ]
  
  response_type: string                    // analytics & downstream routing label; see enum below
  requires_keyboard_close: boolean         // if true, orchestrator removes inline keyboard from previous question (Bug 11, Principle 2)
  
  memory_deltas: {
    new_assistant_turn: object             // turn data to append to working memory cache
    active_context_updates: object         // partial updates to active session Redis state
    new_session: object (nullable)         // new session row to create (rare; orchestrator usually handles session creation)
    close_session: object (nullable)       // { session_id, end_reason } if explicit_end
    attempt_record: {                      // present when serving a question OR processing answer/skip
      operation: "insert" | "update"
      data: {
        student_id: UUID
        question_id: UUID
        served_at?: datetime               // for insert
        answered_at?: datetime             // for update
        is_correct?: boolean | null        // for update; null if skipped
        student_answer?: string | null     // for update; null if skipped
        skipped?: boolean                  // for update
        explanation_shown?: boolean        // for update
        fallback_tier?: integer            // for insert
        is_diagnostic?: boolean            // for insert; true during onboarding diagnostic
      }
    } (nullable)
    close_previous_keyboard: {             // populated when requires_keyboard_close=true (Bug 11)
      tg_message_id: integer
    } (nullable)
  }
  
  observer_events: [                       // events to insert into observer_events table
    {
      event_type: string
      payload: object
    }
  ]
  
  meta: {
    agent: string                          // "varc" | "mentor" | "orchestrator" (for orchestrator-composed responses)
    model_used: string (nullable)          // null when no LLM call (e.g., orchestrator small_talk response)
    input_tokens: integer (default 0)
    output_tokens: integer (default 0)
    cost_usd: float (default 0.0)
    generation_latency_ms: integer
    retrieval_used: boolean
    retrieved_question_id: UUID (nullable)
    fallback_tier: integer (1-6, nullable)
    response_type: string                  // duplicated here for analytics convenience
  }
}
```

**`response_type` enum:**
- `question_serve` — served a new question
- `answer_explanation` — scoring + explanation after student answered
- `skip_explanation` — explanation after student skipped (no scoring)
- `mid_question_doubt_ack` — acknowledged a doubt while preserving current question
- `concept_explanation` — taught a concept (no question retrieval)
- `topic_switch_ack` — acknowledged a subskill/topic switch
- `session_wrap` — explicit end response
- `session_resume_prompt` — returning-after-break "want to resume?" response
- `mentor_strategic_response` — mentor's reactive response
- `mentor_diagnostic_synthesis` — mentor's onboarding synthesis (one-time)
- `continuation_prompt` — orchestrator-composed warm acknowledgment for small_talk
- `session_stats` — orchestrator-composed stats response for stats_request
- `off_topic_redirect` — orchestrator-composed soft-redirect
- `error_fallback` — canned error message after agent failure

**Lifecycle:** returned by agent in step 10, processed in steps 11-13 of orchestrator flow.

**Important invariants:**
- Every AgentResponse from VARC or Mentor MUST have non-empty `keyboard_buttons` (per Principle 1), EXCEPT:
  - `response_type == "session_wrap"` (explicit end, no continuation)
  - When `is_diagnostic_mode == true` and not on Q5 (diagnostic exception)
- `requires_keyboard_close` is true ONLY when `response_type == "question_serve"` (the only case where we're replacing the previously-active question's keyboard)

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
