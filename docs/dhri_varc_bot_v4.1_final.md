# DHRI VARC Bot — Complete Implementation Specification v4.1 (FINAL)

**Single source of truth. Feed this to Claude Code. Do not deviate.**

This version supersedes v4.0. It fills the three gaps that prevented v4.0 from being directly executable:

1. **Gap 1 fixed** — `_fetch_single` rewritten with two clean SQL branches (no nested f-string `$N` interpolation bug).
2. **Gap 2 fixed** — three flagged PYQs have explicit `needs_review: true` handling, retrieval excludes them until reviewed.
3. **Gap 3 fixed** — all six tagger prompts now contain actual few-shot examples drawn from the 48 seed PYQs.

Companion file: `dhri_48_pyqs_v4.json` contains all 48 pre-tagged CAT PYQs (24 from 2024 Slot 1 + 24 from 2023 Slot 1) ready for seed ingest.

Everything else from v4.0 is unchanged.

---

## Table of Contents

1. Project Overview
2. Technology Stack
3. Repository Structure
4. Configuration — Single Source of Truth
5. Database Schema
6. Redis Key Schema
7. Bot Commands
8. Rate Limiting
9. Request Lifecycle — User-Level Lock
10. Session Lifecycle
11. Onboarding
12. Technique Queries — SUBSKILL_TO_TECHNIQUE_QUERY
13. Profile Score Updates
14. Retrieval — PracticeSelector (WITH GAP 1 + GAP 2 FIX)
15. Reranker
16. Embedding
17. Question Context Helper
18. VA Handler — All Types
19. Tagger — Six Type-Specific Prompts (WITH REAL FEW-SHOT EXAMPLES)
20. Ingest Pipeline — store_question
21. Input JSON Format
22. System Prompt
23. Stats Handler
24. main.py — Startup with Assertions
25. DB Client, Redis Client, LLM, Utilities (copy from v3)
26. Helper Functions Reference
27. Seed Data (48 PYQs + minimum fillers)
28. Deployment
29. What Does Not Change from v3
30. Manual Test Script
31. Implementation Order for Claude Code
32. Appendix A — Seed Ingest Script
33. Appendix B — Summary of Changes from v4.0

---

## 1. Project Overview

A Telegram bot that serves as a CAT VARC preparation assistant. Students practice Reading Comprehension, Para Jumbles, Vocabulary, Grammar, and Critical Reasoning through a conversational interface. The bot maintains persistent memory across sessions, tracks performance at the subskill level, and retrieves practice questions matched to each student's specific cognitive weak areas.

Beta target: 50-100 active daily users before building the full web product.

---

## 2. Technology Stack

```
Runtime:          Python 3.11
Bot framework:    python-telegram-bot v21 (async)
Web framework:    FastAPI (webhook receiver)
Database:         PostgreSQL on Neon (asyncpg driver)
Cache + state:    Upstash Redis (upstash-redis HTTP client)
LLM:              OpenRouter API (openai SDK)
Embeddings:       openai/text-embedding-3-small via OpenRouter
Admin panel:      Streamlit (local only, never deployed publicly)
Deployment:       Railway (single service)
Cron jobs:        Railway cron (session cleanup + weekly reports)
```

---

## 3. Repository Structure

```
dhri-bot/
├── main.py
├── config.py
├── requirements.txt
├── .env.example
├── railway.json
│
├── bot/
│   ├── router.py
│   ├── commands.py
│   ├── callbacks.py
│   ├── free_text.py
│   └── keyboards.py
│
├── handlers/
│   ├── onboarding.py
│   ├── home.py
│   ├── practice/
│   │   ├── common.py
│   │   ├── rc.py
│   │   ├── pj.py
│   │   └── va.py
│   ├── doubt.py
│   ├── concept.py
│   ├── stats.py
│   ├── resume.py
│   ├── session_cleanup.py
│   └── weekly_reports.py
│
├── agent/
│   ├── prompts.py
│   ├── explainer.py
│   └── llm.py
│
├── memory/
│   ├── session.py
│   ├── profile.py
│   └── summarizer.py
│
├── retrieval/
│   ├── selector.py
│   ├── pgvector.py
│   ├── reranker.py
│   └── technique_queries.py
│
├── db/
│   ├── client.py
│   ├── queries.py
│   └── schema.sql
│
├── ingest/
│   ├── parser.py
│   ├── verifier.py
│   ├── tagger.py
│   ├── embedder.py
│   └── pipeline.py
│
├── data/
│   └── dhri_48_pyqs_v4.json      # seed data — 48 tagged PYQs
│
└── admin/
    ├── app.py
    └── pages/
        ├── dashboard.py
        ├── ingest.py
        ├── questions.py
        ├── users.py
        ├── analytics.py
        └── feedback.py
```

---

## 4. Configuration — Single Source of Truth

```python
# config.py

from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List, Dict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Telegram
    TELEGRAM_BOT_TOKEN: str
    WEBHOOK_SECRET: str

    # Database
    DATABASE_URL: str           # pooled — for app
    DATABASE_URL_DIRECT: str    # direct — for migrations only

    # Redis
    UPSTASH_REDIS_REST_URL: str
    UPSTASH_REDIS_REST_TOKEN: str

    # OpenRouter
    OPENROUTER_API_KEY: str
    OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1"

    # Models
    MODEL_CHAT: str = "google/gemini-flash-1.5"
    MODEL_COMPLEX: str = "anthropic/claude-haiku-4-5"
    MODEL_TAGGER_STRUCTURED: str = "google/gemini-flash-1.5"
    MODEL_TAGGER_TECHNIQUE: str = "anthropic/claude-sonnet-4-5"
    MODEL_SUMMARIZER: str = "google/gemini-flash-1.5"
    MODEL_VERIFIER: str = "google/gemini-flash-1.5"
    MODEL_EMBEDDING: str = "openai/text-embedding-3-small"

    # Spend
    DAILY_LLM_SPEND_CAP_USD: float = 0.50

    # Admin
    ADMIN_REPORTS_SECRET: str

    # Railway
    RAILWAY_PUBLIC_DOMAIN: str
    PORT: int = 8000

settings = Settings()

# ─── TAXONOMY ──────────────────────────────────────────────────────────────

# Student-facing: 7 skills shown in UI, home screen, stats, weakest-skill prompt
STUDENT_SKILLS: List[str] = [
    "inference",
    "main_idea",
    "author_tone",
    "specific_detail",
    "purpose_and_structure",
    "para_jumbles",
    "vocab_grammar",
]

# Internal subskills: used for fine-grained retrieval, score tracking, tagger output
# Adding a subskill: add here, add to SUBSKILL_TO_TECHNIQUE_QUERY, done.
SUBSKILL_TO_SKILL: Dict[str, str] = {
    # inference group
    "inference_basic":          "inference",
    "strengthen_weaken":        "inference",
    # main_idea group
    "main_idea_full_passage":   "main_idea",
    "passage_summary":          "main_idea",
    # author_tone (no split)
    "author_tone":              "author_tone",
    # specific_detail group
    "specific_detail":          "specific_detail",
    "vocab_in_context":         "specific_detail",
    # purpose_and_structure group
    "purpose_of_example":       "purpose_and_structure",
    "logical_structure":        "purpose_and_structure",
    # para_jumbles group
    "structural_identification":"para_jumbles",
    "sequence_logic":           "para_jumbles",
    "pronoun_reference":        "para_jumbles",
    "example_principle_link":   "para_jumbles",
    # vocab_grammar group
    "grammar_rule":             "vocab_grammar",
    "vocabulary_meaning":       "vocab_grammar",
    "sentence_odd_one_out":     "vocab_grammar",
    "sentence_insertion":       "vocab_grammar",
    "paragraph_completion":     "vocab_grammar",
}

ALL_SUBSKILLS: List[str] = list(SUBSKILL_TO_SKILL.keys())

# Derived groupings — computed from SUBSKILL_TO_SKILL, not hardcoded
RC_SUBSKILLS = [s for s, sk in SUBSKILL_TO_SKILL.items()
                if sk in {"inference","main_idea","author_tone",
                          "specific_detail","purpose_and_structure"}
                and s != "passage_summary"]  # passage_summary uses va_summary type

PJ_SUBSKILLS = [s for s, sk in SUBSKILL_TO_SKILL.items() if sk == "para_jumbles"]

VA_SUBSKILLS = [s for s, sk in SUBSKILL_TO_SKILL.items()
                if sk == "vocab_grammar" or s == "passage_summary"]

# Which subskills are legal for each question type
# Enforced at ingest — any (type, subskill) not here is flagged needs_review
SKILL_TYPE_MATRIX: Dict[str, List[str]] = {
    "rc_question":            RC_SUBSKILLS,
    "pj":                     PJ_SUBSKILLS,
    "va_grammar":             ["grammar_rule"],
    "va_sentence_correction": ["grammar_rule"],
    "va_vocab":               ["vocabulary_meaning"],
    "va_fill_in_blank":       ["vocabulary_meaning", "paragraph_completion"],
    "va_wrong_one_out":       ["sentence_odd_one_out"],
    "va_sentence_insertion":  ["sentence_insertion"],
    "va_summary":             ["passage_summary"],
}

# ─── TRAPS ──────────────────────────────────────────────────────────────────

ALL_TRAPS: List[str] = [
    "half_right_half_wrong",    # dominant
    "out_of_scope",             # dominant
    "too_extreme",              # dominant
    "theme_break",              # dominant for VA-structural
    "true_but_not_inferable",
    "content_over_purpose",     # specific to purpose questions
    "other",                    # catchall for rare cases
    "none",                     # correct option
]

# ─── DISPLAY NAMES ──────────────────────────────────────────────────────────

SKILL_DISPLAY_NAMES: Dict[str, str] = {
    "inference":            "Inference",
    "main_idea":            "Main Idea",
    "author_tone":          "Author Tone",
    "specific_detail":      "Specific Detail",
    "purpose_and_structure":"Purpose & Structure",
    "para_jumbles":         "Para Jumbles",
    "vocab_grammar":        "Vocab & Grammar",
}

# ─── CONSTANTS ────────────────────────────────────────────────────────────

MIN_ATTEMPTS_FOR_WEAKEST_SKILL = 10
```

---

## 5. Database Schema

Run once against Neon direct connection. Never run in application code.

