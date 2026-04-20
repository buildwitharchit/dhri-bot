# DHRI VARC Bot

An adaptive CAT VARC (Verbal Ability & Reading Comprehension) practice assistant delivered over Telegram. Students practice Reading Comprehension, Para Jumbles, and Verbal Ability through a conversational interface that remembers what they got wrong, why they got it wrong, and serves the next question matched to their specific cognitive weak areas.

Target scale for the beta: 50–100 daily active users before the full web product.

---

## The problem

CAT aspirants get drowned by undifferentiated practice. A typical online mock pool has thousands of questions tagged at the skill level (Inference, Main Idea, Tone...), but no system tracks *which kind of wrong option you keep picking*. Students who repeatedly fall for the same trap — e.g. the "half-right-half-wrong" option, or the "too-extreme" option — keep seeing random questions and never get targeted remediation.

DHRI closes that gap with three moves:

1. **Trap-aware tagging.** Every seeded question carries not just a subskill label but an `option_traps` map that names the failure mode of each wrong option (`half_right_half_wrong`, `out_of_scope`, `too_extreme`, `theme_break`, `true_but_not_inferable`, `content_over_purpose`).
2. **Per-user trap profile.** Every attempt updates a running count of which traps a student falls for, surfacing their dominant failure pattern.
3. **Retrieval tuned to weakness.** The next question is retrieved via a pgvector HNSW search over compact "one-line-technique" embeddings, then reranked so candidates carrying the student's dominant trap bubble to the top. Weakest subskill drives the first query; sensible fallbacks (adjacent difficulty, second-weakest subskill) prevent dead ends.

The result is a practice loop that gets tighter over time rather than noisier.

---

## Feature overview

### Student-facing

- **7 student-facing skills** rolled up from 18 internal subskills — students see clean labels like "Inference" and "Purpose & Structure" while retrieval works at higher granularity underneath.
- **Three practice modes:**
  - **`/rc`** — Reading Comprehension. Picks a CAT-level passage that has at least two unseen questions targeting the student's weakest RC subskill, then walks them through sequentially.
  - **`/pj`** — Para Jumbles. Single-question sessions; students enter the sequence as digits, e.g. `4,1,2,3`.
  - **`/va`** — Verbal Ability menu with 7 subtypes (Grammar, Vocabulary, Sentence Correction, Fill in the Blank, Odd One Out, Sentence Insertion, Passage Summary).
- **`/doubt`** — Socratic tutoring on the current question. The system prompt instructs the model to ask what the student picked first, name the trap they fell for, and cap explanations at 200 words.
- **`/concept`** — teaches a VARC concept end-to-end with a concrete example and a quick-check question.
- **`/stats`** — shows a weekly snapshot (questions attempted, accuracy, streak), 7-skill breakdown with score bars, and the student's most common trap.
- **`/resume`** — resumes an unfinished session from the last 48 hours; validates the question is still active before re-presenting.
- **`/done`, `/help`** — manual session end + command reference.
- **Weekly recap** — cron-driven opt-out-by-inactivity digest summarising the week.

### System-facing

- **User-level request lock** — 5-second Redis `SET NX EX` lock per `tg_id`. Concurrent updates for the same user are dropped silently so the bot can't race itself.
- **Daily spend cap** — hard USD cap per day (default `$0.50`), enforced pre-call using approximate pricing then reconciled post-call with actual token usage.
- **Rate limit** — 50 LLM-bound free-text messages per user per day, rolling over at midnight IST. Commands and inline button taps are never debited.
- **Session state** — uniform shape across modes: `questions_in_set`, `current_question_index`, `questions_answered`, `questions_remaining`. PJ and VA use the same shape with a single-element question set.
- **Session snapshots** — stale sessions (idle > 2h) are closed by a cron with a summary and a resumable snapshot, all on the best-effort path so partial failures don't strand the user.
- **Taxonomy assertion** — on startup, the server cross-checks that `ALL_SUBSKILLS` and `SUBSKILL_TO_TECHNIQUE_QUERY` have identical keys. Any drift (e.g. a newly added subskill missing its technique query) is a `RuntimeError` at boot, not a silent retrieval bug.
- **`needs_review` gate** — retrieval SQL always includes `AND q.needs_review = false`. Flagged questions (3 in the seed) never reach students until a human reviewer clears the flag via the admin panel.
- **Ingest pipeline** — two modes:
  - **Seed (`--skip-tagger`)** — uses pre-tagged fields directly; only the embedding step runs.
  - **Full** — two-pass tagger (Gemini Flash for structured tags + Claude Sonnet for the one-line technique), optional independent verifier, then embed + store. Illegal `(type, subskill)` pairs or verifier disagreements flip `needs_review=true` with a debug-friendly log line.

