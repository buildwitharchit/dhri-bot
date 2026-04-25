# Service Contracts — DHRI VARC Bot v4.1

## Overview

DHRI is a single-process FastAPI app, not a microservices mesh. "Services" here means **modules with clear ownership of a domain** — memory, retrieval, scoring, LLM access, etc. Each module publishes a narrow Python API; handlers compose those APIs.

The boundary philosophy:

- **One module, one concern.** `memory/profile.py` owns scoring and traps; it never hits LLMs. `agent/llm.py` owns LLM access and spend tracking; it never reads the `questions` table. Handlers are the only code that compose multiple modules.
- **No module reaches around another.** Handlers don't embed text directly — they call `agent/llm.py::embed`. Retrieval doesn't update scores — it returns candidates and handlers call `memory/profile.py::update_skill_score` on attempts.
- **Errors bubble with typed exceptions.** `SpendCapExceededError` is the only custom exception in v4.1; everything else raises standard `RuntimeError` / `TypeError`. The webhook boundary in `bot/router.py` catches `SpendCapExceededError` and returns a user-facing message.

All Python signatures below use type hints. Every `async def` must be awaited — this is mechanical, not optional (**FIX 6**). `scripts/preflight.py` Check 9 grep-audits missing awaits.

---

## HTTP Boundary — `main.py` (FastAPI)

The webhook entrypoint and admin cron endpoints. Stateless at the FastAPI level.

### `POST /webhook/{settings.WEBHOOK_SECRET}`

**Input.** Raw Telegram `Update` JSON in the request body.

**Output.** `204 No Content` (actually returns `200` with empty body). Never returns content that matters — Telegram discards it.

**Side effects.** Parses the update into a PTB `Update` object and hands off to `bot.router.route_update(update, ptb_app)`. Catches nothing (errors are handled inside `route_update`).

**Auth.** Path secret. The `WEBHOOK_SECRET` is set at startup and encoded in the URL we register with Telegram via `set_webhook`. Telegram never sees or sends this in headers; only Telegram has the URL.

**Latency.** Must return within Telegram's 30-second webhook timeout. Real p95 is ~4s (dominated by LLM calls).

### `GET /health`

Liveness probe for Railway. Returns `{"status": "ok"}` unconditionally. Does not touch DB or Redis.

### `POST /admin/cleanup-sessions`

**Input.** Header `X-Admin-Secret` must equal `settings.ADMIN_REPORTS_SECRET`.

**Output.** `{"closed": int}` on success, `403` if secret mismatches.

**Side effects.** Invokes `handlers.session_cleanup.cleanup_stale_sessions()` which scans open sessions > 2 hours idle, writes snapshots for resumable modes, marks `ended_at`, clears Redis state.

**Trigger.** Railway cron, every 10 minutes.

### `POST /admin/send-reports`

**Input.** Same `X-Admin-Secret` header.

**Output.** `{"status": "ok"}`.

**Side effects.** Calls `handlers.weekly_reports.send_weekly_reports_to_all()`. Scans active users (attempt in last 7 days), sends per-user Telegram recap.

**Trigger.** Railway cron, Sundays 02:30 UTC (`30 2 * * 0`).

### Startup hooks (`@app.on_event("startup")`)

Runs once per process boot:
1. **Taxonomy assertion.** Loads `ALL_SUBSKILLS` from config and `SUBSKILL_TO_TECHNIQUE_QUERY` from `retrieval.technique_queries`. Raises `RuntimeError` on mismatch — process refuses to start.
2. `await init_db_pool()` — creates asyncpg pool with pgvector codec registration.
3. `await init_redis()` — creates Upstash HTTP client.
4. Builds PTB `Application`, initializes it, calls `set_webhook(url=f"https://{settings.RAILWAY_PUBLIC_DOMAIN}/webhook/{settings.WEBHOOK_SECRET}", allowed_updates=["message", "callback_query"])`.

---

## Request Dispatch — `bot/router.py`

Serializes updates per user and dispatches by update type.

### `async route_update(update: telegram.Update, ptb_app: Application) -> None`

**Input.** A parsed Telegram `Update`.

