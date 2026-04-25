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
