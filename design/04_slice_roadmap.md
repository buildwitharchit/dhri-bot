# Slice Roadmap — DHRI VARC Bot

## Principle

Thin vertical slices — each one ships end-to-end, is independently testable, and leaves the system in a state where the previous slice's tests still pass. No "backend complete but no UI" milestones.

This document is part **retrospective** (Slices 1-3 are already built — they describe what v4.1 ships with) and part **forward-looking** (Slices 4+ describe what's next). The retrospective slices are documented at the same granularity as the forward ones so future maintainers can see why decisions were made.

**Legend:**
- ✅ Done — in v4.1, code merged, tests pass.
- 🔶 Partial — scaffolding exists, some functionality stubbed.
- ⬜ Planned — not started.
- ❌ Out of scope — intentionally deferred.

---

## Slice 1 — Foundations ✅ (v4.1)

**Goal.** Establish the skeleton: taxonomy, database schema, Redis conventions, and the LLM gateway. Nothing user-facing yet, but the pieces any later slice depends on must exist and be consistent.

**What's real.**
- `config.py` — 7 skills, 18 subskills, 9 question types, 8 traps. `SKILL_TYPE_MATRIX` enforces legal (type, subskill) pairs. Constants used everywhere — changing any of them is a surgical operation.
- `db/schema.sql` — 10 tables, 12 indexes, pgvector HNSW index, `idx_questions_review` partial index for the admin queue.
- `db/client.py` — asyncpg pool with pgvector codec registered on pool init. Module-level `db` façade for `fetch`/`fetchrow`/`fetchval`/`execute`/`executemany`.
- `memory/session.py` — Upstash HTTP client, `get_state` / `set_state` / `acquire_lock` / `release_lock`. Uniform Redis key schema per spec Section 6.
- `agent/llm.py` — OpenRouter client, spend-cap check (daily Redis counter, 35-day TTL), 3-retry-with-backoff, `SpendCapExceededError` custom exception.
- Startup assertion in `main.py`: `set(ALL_SUBSKILLS) == set(SUBSKILL_TO_TECHNIQUE_QUERY.keys())`.

**What's stubbed.** Nothing in this slice — it's infrastructure.

**Manual test.**
1. `python -c "from config import *"` — no import errors.
2. Run `schema.sql` against Neon direct URL. All 10 tables present.
3. `python -c "import asyncio; from db.client import init_db_pool; asyncio.run(init_db_pool())"` — connects, registers codec, exits cleanly.
4. Startup assertion: temporarily add a stray entry to `SUBSKILL_TO_TECHNIQUE_QUERY`, run `main.py`, confirm `RuntimeError`.

**Completion criteria.** All of the above + no remaining `FIXME:` or `TODO:` in the four files above.

---

## Slice 2 — Seed content ingest ✅ (v4.1)

**Goal.** Load the 48 pre-tagged CAT PYQs into Postgres with embeddings, so retrieval has something to find.

**What's real.**
- `data/dhri_48_pyqs_v4.json` — 8 passages + 48 questions, all pre-tagged with `subskill`, `traps_present`, `option_traps`, `one_line_technique`. Three questions flagged `needs_review=true`.
- `ingest/embedder.py::build_embed_text` — canonical 3-line embedding input. **FIX 5**: both the seed path and the full-tagger path import this helper.
- `ingest/pipeline.py::store_question` — `(type, subskill)` validation against `SKILL_TYPE_MATRIX`, **FIX 8** logging, pgvector binding.
- `ingest/pipeline.py::run_seed_ingest` — loads JSON, inserts passages, embeds each question via `build_embed_text`, calls `store_question`. No tagger, no verifier.
- CLI: `python -m ingest.pipeline --file data/dhri_48_pyqs_v4.json --skip-tagger --skip-verifier`.
- Bug fix during this slice: `tagged_at` stored as naive UTC to match `TIMESTAMP` (not `TIMESTAMPTZ`) schema.

**What's stubbed.**
- Full tagger/verifier path (slice 3).

**Manual test.**
1. Run the CLI ingest against a fresh Neon DB.
2. `SELECT count(*) FROM questions` → 48.
3. `SELECT needs_review, count(*) FROM questions GROUP BY needs_review` → 45 false, 3 true.
4. `SELECT question_id FROM questions WHERE needs_review = true ORDER BY 1` → exactly `cat2023s1_geography_q1`, `cat2023s1_indian_ocean_q3`, `cat_pyq_crafts_q4`.
5. `SELECT count(*) FROM questions WHERE technique_embedding IS NULL` → 0.

**Completion criteria.** 48/48 questions ingested idempotently (rerun is a no-op via `ON CONFLICT DO NOTHING`).

---

## Slice 3 — Adaptive practice, end-to-end ✅ (v4.1)

**Goal.** Users can run `/start`, onboard, tap `/rc` or `/pj` or `/va`, get a retrieval-matched question, answer it, and see their `user_skill_scores` update. This is the MVP.

**What's real.**
- Webhook + router: `main.py`, `bot/router.py` with per-user lock.
- Command + callback routing: `bot/commands.py`, `bot/callbacks.py`.
- Free-text handling with deterministic intent classifier: `bot/free_text.py`, `agent/classifier.py`. Rate limit debited here, nowhere else.
- Onboarding flow: `handlers/onboarding.py`.
- Home screen: `handlers/home.py`.
- Three practice handlers with uniform state shape: `handlers/practice/rc.py`, `pj.py`, `va.py`.
- Shared practice helpers: `handlers/practice/common.py` — the single `record_attempt` funnel that writes attempt + updates scores + updates trap counts + updates session counters.
- Retrieval pipeline: `retrieval/selector.py` with **FIX 1** (two SQL branches) and **FIX 2** (`needs_review = false` on every query), `retrieval/reranker.py` with **FIX 3** (dropped dead `0.2 * 1.0` term).
- EWMA scoring: `memory/profile.py` with `ALPHA = 0.15`.
- Trap tracking: `update_trap_counts` → `most_common_trap` → rerank bonus.
- Weakest-skill aggregation: 18 subskill scores → 7 student-facing skills, threshold 10 attempts before `weakest_skill` is populated.
- Stats view: `handlers/stats.py` (Section 23 verbatim) — 7 skills only, never subskills to student.
- Doubt / concept: `handlers/doubt.py`, `handlers/concept.py`, `agent/explainer.py`, `agent/prompts.py`.
- Session summaries: `memory/summarizer.py` with 3-sentence rigid template, `MODEL_SUMMARIZER` (Gemini Flash).

**What's stubbed.**
- `/weak` command routes to "drill weakest" but the exact button-to-mode mapping is terse.
- `/settings` command acknowledges but does nothing.
- `/broadcast` and `/ban` admin commands are command-map entries with no handler logic.

**Manual tests (Section 30 of spec, which v4.1 passes).**

Onboarding (1-5):
1. `/start` as new user → year keyboard → level keyboard → home.
2. `SELECT count(*) FROM user_skill_scores WHERE tg_id = <new_tg>` → 18.
3. `SELECT weakest_skill FROM user_profiles WHERE tg_id = <new_tg>` → NULL.
4. Home screen shows no "Work on X" chip.

RC practice (6-9):
5. `/rc` → passage loads, none of the 3 flagged questions.
6. Answer correctly → row in `attempts`, `trap_fallen_for='none'`.
7. Answer wrong → `trap_fallen_for` matches the wrong option's entry in `option_traps`.
8. `/done` → session row has `ended_at`, `summary`.

After 10 attempts (10-11):
9. `weakest_skill` is non-NULL.
10. Home screen shows "Work on X ⚡".

PJ (12-16):
11. `/pj` → question loads.
12. Reply `4,1,2,3` → scored.
13. Reply `4,1,2` → rejected as "not a PJ answer".
14. Reply `1,1,2,3` → rejected as duplicate.
15. `pj_mistake_type` populated in `attempts` on wrong answer.

VA — all types (17-22):
16. `/va` → 7-button menu.
17. Tap Grammar → grammar question (or "no questions" if unseeded).
18. Tap Odd One Out → sentences visible labeled 1-5.
19. Tap Sentence Insertion → `source_text` paragraph shown.
20. Tap Passage Summary → source paragraph shown.
21. Answer submission works for all 7 VA types.

Stats (23-24):
22. `/stats` → 7 student-facing skill labels with bars.
23. Never shows internal subskill names like `inference_basic`.

Doubt/concept (25-26):
24. `/doubt why is C correct?` mid-session → LLM response that engages with student reasoning before revealing.
25. `"how do I approach inference"` → concept mode activates (classifier matches `how do i` prefix).

Resume (27-30):
26. `/rc` → answer 1 → wait for cron (or force run cleanup endpoint) → session closed `was_completed=false`.
27. `session_snapshots` row exists.
28. `/resume` → session appears with ⚠️.
29. Tap → validates question IDs still active → rehydrates state → re-sends current question.

Rate limit (31-33):
30. Set counter to 50 via `redis-cli SET rl:msg:<tg_id>:<date> 50` → next free text → cap message.
31. `/stats` still works after cap.
32. Button taps still work after cap.

Retrieval fallback (34-35):
33. Mark all `inference_basic` questions as seen (`INSERT INTO attempts (...)`).
34. `/rc` → fallback finds `strengthen_weaken` or adjacent difficulty.

`needs_review` enforcement (36-39):
35. `SELECT count(*) FROM questions WHERE needs_review = true` → 3.
36. Attempt `/rc` many times → `cat_pyq_crafts_q4` never appears.
37. `UPDATE questions SET needs_review = false WHERE question_id = 'cat_pyq_crafts_q4'` → now it can appear.

Ingest (40-44):
38. Ingest `rc_question` → passage + question rows both created.
39. Ingest `va_summary` → NO passage row, `source_text` in question.
40. Ingest `va_sentence_insertion` → `source_text` in question.
41. Ingest illegal `(type=va_grammar, subskill=inference_basic)` → stored with `needs_review=true` (**FIX 8** logs legal subskills).
42. Ingest verifier-disagreed question → `needs_review=true`.

Taxonomy assertion (45-47):
43. Start server → no `RuntimeError`.
44. Add stray subskill to `ALL_SUBSKILLS` only → startup fails with clear message.
45. Restore.

`_fetch_single` SQL branches (48-49):
46. Trigger PJ fallback where subskill=None → no-subskill branch executes cleanly.
47. Trigger VA with subskill filter → with-subskill branch executes cleanly.

Spend cap (50-51):
48. Set `DAILY_LLM_SPEND_CAP_USD=0.000001` → free-text message → spend cap response.
49. Restore default.

**Completion criteria.** All 51 tests pass. Three flagged questions never surface. Retrieval fallback works through all four configs. No `await` regressions (preflight Check 9 clean).

**Estimated build time.** ~20 hours spread across: DB schema + seed ingest (4h), retrieval pipeline (4h), three practice handlers (6h), LLM integration + summaries (3h), onboarding + stats + resume (3h).

---

## Slice 4 — Supplementary content, full tagger path 🔶 (partial in v4.1)

**Goal.** Bring the question bank from 48 to 110+ by running the full `tagger → verifier → embedder → store` pipeline on additional CAT mocks and sectional tests.

**What's real already.**
- `ingest/tagger.py` — six type-specific prompts with verbatim few-shot examples, dispatcher with **FIX 2** (sentences as readable text) and **FIX 4** (wrong-one-out note).
- `ingest/verifier.py` — independent Gemini Flash answer check.
- `ingest/parser.py` — JSON extraction from LLM output, handles code fences.
- `ingest/pipeline.py::run_full_ingest` — tagger → verifier → embedder → store, with `--skip-tagger` / `--skip-verifier` flags.
- Two-pass tagger strategy (Flash for structured tags, Sonnet for `one_line_technique`) implemented in `store_question`'s tagging branch.

**What's still stubbed / incomplete.**
- No supplementary question JSON has been authored yet. Target counts from Section 27:
  - RC: 27 additional across 8 subskills
  - PJ: 13 additional across 4 subskills
  - VA-structural: 21 additional across 4 types
  - VA-semantic: 18 additional (grammar + vocab)
- No "dry-run" mode in `run_full_ingest` to preview tags before writing.
- No golden-set regression test (does the tagger produce the same subskill for the 48 seed PYQs if re-run?).

**Manual test.**
1. Author 5 RC questions in the input JSON format, run `python -m ingest.pipeline --file <path>` (no skip flags).
2. Inspect DB: tags look reasonable, `needs_review` flags only truly ambiguous cases.
3. Verifier disagreement rate < 10% on known-correct questions.

**Completion criteria.**
- `SELECT count(*) FROM questions` ≥ 110.
- Every subskill has ≥ 3 medium-difficulty questions available.
- Verifier disagreement rate measurably stable over a 20-question batch.

**Estimated build time.** ~2 hours of pipeline polish + however long authoring the content takes.

---

## Slice 5 — Admin Streamlit panel ⬜ (scaffolding only)

**Goal.** Local-only Streamlit app for DB inspection, ingest runs, `needs_review` queue triage, feedback resolution, and user bans.

**What's real.**
- `admin/app.py`, `admin/pages/dashboard.py`, `admin/pages/ingest.py`, `admin/pages/questions.py`, `admin/pages/users.py`, `admin/pages/analytics.py`, `admin/pages/feedback.py` — all empty files.
- `streamlit` in `requirements.txt`.

**What needs building.**

**5a. Dashboard page.**
- Total user count.
- Total question count, broken down by `type` and `needs_review`.
- Today's spend (from Redis `spend:<date>`).
- Past 7 days attempt count, accuracy, top traps.
- Requires: `db.client` + Redis client; no auth beyond "local only".

**5b. Questions page.**
- Filter by `type`, `subskill`, `difficulty`, `needs_review`.
- Per-question detail view with options, explanation, traps, `one_line_technique`.
- Toggle `needs_review` flag (single-button action).
- Toggle `is_active` (soft delete).

**5c. Ingest page.**
- File upload box for JSON.
- Run `run_full_ingest` with live log streaming.
- Display flagged questions at end for manual review.

**5d. Users page.**
- Search by `tg_id` or `username`.
- View profile: scores, attempts, trap counts, streak.
- Ban/unban (`is_banned`, `ban_reason`).

**5e. Analytics page.**
- Skill-average histograms.
- Session length distribution.
- Rate limit hit rate.

**5f. Feedback page.**
- Queue of unresolved feedback rows.
- "Mark resolved" button.
- Link to the referenced question.

**Manual test.**
1. `streamlit run admin/app.py`.
2. Dashboard shows the three flagged seed questions.
3. Clear a flag → refresh retrieval (in the bot) → the question is now reachable.
4. Ban a test user → their next `/start` returns a static ban message from the bot.

**Completion criteria.** All 6 pages functional, local-only (no auth layer beyond "don't deploy this to the public internet"), handles an empty DB gracefully.

**Estimated build time.** ~8 hours. Pages 5a and 5b are most valuable; 5c/5d/5e/5f can ship later.

---

## Slice 6 — Profile cache activation ⬜

**Goal.** Read `profile:{tg_id}` from Redis when present; fall back to Postgres on miss. Today the cache is invalidated correctly but never read.

**Motivation.** At beta scale (50-100 DAU), profile reads are cheap, but every message triggers a `get_or_create_user` call that does 2 Postgres reads (`tg_users` + `user_profiles`). A Redis cache halves that at near-zero cost.

**What's real.** Invalidation on write already in `memory/profile.py` (every `update_skill_score` and `update_trap_counts` does `DEL profile:{tg_id}`).

**What needs building.**
- A `get_cached_profile(tg_id)` helper in `db/queries.py` that tries Redis first.
- Update `get_or_create_user` to write-through to Redis on read.
- A config toggle (default on) so we can disable in testing.

**Manual test.**
1. Send a message. Time the prelude (in logs).
2. Send another message. Time the prelude.
3. Second prelude is ≥ 20% faster.

**Completion criteria.** No regression on Section 30 tests. Measurable p50 improvement.

**Estimated build time.** ~1 hour.

---

## Slice 7 — `/settings` command ⬜

**Goal.** Let users change target year, difficulty, and opt in/out of weekly reports without bothering admin.

**What needs building.**
- `handlers/settings.py` with an inline-keyboard flow.
- Keyboards for year change / difficulty change / notifications on-off.
- A new `user_profiles` column: `weekly_reports_enabled BOOLEAN DEFAULT true`.
- Schema migration (new; not included in the initial `schema.sql`).
- `weekly_reports.py` to respect the toggle.

**Manual test.**
1. `/settings` → see 3 options.
2. Change difficulty → confirm `user_profiles.current_difficulty` updated.
3. Disable reports → verify next weekly cron skips this user.

**Completion criteria.** Full round trip works, no PII leaks, settings persist across sessions.

**Estimated build time.** ~2 hours.

---

## Slice 8 — Admin commands (`/broadcast`, `/ban`) ⬜

**Goal.** Admin can send a message to all users and ban specific users without opening the Streamlit app.

**What needs building.**
- `settings.ADMIN_TG_ID` in `config.py`.
- `/broadcast <message>` handler: checks admin, iterates active users, sends with rate-limited per-user delay.
- `/ban <tg_id> [reason]` handler: checks admin, updates `tg_users.is_banned = true`, clears their state.
- Bot-level check: every incoming update verifies `is_banned = false`; short-circuits with a static message otherwise.

**Manual test.**
1. Non-admin tries `/broadcast` → silent no-op.
2. Admin `/broadcast hello team` → all active users get the message.
3. Admin `/ban 12345678 spam` → banned user's next `/start` returns ban message, no processing.

**Completion criteria.** Broadcast doesn't rate-limit out; banned users truly can't interact.

**Estimated build time.** ~2 hours.

---

## Slice 9 — Question timing ⬜

**Goal.** Populate `attempts.time_taken_secs` so the admin analytics page can show per-question pacing distributions.

**What needs building.**
- Store `question_shown_at` timestamp in state when a question is sent.
- In `record_attempt`, compute `time_taken_secs = now - question_shown_at`.
- Add timing aggregates to Streamlit analytics.

**Manual test.**
1. Answer a question after 15 seconds; `time_taken_secs ≈ 15`.
2. Slow question (60s+) — still works, no overflow.

**Completion criteria.** Column populated for all new attempts; historical rows stay NULL.

**Estimated build time.** ~1 hour.

---

## Slice 10 — Session TTL refresh on doubt ⬜

**Goal.** Fix the known edge case from Path B step 15: a user doubting for 2 hours without answering loses their session.

**What needs building.**
- Single `await set_state(tg_id, state)` call at the end of `handlers/doubt.py::handle_doubt` and `handlers/concept.py::handle_concept`.
- Similar for any new free-text handler added in the future.

**Manual test.**
1. Start an RC session, answer nothing.
2. Send 3 doubts over 2.5 hours.
3. Session state is still alive; answer keyboard still works.

**Completion criteria.** State TTL refreshes on any user interaction, not just practice answers.

**Estimated build time.** ~15 minutes.

---

## Slice 11 — Intent classifier tightening ⬜

**Goal.** Fix two edge cases from Path B "what could go wrong":

**11a.** `"How do I solve this?"` mid-session should be **doubt** (uses current question context), not **concept** (treats as a fresh topic). Today the `how do i` prefix wins regardless of state.

**11b.** A string that matches `is_pj_answer` but mode is RC should route to the RC answer path (which will reject since RC doesn't support PJ-style answers), not the PJ answer handler (which will fail to find a PJ question).

**What needs building.**
- Update `classify_free_text(text, state.state)` to take the full state label, not just `in_active_practice`.
- Branch: PJ-style answer only dispatches if `state == 'PJ_ACTIVE'`.
- Branch: concept prefix only wins if `state == 'IDLE'`.

**Manual test.**
1. `/rc`, mid-session type `"How do I solve this?"` → goes to doubt with question attached.
2. `/rc`, mid-session type `"4,1,2,3"` → goes to doubt (or ignored — pick one), not PJ handler.

**Completion criteria.** No mis-routes on either of the two edge cases.

**Estimated build time.** ~30 minutes.

---

## Slice 12 — Retry mode ⬜

**Goal.** Let users retry questions they got wrong. Flagged in `attempts.is_reattempt = true` so they don't pollute the primary score EWMA.

**What needs building.**
- New handler: `handlers/practice/retry.py`.
- Home screen button: "Retry my mistakes".
- Query: fetch `attempts` where `is_correct = false` in last 14 days.
- Retry presentation: re-show the question; score update uses `ALPHA_RETRY = 0.05` (softer than normal).

**Manual test.**
1. Miss a few questions, wait a day.
2. "Retry my mistakes" → those questions re-appear.
3. Correct retry nudges score up slightly.

**Completion criteria.** Retry attempts don't double-count; users see progress visually.

**Estimated build time.** ~3 hours.

---

## Slice 13 — Agent-generated content ⬜

**Goal.** Use Claude Sonnet to generate new PYQ-style questions (with `source='agent_generated'`) for thin subskills like `logical_structure` (0 seed questions).

**What needs building.**
- `ingest/generator.py` — prompt template, self-verification loop.
- Gate: all agent-generated questions go through `needs_review=true` by default.
- Streamlit admin page to approve/reject in batch.
- Clear visual marker in admin that differentiates `cat_official` from `agent_generated`.

**Manual test.**
1. Run generator for `logical_structure`, 5 questions.
2. All arrive in DB with `needs_review=true`.
3. Human reviews, clears flags on acceptable ones, deletes the rest.

**Completion criteria.** End-to-end generate → review → serve works for at least 5 subskills.

**Estimated build time.** ~6 hours.

---

## Out of scope for the initial build ❌

These are intentionally **not** in the v4.1 scope and are not planned near-term. Listed to make scope boundaries explicit.

- **Web UI.** Telegram is the sole interface. The web product is planned as a separate project after the beta proves retention.
- **Payments / tiers.** Free forever during beta. Monetization is a post-beta decision.
- **Voice / image input.** Text only.
- **Multi-language support.** English only.
- **Quant / DI-LR sections.** VARC only. DHRI (Quant) and DHMI (DI-LR) are future sister bots, not this codebase.
- **Per-user model choice.** Model IDs are global in `config.py`. No per-user override.
- **Question editing by users.** No user can mutate the content bank.
- **Cross-user features.** No leaderboard, no social, no peer study groups in v1.
- **Push notifications (daily nudges).** Telegram has no native push beyond our own `bot.send_message`, and daily unsolicited messages feel spammy in beta. Revisit post-beta.
- **Historical backfill of `time_taken_secs`.** When slice 9 lands, historical rows stay NULL forever. Analytics accepts this gap.

---

## Release checklist (for any slice marked ✅)

Before marking a slice complete, verify:

1. **Section 30 manual tests pass** (for slices touching user-facing flows).
2. **`scripts/preflight.py`** runs clean (all PASS, zero FAIL).
3. **No new `TODO:` / `FIXME:` / `XXX:`** comments in touched files (or, if added, they have a filed issue linked).
4. **`IMPLEMENTATION_NOTES.md`** appended with one block per new/changed file.
5. **`requirements.txt`** updated if new dependencies added. Pinned, not `>=`.
6. **No dead awaits.** Every `async def` is awaited at every call site (FIX 6).
7. **Spec consistency.** If the slice touches retrieval, all four SQL branches still filter `needs_review = false`.

---

## Velocity guidance

From actual v4.1 build experience, rough per-slice duration:

| Slice type                         | Hours |
|------------------------------------|-------|
| Infra / schema only                | 2-4   |
| Single handler + DB helper         | 1-2   |
| New mode (retrieval + handler + state) | 4-6   |
| LLM integration point              | 2-3   |
| Admin Streamlit page               | 1-2 each |
| Ingest pipeline feature            | 2-4   |
| Bug fix / FIX application          | 0.5-1 |

A full day of disciplined work ships one medium slice. The "complete v4.1" run (Slices 1-3) was ~20 hours across 2-3 sessions.

---

## Anti-patterns to watch for

During future work, reject any change that:

- **Adds a new retrieval query without `AND q.needs_review = false`.** Use the existing selector or extend it; don't write ad-hoc queries.
- **Adds a new LLM call site outside `agent/llm.py`.** Every completion must route through the spend-cap wrapper.
- **Reads or writes `state:tg:{tg_id}` outside `memory/session.py`.** Direct Redis access bypasses JSON encoding and TTL conventions.
- **Writes to `attempts` outside `handlers/practice/common.py::record_attempt`.** That funnel keeps scoring, traps, and session counters consistent.
- **Introduces a new field to the state dict without deciding what `session_snapshots` does with it.** Resume will break silently.
- **Changes `config.py` constants without a taxonomy audit.** Every constant there is referenced in 5-10 places.
- **Makes an async function sync or vice versa.** Preflight Check 9 will fail loudly; the compiler won't.