```sql
-- schema.sql

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

CREATE TABLE tg_users (
    tg_id           BIGINT PRIMARY KEY,
    username        VARCHAR(64),
    first_name      VARCHAR(64),
    target_year     SMALLINT,
    experience      VARCHAR(20),
    is_banned       BOOLEAN DEFAULT false,
    ban_reason      TEXT,
    joined_at       TIMESTAMP DEFAULT now(),
    last_active_at  TIMESTAMP DEFAULT now()
);

-- One row per user per SUBSKILL (not student-facing skill).
-- Student-facing skill scores are aggregated at query time.
CREATE TABLE user_skill_scores (
    tg_id           BIGINT REFERENCES tg_users(tg_id) ON DELETE CASCADE,
    subskill        VARCHAR(40) NOT NULL,
    score           FLOAT DEFAULT 0.5,
    attempts_count  INTEGER DEFAULT 0,
    updated_at      TIMESTAMP DEFAULT now(),
    PRIMARY KEY (tg_id, subskill)
);

CREATE TABLE user_profiles (
    tg_id                   BIGINT PRIMARY KEY
                            REFERENCES tg_users(tg_id) ON DELETE CASCADE,
    trap_counts             JSONB DEFAULT '{}',
    most_common_trap        VARCHAR(40) DEFAULT 'none',
    current_difficulty      VARCHAR(10) DEFAULT 'medium'
                            CHECK (current_difficulty IN ('easy','medium','hard')),
    current_streak          INTEGER DEFAULT 0,
    longest_streak          INTEGER DEFAULT 0,
    last_practice_date      DATE,
    total_attempts          INTEGER DEFAULT 0,
    total_correct           INTEGER DEFAULT 0,
    total_sessions          INTEGER DEFAULT 0,
    -- NULL until MIN_ATTEMPTS_FOR_WEAKEST_SKILL (10) reached
    -- Stores student-facing skill name (one of 7), not subskill
    weakest_skill           VARCHAR(40),
    updated_at              TIMESTAMP DEFAULT now()
);

CREATE TABLE passages (
    passage_id      VARCHAR(60) PRIMARY KEY,
    full_text       TEXT NOT NULL,
    word_count      INTEGER,
    topic           VARCHAR(60),
    tone            VARCHAR(30),
    source          VARCHAR(20) NOT NULL
                    CHECK (source IN ('cat_official','mock','custom','agent_generated')),
    year            SMALLINT,
    difficulty      VARCHAR(10) DEFAULT 'medium'
                    CHECK (difficulty IN ('easy','medium','hard')),
    is_active       BOOLEAN DEFAULT true,
    created_at      TIMESTAMP DEFAULT now()
);

CREATE TABLE questions (
    question_id             VARCHAR(60) PRIMARY KEY,
    type                    VARCHAR(30) NOT NULL
                            CHECK (type IN (
                                'rc_question', 'pj',
                                'va_grammar', 'va_vocab',
                                'va_sentence_correction',
                                'va_wrong_one_out', 'va_fill_in_blank',
                                'va_sentence_insertion',
                                'va_summary'
                            )),
    passage_id              VARCHAR(60) REFERENCES passages(passage_id),
    -- For va_summary and va_sentence_insertion: the source paragraph
    -- For all other types: NULL
    source_text             TEXT,
    question_text           TEXT NOT NULL,
    options                 JSONB,
    correct_option          VARCHAR(1),
    correct_order           VARCHAR(10),
    explanation             TEXT,

    -- RC specific
    rc_question_type        VARCHAR(30),

    -- PJ specific
    sentences               JSONB,
    connector_type          VARCHAR(30),
    opening_clue            TEXT,
    pj_connector_map        JSONB,

    -- Taxonomy fingerprint (set at ingest time)
    skill                   VARCHAR(40),    -- student-facing (7 values)
    subskill                VARCHAR(40),    -- internal for retrieval (~18 values)
    traps_present           TEXT[],         -- subset of ALL_TRAPS
    option_traps            JSONB,          -- {"A": "trap_name", "B": null, ...}
    one_line_technique      TEXT,           -- the embedding anchor

    -- Taxonomy versioning
    taxonomy_version        SMALLINT DEFAULT 1,
    tagged_at               TIMESTAMP,
    tagger_model            VARCHAR(80),

    -- pgvector (1536 dims for text-embedding-3-small)
    technique_embedding     vector(1536),

    -- Metadata
    difficulty              VARCHAR(10) DEFAULT 'medium'
                            CHECK (difficulty IN ('easy','medium','hard')),
    source                  VARCHAR(20) NOT NULL,
    year                    SMALLINT,
    question_order          SMALLINT,
    is_active               BOOLEAN DEFAULT true,
    needs_review            BOOLEAN DEFAULT false,
    manually_tagged         BOOLEAN DEFAULT false,
    created_at              TIMESTAMP DEFAULT now()
);

CREATE TABLE sessions (
    session_id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tg_id               BIGINT NOT NULL REFERENCES tg_users(tg_id),
    mode                VARCHAR(20),
    started_at          TIMESTAMP DEFAULT now(),
    ended_at            TIMESTAMP,
    last_active_at      TIMESTAMP DEFAULT now(),
    duration_mins       INTEGER,
    was_completed       BOOLEAN DEFAULT false,
    questions_attempted INTEGER DEFAULT 0,
    questions_correct   INTEGER DEFAULT 0,
    skills_practiced    TEXT[],
    summary             TEXT,
    created_at          TIMESTAMP DEFAULT now()
);

-- Only written for RC_ACTIVE, PJ_ACTIVE, VA_ACTIVE sessions
CREATE TABLE session_snapshots (
    session_id              UUID PRIMARY KEY REFERENCES sessions(session_id),
    tg_id                   BIGINT REFERENCES tg_users(tg_id),
    current_mode            VARCHAR(20),
    current_question_id     VARCHAR(60),
    passage_id              VARCHAR(60),
    questions_in_set        TEXT[],
    questions_answered      JSONB,
    questions_remaining     TEXT[],
    snapped_at              TIMESTAMP DEFAULT now()
);

CREATE TABLE messages (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    session_id      UUID NOT NULL REFERENCES sessions(session_id),
    tg_id           BIGINT NOT NULL REFERENCES tg_users(tg_id),
    tg_message_id   BIGINT,
    role            VARCHAR(10) NOT NULL CHECK (role IN ('user','assistant','system')),
    content         TEXT NOT NULL,
    message_type    VARCHAR(20) DEFAULT 'text',
    question_id     VARCHAR(60),
    created_at      TIMESTAMP DEFAULT now()
);

CREATE TABLE attempts (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tg_id           BIGINT NOT NULL REFERENCES tg_users(tg_id),
    session_id      UUID NOT NULL REFERENCES sessions(session_id),
    question_id     VARCHAR(60) NOT NULL REFERENCES questions(question_id),
    selected_option VARCHAR(10),
    correct_option  VARCHAR(10),
    is_correct      BOOLEAN NOT NULL,
    trap_fallen_for VARCHAR(40) DEFAULT 'none',
    pj_mistake_type VARCHAR(40),
    is_reattempt    BOOLEAN DEFAULT false,
    time_taken_secs INTEGER,
    attempted_at    TIMESTAMP DEFAULT now()
);

CREATE TABLE feedback (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tg_id           BIGINT REFERENCES tg_users(tg_id),
    question_id     VARCHAR(60) REFERENCES questions(question_id),
    session_id      UUID REFERENCES sessions(session_id),
    message         TEXT NOT NULL,
    is_resolved     BOOLEAN DEFAULT false,
    created_at      TIMESTAMP DEFAULT now()
);

-- INDEXES
CREATE INDEX idx_sessions_tg_id ON sessions(tg_id, started_at DESC);
CREATE INDEX idx_sessions_open ON sessions(tg_id, last_active_at) WHERE ended_at IS NULL;
CREATE INDEX idx_messages_session ON messages(session_id, created_at ASC);
CREATE INDEX idx_messages_tg_id ON messages(tg_id, created_at DESC);
CREATE INDEX idx_attempts_tg_id ON attempts(tg_id, attempted_at DESC);
CREATE INDEX idx_attempts_session ON attempts(session_id);
CREATE INDEX idx_attempts_tg_question ON attempts(tg_id, question_id);
CREATE INDEX idx_questions_type_difficulty ON questions(type, difficulty, is_active);
CREATE INDEX idx_questions_skill ON questions(skill, difficulty) WHERE is_active = true;
CREATE INDEX idx_questions_subskill ON questions(subskill, difficulty) WHERE is_active = true;
CREATE INDEX idx_questions_review ON questions(needs_review) WHERE needs_review = true;
CREATE INDEX idx_skill_scores_tg ON user_skill_scores(tg_id, score);
CREATE INDEX idx_questions_embedding ON questions
    USING hnsw (technique_embedding vector_cosine_ops);
```

---

## 6. Redis Key Schema

```
User-level lock:
  Key:   lock:user:{tg_id}
  Value: "1"
  TTL:   5 seconds

Session state (IDs only — no text):
  Key:   state:tg:{tg_id}
  Value: JSON
  TTL:   7200 seconds, reset on every message

Rate limiting:
  Key:   rl:msg:{tg_id}:{date_ist}
  Value: counter
  TTL:   seconds until midnight IST
  Limit: 50 user LLM-triggering messages/day

Profile cache:
  Key:   profile:{tg_id}
  Value: user_profiles JSON
  TTL:   1800 seconds

Daily spend:
  Key:   spend:{date_iso}
  Value: float USD (approximate)
  TTL:   3024000 seconds (35 days)

Practice lock:
  Key:   lock:practice:{tg_id}
  Value: "1"
  TTL:   30 seconds
```

---

## 7. Bot Commands

```
/start    /rc    /pj    /va    /doubt    /concept
/stats    /weak    /resume    /settings    /feedback
/done    /help

Admin (scope to your tg_id):
/broadcast    /ban
```

---

## 8. Rate Limiting

Counts: free-text messages routed through LLM call path only. Does not count: commands, inline keyboard taps, static responses. Limit: 50 per user per day, midnight IST reset.

---

## 9. Request Lifecycle — User-Level Lock

```python
# bot/router.py

async def route_update(update: Update, ptb_app):
    tg_id = get_tg_id(update)
    if not tg_id:
        return

    lock_acquired = await acquire_lock(f"lock:user:{tg_id}", 5)
    if not lock_acquired:
        return  # another update processing — drop this one

    try:
        await _process_update(update, ptb_app, tg_id)
    except SpendCapExceededError:
        await send_error_to_update(update, "spend_cap", ptb_app.bot)
    except Exception as e:
        logger.exception(f"Unhandled error tg_id={tg_id}: {e}")
        await send_error_to_update(update, "generic", ptb_app.bot)
    finally:
        await release_lock(f"lock:user:{tg_id}")
```

---

## 10. Session Lifecycle

### State object — uniform structure all modes

```json
{
  "state": "RC_ACTIVE",
  "session_id": "uuid",
  "mode": "rc",
  "passage_id": "cat2023_rc4",
  "questions_in_set": ["q1", "q2", "q3", "q4"],
  "current_question_index": 0,
  "questions_answered": {},
  "questions_remaining": ["q1", "q2", "q3", "q4"],
  "session_started_at": "2024-01-15T10:30:00Z"
}
```

PJ and VA use same structure with `questions_in_set` of length 1. `passage_id` null for non-RC.

### Session cleanup — cron every 10 minutes

```python
# handlers/session_cleanup.py

RESUMABLE_MODES = {'rc', 'pj', 'va'}

async def cleanup_stale_sessions() -> int:
    stale = await db.fetch("""
        SELECT session_id, tg_id, mode FROM sessions
        WHERE ended_at IS NULL
          AND last_active_at < now() - interval '2 hours'
    """)
    closed = 0
    for session in stale:
        try:
            await close_session_silently(
                session['session_id'], session['tg_id'], session['mode']
            )
            closed += 1
        except Exception as e:
            logger.error(f"Failed to close {session['session_id']}: {e}")
            try:
                await db.execute("""
                    UPDATE sessions SET ended_at = now(), was_completed = false,
                    summary = NULL WHERE session_id = $1 AND ended_at IS NULL
                """, session['session_id'])
            except Exception as e2:
                logger.error(f"Fallback close failed: {e2}")
    return closed

async def close_session_silently(session_id: str, tg_id: int, mode: str):
    summary = None
    try:
        summary = await generate_session_summary(session_id, tg_id)
    except Exception as e:
        logger.warning(f"Summary failed for {session_id}: {e}")

    if mode in RESUMABLE_MODES:
        try:
            state = await get_state_from_db_or_redis(tg_id, session_id)
            if state:
                await write_session_snapshot(session_id, tg_id, state)
        except Exception as e:
            logger.warning(f"Snapshot failed for {session_id}: {e}")

    await db.execute("""
        UPDATE sessions SET
            ended_at = now(), was_completed = false, summary = $1,
            duration_mins = EXTRACT(EPOCH FROM (now() - started_at)) / 60
        WHERE session_id = $2 AND ended_at IS NULL
    """, summary, session_id)

    await redis.delete(f"state:tg:{tg_id}")
```

---

## 11. Onboarding

No diagnostic. Immediate practice after year + level selection.

```python
# handlers/onboarding.py

async def initialize_skill_scores(tg_id: int):
    """Insert 0.5 row for every subskill."""
    from config import ALL_SUBSKILLS
    await db.executemany("""
        INSERT INTO user_skill_scores (tg_id, subskill, score)
        VALUES ($1, $2, 0.5) ON CONFLICT DO NOTHING
    """, [(tg_id, subskill) for subskill in ALL_SUBSKILLS])
```

---

## 12. Technique Queries — SUBSKILL_TO_TECHNIQUE_QUERY