**Output.** None. Side effects only — any replies go via `ptb_app.bot.send_message`.

**Side effects.**
1. Extracts `tg_id` (no-op if missing, e.g. channel updates we don't handle).
2. Acquires `lock:user:{tg_id}` with 5s TTL (`SET NX EX 5`).
3. If lock busy → silently drops the update (no reply). This is intentional: user rapid-firing taps shouldn't get error spam.
4. In `try`: calls `_process_update(update, ptb_app, tg_id)`.
5. Catches `SpendCapExceededError` → calls `bot.utils.send_error_to_update(update, "spend_cap", ptb_app.bot)`.
6. Catches any other `Exception` → logs with traceback, sends generic error.
7. `finally` releases the lock.

**Internal dispatch (inside `_process_update`):**
- If `update.message`: decide whether it's a command (starts with `/`) or free text.
  - Command → `bot.commands.route_command(update, ptb_app)`
  - Free text → `bot.free_text.handle_free_text(update, ptb_app)`
- If `update.callback_query`: → `bot.callbacks.route_callback(update, ptb_app)`

**Does not.** Touch profile or state — those are loaded inside the handler layer. Router is purely about serialization and dispatch.

---

## Command Layer — `bot/commands.py`

Routes slash commands to their handlers. Commands never count against the rate limit.

### `async route_command(update: Update, ptb_app) -> None`

**Input.** Update with `message.text` starting with `/`.

**Output.** None.

**Side effects.** Parses the command token, calls `get_or_create_user` to get the profile, loads Redis state, then calls the appropriate handler.

**Command → handler mapping.**

| Command     | Handler                                                |
|-------------|--------------------------------------------------------|
| `/start`    | `handlers.onboarding.handle_start`                     |
| `/rc`       | `handlers.practice.rc.start_rc_session`                |
| `/pj`       | `handlers.practice.pj.start_pj_session`                |
| `/va`       | `handlers.practice.va.show_va_menu`                    |
| `/doubt`    | `handlers.doubt.handle_doubt` (w/ remaining text)      |
| `/concept`  | `handlers.concept.handle_concept` (w/ remaining text)  |
| `/stats`    | `handlers.stats.handle_stats`                          |
| `/weak`     | Alias → "Work on weakest" — invokes drill-weakest flow |
| `/resume`   | `handlers.resume.handle_resume`                        |
| `/settings` | (stub — reserved for target year / difficulty edit)    |
| `/feedback` | Feedback submission (inserts row into `feedback`)      |
| `/done`     | Closes current session via `handlers.practice.common.close_session` |
| `/help`     | Static text with command list                          |
| `/broadcast`| Admin only (scope: admin `tg_id`)                      |
| `/ban`      | Admin only                                             |

**Admin scoping.** `/broadcast` and `/ban` verify `tg_id` matches `settings.ADMIN_TG_ID`. Silently no-op if not.

---

## Callback Layer — `bot/callbacks.py`

Routes inline button taps. Callbacks never count against the rate limit.

### `async route_callback(update: Update, ptb_app) -> None`

**Input.** `update.callback_query.data` — a prefix-coded string (e.g. `"mode_rc"`, `"answer_B"`, `"va_type_va_grammar"`, `"resume_<uuid>"`).

**Output.** None.

**Side effects.** Calls `callback_query.answer()` early to remove the loading spinner. Loads profile + state. Routes by prefix:

| Prefix              | Handler                                                      |
|---------------------|--------------------------------------------------------------|
| `mode_rc`           | `handlers.practice.rc.start_rc_session`                      |
| `mode_pj`           | `handlers.practice.pj.start_pj_session`                      |
| `mode_va`           | `handlers.practice.va.show_va_menu`                          |
| `va_type_<type>`    | `handlers.practice.va.handle_va_type_selection`              |
| `answer_<letter>`   | Route to current mode's answer handler based on `state.state`|
| `onboard_year_<y>`  | `handlers.onboarding.handle_year_selection`                  |
| `onboard_level_<l>` | `handlers.onboarding.handle_level_selection`                 |
| `drill_weakest`     | Looks at `profile.weakest_skill`, starts matching mode       |
| `stats_full`        | Extended stats view (all 18 subskills)                       |
| `resume_<uuid>`     | `handlers.resume.resume_session(session_id)`                 |

**Answer dispatch logic.** `state.state` determines handler:
- `RC_ACTIVE` → `handlers.practice.rc.handle_rc_answer`
- `VA_ACTIVE` → `handlers.practice.va.handle_va_answer`
- `PJ_ACTIVE` never reaches this path — PJ answers come through free text, not buttons.

---

## Free-Text Layer — `bot/free_text.py`

### `async handle_free_text(update: Update, ptb_app) -> None`

**Input.** A non-slash message.

**Output.** None.

**Side effects.**
1. Gets profile via `get_or_create_user`.
2. Loads state via `get_state(tg_id)`.
3. Determines intent via `agent.classifier.classify_free_text(text, in_active_practice=state.state in ('PJ_ACTIVE','RC_ACTIVE','VA_ACTIVE'))`. Returns one of `"pj_answer"`, `"concept"`, `"doubt"`.
4. **Rate-limit debit** via `db.queries.check_and_increment_rate_limit(tg_id)`. If denied:
   - Sends `"You've hit today's message cap..."` and stops.
   - **Commands and button taps don't reach this path**, so they remain usable even at cap.
5. Routes by intent:
   - `pj_answer` → `handlers.practice.pj.handle_pj_answer_text`
   - `concept` → `handlers.concept.handle_concept` (topic = full user text)
   - `doubt` → `handlers.doubt.handle_doubt` (text = full user text)

**Contract.** This is the single place where rate limit is debited. Adding a new free-text route? Make sure it goes through this function.

---

## Memory Service — `memory/session.py`

Redis client + state + lock primitives. Owns nothing in Postgres.

### `async init_redis() -> None`

Creates the Upstash Redis HTTP client singleton. Idempotent — calling twice is a no-op. Must be called at startup.

### `redis` (module-level façade)

Thin async wrapper with passthroughs for `get`, `set(key, value, ex=...)`, `setex`, `delete`, `incr`, `expire`, `ttl`, and a custom `set_nx(key, value, ex)` that returns `True` iff the key was set (SET NX EX). Used wherever Redis is touched.

### `async get_state(tg_id: int) -> dict | None`

Returns the parsed JSON at `state:tg:{tg_id}`, or `None` if absent. Auto-clears and returns `None` on malformed JSON (defensive — shouldn't happen but doesn't crash if it does).

### `async set_state(tg_id: int, state: dict, ttl: int = 7200) -> None`

Serializes and writes with TTL.

### `async clear_state(tg_id: int) -> None`

Removes the key.

### `async acquire_lock(key: str, ttl: int) -> bool`

SETNX-with-expire. Returns `True` iff this caller acquired.

### `async release_lock(key: str) -> None`

DELs the key.

**Does not do.** Queries Postgres. State reconstruction from DB snapshots lives in `db/queries.py::get_state_from_db_or_redis` — that function is the only one allowed to "look in two places".

---

## Profile Service — `memory/profile.py`

Owns scoring math, trap counters, and the weakest-skill aggregation. All writes invalidate the optional `profile:{tg_id}` Redis cache.

### `async update_skill_score(tg_id: int, subskill: str, is_correct: bool) -> None`

Applies the EWMA formula (`ALPHA = 0.15`), upserts into `user_skill_scores`, increments `user_profiles.total_attempts` and `total_correct`. If total crosses `MIN_ATTEMPTS_FOR_WEAKEST_SKILL (=10)`, recomputes `user_profiles.weakest_skill` via `get_weakest_student_skill`.

**Side effects.** Three DB writes + a Redis `DEL`. No LLM calls. No retrieval calls.

### `async get_weakest_student_skill(tg_id: int) -> str`

Reads all 18 subskill scores, groups into 7 student-facing skills via `SUBSKILL_TO_SKILL`, averages each bucket, returns the skill name with the lowest average. Falls back to `"inference"` if the user has no scores at all.

### `async get_weakest_subskill_in_group(tg_id: int, subskill_group: list[str]) -> str`

Used by retrieval: among a subset of subskills (e.g. all 8 RC subskills), returns the one with the lowest user score. Falls back to `subskill_group[0]` if the user has no rows.

### `async update_trap_counts(tg_id: int, trap: str) -> None`

Reads `user_profiles.trap_counts`, increments `trap_counts[trap]`, sets `most_common_trap` to `argmax(trap_counts)`. Called from `handlers/practice/common.py::record_attempt` when a wrong answer has a non-`none` trap.

### `async get_most_common_trap(tg_id: int) -> str`

Reads `user_profiles.most_common_trap`. Returns `"none"` if no row exists. Used by the reranker to give a trap-match bonus to questions carrying the user's dominant trap.

---

## Summarizer Service — `memory/summarizer.py`

### `async generate_session_summary(session_id: str, tg_id: int) -> str`

Reads the session header (mode, totals) + the ordered attempts, calls `MODEL_SUMMARIZER` (Gemini Flash) with a rigid 3-sentence-exactly prompt, returns the string.

**Contract.**
- Always returns a string (falls back to a templated sentence on LLM failure).
- Never raises.
- Does not touch Redis, does not write to Postgres. Caller (handler or cleanup cron) writes the summary into `sessions.summary`.

---

## LLM Service — `agent/llm.py`

Owns OpenRouter access, spend tracking, retries. Nothing else in the codebase talks to OpenRouter directly.

### `SpendCapExceededError`

Custom exception. Raised by the three public functions below if today's spend + this call's estimate would exceed `DAILY_LLM_SPEND_CAP_USD`. Caught by `bot/router.py`, converted to a user-facing message.

### `async llm_call_with_retry(system: str, user: str, model: str) -> str`

Chat completion with two messages: system + user. Returns the assistant content string.

**Pre-flight.** Estimates tokens (`len(text)//4 + 400`), estimates USD, checks cap. Raises `SpendCapExceededError` over cap.

**Retry.** Up to 3 attempts with exponential backoff (1s, 2s, 4s). Retries on `openai.RateLimitError` and any `Exception` (logged). Fails with `RuntimeError` on 3rd failure.

**Post-flight.** Reads `resp.usage.total_tokens` if present, else uses estimate. Increments `spend:{date}` in Redis.

### `async llm_call_with_retry_messages(system: str, messages: list[dict], model: str) -> str`

Same contract but accepts a pre-built `messages` array (for passing chat history in doubt mode).

**Shape of `messages`.** `[{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}, ...]`. No system role — that's prepended internally.

### `async embed(text: str) -> list[float]`

Calls `MODEL_EMBEDDING` (`openai/text-embedding-3-small`). Returns a 1536-length vector.

**Same spend-cap + retry semantics.** Embedding calls are much cheaper (~$0.02/Mtoken) so they rarely trip the cap.

### Does not do

- Retrieval (that's `retrieval/selector.py`).
- Prompt composition (that's `agent/explainer.py` and `ingest/tagger.py`).
- Raw HTTP. All access goes through the `openai.AsyncOpenAI` client configured with OpenRouter base URL.

---

## Explainer Service — `agent/explainer.py`

Composes the LLM calls for the free-text paths (doubt, concept). Owns the context string that feeds `{context}` in the system prompt.

### `build_context(profile: dict, last_summaries: list[str] | None = None) -> str`

Pure function. Returns a short plain-text block:

```
tg_id=12345678
Weakest skill: Inference
Most common trap: half_right_half_wrong
Current streak: 4 day(s)
Total attempts: 42

Recent sessions:
- RC: 4 questions, 50% accuracy. Trap: out_of_scope. Focus on not overreaching.
- PJ: 1 question, correct. Keep locking the opener first.
```

### `build_messages_for_llm(history: list[dict], user_text: str) -> list[dict]`

Pure function. Filters a DB-read message list to `{role, content}` pairs for `user` and `assistant` only (drops system rows), appends the new user turn. Returns the array ready for `llm_call_with_retry_messages`.

### `async explain(profile, user_text, history, last_summaries, model) -> str`

High-level helper. Formats `SYSTEM_PROMPT_TEMPLATE` with the composed context, then calls `llm_call_with_retry_messages`. Used by both `handlers/doubt.py` and `handlers/concept.py` (which differ only in what they stuff into `user_text`).

---

## Classifier Service — `agent/classifier.py`

Deterministic, no LLM. Runs on every free-text message.

### `is_pj_answer(text: str) -> bool`

Returns `True` iff `text` matches 4 distinct digits from `{1,2,3,4}` with optional separators (commas, spaces, dashes). `"4,1,2,3"` → True. `"DCBA"` → False (letters rejected — seed JSON is the canonical format). `"1,1,2,3"` → False (duplicate).

### `normalise_pj_answer(text: str) -> str`

Returns the 4-character canonical form (`"4123"`) for comparison against `questions.correct_order` after stripping commas/whitespace.

### `classify_free_text(text: str, in_active_practice: bool = False) -> "pj_answer" | "concept" | "doubt"`

1. If `in_active_practice and is_pj_answer(text)` → `"pj_answer"`.
2. Else if text.lower().startswith one of `{"how do i","how should i","how to","what is","what are","explain","teach me","help me understand"}` → `"concept"`.
3. Else → `"doubt"`.

Cheap by design. Runs before rate-limit debit so rejected inputs don't burn the day's quota.

---

## Retrieval Service — `retrieval/selector.py`

The adaptive question picker. Owns all pgvector queries and the fallback chain logic.

### `class PracticeSelector`

Stateless. Instantiated per retrieval call.

### `async get_rc_passage(profile: dict) -> dict | None`

**Input.** User's profile dict (needs `tg_id`, `current_difficulty`).

**Output.** `{"passage": dict, "questions": [dict, ...]}` or `None` when the full fallback chain is exhausted.

**Fallback chain.**
1. Weakest RC subskill + requested difficulty.
2. Weakest RC subskill + adjacent difficulty (easier).
3. Weakest RC subskill + adjacent difficulty (harder).
4. Second-weakest RC subskill + requested difficulty.

Returns the first config that yields a passage with ≥2 unseen questions (picked by max count of unseen questions per passage among reranked top-20).

### `async get_pj(profile: dict) -> dict | None`

**Output.** Single question dict or `None`.

**Fallback chain.**
1. Weakest PJ subskill + requested difficulty.
2. Any PJ subskill + requested difficulty.
3. Weakest PJ subskill + easier difficulty.
4. Weakest PJ subskill + harder difficulty.

### `async get_va(profile: dict, va_type: str, subskill: str | None = None) -> dict | None`

**Input.** Profile + a specific VA type (e.g. `"va_grammar"`) + optional preferred subskill.

**Output.** Single question dict or `None`.

**Logic.** If subskill isn't passed or isn't legal for `va_type` per `SKILL_TYPE_MATRIX`, picks the user's weakest subskill within the legal set. Runs the difficulty fallback chain.

### `async _fetch_single(tg_id, q_type, subskill, difficulty, seen_ids, profile) -> dict | None`

**Two SQL branches** — one with `subskill` filter, one without (for the "any PJ" fallback). Do not collapse these. Both queries always include:

```sql
AND q.is_active = true
AND q.needs_review = false
AND q.question_id != ALL($seen_ids)
ORDER BY q.technique_embedding <=> $query_vector
LIMIT 10
```

This is **FIX 1** (two branches) and **FIX 2** (`needs_review` gate).

### `async get_seen_question_ids(tg_id: int) -> list[str]`

`SELECT DISTINCT question_id FROM attempts WHERE tg_id = $1`. Used to exclude already-attempted questions from candidates.

### Contract the selector respects

- **Always embeds the subskill's technique query**, never the question stem.
- **Never returns `needs_review = true` questions.**
- **Never returns questions the user has attempted.**
- **Always reranks via `retrieval/reranker.py::rerank`** before picking.
- **Stateless.** No caching; every call hits the DB fresh.

---

## Reranker Service — `retrieval/reranker.py`

### `rerank(candidates: list[dict], profile_trap: str) -> list[dict]`

Pure function (non-async). Scores each candidate:

```
composite = 0.62 * similarity + 0.38 * trap_match
         where trap_match = 1.0 if profile_trap in candidate.traps_present else 0.0
```

Returns candidates sorted by `composite` descending. This is **FIX 3** — the v4.0 formula had a `0.2 * 1.0` difficulty_fit term that was constant (difficulty is already filtered in SQL) and contributed no ranking signal.

**Weights are fixed.** No per-user tuning yet. Expected to be revisited after 2 weeks of user data.

---

## DB Client — `db/client.py`

### `async init_db_pool() -> None`

Creates the asyncpg pool (min_size=1, max_size=10, command_timeout=30). Registers the pgvector codec on every connection via the pool's `init=` hook, so `$N::vector` parameter binds work natively.

### `db` (module-level façade)

Methods: `fetch`, `fetchrow`, `fetchval`, `execute`, `executemany`. Each acquires a connection from the pool, runs the query, releases. No transaction management — every call is its own implicit transaction.

**Contract.** All queries use `$N` parameter binding (asyncpg convention). Never `%s`, never f-string interpolation of values. Vectors can be passed as Python lists; the pgvector codec encodes them.

---

## DB Query Helpers — `db/queries.py`

Composed CRUD operations. These wrap raw SQL into functions handlers can call without knowing the schema.

### `async initialize_skill_scores(tg_id: int) -> None`

Seeds 18 rows into `user_skill_scores` at `score = 0.5`.

### `async get_or_create_user(tg_id: int, username: str | None, first_name: str | None) -> dict`

Upserts into `tg_users`. On first contact, creates `user_profiles` and calls `initialize_skill_scores`. On subsequent contact, updates `last_active_at` and optionally refreshes `username`/`first_name`. Returns a merged dict of `tg_users ∪ user_profiles`.

### `async create_session(tg_id: int, mode: str) -> str`

`INSERT INTO sessions (...) RETURNING session_id`. Returns the UUID as a string.

### `async write_message(session_id, tg_id, role, content, message_type='text', question_id=None, tg_message_id=None) -> None`

Inserts into `messages`.

### `async write_session_snapshot(session_id: str, tg_id: int, state: dict) -> None`

Upserts into `session_snapshots` from the Redis state dict. Computes `current_question_id` from `state["questions_in_set"][state["current_question_index"]]` — **FIX 7**.

### `async get_session_messages(session_id: str, limit: int | None = None) -> list[dict]`

Returns messages ordered by `created_at ASC`. Used by doubt mode to reconstruct chat history.

### `async get_week_stats(tg_id: int) -> {"questions": int, "accuracy": float}`

Aggregates attempts in the last 7 days. `accuracy` is a percentage (0-100), not a fraction.

### `async get_state_from_db_or_redis(tg_id: int, session_id: str) -> dict | None`

Prefers live Redis state. Falls back to `session_snapshots` if Redis is empty or the session_id doesn't match. Reconstructs the `state.state` label from mode (`rc → RC_ACTIVE`, etc.). Only function allowed to straddle Redis and Postgres.

### `async check_and_increment_rate_limit(tg_id: int) -> (bool, int)`

Atomic-ish check-and-increment. Returns `(allowed, count)`. Does **not** increment if count ≥ 50. First INCR of the day sets the key's TTL to seconds-until-IST-midnight.

---

## Handlers — `handlers/*`

Handlers compose modules. They are the only layer that:
- Reads both profile and state
- Calls retrieval AND scoring AND LLM AND Telegram
- Writes to multiple tables

Every handler takes a similar argument shape: `(update: Update, profile: dict, state: dict, bot)` or some subset. They are all `async def`.

### Onboarding — `handlers/onboarding.py`

- `async handle_start(update, bot)` — `/start` entry. Upserts user, routes to year selection (new user) or home (existing user).
- `async handle_year_selection(update, year, bot)` — callback handler, updates `tg_users.target_year`, shows level keyboard.
- `async handle_level_selection(update, level, bot)` — updates `tg_users.experience`, shows home.

### Home — `handlers/home.py`

- `async show_home(update, profile, bot)` — renders home keyboard with optional "Work on X ⚡" chip if `profile.weakest_skill` is set.

### Practice — `handlers/practice/`

#### `common.py` — shared helpers

- `get_question_context(question, passage=None) -> str` — Section 17 verbatim. RC → full passage text; summary/insertion → `source_text`; PJ/wrong_one_out → labeled sentences; else empty.
- `parse_options(value) -> dict` and `parse_option_traps(value) -> dict` — handle JSONB-as-dict-or-string.
- `async record_attempt(*, tg_id, session_id, question, selected_option, is_correct, trap_fallen_for='none', pj_mistake_type=None, time_taken_secs=None) -> None` — single place where an attempt row is written, skill score is updated, trap counter is bumped, and session counters are incremented. Every practice handler routes through this.
- `resolve_trap_for_selection(question, selected_option) -> str` — lookup `option_traps[selected]`, default `"none"`.
- `async close_session(session_id, *, was_completed, summary=None) -> None` — sets `ended_at`, `was_completed`, `summary`, computes `duration_mins`.

#### `rc.py`, `pj.py`, `va.py`

Each follows the same 3-function shape:
- A **start** function — runs selector, creates session, sets state, sends passage (RC) or question (PJ/VA).
- A **handle answer** function — fetches question from DB, compares, records via `record_attempt`, sends feedback, advances state, either sends next question (RC) or closes session (PJ/VA).
- For RC only, an internal `_send_question(chat_id, question, passage, idx, total, bot)` helper.
- For VA only, `show_va_menu` + `handle_va_type_selection` + `send_va_question`.

**PJ specifics.** `handle_pj_answer_text(update, profile, state, text, bot)` is invoked from free text, not callbacks. It uses `normalise_pj_answer` to canonicalise and compares against `questions.correct_order` stripped of commas.

**All three respect FIX 7.** Current question is always `questions_in_set[current_question_index]`.

### Doubt / Concept — `handlers/doubt.py`, `handlers/concept.py`

- `async handle_doubt(update, profile, state, text, bot)` — if in an active practice session, attaches the current question as context. Builds message history via `get_session_messages(session_id, limit=10)`. Calls `agent.explainer.explain(...)` with `MODEL_COMPLEX` (Claude Haiku). Writes user + assistant turns to `messages`.
- `async handle_concept(update, profile, state, topic, bot)` — no question context. Calls `explain(...)` with `MODEL_CHAT` (Gemini Flash) and a teaching-focused user template.

### Stats — `handlers/stats.py`

- `async handle_stats(message, state, profile, bot)` — Section 23 verbatim. Reads all 18 subskill scores, groups into 7 via `SUBSKILL_TO_SKILL`, averages per bucket, renders bar chart with `█` and `░` characters. Fetches `get_week_stats` for week summary. Shows `most_common_trap` only if not `"none"`.

### Resume — `handlers/resume.py`

- `async handle_resume(update, profile, state, bot)` — lists unfinished sessions from last 48h (`ended_at IS NULL OR (was_completed = false AND session_id IN session_snapshots)`). Renders inline buttons per session with `⚠️` markers.
- `async resume_session(update, session_id, bot)` — calls `get_state_from_db_or_redis(tg_id, session_id)`, validates that all questions are still active, rehydrates Redis, re-sends current question.

### Cleanup cron — `handlers/session_cleanup.py`

- `async cleanup_stale_sessions() -> int` — fetches open sessions > 2h idle, calls `close_session_silently` for each, returns count.
- `async close_session_silently(session_id, tg_id, mode)` — generates summary (best effort), writes snapshot if `mode in RESUMABLE_MODES`, sets `ended_at`, clears Redis state. Swallows summary failures (logs warning) rather than aborting the whole close.

### Weekly reports cron — `handlers/weekly_reports.py`

- `async send_weekly_reports_to_all()` — iterates users with attempts in last 7 days, composes a 5-line recap per user, sends via `bot.send_message`. Best-effort: per-user errors logged, don't abort the run.

---

## Ingest — `ingest/*`

One-shot content loader. Runs offline, not per-request.

### `ingest/embedder.py`

- `build_embed_text(tags: dict) -> str` — canonical 3-line embedding input. **FIX 5** says both the full tagger path and the seed-ingest path must call this helper, never duplicate the logic.

### `ingest/tagger.py`

Six type-specific prompt templates (RC, PJ, VA-structural, VA-insertion, VA-summary, VA-semantic) with verbatim few-shot examples.

- `get_tagger_prompt(q_type: str, question: dict) -> str` — dispatcher. Selects the right template and formats it. **FIX 2**: formats `sentences` as `"1: ...\n2: ..."` readable text, not a dict.
- **FIX 4**: `VA_STRUCTURAL_TAGGER_PROMPT` includes the note that for wrong-one-out, `correct_option` is the odd letter and `correct_order` is the remaining four in sequence.

### `ingest/verifier.py`

- `async verify_question_answer(question: dict) -> {"verifier_answer": str, "disagrees_with_key": bool}` — independent Gemini Flash call. Caller flags `needs_review=true` on disagreement.

### `ingest/parser.py`

- `parse_tagger_output(raw: str) -> dict` — extracts JSON from the LLM response (handles ```json code fences, leading/trailing prose, etc.).

### `ingest/pipeline.py`

- `async store_question(question: dict) -> None` — validates `(type, subskill)` against `SKILL_TYPE_MATRIX`. **FIX 8**: logs legal subskills for the type on violation. Sets `needs_review=true` on illegal pair or on verifier disagreement. Inserts passage row for RC. Inserts question row.
- `async run_seed_ingest(file_path: str) -> None` — reads the 48-PYQ JSON, inserts passages, embeds each question via `build_embed_text` + `embed()`, calls `store_question`. No tagger, no verifier.
- `async run_full_ingest(file_path: str) -> None` — runs tagger → verifier → embedder → store. Used for supplementary content.
- CLI entrypoint: `python -m ingest.pipeline --file <path> [--skip-tagger] [--skip-verifier]`.

---

## Cross-cutting contracts

Rules that bind every service.

### Every async call must be awaited

Mechanical rule. See `scripts/preflight.py` Check 9 for the grep audit.

### Every retrieval SQL must filter `needs_review = false`

No exceptions. The partial index `idx_questions_review` exists specifically to support the admin queue for flagged questions.

### Only `agent/llm.py` talks to OpenRouter

No other module imports `openai` or makes HTTP calls to OpenRouter. Retry / spend-cap logic is centralized.

### Only `handlers/practice/common.py::record_attempt` writes attempts

No other code path inserts into `attempts`. This centralizes score updates, trap counter updates, and session counter updates in one place.

### Rate limit is debited only in `bot/free_text.py`

Commands and callbacks never touch it. Any new free-text route must go through `handle_free_text` to ensure debit.

### State is written through `memory/session.py::set_state`

Not through direct Redis calls. This enforces the TTL and JSON encoding consistently.

---

## Error handling semantics

Summary of how each layer handles failures.

| Layer               | Catches                     | Does                                                 |
|---------------------|-----------------------------|------------------------------------------------------|
| `main.py` (webhook) | nothing                     | Returns 200 even on handler error (see router)       |
| `bot/router.py`     | `SpendCapExceededError` + `Exception` | Sends user-facing error, releases lock      |
| `agent/llm.py`      | `RateLimitError` (retry)    | 3 attempts, raises `RuntimeError` on exhaustion      |
| `ingest/pipeline.py`| `Exception` per question    | Logs, marks `needs_review=true`, continues batch     |
| `handlers/*`        | (mostly nothing)            | Let exceptions bubble to router                      |
| `session_cleanup`   | per-session `Exception`     | Logs, attempts fallback UPDATE, continues           |
| `weekly_reports`    | per-user `Exception`        | Logs, continues                                      |

---

## Known contract debt

- **`admin/*`** is stub-only. Streamlit scaffolding exists but page logic isn't implemented. The CRUD contracts for `feedback` resolution, `needs_review` toggles, and user bans are defined in `db/queries.py` but not wired to UI.
- **`/settings`** command is unimplemented.
- **`/broadcast` and `/ban`** are defined in the command map but do not have handler logic.
- **`attempts.time_taken_secs`** column exists, never populated. No contract consumes it yet.
- **`profile:{tg_id}` cache** is invalidated on every write but never read. No contract consumes it yet.