---

## High-level architecture

```
               ┌────────────────────────────────────────────────────┐
               │                    Telegram                        │
               │     (user chats with @dhri_varc_bot)               │
               └──────────────────────┬─────────────────────────────┘
                                      │ webhook POST
                                      ▼
               ┌────────────────────────────────────────────────────┐
               │                 FastAPI (main.py)                  │
               │  /webhook/<secret>   /health   /admin/*            │
               └──────────────────────┬─────────────────────────────┘
                                      │
                                      ▼
               ┌────────────────────────────────────────────────────┐
               │  bot/router.py — user-level lock + dispatch        │
               │   acquire_lock(user:{tg_id}, 5s) → drop if busy    │
               └────────┬──────────────────┬──────────────────┬─────┘
                        │                  │                  │
                commands/            callbacks/           free-text/
                (/rc /pj /va …)      (buttons)          (doubt/concept
                                                        /PJ answer)
                        │                  │                  │
                        ▼                  ▼                  ▼
               ┌────────────────────────────────────────────────────┐
               │                 handlers/*                         │
               │  onboarding · practice/{rc,pj,va} · doubt ·        │
               │  concept · stats · resume · session_cleanup        │
               └────────┬─────────────────┬──────────────────┬──────┘
                        │                 │                  │
        ┌───────────────▼────┐   ┌────────▼────────┐   ┌─────▼──────┐
        │  retrieval/        │   │  memory/        │   │  agent/    │
        │  selector →        │   │  profile →      │   │  prompts → │
        │  technique_queries │   │  session state  │   │  explainer │
        │  reranker          │   │  summariser     │   │  classifier│
        │  pgvector          │   │                 │   │  llm       │
        └─────────┬──────────┘   └────────┬────────┘   └─────┬──────┘
                  │                       │                  │
                  ▼                       ▼                  ▼
        ┌───────────────────┐   ┌──────────────────┐  ┌──────────────────┐
        │  Neon Postgres    │   │  Upstash Redis   │  │  OpenRouter      │
        │  + pgvector HNSW  │   │  state · locks · │  │  Gemini Flash    │
        │                   │   │  rate limit ·    │  │  Claude Haiku    │
        │  8 tables + FKs   │   │  spend counter   │  │  Claude Sonnet   │
        └───────────────────┘   └──────────────────┘  │  TE3-small embed │
                                                      └──────────────────┘
                                                   (text-embedding-3-small,
                                                     1536 dims)
```

### Data flow for a single RC attempt

1. User taps **Reading Comp** → callback `mode_rc` → `handlers/practice/rc.start_rc_session`.
2. `PracticeSelector.get_rc_passage(profile)` determines the weakest RC subskill, embeds its technique query (Section 12 library), runs pgvector `<=>` cosine search over the `questions` HNSW index, filters out `needs_review=true` + already-seen, reranks by `0.62 * sim + 0.38 * trap_match`, and returns the passage whose candidate set has ≥2 unseen questions.
3. A new `sessions` row is created; uniform state is persisted to Redis with a 2-hour TTL.
4. Passage text + first question sent via Telegram. Student taps A/B/C/D → callback `answer_<letter>`.
5. `record_attempt` inserts an `attempts` row, applies EWMA update (α=0.15) to the subskill score, increments trap counter if applicable, updates `sessions.skills_practiced`.
6. Feedback text is sent (correct/incorrect, trap name, explanation). State advances `current_question_index`.
7. When the question set is exhausted, a three-sentence summary is generated by the summariser model, persisted to `sessions.summary`, and the home keyboard is re-shown.

### Trap-aware retrieval

The cognitive fingerprint for each question is a narrow 3-line embedding:

```
<one_line_technique sentence>
Skill: <subskill>
Trap: <primary trap>
```

At retrieval time, the same shape is built from the student's current subskill query. Cosine similarity surfaces conceptually near questions; reranking multiplies that by the student's dominant trap so questions exposing their recurring failure mode float up.

This is deliberately narrow. A fatter embedding (solving strategy + cognitive operation + secondary skill) was evaluated and dropped — it diluted the trap signal and produced generic neighbours.

---

## Technology stack