```python
# retrieval/technique_queries.py

from config import ALL_SUBSKILLS

SUBSKILL_TO_TECHNIQUE_QUERY = {
    # ─── RC subskills ───────────────────────────────────────────────────────
    "inference_basic": (
        "derive unstated conclusion from passage premises — "
        "eliminate options true in world but unsupported by text — "
        "find minimum assumption passage must be making — "
        "distinguish what passage implies from what it explicitly states"
    ),
    "strengthen_weaken": (
        "identify core claim author is defending — "
        "determine which statement makes argument more or less valid — "
        "find assumption the argument depends on — "
        "strengthen adds evidence for claim, weaken removes support"
    ),
    "main_idea_full_passage": (
        "identify central argument encompassing entire passage — "
        "correct answer covers everything not just one section — "
        "reject options describing only part of the passage — "
        "the answer is the claim everything else supports"
    ),
    "author_tone": (
        "detect author evaluative stance from word choice not topic — "
        "locate stance words adjectives adverbs expressing attitude — "
        "distinguish sardonic from critical from cautious from appreciative — "
        "how the author says it, not what they say"
    ),
    "specific_detail": (
        "locate explicit information stated directly in passage — "
        "find which paragraph contains the stated fact — "
        "match question to exact location in text"
    ),
    "vocab_in_context": (
        "determine word meaning as used in passage not dictionary — "
        "read surrounding sentences for semantic constraints — "
        "non-standard or technical usage common in CAT passages"
    ),
    "purpose_of_example": (
        "identify why author included this paragraph or example — "
        "not what it says but what it DOES in the argument — "
        "purpose asks for rhetorical function not content"
    ),
    "logical_structure": (
        "identify how passage is organized argumentatively — "
        "determine relationship between passage sections — "
        "recognize claim-evidence, problem-solution, compare-contrast"
    ),
    # ─── PJ subskills ────────────────────────────────────────────────────────
    "structural_identification": (
        "identify mandatory first sentence: no pronouns no backward references — "
        "identify mandatory last sentence: conclusion markers no dangling refs — "
        "lock opening and closing before arranging the middle"
    ),
    "sequence_logic": (
        "therefore thus consequently must follow their cause — "
        "however but must follow statement they contrast — "
        "transition words create mandatory sequence constraints"
    ),
    "pronoun_reference": (
        "sentence with pronoun cannot precede its antecedent — "
        "it they this these must follow noun introducing what they refer to — "
        "map all pronouns before ordering"
    ),
    "example_principle_link": (
        "general principle followed by specific example — "
        "for example for instance must follow the principle they illustrate — "
        "identify claim sentence and evidence sentence"
    ),
    # ─── VA subskills ────────────────────────────────────────────────────────
    "grammar_rule": (
        "identify correct sentence by applying specific grammatical rule — "
        "subject verb agreement tense consistency parallelism modifier placement — "
        "diagnose which rule applies before evaluating options"
    ),
    "vocabulary_meaning": (
        "select word matching semantic and tonal register of context — "
        "formal academic vocabulary in dense non-fiction — "
        "synonym fill-in-blank contextual usage"
    ),
    "sentence_odd_one_out": (
        "four sentences share specific angle odd one shares topic different aspect — "
        "coherence is about perspective and sub-theme not just topic — "
        "the odd sentence breaks logical or thematic continuity"
    ),
    "sentence_insertion": (
        "place sentence in slot bridging its two neighbours — "
        "inserted sentence must flow from prior and lead into next — "
        "check both the sentence before AND the sentence after the blank — "
        "theme direction and pronoun references must all match"
    ),
    "paragraph_completion": (
        "choose sentence logically and tonally completing the paragraph — "
        "must be consistent with argument direction and author stance — "
        "cannot contradict or ignore what paragraph established"
    ),
    "passage_summary": (
        "capture central claim including its causal backbone — "
        "preserve both halves of a two-part argument — "
        "reject options stating observation without mechanism — "
        "reject options adding claims not present in passage"
    ),
}

# Validated at startup — see main.py
```

---

## 13. Profile Score Updates

Score tracked at subskill level. Student-facing display aggregates to 7 skills.

```python
# memory/profile.py

from config import SUBSKILL_TO_SKILL, MIN_ATTEMPTS_FOR_WEAKEST_SKILL
import json

ALPHA = 0.15

async def update_skill_score(tg_id: int, subskill: str, is_correct: bool):
    row = await db.fetchrow("""
        SELECT score, attempts_count FROM user_skill_scores
        WHERE tg_id = $1 AND subskill = $2
    """, tg_id, subskill)

    current = row['score'] if row else 0.5
    new_score = current * (1 - ALPHA) + (1.0 if is_correct else 0.0) * ALPHA

    await db.execute("""
        INSERT INTO user_skill_scores (tg_id, subskill, score, attempts_count)
        VALUES ($1, $2, $3, 1)
        ON CONFLICT (tg_id, subskill) DO UPDATE SET
            score = $3,
            attempts_count = user_skill_scores.attempts_count + 1,
            updated_at = now()
    """, tg_id, subskill, round(new_score, 4))

    await db.execute("""
        UPDATE user_profiles SET
            total_attempts = total_attempts + 1,
            total_correct = total_correct + $1,
            updated_at = now()
        WHERE tg_id = $2
    """, 1 if is_correct else 0, tg_id)

    total = await db.fetchval(
        "SELECT total_attempts FROM user_profiles WHERE tg_id = $1", tg_id
    )
    if total and total >= MIN_ATTEMPTS_FOR_WEAKEST_SKILL:
        weakest = await get_weakest_student_skill(tg_id)
        await db.execute(
            "UPDATE user_profiles SET weakest_skill = $1 WHERE tg_id = $2",
            weakest, tg_id
        )

    await redis.delete(f"profile:{tg_id}")

async def get_weakest_student_skill(tg_id: int) -> str:
    """Aggregate subskill scores to student-facing skills."""
    rows = await db.fetch("""
        SELECT subskill, score FROM user_skill_scores WHERE tg_id = $1
    """, tg_id)

    skill_scores = {}
    for row in rows:
        student_skill = SUBSKILL_TO_SKILL.get(row['subskill'])
        if not student_skill:
            continue
        if student_skill not in skill_scores:
            skill_scores[student_skill] = []
        skill_scores[student_skill].append(row['score'])

    if not skill_scores:
        return "inference"  # safe default

    avg_scores = {
        skill: sum(scores) / len(scores)
        for skill, scores in skill_scores.items()
    }
    return min(avg_scores, key=avg_scores.get)

async def get_weakest_subskill_in_group(tg_id: int, subskill_group: list) -> str:
    """Return subskill with lowest score within a group."""
    row = await db.fetchrow("""
        SELECT subskill FROM user_skill_scores
        WHERE tg_id = $1 AND subskill = ANY($2)
        ORDER BY score ASC LIMIT 1
    """, tg_id, subskill_group)
    return row['subskill'] if row else subskill_group[0]

async def update_trap_counts(tg_id: int, trap: str):
    profile = await db.fetchrow(
        "SELECT trap_counts FROM user_profiles WHERE tg_id = $1", tg_id
    )
    trap_counts = dict(profile['trap_counts'] or {})
    trap_counts[trap] = trap_counts.get(trap, 0) + 1
    most_common = max(trap_counts, key=trap_counts.get)
    await db.execute("""
        UPDATE user_profiles SET trap_counts = $1, most_common_trap = $2
        WHERE tg_id = $3
    """, json.dumps(trap_counts), most_common, tg_id)
    await redis.delete(f"profile:{tg_id}")

async def get_most_common_trap(tg_id: int) -> str:
    """Return user's dominant trap for reranking. Returns 'none' if no data."""
    row = await db.fetchrow(
        "SELECT most_common_trap FROM user_profiles WHERE tg_id = $1", tg_id
    )
    return row['most_common_trap'] if row else 'none'
```

---

## 14. Retrieval — PracticeSelector (GAP 1 + GAP 2 FIX)

**CRITICAL FIX 1 (Gap 1):** The `_fetch_single` function in v4.0 had a nested f-string `$N` interpolation bug that would fail at runtime. This version uses two clean SQL branches — one with subskill filter, one without.

**CRITICAL FIX 2 (Gap 2):** All retrieval SQL adds `AND q.needs_review = false` so flagged questions are excluded from practice until manually reviewed. The 48-PYQ seed contains 3 flagged questions; this filter ensures they never reach students.

```python
# retrieval/selector.py

from config import RC_SUBSKILLS, PJ_SUBSKILLS, VA_SUBSKILLS, SUBSKILL_TO_SKILL
from retrieval.technique_queries import SUBSKILL_TO_TECHNIQUE_QUERY
from retrieval.reranker import rerank
from memory.profile import get_weakest_subskill_in_group, get_most_common_trap
from agent.llm import embed

DIFFICULTY_ORDER = ['easy', 'medium', 'hard']


class PracticeSelector:

    async def get_rc_passage(self, profile: dict) -> dict | None:
        """
        Fallback chain:
        1. weakest RC subskill + requested difficulty (unseen, not flagged)
        2. weakest RC subskill + adjacent difficulty (both directions)
        3. second-weakest RC subskill + requested difficulty
        4. None — surface exhausted message
        """
        tg_id = profile['tg_id']
        difficulty = profile['current_difficulty']
        seen_ids = await self.get_seen_question_ids(tg_id)
        weakest = await get_weakest_subskill_in_group(tg_id, RC_SUBSKILLS)

        configs = [
            (weakest, difficulty),
            (weakest, self._adjacent(difficulty, 'easier')),
            (weakest, self._adjacent(difficulty, 'harder')),
            (await self._second_weakest(tg_id, RC_SUBSKILLS, weakest), difficulty),
        ]
        for subskill, diff in configs:
            if not subskill or not diff:
                continue
            result = await self._fetch_rc(tg_id, subskill, diff, seen_ids, profile)
            if result:
                return result
        return None

    async def _fetch_rc(self, tg_id, subskill, difficulty, seen_ids, profile):
        query_vector = await embed(SUBSKILL_TO_TECHNIQUE_QUERY[subskill])
        candidates = await db.fetch("""
            SELECT q.question_id, q.passage_id,
                   1 - (q.technique_embedding <=> $1::vector) as similarity,
                   q.subskill, q.traps_present, q.difficulty
            FROM questions q
            WHERE q.type = 'rc_question'
              AND q.subskill = $2
              AND q.difficulty = $3
              AND q.is_active = true
              AND q.needs_review = false
              AND q.question_id != ALL($4)
              AND q.passage_id IS NOT NULL
            ORDER BY q.technique_embedding <=> $1
            LIMIT 20
        """, query_vector, subskill, difficulty, seen_ids)

        if not candidates:
            return None

        reranked = rerank(candidates, await get_most_common_trap(tg_id))

        # Pick the passage with the most unseen candidate questions
        passage_counts = {}
        for q in reranked:
            pid = q['passage_id']
            passage_counts[pid] = passage_counts.get(pid, 0) + 1

        best_pid = max(passage_counts, key=passage_counts.get)
        if passage_counts[best_pid] < 2:
            return None  # need at least 2 unseen questions on the passage

        unseen_qs = [q for q in reranked if q['passage_id'] == best_pid]
        qids = [q['question_id'] for q in unseen_qs]
        full_questions = await db.fetch("""
            SELECT * FROM questions
            WHERE question_id = ANY($1) AND needs_review = false
            ORDER BY question_order
        """, qids)
        passage = await db.fetchrow(
            "SELECT * FROM passages WHERE passage_id = $1", best_pid
        )
        return {"passage": passage, "questions": full_questions}

    async def get_pj(self, profile: dict) -> dict | None:
        """Fallback: weakest PJ subskill → any PJ subskill → adjacent difficulty."""
        tg_id = profile['tg_id']
        difficulty = profile['current_difficulty']
        seen_ids = await self.get_seen_question_ids(tg_id)
        weakest = await get_weakest_subskill_in_group(tg_id, PJ_SUBSKILLS)

        configs = [
            (weakest, difficulty),
            (None, difficulty),  # any PJ
            (weakest, self._adjacent(difficulty, 'easier')),
            (weakest, self._adjacent(difficulty, 'harder')),
        ]
        for subskill, diff in configs:
            if not diff:
                continue
            result = await self._fetch_single(
                tg_id, 'pj', subskill, diff, seen_ids, profile
            )
            if result:
                return result
        return None

    async def get_va(
        self, profile: dict, va_type: str, subskill: str | None = None
    ) -> dict | None:
        """Fetch one VA question of a specific type."""
        from config import SKILL_TYPE_MATRIX
        tg_id = profile['tg_id']
        difficulty = profile['current_difficulty']
        seen_ids = await self.get_seen_question_ids(tg_id)

        legal_subskills = SKILL_TYPE_MATRIX.get(va_type, [])
        if not subskill or subskill not in legal_subskills:
            subskill = await get_weakest_subskill_in_group(tg_id, legal_subskills)

        configs = [
            (subskill, difficulty),
            (subskill, self._adjacent(difficulty, 'easier')),
            (subskill, self._adjacent(difficulty, 'harder')),
        ]
        for sk, diff in configs:
            if not sk or not diff:
                continue
            result = await self._fetch_single(
                tg_id, va_type, sk, diff, seen_ids, profile
            )
            if result:
                return result
        return None

    async def _fetch_single(
        self, tg_id, q_type, subskill, difficulty, seen_ids, profile
    ):
        """
        Fetch a single question. TWO SQL BRANCHES — one with subskill filter,
        one without. This avoids the nested f-string $N interpolation bug
        that existed in v4.0. Do not collapse these branches.

        Also enforces needs_review = false so flagged questions never reach
        students during practice.
        """
        query_text = SUBSKILL_TO_TECHNIQUE_QUERY.get(
            subskill, SUBSKILL_TO_TECHNIQUE_QUERY['inference_basic']
        )
        query_vector = await embed(query_text)

        if subskill:
            candidates = await db.fetch("""
                SELECT q.*,
                       1 - (q.technique_embedding <=> $1::vector) as similarity
                FROM questions q
                WHERE q.type = $2
                  AND q.subskill = $3
                  AND q.difficulty = $4
                  AND q.is_active = true
                  AND q.needs_review = false
                  AND q.question_id != ALL($5)
                ORDER BY q.technique_embedding <=> $1
                LIMIT 10
            """, query_vector, q_type, subskill, difficulty, seen_ids)
        else:
            candidates = await db.fetch("""
                SELECT q.*,
                       1 - (q.technique_embedding <=> $1::vector) as similarity
                FROM questions q
                WHERE q.type = $2
                  AND q.difficulty = $3
                  AND q.is_active = true
                  AND q.needs_review = false
                  AND q.question_id != ALL($4)
                ORDER BY q.technique_embedding <=> $1
                LIMIT 10
            """, query_vector, q_type, difficulty, seen_ids)

        if not candidates:
            return None
        reranked = rerank(candidates, await get_most_common_trap(tg_id))
        return dict(reranked[0])

    async def get_seen_question_ids(self, tg_id: int) -> list:
        rows = await db.fetch(
            "SELECT DISTINCT question_id FROM attempts WHERE tg_id = $1", tg_id
        )
        return [r['question_id'] for r in rows]

    def _adjacent(self, difficulty: str, direction: str) -> str | None:
        idx = DIFFICULTY_ORDER.index(difficulty)
        if direction == 'easier':
            return DIFFICULTY_ORDER[idx - 1] if idx > 0 else None
        return DIFFICULTY_ORDER[idx + 1] if idx < 2 else None

    async def _second_weakest(self, tg_id, group, exclude) -> str | None:
        rows = await db.fetch("""
            SELECT subskill FROM user_skill_scores
            WHERE tg_id = $1 AND subskill = ANY($2) AND subskill != $3
            ORDER BY score ASC LIMIT 1
        """, tg_id, group, exclude)
        return rows[0]['subskill'] if rows else None
```

