# DHRI VARC Bot — Implementation Notes

Paper trail for the v4.1 build. One block per file in Section 31 order.

---

## db/schema.sql
- Depends on: nothing (run manually against Neon direct connection)
- Fixes applied: none
- Assumptions: verbatim Section 5 including `idx_questions_review` partial index and HNSW on `technique_embedding`.
- Contract source: Section 5 (verbatim)

## config.py
- Depends on: pydantic-settings
- Fixes applied: none
- Assumptions: verbatim Section 4. SKILL_TYPE_MATRIX covers all 9 types. MIN_ATTEMPTS_FOR_WEAKEST_SKILL=10.
- Contract source: Section 4 (verbatim)

## db/client.py
- Depends on: asyncpg, pgvector, config.settings
- Fixes applied: none (FIX 6 applies to callers of async methods; module exposes only async methods)
- Assumptions: pool min 1/max 10, per-call acquire; pgvector codec registered via asyncpg `init` hook so vectors bind natively as `$N::vector` (spec rule #10).
- Contract source: derived from v3-copy contract (user)

## memory/session.py
- Depends on: upstash-redis.asyncio, json, config.settings
- Fixes applied: none
- Assumptions: STATE_TTL=7200 (Section 6). `set_nx` wraps SET NX EX. get_state self-heals malformed JSON by deleting the key.
- Contract source: Section 6 + v3-copy contract

## agent/llm.py
- Depends on: openai (AsyncOpenAI), config.settings, memory.session.redis
- Fixes applied: none (FIX 6 applies to callers)
- Assumptions: OpenAI SDK v1 AsyncOpenAI with OpenRouter base_url. SpendCapExceededError defined here. Crude $/token table for cap heuristic; replaced by `usage.total_tokens` when API returns it. 3 retries with exponential backoff.
- Contract source: v3-copy contract (user)

## db/queries.py
- Depends on: asyncpg (db), memory.session, config
- Fixes applied: none
- Assumptions: All Section 26 helpers implemented per user contract. `get_state_from_db_or_redis` prefers live Redis state, falls back to session_snapshots. `check_and_increment_rate_limit` only increments when allowed — no false-positive drain.
- Contract source: Section 26 + user contract

## bot/utils.py
- Depends on: python-telegram-bot, html, datetime
- Fixes applied: none
- Assumptions: MAX_TELEGRAM_MESSAGE_CHARS=4000. Splits on paragraph, then newline, then space. IST offset hardcoded +05:30. get_seconds_until_ist_midnight always ≥1.
- Contract source: v3-copy contract

## bot/keyboards.py
- Depends on: python-telegram-bot, config
- Fixes applied: none
- Assumptions: home_quick_keyboard inserts "Work on X" row only when weakest_skill is set (Section 30 test 5 & 11). All 7 VA types wired.
- Contract source: Sections 11, 18, 23 button layouts

## retrieval/technique_queries.py
- Depends on: config.ALL_SUBSKILLS
- Fixes applied: none
- Assumptions: verbatim Section 12 — every key in ALL_SUBSKILLS present. main.py startup assertion validates equality.
- Contract source: Section 12 (verbatim)

## retrieval/pgvector.py
- Depends on: math
- Fixes applied: none
- Assumptions: cosine + literal helpers only. Real vector handling goes through pgvector codec in db/client.py.
- Contract source: implied by Section 31 step (j)

## retrieval/reranker.py
- Depends on: —
- Fixes applied: **FIX 3** — composite is `0.62 * sim + 0.38 * trap_score`. Dead `0.2 * 1.0` term removed.
- Assumptions: difficulty is already a SQL filter so omitting its reranker weight is correct.
- Contract source: Section 15 + FIX 3

## retrieval/selector.py
- Depends on: config, technique_queries, reranker, memory.profile, agent.llm.embed, db
- Fixes applied: **GAP 1** (two SQL branches in `_fetch_single`), **GAP 2 / FIX 2** (`AND q.needs_review = false` on every retrieval query — 4 occurrences)
- Assumptions: verbatim Section 14 logic with `dict(row)` coercions so rerank and dict access work.
- Contract source: Section 14 (verbatim, with Gap fixes)

## memory/profile.py
- Depends on: config, db, redis
- Fixes applied: none
- Assumptions: verbatim Section 13. `get_weakest_student_skill` defaults to "inference" on zero data. trap_counts tolerated as str (JSONB) or dict.
- Contract source: Section 13 (verbatim)

## memory/summarizer.py
- Depends on: agent.llm, config.settings, db
- Fixes applied: none
- Assumptions: 3-sentence summary, plain prose. Falls back to a deterministic template if LLM call fails.
- Contract source: v3-copy contract

## agent/prompts.py
- Depends on: —
- Fixes applied: none
- Assumptions: verbatim Section 22.
- Contract source: Section 22 (verbatim)

## agent/explainer.py
- Depends on: agent.llm, agent.prompts, config
- Fixes applied: none
- Assumptions: `build_context` uses SKILL_DISPLAY_NAMES; `build_messages_for_llm` appends current user turn to prior history.
- Contract source: Section 22 + Section 26 helper sketch

## agent/classifier.py
- Depends on: re
- Fixes applied: none
- Assumptions: deterministic, no LLM. `is_pj_answer` accepts only 4 distinct digits from {1,2,3,4} (letters rejected) — seed JSON is canonical. `normalise_pj_answer` removed; no normalisation needed.
- Contract source: v3-copy contract (user) for bot/free_text.py; PJ format confirmed digit-only by user

## handlers/onboarding.py
- Depends on: db.queries, memory.session, bot.keyboards
- Fixes applied: none
- Assumptions: two-step flow (year → level). Defensive: re-seeds skill scores if user exists but has none.
- Contract source: Section 11

## handlers/home.py
- Depends on: bot.keyboards, bot.utils, memory.session
- Fixes applied: none
- Assumptions: renders `home_quick_keyboard(profile)` and sets IDLE.
- Contract source: implied by Section 11 + test 11

## handlers/practice/common.py
- Depends on: db, memory.profile
- Fixes applied: none
- Assumptions: `get_question_context` is verbatim Section 17. `record_attempt` also updates user_skill_scores, trap counts, and `sessions.skills_practiced` (idempotent append).
- Contract source: Section 17 + test 7/8 + test 16

## handlers/practice/rc.py
- Depends on: retrieval.selector, handlers.practice.common, db.queries, memory
- Fixes applied: none
- Assumptions: RC sessions walk the ≥2-question set sequentially. On empty result, surfaces exhausted message.
- Contract source: Sections 10, 14, 17

## handlers/practice/pj.py
- Depends on: retrieval.selector, handlers.practice.common, agent.classifier
- Fixes applied: **FIX 7** (questions_in_set[index], never `current_question_id`)
- Assumptions: single-question PJ session. Answer format is digits only (1-4), matching seed JSON's `correct_order`. Submitted string is stripped to digits and compared directly — no letter normalisation. User prompt: "Enter the sequence (e.g., 4,1,2,3)". PJ answer does NOT hit LLM → does NOT count against rate limit.
- Contract source: Section 10 uniform state + FIX 7; seed JSON canonical (user direction)

## handlers/practice/va.py
- Depends on: retrieval.selector, handlers.practice.common
- Fixes applied: none
- Assumptions: verbatim Section 18 menu + send_va_question. Answer via inline A/B/C/D buttons. `va_type` persisted in state for potential future redrill.
- Contract source: Section 18 (verbatim) + tests 17-22

## handlers/doubt.py
- Depends on: agent.explainer, db.queries
- Fixes applied: none
- Assumptions: attaches current question context when in an active session; uses MODEL_COMPLEX for doubt explanations.
- Contract source: v3-copy contract

## handlers/concept.py
- Depends on: agent.explainer, db.queries
- Fixes applied: none
- Assumptions: MODEL_CHAT (cheaper) for concept teaching; instructs the model to cap at 180 words.
- Contract source: v3-copy contract

## handlers/stats.py
- Depends on: config, db, db.queries
- Fixes applied: none
- Assumptions: verbatim Section 23. Aggregates subskill scores to 7 student skills. Never shows subskill labels (test 24).
- Contract source: Section 23 (verbatim)

## handlers/resume.py
- Depends on: db, memory.session, bot.keyboards, handlers.practice.common
- Fixes applied: none
- Assumptions: lists up to 5 resumable sessions in last 48h; validates question still active + not flagged before re-presenting. If invalid, suggests starting fresh.
- Contract source: v3-copy contract + tests 27-30

## handlers/session_cleanup.py
- Depends on: db, memory.session, memory.summarizer, db.queries
- Fixes applied: none
- Assumptions: verbatim Section 10; summary failure is non-fatal; snapshot failure is non-fatal; fallback UPDATE if the clean close raises.
- Contract source: Section 10 (verbatim)

## handlers/weekly_reports.py
- Depends on: config, db, db.queries, python-telegram-bot
- Fixes applied: none
- Assumptions: 5-line-max per user; swallows per-user send errors so one bad chat_id doesn't block the cron.
- Contract source: v3-copy contract

## bot/commands.py
- Depends on: every handler module
- Fixes applied: none
- Assumptions: 14 commands wired per Section 7; commands never count against rate limit; /settings /feedback /broadcast /ban are stubs.
- Contract source: Section 7 + v3-copy contract

## bot/callbacks.py
- Depends on: every handler module
- Fixes applied: none
- Assumptions: routes callbacks by data prefix. Answer buttons dispatch based on the active state label. `drill_weakest` routes by weakest student-facing skill family.
- Contract source: v3-copy contract

## bot/free_text.py
- Depends on: agent.classifier, db.queries (rate limit), handlers.concept/doubt/pj
- Fixes applied: none
- Assumptions: rate limit is debited **only** on LLM-bound intents (concept, doubt). PJ answers (no LLM) skip the debit. Commands and callbacks are never routed here.
- Contract source: v3-copy contract (Section 8 rate-limit rule)

## bot/router.py
- Depends on: memory.session (lock), db (ban check), agent.llm.SpendCapExceededError, bot.utils
- Fixes applied: none
- Assumptions: Section 9 verbatim. 5-second user-level lock. Banned users silently dropped.
- Contract source: Section 9

## main.py
- Depends on: everything
- Fixes applied: none
- Assumptions: Section 24 verbatim. Startup taxonomy assertion raises RuntimeError on mismatch. Admin endpoints check ADMIN_REPORTS_SECRET.
- Contract source: Section 24

## ingest/tagger.py
- Depends on: —
- Fixes applied: **FIX 2** (sentences formatted as `k: v` lines in get_tagger_prompt), **FIX 4** (wrong-one-out note above the question in VA_STRUCTURAL_TAGGER_PROMPT)
- Assumptions: six prompts verbatim including all few-shot examples. Dispatcher maps all 9 question types; unknown types fall back to RC.
- Contract source: Section 19 (verbatim) + FIX 2 + FIX 4

## ingest/embedder.py
- Depends on: agent.llm.embed
- Fixes applied: **FIX 5** (sole owner of `build_embed_text`; both pipeline paths call it)
- Assumptions: verbatim Section 16.
- Contract source: Section 16 + FIX 5

## ingest/parser.py
- Depends on: json, re
- Fixes applied: none
- Assumptions: tolerates markdown fences and surrounding prose around the JSON object.
- Contract source: implied by Section 20

## ingest/verifier.py
- Depends on: agent.llm, config
- Fixes applied: none
- Assumptions: skips verification for questions without a `correct_option` (PJs). Verifier disagreement sets `_verification_flagged=True`, which store_question translates to needs_review=true.
- Contract source: Section 19 "two-pass" hint + tests 43-44

## ingest/pipeline.py
- Depends on: db, ingest.*, config, agent.llm
- Fixes applied: **FIX 5** (shared build_embed_text in both seed and full path), **FIX 8** (logs legal subskills on illegal pair)
- Assumptions: `--skip-tagger` triggers run_seed_ingest; default runs full tagger + verifier unless `--skip-verifier`. Startup taxonomy assertion runs in CLI too.
- Contract source: Section 20 + Appendix A + FIX 5 + FIX 8

## admin/*
- Depends on: streamlit, db
- Fixes applied: none
- Assumptions: local-only Streamlit panel (auth.py is a placeholder). Dashboard, ingest, questions, users, analytics, feedback pages each render a minimal view; questions page supports clearing `needs_review` (test 38).
- Contract source: Section 3 + manual test script section on admin

## deployment files
- `requirements.txt`: verbatim Section 28
- `railway.json`: uvicorn start, /health check, on-failure restart
- `.env.example`: already present pre-build
- `.gitignore`: standard Python ignores

---

## Spec observations

1. **Section 30 test 13 corrected.** Original spec wording said "4-letter distinct A-D answer", but seed JSON is canonical: sentences keyed "1"-"4", `correct_order` stored as digits. Local test record now reads: **"Valid 4-digit distinct 1-4 answer → scored."** PJ validator accepts digits only; letters are rejected.
2. **`check_and_increment_rate_limit`** lives in `db/queries.py` rather than bot layer — keeps Redis logic out of the handler surface.