| Layer            | Choice                                         | Why                                                                |
| ---------------- | ---------------------------------------------- | ------------------------------------------------------------------ |
| Bot framework    | python-telegram-bot v21 (async)                | Mature async, covers webhooks + inline keyboards out of the box.   |
| Web framework    | FastAPI + uvicorn                              | Single webhook route + admin hooks; minimal ceremony.              |
| Database         | Postgres on Neon + pgvector                    | HNSW over 1536-dim embeddings; cheap pooled connections.           |
| State / locks    | Upstash Redis (HTTP client)                    | Serverless, no VPC setup, matches Railway deploy model.            |
| LLM access       | OpenRouter (`openai` SDK with custom base_url) | One key for Flash / Haiku / Sonnet / embedding; single spend cap.  |
| Embeddings       | `openai/text-embedding-3-small` (1536 dims)    | Cheap, strong enough for a technique-level fingerprint.            |
| Admin panel      | Streamlit (local only)                         | Fastest path to an internal review UI; never deployed publicly.    |
| Deployment       | Railway (single service)                       | One command to ship; cron for cleanup + weekly reports.            |

---

## Repository layout

```
dhri-bot/
├── main.py                    # FastAPI + webhook + admin endpoints + startup assertion
├── config.py                  # Pydantic settings + taxonomy (ALL_SUBSKILLS, skills, traps)
├── requirements.txt
├── railway.json               # Deploy manifest
├── .env.example
│
├── bot/                       # Telegram surface
│   ├── router.py              # User-level lock, dispatch, error surface
│   ├── commands.py            # /start /rc /pj /va /doubt /concept /stats …
│   ├── callbacks.py           # Inline keyboard routing
│   ├── free_text.py           # Intent classification + rate limit debit
│   ├── keyboards.py           # Home / answer / VA menu / onboarding
│   └── utils.py               # HTML escape, long-message splitter, IST helpers
│
├── handlers/
│   ├── onboarding.py          # Year + level flow
│   ├── home.py                # Home screen
│   ├── practice/
│   │   ├── common.py          # Section 17 context helper, record_attempt, close_session
│   │   ├── rc.py              # Reading comp session flow
│   │   ├── pj.py              # Para jumble session flow
│   │   └── va.py              # VA menu + all 7 subtypes
│   ├── doubt.py               # Socratic doubt mode
│   ├── concept.py             # Concept teaching mode
│   ├── stats.py               # 7-skill rollup display
│   ├── resume.py              # /resume + snapshot hydration
│   ├── session_cleanup.py     # 2-hour idle cleanup (cron)
│   └── weekly_reports.py      # Weekly recap (cron)
│
├── agent/
│   ├── prompts.py             # SYSTEM_PROMPT_TEMPLATE
│   ├── explainer.py           # Context builder + explanation call
│   ├── classifier.py          # Deterministic free-text intent (no LLM)
│   └── llm.py                 # OpenRouter wrapper + spend cap + retries
│
├── memory/
│   ├── session.py             # Redis state + locks
│   ├── profile.py             # EWMA score updates + trap counters
│   └── summarizer.py          # 3-sentence session summary
│
├── retrieval/
│   ├── selector.py            # PracticeSelector — weakest-first + fallbacks
│   ├── technique_queries.py   # 18 subskill→technique query strings
│   ├── reranker.py            # 0.62 * sim + 0.38 * trap_match
│   └── pgvector.py            # Vector math helpers
│
├── db/
│   ├── schema.sql             # Neon schema (run once, direct URL)
│   ├── client.py              # asyncpg pool + pgvector codec registration
│   └── queries.py             # Session + message + rate-limit helpers
│
├── ingest/
│   ├── pipeline.py            # store_question, run_seed_ingest, run_full_ingest
│   ├── tagger.py              # 6 type-specific prompts w/ few-shot examples
│   ├── embedder.py            # build_embed_text + embed wrapper
│   ├── parser.py              # Tagger JSON extractor
│   └── verifier.py            # Independent correctness verifier
│
├── admin/                     # Streamlit (localhost only)
│   ├── app.py
│   └── pages/                 # dashboard, ingest, questions, users,
│                              # analytics, feedback
│
└── data/
    └── dhri_48_pyqs_v4.json   # 48-question seed (24 CAT 2024 + 24 CAT 2023)
```

---

## Data model (condensed)

```
tg_users          one row per Telegram user
user_profiles     per-user rollup: trap_counts, most_common_trap, weakest_skill, streak
user_skill_scores one row per (tg_id, subskill) with EWMA score + attempts_count

passages          RC source passages (full_text + topic + tone + difficulty)
questions         everything: options, correct_option, correct_order, trap map,
                  one_line_technique, technique_embedding vector(1536), needs_review

sessions          one per practice session; ended_at, was_completed, summary
session_snapshots persists the uniform state blob for /resume
messages          user + assistant turns, per session
attempts          per-question record: selected, correct, trap_fallen_for, pj_mistake_type
feedback          user-reported bugs on questions
```