---

## 15. Reranker

```python
# retrieval/reranker.py

def rerank(
    candidates: list[dict],
    profile_trap: str,
    weights: tuple[float, float, float] = (0.5, 0.3, 0.2)
) -> list[dict]:
    """
    Composite score: similarity × 0.5 + trap_match × 0.3 + difficulty_fit × 0.2
    Weights are starting points. Log decisions and tune after 2 weeks of data.
    """
    w_sim, w_trap, w_diff = weights
    scored = []
    for q in candidates:
        sim = q.get('similarity', 0.0)
        traps = q.get('traps_present', [])
        trap = 1.0 if profile_trap and profile_trap in traps else 0.0
        composite = w_sim * sim + w_trap * trap + w_diff * 1.0
        scored.append((composite, q))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [q for _, q in scored]
```

---

## 16. Embedding

```python
def build_embed_text(tags: dict) -> str:
    """
    Three fields, ~40 tokens. The cognitive fingerprint.
    Do NOT add secondary_skill, solving_strategy, cognitive_operation —
    those columns are dropped. The narrow embedding is correct.
    """
    traps = tags.get('traps_present', [])
    trap_str = traps[0] if traps else 'none'
    return (
        f"{tags['one_line_technique']}\n"
        f"Skill: {tags['subskill']}\n"
        f"Trap: {trap_str}"
    )
```

---

## 17. Question Context Helper

```python
# handlers/practice/common.py

def get_question_context(question: dict, passage: dict = None) -> str:
    """
    Return the text context shown alongside a question.
    RC: full passage text
    va_summary / va_sentence_insertion: source paragraph
    PJ / va_wrong_one_out: the labeled sentences
    Others: empty string (question is self-contained)
    """
    q_type = question['type']

    if q_type == 'rc_question':
        return passage['full_text'] if passage else ""

    if q_type in ('va_summary', 'va_sentence_insertion'):
        return question.get('source_text', '')

    if q_type in ('va_wrong_one_out', 'pj'):
        sentences = question.get('sentences') or {}
        return "\n".join(f"{k}) {v}" for k, v in sentences.items())

    return ""
```

---

## 18. VA Handler — All Types

```python
# handlers/practice/va.py

VA_TYPE_LABELS = {
    "va_grammar":             "Grammar",
    "va_sentence_correction": "Sentence Correction",
    "va_vocab":               "Vocabulary",
    "va_fill_in_blank":       "Fill in the Blank",
    "va_wrong_one_out":       "Odd One Out",
    "va_sentence_insertion":  "Sentence Insertion",
    "va_summary":             "Passage Summary",
}

async def show_va_menu(message_or_callback, state, profile, bot):
    """Show VA subtype selection. All 7 types as inline buttons."""
    await reply_to(
        message_or_callback,
        "What do you want to work on?",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Grammar", callback_data="va_type_va_grammar"),
                InlineKeyboardButton("Vocabulary", callback_data="va_type_va_vocab"),
            ],
            [
                InlineKeyboardButton("Sentence Correction",
                                    callback_data="va_type_va_sentence_correction"),
                InlineKeyboardButton("Fill in Blank",
                                    callback_data="va_type_va_fill_in_blank"),
            ],
            [
                InlineKeyboardButton("Odd One Out",
                                    callback_data="va_type_va_wrong_one_out"),
                InlineKeyboardButton("Sentence Insertion",
                                    callback_data="va_type_va_sentence_insertion"),
            ],
            [
                InlineKeyboardButton("Passage Summary",
                                    callback_data="va_type_va_summary"),
            ],
        ])
    )

async def handle_va_type_selection(callback, va_type: str, state, profile, bot):
    """Called when student taps a VA type button."""
    selector = PracticeSelector()
    question = await selector.get_va(profile, va_type)

    if not question:
        await callback.answer()
        await callback.message.reply_text(
            f"No {VA_TYPE_LABELS[va_type]} questions available right now. "
            "Try a different type!"
        )
        return

    session_id = await create_session(profile['tg_id'], 'va')
    state_update = {
        "state": "VA_ACTIVE",
        "session_id": session_id,
        "mode": "va",
        "va_type": va_type,
        "questions_in_set": [question['question_id']],
        "current_question_index": 0,
        "questions_answered": {},
        "questions_remaining": [question['question_id']],
        "session_started_at": datetime.utcnow().isoformat(),
    }
    await set_state(profile['tg_id'], state_update)

    context_text = get_question_context(question)
    await send_va_question(callback.message, question, context_text, va_type, bot)

async def send_va_question(
    message, question: dict, context_text: str, va_type: str, bot
):
    """Send a VA question with appropriate context and answer buttons."""
    type_label = VA_TYPE_LABELS.get(va_type, "VA")

    parts = [f"📝 <b>{type_label}</b>"]

    if context_text:
        parts.append(f"\n<i>{escape_html(context_text)}</i>\n")

    parts.append(escape_html(question['question_text']))

    options = question.get('options', {})
    for letter in ['A', 'B', 'C', 'D']:
        if letter in options:
            parts.append(f"\n{letter}) {escape_html(options[letter])}")

    text = "\n".join(parts)
    await send_long_message(message.chat_id, text, bot,
                            reply_markup=answer_keyboard())
```

---


## 19. Tagger — Six Type-Specific Prompts (GAP 3 FIX — real few-shot examples)

Each prompt lists only the skills and traps legal for that question type, AND contains actual few-shot examples drawn from the 48 seed PYQs. Do not leave placeholders — Flash produces generic tags without real examples.

```python
# ingest/tagger.py

# ─────────────────────────────────────────────────────────────────────────────
# RC TAGGER
# ─────────────────────────────────────────────────────────────────────────────

RC_TAGGER_PROMPT = """Tag this RC question using ONLY the listed values.
Return valid JSON matching the schema below.

VALID SUBSKILLS (pick exactly one):
inference_basic | strengthen_weaken | main_idea_full_passage |
author_tone | specific_detail | vocab_in_context |
purpose_of_example | logical_structure

VALID TRAPS (1-3 items per question; never mix 'none' with others):
half_right_half_wrong | out_of_scope | too_extreme |
true_but_not_inferable | content_over_purpose | other | none

OPTION_TRAPS: for each wrong option assign one trap from the list above.
Correct option = null. Example:
{{"A": "out_of_scope", "B": null, "C": "too_extreme", "D": "true_but_not_inferable"}}

DIFFICULTY: easy | medium | hard (use hard for CAT-level multi-step reasoning)

ONE_LINE_TECHNIQUE: a single sentence (≤25 words) naming the cognitive move
that cracks this question. This is the embedding anchor — make it specific
and transferable, not a restatement of the question.

─── FEW-SHOT EXAMPLES ───

EXAMPLE 1 (main_idea_full_passage):
Passage topic: conservation biology — western barred bandicoot
Question: Which one of the following statements provides a gist of this passage?
A) The onslaught of animals brought in by the British led to the extinction of the western barred bandicoot.
B) Marsupials are going extinct due to the colonial era transformation of the ecosystem.
C) A type of bandicoots was nearly wiped out by invasive species but rescuers now pin hopes on a remnant island population.
D) The negligent attitude of the British colonists led to their annihilation.
Correct: C
Explanation: C captures both halves — near-wipeout by invasive species AND the present-day revival effort.

Expected tag output:
{{
  "subskill": "main_idea_full_passage",
  "traps_present": ["too_extreme", "out_of_scope", "half_right_half_wrong"],
  "option_traps": {{"A": "too_extreme", "B": "out_of_scope", "C": null, "D": "half_right_half_wrong"}},
  "difficulty": "medium",
  "one_line_technique": "Main idea must cover both halves of the passage — problem AND response; reject options that overstate or cover only one side."
}}

EXAMPLE 2 (purpose_of_example):
Passage topic: streaming services and digital art
Question: What is the purpose of the 'Netflix editing Stranger Things' example used in the passage?
A) To show that art in the digital age is no longer sacrosanct.
B) To show streaming services control access to the cultural commons.
C) To show unsubstantiated reports are increasing distrust of streaming services.
D) To show a practice that justifies fears that streaming services cannot be trusted as custodians of cultural artefacts.
Correct: D
Explanation: The example directly follows 'seemed like vindication to those who had long warned' — its rhetorical job is to evidence pre-existing distrust.

Expected tag output:
{{
  "subskill": "purpose_of_example",
  "traps_present": ["content_over_purpose", "out_of_scope", "half_right_half_wrong"],
  "option_traps": {{"A": "content_over_purpose", "B": "out_of_scope", "C": "half_right_half_wrong", "D": null}},
  "difficulty": "hard",
  "one_line_technique": "For purpose questions, ask what JOB this example does in the argument — not what it describes; content-over-purpose is the dominant trap."
}}

EXAMPLE 3 (inference_basic):
Passage topic: crafts and labour
Question: We can infer from the passage that medieval crafts guilds resembled mass production in that both
A) discouraged innovation by restricting entry through strict rules.
B) did not always employ egalitarian production processes.
C) did not necessarily promote creativity.
D) focused excessively on product quality.
Correct: C
Explanation: Mass production prioritises efficiency; guilds' hierarchy 'knocked the innovative spirit out'. Shared trait = failure to promote creativity.

Expected tag output:
{{
  "subskill": "inference_basic",
  "traps_present": ["half_right_half_wrong", "out_of_scope", "true_but_not_inferable"],
  "option_traps": {{"A": "half_right_half_wrong", "B": "true_but_not_inferable", "C": null, "D": "out_of_scope"}},
  "difficulty": "hard",
  "one_line_technique": "Comparison inference = find the trait true of BOTH items; eliminate options true of only one side."
}}

─── QUESTION TO TAG ───

Passage topic: {topic}
Question: {question_text}
A) {A}
B) {B}
C) {C}
D) {D}
Correct: {correct_option}
Explanation: {explanation}

Return JSON only."""


# ─────────────────────────────────────────────────────────────────────────────
# PJ TAGGER
# ─────────────────────────────────────────────────────────────────────────────

PJ_TAGGER_PROMPT = """Tag this PJ (para jumble) question using ONLY the listed values.

VALID SUBSKILLS (pick one):
structural_identification | sequence_logic | pronoun_reference | example_principle_link

Choose based on the dominant clue that solves the PJ:
- structural_identification: opener/closer locked by topic sentence or conclusion markers
- sequence_logic: transition words (therefore, however, so, despite) force order
- pronoun_reference: pronouns (it, they, this) force antecedent to come first
- example_principle_link: general claim → specific example mandatory order

VALID TRAPS: out_of_scope | other | none (PJs rarely have option-level traps)

PJ_CONNECTOR_MAP: for each sentence with a transition word/phrase, output:
{{"<sentence_label>": {{"connector": "<word>", "expected_position": <1-4 or 1-5>, "cannot_be_opening": <bool>}}}}
Empty {{}} if no clear connectors.

OPENING_CLUE: one sentence explaining why the first sentence cannot be anything else.

DIFFICULTY: easy | medium | hard

─── FEW-SHOT EXAMPLES ───

EXAMPLE 1 (sequence_logic):
Sentences:
1. Algorithms hosted on the internet are accessed by many, so biases in AI models have resulted in much larger impact.
2. Though 'algorithmic bias' is the popular term, the foundation of such bias is not in algorithms, but in the data.
3. Despite their widespread impact, it is relatively easier to fix AI biases than human-generated biases.
4. The impact of biased decisions made by humans is localised, but with the advent of AI, the impact is spread over a much wider scale.
Correct order: 4,1,2,3
Explanation: 4 opens the contrast (localised vs widespread). 1 extends via 'so'. 2 clarifies the TRUE source via 'though'. 3 concludes via 'despite'.

Expected tag output:
{{
  "subskill": "sequence_logic",
  "connector_type": "contrast_then_clarification",
  "opening_clue": "Sentence 4 stands alone with no backward reference; all others have connectors pointing back.",
  "pj_connector_map": {{
    "1": {{"connector": "so", "expected_position": 2, "cannot_be_opening": true}},
    "2": {{"connector": "though", "expected_position": 3, "cannot_be_opening": true}},
    "3": {{"connector": "despite", "expected_position": 4, "cannot_be_opening": true}}
  }},
  "traps_present": ["none"],
  "option_traps": {{}},
  "difficulty": "medium",
  "one_line_technique": "Lock the opener as the only sentence with no backward reference; then use connectors (though, despite, so) to fix the rest."
}}

EXAMPLE 2 (structural_identification):
Sentences:
1. What precisely are the 'unusual elements' that make a particular case so attractive to a certain kind of audience?
2. It might be a particularly savage level of depravity, very often related to the amount of mystery involved.
3. Unsolved, and perhaps unsolvable cases offer something that 'ordinary' murder doesn't.
4. Why are some crimes destined for perpetual re-examination and others locked into permanent obscurity?
Correct order: 4,1,2,3
Explanation: 4 opens with the broad question. 1 narrows. 2 answers. 3 concludes with the mystery-specific payoff.

Expected tag output:
{{
  "subskill": "structural_identification",
  "connector_type": "question_to_answer",
  "opening_clue": "Sentence 4 is a broad framing question; sentence 1 ('What precisely...') is a narrowing question that cannot precede the broad one.",
  "pj_connector_map": {{
    "1": {{"connector": "what precisely", "expected_position": 2, "cannot_be_opening": false}},
    "3": {{"connector": "unsolved", "expected_position": 4, "cannot_be_opening": true}}
  }},
  "traps_present": ["none"],
  "option_traps": {{}},
  "difficulty": "medium",
  "one_line_technique": "When two questions appear in a PJ, the broader (why X in general) comes before the narrower (what precisely is X)."
}}

─── QUESTION TO TAG ───

Sentences: {sentences}
Correct order: {correct_order}
Explanation: {explanation}

Return JSON only."""


# ─────────────────────────────────────────────────────────────────────────────
# VA STRUCTURAL TAGGER (odd-one-out)
# ─────────────────────────────────────────────────────────────────────────────

VA_STRUCTURAL_TAGGER_PROMPT = """Tag this odd-one-out question.

VALID SUBSKILLS: sentence_odd_one_out
VALID TRAPS: theme_break | out_of_scope | other | none
OPTION_TRAPS: wrong option = trap, correct option = null.
DIFFICULTY: easy | medium | hard

─── FEW-SHOT EXAMPLES ───

EXAMPLE 1:
Sentences:
1. Animals have an interest in fulfilling their basic needs, but also in avoiding suffering.
2. Singer viewed himself as a utilitarian, presenting a direct moral theory concerning animal rights.
3. He argued for extending moral consideration to animals because animals have significant interests.
4. The event that publicly announced animal rights as a legitimate issue was Peter Singer's Animal Liberation text in 1975.
5. As such, we ought to view their interests alongside and equal to human interests.
Correct answer: Sentence 1 is the odd one
Correct order of other four: 4,2,3,5
Explanation: Sentences 4→2→3→5 chain Singer's utilitarian framework. Sentence 1 is a generic moral claim that doesn't reference Singer.

Expected tag output:
{{
  "subskill": "sentence_odd_one_out",
  "traps_present": ["theme_break"],
  "option_traps": {{"A": null, "B": "theme_break", "C": "theme_break", "D": "theme_break"}},
  "difficulty": "medium",
  "one_line_technique": "Odd-one-out = same topic, wrong angle; if four sentences argue FROM a specific framework and one argues WITHOUT referencing it, that one is odd."
}}

EXAMPLE 2:
Sentences:
1. Urbanites have more and better options for getting around: Uber, dockless bicycles, scooters.
2. When more people use buses or trains the service usually improves.
3. Worsening services, terrorist attacks and a rise in fares have been blamed for the trend.
4. Public transport is being squeezed structurally as people's need to travel is diminishing.
5. There has been a puzzling decline in the use of urban public transport in the west.
Correct answer: Sentence 2 is the odd one
Correct order of other four: 5,3,4,1
Explanation: 5 introduces puzzle. 3 gives proximate causes. 4 gives structural cause. 1 reinforces with alternatives. Sentence 2 claims the opposite dynamic.

Expected tag output:
{{
  "subskill": "sentence_odd_one_out",
  "traps_present": ["theme_break"],
  "option_traps": {{"A": "theme_break", "B": null, "C": "theme_break", "D": "theme_break"}},
  "difficulty": "medium",
  "one_line_technique": "Odd-one-out often breaks on DIRECTION not topic; same subject pointing the opposite way is the outlier."
}}

─── QUESTION TO TAG ───

Sentences: {sentences}
Correct answer: {correct_option}
Correct order: {correct_order}
Explanation: {explanation}

Return JSON only."""


# ─────────────────────────────────────────────────────────────────────────────
# VA INSERTION TAGGER
# ─────────────────────────────────────────────────────────────────────────────

VA_INSERTION_TAGGER_PROMPT = """Tag this sentence insertion question.

VALID SUBSKILLS: sentence_insertion
VALID TRAPS: theme_break | out_of_scope | half_right_half_wrong | other | none
OPTION_TRAPS: wrong option = trap, correct option = null.
DIFFICULTY: easy | medium | hard

─── FEW-SHOT EXAMPLES ───

EXAMPLE 1:
Source paragraph: "___(1)___. You can't just put things anywhere you want to. The evolved architecture of the brain is haphazard and disjointed. ___(2)___. Evolution doesn't design things... ___(3)___. The brain is more like a big, old house with piecemeal renovations..."
Missing sentence: "The brain isn't organized the way you might set up your home office or bathroom medicine cabinet."
Options: A) Option 4, B) Option 2, C) Option 1, D) Option 3
Correct: C
Explanation: Blank 1 introduces the contrast between intuitive organization (home office) and the brain's evolutionary complexity. The next sentence ('You can't just put things anywhere') directly builds on this.

Expected tag output:
{{
  "subskill": "sentence_insertion",
  "traps_present": ["theme_break"],
  "option_traps": {{"A": "theme_break", "B": "theme_break", "C": null, "D": "theme_break"}},
  "difficulty": "medium",
  "one_line_technique": "An inserted sentence that introduces a metaphor often goes FIRST; if the same idea is already developed downstream, the sentence is an opener."
}}

EXAMPLE 2:
Source paragraph: "The experience of reading philosophy is often disquieting. When reading philosophy, the values around which one has heretofore organised one's life may come to look provincial, flatly wrong, or even evil. ___(1)___. When beliefs previously held as truths are rendered implausible, new beliefs may be required. ___(2)___. What's worse, philosophers admonish each other to remain unsutured..."
Missing sentence: "This philosophical cut at one's core beliefs, values, and way of life is difficult enough."
Options: A) Blank A, B) Blank B, C) Blank C, D) Blank D
Correct: B
Explanation: Blank B (Option 2) follows the description of values appearing 'provincial, flatly wrong'. The sentence summarises that cut and leads into 'what's worse'.

Expected tag output:
{{
  "subskill": "sentence_insertion",
  "traps_present": ["theme_break"],
  "option_traps": {{"A": "theme_break", "B": null, "C": "theme_break", "D": "theme_break"}},
  "difficulty": "medium",
  "one_line_technique": "'This X is difficult enough' patterns follow a description of X; identify what X is and place the sentence after it, not before."
}}

EXAMPLE 3:
Source paragraph: (Renaissance music paragraph) "...This music boom lasted for thirty years... ___(2)___. The rebirth in both literature and music originated in Italy... Renaissance music was mostly polyphonic in texture. ___(3)___. Extreme contrasts in dynamics, rhythm, and tone colour do not occur..."
Missing sentence: "Comprehending a wide range of emotions, Renaissance music nevertheless portrayed all emotions in a balanced and moderate fashion."
Options: A) Option 3, B) Option 4, C) Option 1, D) Option 2
Correct: A
Explanation: Position 3 follows 'Renaissance music was mostly polyphonic' and sets up 'Extreme contrasts... do not occur'. The emotional-balance claim bridges polyphony and the lack-of-contrasts naturally.

Expected tag output:
{{
  "subskill": "sentence_insertion",
  "traps_present": ["theme_break"],
  "option_traps": {{"A": null, "B": "theme_break", "C": "theme_break", "D": "theme_break"}},
  "difficulty": "medium",
  "one_line_technique": "A general claim goes BEFORE specific instances that illustrate it; find the slot where the sentence bridges general→specific."
}}

─── QUESTION TO TAG ───

Source paragraph: {source_text}
Question: {question_text}
A) {A}
B) {B}
C) {C}
D) {D}
Correct: {correct_option}
Explanation: {explanation}

Return JSON only."""


# ─────────────────────────────────────────────────────────────────────────────
# VA SUMMARY TAGGER
# ─────────────────────────────────────────────────────────────────────────────

VA_SUMMARY_TAGGER_PROMPT = """Tag this passage summary question.

VALID SUBSKILLS: passage_summary
VALID TRAPS: out_of_scope | too_extreme | half_right_half_wrong | other | none
OPTION_TRAPS: wrong option = trap, correct option = null.
DIFFICULTY: easy | medium | hard

─── FEW-SHOT EXAMPLES ───

EXAMPLE 1:
Source paragraph: "Scientific research shows that many animals are very intelligent... Many animals also display wide-ranging emotions, including joy, happiness, empathy, compassion, grief... It's not surprising that animals share many emotions with us because we also share brain structures, located in the limbic system, that are the seat of our emotions."
Question: Which option best captures the essence of the passage?
A) The advanced sensory and motor abilities of animals is the reason why they can display wide-ranging emotions.
B) The similarity in brain structure explains why animals show emotions typically associated with humans.
C) Animals can show emotions which are typically associated with humans.
D) Animals are more intelligent than us in sensing danger and detecting diseases.
Correct: B
Explanation: B preserves the causal backbone — shared limbic structures explain shared emotions. C drops the WHY.

Expected tag output:
{{
  "subskill": "passage_summary",
  "traps_present": ["half_right_half_wrong", "out_of_scope"],
  "option_traps": {{"A": "half_right_half_wrong", "B": null, "C": "half_right_half_wrong", "D": "out_of_scope"}},
  "difficulty": "medium",
  "one_line_technique": "A summary must preserve the passage's causal backbone; options stating only the observation (without mechanism) are incomplete."
}}

EXAMPLE 2:
Source paragraph: "Colonialism is not a modern phenomenon... In the sixteenth century, colonialism changed decisively because of technological developments in navigation... The modern European colonial project emerged when it became possible to move large numbers of people across the ocean..."
Question: Which option best captures the essence of the passage?
A) Colonialism surged in the 16th century due to advancements in navigation, enabling British settlements.
B) As a result of developments in navigation, European colonialism led to displacement and political changes in the 16th century.
C) Colonialism, conceptualized in the 16th century, allowed colonizers to expand.
D) Technological advancements in navigation in the 16th century transformed colonialism, enabling Europeans to establish settlements and exert political dominance over distant regions.
Correct: D
Explanation: D preserves continuity-vs-change: colonialism was TRANSFORMED, not invented. C wrongly says 'conceptualized in the 16th century'.

Expected tag output:
{{
  "subskill": "passage_summary",
  "traps_present": ["half_right_half_wrong", "too_extreme"],
  "option_traps": {{"A": "too_extreme", "B": "half_right_half_wrong", "C": "half_right_half_wrong", "D": null}},
  "difficulty": "medium",
  "one_line_technique": "Summary must preserve continuity-vs-change distinction; when passage says X 'changed decisively', don't pick an option saying X was 'conceptualized'."
}}

EXAMPLE 3:
Source paragraph: "Certain codes may be so widely distributed... that they appear not to be constructed but 'naturally' given... However, this does not mean that no codes have intervened; rather, that the codes have been profoundly naturalized. The operation of naturalized codes reveals... the depth and near-universality of the codes in use. This has the (ideological) effect of concealing the practices of coding."
Question: Which option best captures the essence of the passage?
A) All codes have a natural origin but some are so widespread that they become universal.
B) Not all codes are natural but certain codes are naturalized. Ideology aims to hide the mechanism of coding behind signs.
C) Language and visual signs are codes. However, some codes are so widespread that they seem naturally given and also hide the mechanism of coding behind the signs.
D) Learning signs at an early age makes all such codes appear natural. This naturalization is the effect of ideology.
Correct: C
Explanation: C captures both moves — codes SEEM natural despite being constructed AND this appearance conceals the coding mechanism. B attributes to ideology an 'aim' to hide; passage calls it an effect.

Expected tag output:
{{
  "subskill": "passage_summary",
  "traps_present": ["too_extreme", "out_of_scope", "half_right_half_wrong"],
  "option_traps": {{"A": "too_extreme", "B": "out_of_scope", "C": null, "D": "half_right_half_wrong"}},
  "difficulty": "hard",
  "one_line_technique": "Summary options must not add claims the passage doesn't make — watch for added intent claims ('aims to') or absolute claims ('all codes')."
}}

─── QUESTION TO TAG ───

Source paragraph: {source_text}
Question: {question_text}
A) {A}
B) {B}
C) {C}
D) {D}
Correct: {correct_option}
Explanation: {explanation}

Return JSON only."""


# ─────────────────────────────────────────────────────────────────────────────
# VA SEMANTIC TAGGER (grammar / vocab / fill-in-blank)
# ─────────────────────────────────────────────────────────────────────────────

VA_SEMANTIC_TAGGER_PROMPT = """Tag this grammar/vocabulary question.

VALID SUBSKILLS (pick one — must match question type):
- For va_grammar or va_sentence_correction: grammar_rule
- For va_vocab: vocabulary_meaning
- For va_fill_in_blank: vocabulary_meaning or paragraph_completion

VALID TRAPS: out_of_scope | half_right_half_wrong | other | none
OPTION_TRAPS: wrong option = trap, correct option = null.
DIFFICULTY: easy | medium | hard

─── FEW-SHOT EXAMPLES ───

EXAMPLE 1 (va_grammar — subject-verb agreement):
Question: Which of the following sentences is grammatically correct?
A) Neither the manager nor the employees was aware of the change.
B) Neither the manager nor the employees were aware of the change.
C) Neither the manager nor the employees is aware of the change.
D) Neither the manager nor the employees has been aware of the change.
Correct: B
Explanation: With 'neither... nor', the verb agrees with the subject closest to it. 'Employees' is plural → 'were'.

Expected tag output:
{{
  "subskill": "grammar_rule",
  "traps_present": ["half_right_half_wrong"],
  "option_traps": {{"A": "half_right_half_wrong", "B": null, "C": "half_right_half_wrong", "D": "half_right_half_wrong"}},
  "difficulty": "medium",
  "one_line_technique": "With neither/nor or either/or, verb agrees with the subject CLOSER to it, not the first subject."
}}

EXAMPLE 2 (va_vocab — contextual meaning):
Question: Choose the word that best fits the blank: "The professor's _____ remarks during the lecture surprised the students, as he was usually reserved."
A) loquacious
B) taciturn
C) reticent
D) circumspect
Correct: A
Explanation: The contrast 'usually reserved' signals the blank needs a word meaning the opposite — talkative. Only 'loquacious' fits.

Expected tag output:
{{
  "subskill": "vocabulary_meaning",
  "traps_present": ["half_right_half_wrong"],
  "option_traps": {{"A": null, "B": "half_right_half_wrong", "C": "half_right_half_wrong", "D": "out_of_scope"}},
  "difficulty": "medium",
  "one_line_technique": "Contextual vocabulary — look for a contrast or similarity marker in the surrounding sentence that constrains the blank's meaning."
}}

EXAMPLE 3 (va_sentence_correction — modifier placement):
Question: Which sentence is correctly constructed?
A) Walking through the garden, the flowers bloomed beautifully.
B) Walking through the garden, she saw flowers blooming beautifully.
C) The flowers, walking through the garden, bloomed beautifully.
D) Beautifully blooming, she walked through the garden of flowers.
Correct: B
Explanation: The participle 'walking' must modify a human subject, not 'flowers'. B is the only option where 'walking' correctly modifies 'she'.

Expected tag output:
{{
  "subskill": "grammar_rule",
  "traps_present": ["half_right_half_wrong"],
  "option_traps": {{"A": "half_right_half_wrong", "B": null, "C": "half_right_half_wrong", "D": "half_right_half_wrong"}},
  "difficulty": "medium",
  "one_line_technique": "Dangling modifier test — the introductory participial phrase must modify the subject of the main clause; 'walking' needs a person, not an object."
}}

─── QUESTION TO TAG ───

Question type: {type}
Question: {question_text}
A) {A}
B) {B}
C) {C}
D) {D}
Correct: {correct_option}
Explanation: {explanation}

Return JSON only."""


# ─────────────────────────────────────────────────────────────────────────────
# DISPATCHER
# ─────────────────────────────────────────────────────────────────────────────

def get_tagger_prompt(q_type: str, question: dict) -> str:
    """Select prompt based on question type and format it with question fields."""
    mapping = {
        'rc_question':            RC_TAGGER_PROMPT,
        'pj':                     PJ_TAGGER_PROMPT,
        'va_wrong_one_out':       VA_STRUCTURAL_TAGGER_PROMPT,
        'va_sentence_insertion':  VA_INSERTION_TAGGER_PROMPT,
        'va_summary':             VA_SUMMARY_TAGGER_PROMPT,
        'va_grammar':             VA_SEMANTIC_TAGGER_PROMPT,
        'va_sentence_correction': VA_SEMANTIC_TAGGER_PROMPT,
        'va_vocab':               VA_SEMANTIC_TAGGER_PROMPT,
        'va_fill_in_blank':       VA_SEMANTIC_TAGGER_PROMPT,
    }
    template = mapping.get(q_type, RC_TAGGER_PROMPT)

    # Build format kwargs with safe defaults
    fmt_kwargs = {
        "type": q_type,
        "topic": question.get("topic") or question.get("passage_topic") or "general",
        "question_text": question.get("question_text", ""),
        "correct_option": question.get("correct_option") or "",
        "correct_order": question.get("correct_order") or "",
        "explanation": question.get("explanation", ""),
        "source_text": question.get("source_text") or question.get("passage_text") or "",
        "sentences": question.get("sentences") or {},
    }
    opts = question.get("options") or {}
    fmt_kwargs["A"] = opts.get("A", "")
    fmt_kwargs["B"] = opts.get("B", "")
    fmt_kwargs["C"] = opts.get("C", "")
    fmt_kwargs["D"] = opts.get("D", "")

    return template.format(**fmt_kwargs)
```