Key indexes:

- `idx_questions_embedding` — HNSW on `technique_embedding vector_cosine_ops`
- `idx_questions_subskill` + `idx_questions_type_difficulty` — fast filtering before vector search
- `idx_questions_review` — partial index on `needs_review = true` for admin queue
- `idx_sessions_open` — partial index on open sessions for the cleanup cron

---

## Technical implementation highlights

### 1. Uniform session state (`memory/session.py` + Redis)

Every practice mode serialises to the same JSON shape:

```json
{
  "state": "RC_ACTIVE",
  "session_id": "uuid",
  "mode": "rc",
  "passage_id": "cat2024_bandicoots",
  "questions_in_set": ["q1", "q2", "q3", "q4"],
  "current_question_index": 0,
  "questions_answered": {},
  "questions_remaining": ["q1", "q2", "q3", "q4"],
  "session_started_at": "2026-01-15T10:30:00Z"
}
```

PJ and VA sessions use the same shape with `questions_in_set` of length 1. This makes `/resume` a single code path regardless of mode, and the session-cleanup cron needs only one snapshot writer.

### 2. pgvector retrieval with two-branch fallback

The core `_fetch_single` in `retrieval/selector.py` has **two explicit SQL branches** — one with a subskill filter, one without. They are not merged behind dynamic SQL because parameter binding against `asyncpg`'s `$N` placeholders interacts poorly with nested f-string interpolation. Keeping them separate makes it trivially correct and trivially reviewable:

```python
if subskill:
    candidates = await db.fetch("""
        SELECT q.*, 1 - (q.technique_embedding <=> $1::vector) as similarity
        FROM questions q
        WHERE q.type = $2 AND q.subskill = $3 AND q.difficulty = $4
          AND q.is_active = true AND q.needs_review = false
          AND q.question_id != ALL($5)
        ORDER BY q.technique_embedding <=> $1
        LIMIT 10
    """, query_vector, q_type, subskill, difficulty, seen_ids)
else:
    # … same query without the subskill filter, $N renumbered by hand
```

### 3. Spend cap as a Redis counter with actual reconciliation

```python
async def _chat_completion(model, messages):
    est_usd = estimate_cost(messages, model)
    await _check_spend_cap(est_usd)    # fast local estimate, cheap Redis read
    resp = await _client.chat.completions.create(...)
    actual_usd = (resp.usage.total_tokens / 1_000_000) * _price(model)
    await _add_spend(actual_usd)       # reconcile with real usage
```

The pre-call check is an optimistic estimate. The post-call update uses the real `usage.total_tokens` so persistent bias doesn't accumulate. Daily keys TTL at 35 days so short-horizon billing history stays recoverable without extra cost.

### 4. Trap-weighted reranker

```python
def rerank(candidates, profile_trap):
    scored = []
    for q in candidates:
        sim = float(q.get("similarity", 0.0) or 0.0)
        trap_match = 1.0 if profile_trap and profile_trap in q.get("traps_present", []) else 0.0
        composite = 0.62 * sim + 0.38 * trap_match
        scored.append((composite, q))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [q for _, q in scored]
```

Weights (`0.62` / `0.38`) are starting points — the plan is to tune them after two weeks of attempt data. Difficulty isn't in the formula because it's already filtered in SQL; adding a constant for every candidate would be dead weight.

### 5. Type-specific tagger with real few-shot examples

The ingest pipeline uses six different prompts (RC, PJ, VA-structural, VA-insertion, VA-summary, VA-semantic) rather than one generic one. Each prompt contains 2–3 real few-shot examples drawn from the 48-question seed. The structured tagger (Gemini Flash) returns JSON with `subskill`, `traps_present`, `option_traps`, `difficulty`, and for PJ the `connector_type` / `pj_connector_map` / `opening_clue`. A second pass (Claude Sonnet) writes the `one_line_technique` — the embedding anchor.

### 6. Startup assertion against taxonomy drift

```python
missing = set(ALL_SUBSKILLS) - set(SUBSKILL_TO_TECHNIQUE_QUERY.keys())
extra   = set(SUBSKILL_TO_TECHNIQUE_QUERY.keys()) - set(ALL_SUBSKILLS)
if missing or extra:
    raise RuntimeError(f"Taxonomy mismatch: missing={missing}, extra={extra}.")
```

Adding a subskill to one dict but not the other is the single easiest way to silently break retrieval. This assertion turns it into a boot failure with a clear message.