### Two-pass model strategy (unchanged from v3)

- **Pass 1 — Gemini Flash:** return structured tags (`subskill`, `traps_present`, `option_traps`, `difficulty`, `connector_type`, `opening_clue`, `pj_connector_map`). Fast, cheap, reliable for classification.
- **Pass 2 — Claude Sonnet:** generate `one_line_technique` from the Flash output + question text. Sonnet is better at producing the specific, transferable one-liner that drives retrieval.

Both passes consume the same prompts above. Pass 2 ignores the example tag outputs and only writes `one_line_technique`.

---

## 20. Ingest Pipeline — store_question

```python
# ingest/pipeline.py

from config import SUBSKILL_TO_SKILL, SKILL_TYPE_MATRIX
import json
import logging

logger = logging.getLogger(__name__)


async def store_question(question: dict) -> None:
    """
    Store a tagged, verified question. Validates (type, subskill) pair and
    flags needs_review if mismatch or verifier disagreement.
    """
    is_flagged = question.get('_verification_flagged', False) \
                 or question.get('needs_review', False)
    tags = question.get('_tags', {})

    # Validate (type, subskill) pair
    q_type = question['type']
    subskill = tags.get('subskill') or question.get('subskill')
    legal_subskills = SKILL_TYPE_MATRIX.get(q_type, [])
    if subskill not in legal_subskills:
        logger.warning(
            f"Illegal (type={q_type}, subskill={subskill}) "
            f"for question_id={question['question_id']} — flagging for review"
        )
        is_flagged = True

    # Derive student-facing skill
    skill_value = SUBSKILL_TO_SKILL.get(subskill) if subskill else None

    # Create passage row only for rc_question
    if q_type == 'rc_question' and question.get('passage_id'):
        await db.execute("""
            INSERT INTO passages (passage_id, full_text, word_count, topic, tone,
                                  source, year, difficulty)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (passage_id) DO NOTHING
        """, question['passage_id'],
             question.get('passage_text') or question.get('full_text', ''),
             len((question.get('passage_text') or question.get('full_text', '')).split()),
             question.get('topic'),
             question.get('tone'),
             question['source'],
             question.get('year'),
             question.get('difficulty', 'medium'))

    # source_text only for va_summary and va_sentence_insertion
    source_text = question.get('source_text') \
        if q_type in ('va_summary', 'va_sentence_insertion') else None

    await db.execute("""
        INSERT INTO questions (
            question_id, type, passage_id, source_text, question_text,
            options, correct_option, correct_order, explanation,
            rc_question_type, sentences, connector_type, opening_clue,
            pj_connector_map, skill, subskill, traps_present, option_traps,
            one_line_technique, taxonomy_version, tagged_at, tagger_model,
            technique_embedding, difficulty, source, year, question_order,
            needs_review
        ) VALUES (
            $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,
            $11,$12,$13,$14,$15,$16,$17,$18,$19,$20,
            $21,$22,$23,$24,$25,$26,$27,$28
        )
        ON CONFLICT (question_id) DO NOTHING
    """,
        question['question_id'],
        q_type,
        question.get('passage_id'),
        source_text,
        question['question_text'],
        json.dumps(question.get('options')) if question.get('options') else None,
        question.get('correct_option'),
        question.get('correct_order'),
        question.get('explanation'),
        question.get('rc_question_type') if q_type == 'rc_question' else None,
        json.dumps(question.get('sentences')) if question.get('sentences') else None,
        tags.get('connector_type') or question.get('connector_type'),
        tags.get('opening_clue') or question.get('opening_clue'),
        json.dumps(tags.get('pj_connector_map') or question.get('pj_connector_map') or {}),
        skill_value,
        subskill,
        tags.get('traps_present') or question.get('traps_present') or [],
        json.dumps(tags.get('option_traps') or question.get('option_traps') or {}),
        tags.get('one_line_technique') or question.get('one_line_technique'),
        tags.get('taxonomy_version', 1),
        tags.get('tagged_at'),
        tags.get('tagger_model'),
        question['_vector'],
        question.get('difficulty', 'medium'),
        question['source'],
        question.get('year'),
        question.get('question_order'),
        is_flagged
    )
```

---

## 21. Input JSON Format

### rc_question

```json
{
  "type": "rc_question",
  "question_id": "cat_pyq_bandicoots_q1",
  "passage_id": "cat_pyq_bandicoots",
  "question_text": "Which one of the following statements provides a gist of this passage?",
  "options": {"A": "...", "B": "...", "C": "...", "D": "..."},
  "correct_option": "C",
  "explanation": "...",
  "rc_question_type": "main_idea",
  "source": "cat_official",
  "year": 2024,
  "difficulty": "medium",
  "question_order": 1
}
```

### pj

```json
{
  "type": "pj",
  "question_id": "cat2023s1_aibias_pj",
  "question_text": "The four sentences, when properly sequenced, yield a coherent paragraph. Sequence them.",
  "sentences": {
    "1": "...",
    "2": "...",
    "3": "...",
    "4": "..."
  },
  "correct_order": "4,1,2,3",
  "explanation": "...",
  "source": "cat_official",
  "year": 2023,
  "difficulty": "medium"
}
```

### va_summary / va_sentence_insertion (use source_text, not passage_id)

```json
{
  "type": "va_summary",
  "question_id": "cat_pyq_animals_summary",
  "source_text": "Scientific research shows that many animals... [full paragraph]",
  "question_text": "Which option best captures the essence of the passage?",
  "options": {"A": "...", "B": "...", "C": "...", "D": "..."},
  "correct_option": "B",
  "explanation": "...",
  "source": "cat_official",
  "year": 2024,
  "difficulty": "medium"
}
```

### va_wrong_one_out

```json
{
  "type": "va_wrong_one_out",
  "question_id": "cat_pyq_singer_odd",
  "question_text": "Identify the odd sentence.",
  "sentences": {"1": "...", "2": "...", "3": "...", "4": "...", "5": "..."},
  "options": {"A": "Sentence 1", "B": "Sentence 2", "C": "Sentence 3", "D": "Sentence 4"},
  "correct_option": "A",
  "correct_order": "4,2,3,5",
  "explanation": "..."
}
```

### va_grammar / va_vocab / va_fill_in_blank / va_sentence_correction

Standard 4-option MCQ. No `source_text`, no `sentences`, no `passage_id`.

---

## 22. System Prompt

```python
# agent/prompts.py

SYSTEM_PROMPT_TEMPLATE = """You are a CAT VARC expert tutor — direct, specific, practical.

TEACHING RULES:

1. Never give the answer before engaging with the student's reasoning.
   Ask what they picked and why before explaining.

2. Always name the trap. Wrong CAT options fail for specific reasons.
   Use these trap names: half_right_half_wrong, out_of_scope, too_extreme,
   theme_break, true_but_not_inferable, content_over_purpose.

3. Connect to student's pattern. If profile shows repeated trap: say so.

4. Keep explanations under 200 words. Offer to go deeper if needed.

5. For tone questions use only: critical, appreciative, neutral, sardonic,
   cautious, optimistic, pessimistic, analytical, ironic, measured.

6. Format with HTML only:
   <b>bold</b> for labels and key terms
   <i>italic</i> for passage quotes

7. End every RC explanation with one follow-up question on a related concept.

Student context:
{context}"""
```

---

## 23. Stats Handler

Shows scores at student-facing skill level (7 skills), not subskill level.

```python
# handlers/stats.py

from config import SUBSKILL_TO_SKILL, STUDENT_SKILLS, SKILL_DISPLAY_NAMES

async def handle_stats(message, state, profile, bot):
    tg_id = profile['tg_id']

    rows = await db.fetch(
        "SELECT subskill, score FROM user_skill_scores WHERE tg_id = $1", tg_id
    )

    skill_scores = {}
    for row in rows:
        student_skill = SUBSKILL_TO_SKILL.get(row['subskill'])
        if not student_skill:
            continue
        if student_skill not in skill_scores:
            skill_scores[student_skill] = []
        skill_scores[student_skill].append(row['score'])

    avg_by_skill = {
        skill: sum(scores) / len(scores)
        for skill, scores in skill_scores.items()
    }

    week = await get_week_stats(tg_id)

    lines = ["<b>📊 Your VARC stats</b>\n"]
    if week['questions'] > 0:
        lines.append(
            f"This week: {week['questions']} questions "
            f"· {week['accuracy']:.0f}% accuracy"
        )

    streak = profile.get('current_streak', 0)
    if streak > 1:
        lines.append(f"🔥 {streak}-day streak")

    lines.append("\n<b>Skill breakdown:</b>")
    for skill in STUDENT_SKILLS:
        avg = avg_by_skill.get(skill, 0.5)
        label = SKILL_DISPLAY_NAMES[skill]
        bar = "█" * int(avg * 10) + "░" * (10 - int(avg * 10))
        lines.append(f"{label}: {avg*100:.0f}% {bar}")

    trap = profile.get('most_common_trap', 'none')
    if trap and trap != 'none':
        lines.append(f"\nMost common mistake: {trap.replace('_', ' ')}")

    await send_long_message(
        message.chat_id, "\n".join(lines), bot,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("Drill my weakest", callback_data="drill_weakest"),
            InlineKeyboardButton("Full breakdown", callback_data="stats_full"),
        ]])
    )
```

---

## 24. main.py — Startup with Assertions

```python
# main.py

from config import settings, ALL_SUBSKILLS
from fastapi import FastAPI, Request, Response
from telegram import Update
from telegram.ext import Application

WEBHOOK_PATH = f"/webhook/{settings.WEBHOOK_SECRET}"

app = FastAPI()
ptb_app = None

@app.on_event("startup")
async def startup():
    global ptb_app

    # Taxonomy consistency check — fail fast at startup
    from retrieval.technique_queries import SUBSKILL_TO_TECHNIQUE_QUERY
    missing = set(ALL_SUBSKILLS) - set(SUBSKILL_TO_TECHNIQUE_QUERY.keys())
    extra = set(SUBSKILL_TO_TECHNIQUE_QUERY.keys()) - set(ALL_SUBSKILLS)
    if missing or extra:
        raise RuntimeError(
            f"Taxonomy mismatch: missing={missing}, extra={extra}. "
            "ALL_SUBSKILLS and SUBSKILL_TO_TECHNIQUE_QUERY must have identical keys."
        )

    await init_db_pool()
    await init_redis()

    ptb_app = Application.builder().token(settings.TELEGRAM_BOT_TOKEN).build()
    await ptb_app.initialize()

    webhook_url = f"https://{settings.RAILWAY_PUBLIC_DOMAIN}{WEBHOOK_PATH}"
    await ptb_app.bot.set_webhook(
        url=webhook_url,
        allowed_updates=["message", "callback_query"]
    )

@app.post(WEBHOOK_PATH)
async def webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, ptb_app.bot)
    await route_update(update, ptb_app)
    return Response(status_code=200)

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.post("/admin/cleanup-sessions")
async def cleanup_endpoint(request: Request):
    if request.headers.get("X-Admin-Secret") != settings.ADMIN_REPORTS_SECRET:
        return Response(status_code=403)
    from handlers.session_cleanup import cleanup_stale_sessions
    count = await cleanup_stale_sessions()
    return {"closed": count}

@app.post("/admin/send-reports")
async def reports_endpoint(request: Request):
    if request.headers.get("X-Admin-Secret") != settings.ADMIN_REPORTS_SECRET:
        return Response(status_code=403)
    from handlers.weekly_reports import send_weekly_reports_to_all
    await send_weekly_reports_to_all()
    return {"status": "ok"}
```

---

## 25. DB Client, Redis Client, LLM, Utilities

These are unchanged from v3. Copy exactly:

- `db/client.py` — asyncpg pool with pgvector registration
- `memory/session.py` — init_redis, get_state, set_state, acquire_lock, release_lock
- `agent/llm.py` — LLM wrapper, spend tracking, embed, RateLimitError handling
- `bot/utils.py` — IST helpers, reply_to, send_long_message, escape_html
- `memory/summarizer.py` — session summary generation
- `handlers/session_cleanup.py` (outside the snippet in Section 10)
- `handlers/weekly_reports.py`

If any file referenced above doesn't exist in your v3 copy, stop and ask before proceeding.

---

## 26. Helper Functions Reference

All functions referenced but not defined in this spec. Copy from v3 Section 30 with **one change**: replace any references to the old `user_skill_scores.skill` column with `user_skill_scores.subskill`.

```python
# Changed function — user_skill_scores now has 'subskill' column not 'skill'

async def initialize_skill_scores(tg_id: int):
    from config import ALL_SUBSKILLS
    await db.executemany("""
        INSERT INTO user_skill_scores (tg_id, subskill, score)
        VALUES ($1, $2, 0.5) ON CONFLICT DO NOTHING
    """, [(tg_id, subskill) for subskill in ALL_SUBSKILLS])

# All other helpers unchanged: get_state, set_state, create_session, write_message,
# write_session_snapshot, get_session_messages, get_or_create_user, get_week_stats,
# format_ago, format_skill, home_quick_keyboard, build_context, build_messages_for_llm,
# get_state_from_db_or_redis, reply_to, escape_html, send_long_message,
# get_ist_date, get_seconds_until_ist_midnight, get_most_common_trap.
```

---

## 27. Seed Data — 48 PYQs (primary seed) + minimum fillers

### Primary seed: 48 CAT PYQs

A companion file `dhri_48_pyqs_v4.json` contains all 48 questions in the v4 input format.

**Before ingesting:**

1. **Do not manually re-tag.** These questions are already tagged and verified. Set `manually_tagged = true` in the ingest pipeline for these to skip the Flash+Sonnet tagger.
2. **Three questions have `needs_review: true`:**
   - `cat_pyq_crafts_q4` — official key reasoning is thin
   - `cat2023s1_indian_ocean_q3` — source had duplicate options (options restored semantically)
   - `cat2023s1_geography_q1` — source had duplicate options (options restored semantically)
   These will NOT appear in retrieval until a human reviewer clears `needs_review`.

### Ingest command for the 48 PYQs

```bash
python -m ingest.pipeline \
  --file data/dhri_48_pyqs_v4.json \
  --skip-tagger \
  --skip-verifier
```

The `--skip-tagger` flag tells the pipeline to use the already-present `subskill`, `traps_present`, `option_traps`, `one_line_technique` fields directly. Only the embedder runs.

### Supplement to reach minimum counts

The 48 PYQs give you coverage but not depth. Supplement to reach these targets per subskill before going live:

```
RC (rc_question) — target ~50 total:
  specific_detail:          already 11 → add 4
  inference_basic:          already 8  → add 4
  main_idea_full_passage:   already 3  → add 4
  purpose_of_example:       already 4  → add 3
  author_tone:              already 1  → add 4
  strengthen_weaken:        already 3  → add 2
  vocab_in_context:         already 2  → add 3
  logical_structure:        already 0  → add 3

PJ — target ~15 total:
  sequence_logic:           already 1  → add 4
  structural_identification:already 1  → add 4
  pronoun_reference:        already 0  → add 3
  example_principle_link:   already 0  → add 2

VA-structural — target ~30 total:
  va_wrong_one_out:         already 4  → add 4
  va_sentence_insertion:    already 5  → add 5
  va_summary:               already 5  → add 5
  va_fill_in_blank:         already 0  → add 7

VA-semantic — target ~18 total:
  va_grammar:               already 0  → add 10
  va_vocab:                 already 0  → add 8

Total target after seeding: ~110 questions
```