---

## Setup

### Prerequisites

- Python 3.11
- A Neon Postgres project (pooled + direct connection strings)
- An Upstash Redis instance
- An OpenRouter API key (monthly hard cap recommended: $10)
- A Telegram bot token from @BotFather

### Local development

```bash
# 1. Install
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. Copy and fill in your secrets
cp .env.example .env
# …edit .env…

# 3. Apply schema (direct connection)
psql "$DATABASE_URL_DIRECT" -f db/schema.sql

# 4. Seed ingest — 48 pre-tagged PYQs, 45 active + 3 needs_review
.venv/bin/python -m ingest.pipeline \
  --file data/dhri_48_pyqs_v4.json \
  --skip-tagger --skip-verifier

# 5. Run the bot locally
.venv/bin/uvicorn main:app --reload --port 8000
```

### Admin panel (local only)

```bash
.venv/bin/streamlit run admin/app.py
```

Do not expose this to the internet — it has no auth beyond "you're running it on your own machine."

### Deploy (Railway)

1. Create Neon project; apply `db/schema.sql` via direct URL.
2. Create Upstash Redis.
3. Set OpenRouter monthly hard cap in dashboard.
4. In BotFather: create bot, register all 13 commands, scope `/broadcast` and `/ban` to your own `tg_id`.
5. Create Railway service; paste all env vars from `.env.example`.
6. Deploy.
7. Verify `GET /health` returns `{"status": "ok"}` and `getWebhookInfo` shows the webhook.
8. Add Railway crons:
   - `*/10 * * * *` → `POST /admin/cleanup-sessions` with header `X-Admin-Secret`
   - `30 2 * * 0`  → `POST /admin/send-reports` with header `X-Admin-Secret`
9. Run the seed ingest pointed at the Neon pooled URL (same command as local).
10. Run the Section 30 smoke test script end-to-end against the deployed bot.

---

## Operations

### Daily hygiene

- Watch `DAILY_LLM_SPEND_CAP_USD` vs. actual `spend:<date>` Redis keys.
- Watch `SELECT count(*) FROM questions WHERE needs_review = true` — anything above the seed 3 means the tagger or verifier is flagging new questions; review and clear.
- Watch session cleanup logs for repeated failures — those would indicate Redis or DB transience.

### Adding content

Supplementary questions should go through the full tagger pipeline:

```bash
.venv/bin/python -m ingest.pipeline --file data/supplement.json
```

Anything tagged with an illegal `(type, subskill)` pair or that the verifier disagrees with is stored with `needs_review=true` so it never reaches a student until reviewed via the admin `Questions` page.

### Adding a subskill

1. Add the subskill to `config.SUBSKILL_TO_SKILL` (maps it to one of the 7 student-facing skills).
2. Add the subskill to `retrieval/technique_queries.SUBSKILL_TO_TECHNIQUE_QUERY` with a one-sentence technique description.
3. If relevant, add it to the appropriate list in `config.SKILL_TYPE_MATRIX`.
4. Restart. The startup assertion will confirm the taxonomy is consistent.

### Emergency stops

- **Cut all LLM spend:** set `DAILY_LLM_SPEND_CAP_USD=0.000001` and redeploy. The bot stays up; LLM-bound flows return a clear "out of budget" message.
- **Ban a user:** `UPDATE tg_users SET is_banned = true, ban_reason = '…' WHERE tg_id = …;`. The router drops banned users silently.
- **Freeze a bad question:** `UPDATE questions SET needs_review = true WHERE question_id = '…';`. Retrieval immediately stops serving it.

---

## Testing

Compile, import, and behavioural checks can be run without any external services:

```bash
# Taxonomy assertion
TELEGRAM_BOT_TOKEN=x WEBHOOK_SECRET=x DATABASE_URL=x DATABASE_URL_DIRECT=x \
UPSTASH_REDIS_REST_URL=x UPSTASH_REDIS_REST_TOKEN=x OPENROUTER_API_KEY=x \
ADMIN_REPORTS_SECRET=x RAILWAY_PUBLIC_DOMAIN=x \
.venv/bin/python -c "from main import app; print('OK')"
```

Runtime tests against a real DB + Redis + OpenRouter are enumerated in `IMPLEMENTATION_NOTES.md`'s Section 30 summary.

---

## Status

Beta build. Seed data (48 CAT PYQs + 8 passages) is in `data/dhri_48_pyqs_v4.json`. Supplementary content must be authored and run through the tagger to reach the minimum-per-subskill counts listed in the spec (Section 27) before going live.

## License

Private / internal. Not for redistribution.