Supplement from CAT mocks, sectional tests, or teacher-authored items. Run these through the full Flash+Sonnet tagger.

---

## 28. Deployment

### requirements.txt

```
python-telegram-bot==21.0.1
fastapi==0.111.0
uvicorn==0.29.0
asyncpg==0.29.0
upstash-redis==1.1.0
openai==1.30.0
pgvector==0.2.5
streamlit==1.35.0
pydantic==2.7.0
pydantic-settings==2.3.0
python-dotenv==1.0.1
httpx==0.27.0
```

### Deployment checklist

1. Neon: create project, get pooled + direct connection strings
2. Neon: run schema.sql against direct connection, verify all tables
3. Upstash: create Redis instance
4. OpenRouter: set monthly hard cap $10 in dashboard
5. BotFather: create bot, set all 13 commands
6. BotFather: set admin command scope to your tg_id
7. Railway: create service, set all env vars
8. Railway: deploy
9. Verify: `GET /health` → `{"status": "ok"}`
10. Verify: Telegram getWebhookInfo → webhook set
11. Railway: add cleanup cron (`*/10 * * * *`)
12. Railway: add weekly report cron (`30 2 * * 0`)
13. Seed 48 PYQs: `python -m ingest.pipeline --file data/dhri_48_pyqs_v4.json --skip-tagger --skip-verifier`
14. Verify startup assertion passes (no RuntimeError in logs)
15. Verify DB: `SELECT count(*), needs_review FROM questions GROUP BY needs_review` → 45 false, 3 true
16. Run manual test script (Section 30)

---

## 29. What Does Not Change from v3

Zero changes needed in:

```
bot/router.py         — user-level lock, rate limit logic
bot/free_text.py      — intent classifier, media handling
bot/commands.py       — /done, /stats, /resume, /help etc
bot/callbacks.py      — callback routing
agent/llm.py          — LLM wrapper, spend tracking, embed
db/client.py          — asyncpg pool, pgvector registration
memory/summarizer.py  — session summary generation
handlers/session_cleanup.py  — cron cleanup
handlers/weekly_reports.py   — weekly report sending
handlers/doubt.py     — doubt mode handler
handlers/concept.py   — concept mode handler
handlers/resume.py    — resume flow
retrieval/reranker.py — reranker formula
railway.json          — deployment config
```

---

## 30. Manual Test Script

```
Onboarding:
  1. /start as new user → year + level → home screen
  2. Verify DB: tg_users, user_profiles, user_skill_scores
  3. Count user_skill_scores rows = len(ALL_SUBSKILLS) = 18
  4. weakest_skill IS NULL (0 attempts)
  5. Home screen has no "Work on X" button

RC Practice:
  6. /rc → passage loads (never one of the 3 needs_review questions)
  7. Answer correctly → attempt in DB, trap_fallen_for='none'
  8. Answer wrong → trap_fallen_for matches option_traps[selected]
  9. /done → session summary in DB

After 10 attempts:
  10. weakest_skill IS NOT NULL (one of 7 student skills)
  11. Home screen shows "Work on X ⚡"

PJ:
  12. /pj → question loads
  13. Valid 4-letter distinct A-D answer → scored
  14. 3-letter answer → not treated as PJ answer
  15. Duplicate letters (AABD) → not treated as PJ answer
  16. pj_mistake_type recorded in attempts

VA — all types:
  17. /va → 7-button menu appears
  18. Tap Grammar → grammar question loads (may say "no questions" if not seeded)
  19. Tap Odd One Out → question loads with sentences visible
  20. Tap Sentence Insertion → question loads with source_text paragraph shown
  21. Tap Passage Summary → question loads with source paragraph shown
  22. Answer submission works for all VA types

Stats:
  23. /stats → shows 7 student-facing skill labels
  24. Never shows subskill names (inference_basic etc) to student

Doubt and concept:
  25. /doubt → Socratic response (asks what you picked first)
  26. Type "how do I approach inference" → concept mode activates

Resume:
  27. /rc → answer 1 → wait for cron → session closed was_completed=false
  28. session_snapshot exists
  29. /resume → session appears with ⚠️
  30. Tap → validates questions active → resumes

Rate limit:
  31. Set counter to 50 → next free text → limit message
  32. /stats (command) still works after limit
  33. Button tap still works after limit

Retrieval fallback:
  34. Mark all inference_basic questions as seen
  35. /rc → fallback to strengthen_weaken or adjacent difficulty

needs_review enforcement (NEW):
  36. Query: SELECT count(*) FROM questions WHERE needs_review = true → 3
  37. Attempt /rc many times → verify cat_pyq_crafts_q4 NEVER appears
  38. Manually clear one flag: UPDATE questions SET needs_review = false
      WHERE question_id = 'cat_pyq_crafts_q4'
  39. /rc → now that question can appear

Ingest:
  40. Ingest rc_question → passage row created, question row created
  41. Ingest va_summary → NO passage row, source_text in question row
  42. Ingest va_sentence_insertion → source_text in question row
  43. Ingest illegal (type=va_grammar, subskill=inference_basic)
      → question stored with needs_review=true
  44. Ingest verifier-disagreed question → needs_review=true

Taxonomy assertion:
  45. Start server → no RuntimeError (assertion passes)
  46. Temporarily add skill to ALL_SUBSKILLS but not SUBSKILL_TO_TECHNIQUE_QUERY
      → startup fails with clear RuntimeError message
  47. Restore

_fetch_single SQL branches (Gap 1 verification):
  48. Trigger /pj fallback path where subskill=None (all PJ subskills exhausted)
      → SQL executes without error (verifies the no-subskill branch)
  49. Trigger /va with subskill filter → SQL executes without error

Spend cap:
  50. Set DAILY_LLM_SPEND_CAP_USD=0.000001 → message → spend cap response
  51. Restore

Admin panel (local):
  52. Dashboard shows correct counts and spend
  53. Question browser filters by skill and subskill separately
  54. Ingest page runs pipeline, shows flagged questions
  55. needs_review=true queue view works, can toggle flags
```

---

## 31. Implementation Order for Claude Code

**Paste this prompt to Claude Code:**

> Implement `dhri_varc_bot_v4.1_final.md` (parts 1 and 2) exactly as specified.
>
> **Rules:**
>
> 1. Read the entire spec (both parts) before writing any file.
> 2. If any function or pattern is ambiguous, ask before inventing.
> 3. Implement in this order:
>    a. `db/schema.sql` (run manually against Neon direct, verify all tables)
>    b. `config.py`
>    c. `db/client.py` (copy from v3)
>    d. `memory/session.py` (copy from v3)
>    e. `agent/llm.py` (copy from v3)
>    f. `db/queries.py`
>    g. `bot/utils.py` (copy from v3)
>    h. `bot/keyboards.py`
>    i. `retrieval/technique_queries.py` (Section 12)
>    j. `retrieval/pgvector.py`
>    k. `retrieval/reranker.py` (Section 15)
>    l. `retrieval/selector.py` (Section 14 — NOTE THE TWO SQL BRANCHES)
>    m. `memory/profile.py` (Section 13)
>    n. `memory/summarizer.py` (copy from v3)
>    o. `agent/prompts.py` + `agent/explainer.py`
>    p. `agent/classifier.py`
>    q. `handlers/onboarding.py` (Section 11)
>    r. `handlers/home.py`
>    s. `handlers/practice/common.py` (Section 17)
>    t. `handlers/practice/rc.py`
>    u. `handlers/practice/pj.py`
>    v. `handlers/practice/va.py` (Section 18)
>    w. `handlers/doubt.py` + `handlers/concept.py`
>    x. `handlers/stats.py` (Section 23) + `handlers/resume.py`
>    y. `handlers/session_cleanup.py` + `handlers/weekly_reports.py` (copy from v3)
>    z. `bot/commands.py` + `bot/callbacks.py` + `bot/free_text.py`
>    aa. `bot/router.py` (copy from v3)
>    ab. `main.py` (Section 24)
>    ac. `ingest/tagger.py` (Section 19 — DO NOT ABBREVIATE THE FEW-SHOT EXAMPLES)
>    ad. `ingest/pipeline.py` (Section 20)
>    ae. `ingest/parser.py` + `ingest/verifier.py` + `ingest/embedder.py`
>    af. `admin/` (Streamlit pages)
> 4. State what you built after each file and what comes next.
> 5. Do not add features not in the spec. Do not rename things.
> 6. Run the 48-PYQ seed ingest with `--skip-tagger --skip-verifier`. Verify 3 rows have `needs_review=true`.
> 7. The manual test script (Section 30) must pass before declaring done.
> 8. Pay special attention to:
>    - Section 14's two-branch SQL in `_fetch_single` — do not collapse it into f-string interpolation
>    - Section 14's `AND q.needs_review = false` filter on every retrieval query
>    - Section 19's few-shot examples — keep them verbatim in the tagger prompts

---

## Appendix A — Ingest Script for the 48 PYQs

Companion file: `dhri_48_pyqs_v4.json` (provided separately).

The pipeline command for seed ingest:

```python
# ingest/pipeline.py — seed mode

async def run_seed_ingest(file_path: str):
    """
    Ingest the 48 pre-tagged PYQs without running tagger/verifier.
    Only embeddings are computed.
    """
    with open(file_path) as f:
        data = json.load(f)

    # Ingest passages first
    for p in data['passages']:
        await db.execute("""
            INSERT INTO passages (passage_id, full_text, word_count, topic, tone,
                                  source, year, difficulty, is_active)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            ON CONFLICT (passage_id) DO NOTHING
        """, p['passage_id'], p['full_text'], p['word_count'], p['topic'],
             p['tone'], p['source'], p['year'], p['difficulty'], p['is_active'])

    # Ingest questions
    for q in data['questions']:
        # Build embedding from one_line_technique + subskill + primary trap
        traps = q.get('traps_present') or []
        trap_str = traps[0] if traps else 'none'
        embed_text = (
            f"{q['one_line_technique']}\n"
            f"Skill: {q['subskill']}\n"
            f"Trap: {trap_str}"
        )
        vector = await embed(embed_text)
        q['_vector'] = vector
        q['_tags'] = {
            'subskill': q['subskill'],
            'traps_present': q['traps_present'],
            'option_traps': q['option_traps'],
            'one_line_technique': q['one_line_technique'],
            'taxonomy_version': 1,
            'tagged_at': datetime.utcnow(),
            'tagger_model': 'manual_pyq_v4',
            'connector_type': q.get('connector_type'),
            'opening_clue': q.get('opening_clue'),
            'pj_connector_map': q.get('pj_connector_map'),
        }
        await store_question(q)

    logger.info(f"Seeded {len(data['questions'])} questions from {file_path}")
```

---

## Appendix B — Summary of Changes from v4.0

| # | Issue in v4.0 | Fix in v4.1 |
|---|---------------|-------------|
| 1 | `_fetch_single` used nested f-string `$N` interpolation that produces literal `$N` in SQL at runtime | Rewrote as two clean SQL branches — one with subskill filter, one without |
| 2 | Retrieval queries did not filter `needs_review` — 3 flagged PYQs would reach students | Added `AND q.needs_review = false` to all retrieval SQL in Section 14 |
| 3 | All six tagger prompts contained `[Insert 3 PYQ examples here]` placeholders | Filled all six with real, verbatim few-shot examples from the 48 seed PYQs |
| 4 | Seed ingest path ambiguous — would the 48 PYQs run through Flash+Sonnet? | Added `--skip-tagger --skip-verifier` flag and explicit seed pipeline in Appendix A |
| 5 | Passage `full_text` fields were placeholders in v4.0 | Companion JSON `dhri_48_pyqs_v4.json` contains full passage text for all 8 passages |
| 6 | No DB index on `needs_review` | Added `idx_questions_review` partial index |
| 7 | Manual test script didn't verify `needs_review` enforcement | Added tests 36-39 and 48-49 |

---

*End of v4.1 spec. This document plus `dhri_48_pyqs_v4.json` is everything Claude Code needs.*
