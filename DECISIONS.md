# DECISIONS.md — DHRI Architecture & Engineering Decisions Log

This file is the chronological log of every meaningful architectural, engineering, and product decision made during dhri's development. Each entry captures **what was decided, why, what was rejected, and what we'll learn from it.**

This log lives on `main` only. Every branch merge adds entries here. It's the single source of truth for "how dhri became dhri" — useful for portfolio, interviews, and for the next person (or me 6 months from now) trying to understand why things are the way they are.

**Format for each entry:**
```
## YYYY-MM-DD — Title
**Status:** Decided | Reconsidered | Reversed
**Slice / Phase:** <which slice if applicable>
**Decision:** What we're doing
**Why:** The reasoning, including alternatives considered
**Rejected alternatives:** Specific options we considered and didn't take, plus why
**Tradeoffs accepted:** What we're giving up
**Revisit when:** Trigger condition that should make us reconsider
```

---

## 2026-04-25 — DHRI v5 architecture: 6-service split, planner-driven routing

**Status:** Decided  
**Slice / Phase:** Pre-implementation, all slices

**Decision:** v5 will be built around 6 services: Message Bus, Orchestrator, VARC Agent, Mentor Agent, Memory Service, Profile Service. A scheduler service is deferred until proactive Mentor outreach is added (post-v1).

The Orchestrator runs a single Planner LLM call (Gemini Flash) that produces a combined intent classification + context_needs flags + response_guidance, eliminating the need for separate planner / router / context-fetcher LLM calls.

**Why:** v4 felt broken because every interaction passed through a state machine that fragmented the conversation. v5 is designed to feel like a continuous conversation while preserving structured behavior under the hood. A single planner call with combined responsibilities reduces latency (one LLM call instead of three) and makes routing decisions explainable.

**Rejected alternatives:**
- **Agentic tool-calling within VARC** (giving VARC a toolbox of retrieval/explanation/etc. and letting it choose): rejected because it's slower, less predictable, and doesn't add accuracy at this scale. Code-based routing is cheaper and clearer.
- **Separate planner, intent classifier, and context fetcher LLM calls:** rejected because three sequential LLM calls add 3-5 seconds and 3x cost for marginal accuracy gain.
- **Mentor fetches its own context via tool calls:** rejected. Orchestrator plans context for ALL agents uniformly. This keeps agents stateless and orchestration centralized.

**Tradeoffs accepted:**
- Planner correctness becomes a single point of failure. We accept this and plan to iterate on the planner prompt with real misclassification examples after slice 4.
- Combined response_guidance + context_needs in one JSON makes the planner prompt slightly more complex. Worth it for the latency win.

**Revisit when:**
- Planner accuracy < 85% on a 50-case eval set
- Latency p95 > 6 seconds and planner is the bottleneck

---

## 2026-04-25 — Profile reads use templates, not LLM

**Status:** Decided  
**Slice / Phase:** Slice 5

**Decision:** Profile service's `get_tutor_brief` is a Python string template filled with structured data from `student_profile`, top-N notes from `student_notes`, and computed skill signals. No LLM in the read path.

Notes retrieval uses SQL with `confidence × exp(-Δt / 30 days)` scoring, ordered descending, top 20.

Note writes (slice 7) use LLM extraction during session-end pipeline.

**Why:** A profile read happens on every turn. An LLM in the read path adds 1-2 seconds and ~$0.001 per turn. At 500 turns/day per active user, that's $1.50/user/day just for profile reads. Templates are free, instant, and deterministic. The "personalization feel" comes from what data is in the profile (rich, accurate notes), not from how the brief is generated.

**Rejected alternatives:**
- **LLM-summarized brief on every read:** rejected for cost and latency reasons above.
- **Vector search on notes for semantic retrieval:** rejected for v1 because note volume is small (10-50/student initially); SQL filtering by category + confidence + recency is sufficient. Embedding every note also adds cost on writes. Revisit when student note count > 200.
- **Storing brief in Redis cache:** rejected because the brief depends on real-time skill signal calculations which change after every question attempt; cache invalidation would be tricky. Skill signals themselves ARE cached (1-hour TTL).

**Tradeoffs accepted:**
- Brief is structured / templated, not free-flowing prose. Risk: VARC LLM might use it more mechanically. Mitigated by giving the LLM the raw data and trusting it to weave naturally.
- No semantic search means we miss notes that are conceptually relevant but don't share keywords. Acceptable tradeoff given small note volume.

**Revisit when:**
- Student note count > 200 (semantic search becomes valuable)
- Profile-aware responses feel mechanical in user testing

---

## 2026-04-25 — VARC retrieval: 6-tier fallback ladder, always returns a question

**Status:** Decided  
**Slice / Phase:** Slice 2

**Decision:** VARC retrieval uses a 6-tier fallback. Tier 1 (best) requires unseen + matching subskill + matching difficulty + matching profile signals. If empty, walk down through progressively relaxed filters. Tier 6 (last resort) is any seen question, oldest first.

When serving tier 5 or tier 6 (repeated) questions, VARC agent prepends an acknowledgment: "We've seen this before, let's try with fresh eyes" (tier 5) or "Running low on new questions, let's revisit" (tier 6).

**Why:** v4 returned "no questions left" when constraints couldn't be met, blocking the user. This is a worse experience than serving an imperfect question. Always-return-a-question is the right product instinct, but unacknowledged repeats feel buggy. The acknowledgment turns a degraded experience into a teaching moment.

**Rejected alternatives:**
- **Block when constraints fail (v4 behavior):** rejected because dead ends break flow.
- **Random selection on fallback:** rejected because randomness loses learning signal (oldest-first means we test long-term retention).
- **Generate new questions on demand via LLM:** rejected because question quality control is hard, and CAT-style PYQs have specific authenticity that LLM-generated questions can't easily match in v1. Worth revisiting once we have reliable eval pipeline.

**Tradeoffs accepted:**
- Students who go through all 48 questions hit repeats sooner. Mitigated by seeding more questions over time.
- Tier 6 always returns ANY question, even if it's a poor pedagogical match. Acceptable as last resort with explicit acknowledgment.

**Revisit when:**
- We have > 200 questions in the bank (some tiers become unnecessary)
- We have a reliable pipeline for LLM-generated questions

---

## 2026-04-25 — Onboarding: FSM for data, agent for diagnostic, mentor for synthesis

**Status:** Decided  
**Slice / Phase:** Slice 6

**Decision:** Onboarding has three parts:
1. **FSM (no LLM):** Sequential data collection via Telegram inline keyboards — name, target_year, experience, prep stage, hours/day, optional colleges, optional why-CAT.
2. **VARC Agent (diagnostic_mode):** 5 questions (2 easy, 2 medium, 1 hard) following pattern: Q1 easy_inference, Q2 easy_main_idea, Q3 medium_inference, Q4 medium_specific_detail, Q5 hard_inference. Skippable.
3. **Mentor Agent (one-time synthesis):** Reviews diagnostic results + profile, gives warm 3-4 paragraph synthesis with weak areas + suggested focus.

**Why:** Pretending data collection is a "conversation" is dishonest UX — names and target years are forms, structurally. FSM is fast (button taps, no LLM) and reliable. The diagnostic test IS conversational and benefits from VARC agent's natural feel. Synthesis benefits from Sonnet's emotional intelligence to make a warm first impression.

This split saves ~7 LLM calls vs. a fully agent-driven onboarding (just 5 calls for the diagnostic explanations + 1 for synthesis).

**Rejected alternatives:**
- **Fully agent-driven onboarding:** rejected because LLM-collected data has higher error rates (typos, ambiguity), is slower, and costs more.
- **Skip diagnostic for v1, just collect data:** rejected because the diagnostic is the highest-signal moment for first-impression personalization. Without it, mentor synthesis would be generic.
- **Mandatory diagnostic (no skip):** rejected because forcing it would alienate users who want to dive straight to practice.

**Tradeoffs accepted:**
- FSM is more code than agent-driven. Acceptable given the reliability gain.
- 5 questions is short — won't perfectly diagnose subskill strengths. Acceptable as initial signal; real signal accumulates over first 50 practice questions.

**Revisit when:**
- Onboarding completion rate < 60% (something is too long or too painful)

---

## 2026-04-25 — Mentor architecture: reactive + observer, no initiator (yet)

**Status:** Decided  
**Slice / Phase:** Slice 8

**Decision:** Mentor agent has two modes in v1:
1. **Reactive:** Routed to when planner classifies intent.domain = 'mentor' (strategic queries, anxiety, motivation).
2. **Observer:** Called inline by orchestrator after every agent response. Cheap LLM (Gemini Flash) scans for patterns and writes raw events to `observer_events` table. Does NOT modify the response. Events are consumed by session-end extractor.

Initiator mode (proactive outreach: "you haven't practiced in 3 days") is deferred to v2.

Because there's no initiator, **there's no scheduler service in v1.** Session cleanup is a Railway cron job calling /admin/cleanup, not a service.

**Why:** Reactive + observer covers 80% of the "feels like a thoughtful tutor" experience. Initiator is a distinct product surface (push notifications) that needs separate infrastructure (rate limits per user/day, opt-in consent, scheduled task management). Building it in v1 increases scope without much marginal value, since users will be opening the bot themselves during the early phase.

Observer is critical: it's how dhri builds the "the bot remembers what I struggled with" feeling. Cheap to run (Gemini Flash, ~$0.0002/event) and doesn't add latency to user-visible responses (writes asynchronously after response is sent).

**Rejected alternatives:**
- **Build all three modes (reactive + observer + initiator) in v1:** rejected for scope reasons.
- **Skip observer, only do reactive:** rejected because observer is the secret sauce for cross-session pattern detection. Without it, the system feels less "aware" over time.
- **Have VARC do its own pattern detection:** rejected because VARC is in the latency-critical path; it can't afford a second LLM call per turn.

**Tradeoffs accepted:**
- No proactive outreach in v1 — users have to open the bot themselves. Acceptable for first 50 users (early adopters re-engage organically).
- Observer events accumulate in DB and are only consumed at session-end. There's a window where patterns are detected but not yet visible. Acceptable.

**Revisit when:**
- We have > 50 weekly active users (proactive outreach starts paying off)
- Re-engagement rate drops below 40%

---

## 2026-04-25 — Loading UX: thinking message + typing indicator, single edit

**Status:** Decided  
**Slice / Phase:** Slice 1 (foundational)

**Decision:** On every user message:
1. Immediately send "🤔 Thinking..." as a new message; save its message_id
2. Set Telegram typing indicator (sendChatAction)
3. Refresh chat action every 4 seconds while processing
4. When response is ready, editMessageText to replace "🤔 Thinking..." with final content

No mid-process status updates. No "Pulling question...", "Looking at memory..." progress messages. One edit at the end.

**Why:** Mid-process updates burn Telegram message edit rate limits fast (Telegram limits message edits to 1/second per chat). A 5-second response with 4 status updates would hit the limit. Also, status updates leak implementation details users don't care about — they want a thoughtful answer, not a build log.

The typing indicator (chatAction) is a separate API with much higher rate limits — it's free to refresh every 4 seconds.

The thinking message + final edit gives users:
- Immediate feedback that the bot received the message (visible message)
- Continuous "I'm working on it" feeling (typing animation)
- A clean final response that replaces the placeholder (no message clutter)

**Rejected alternatives:**
- **Stream tokens as they arrive:** rejected because Telegram doesn't support real streaming; only edits, which would hit rate limits.
- **No thinking message, just typing indicator:** rejected because new users who don't know what "the typing animation" means feel ignored. The explicit "🤔 Thinking..." message is reassuring.
- **Multi-stage status updates:** rejected for rate-limit reasons above.

**Tradeoffs accepted:**
- Users don't see what the bot is doing internally (could feel opaque for slow turns). Acceptable; final response speaks for itself.
- For very long operations (>10s), user might wonder if the bot is stuck. Acceptable for v1; revisit if we have many slow turns.

**Revisit when:**
- p95 response latency > 10 seconds (might need an "still working..." check-in edit at the 6-second mark)

---

## 2026-04-25 — Same tech stack as v4: Postgres + Redis + OpenRouter + Railway + Telegram

**Status:** Decided  
**Slice / Phase:** All slices

**Decision:** v5 uses the same tech stack as v4:
- **Postgres** (Neon, hosted): persistent storage
- **Redis** (Upstash, hosted): cache + session ephemera
- **OpenRouter**: model dispatch across Anthropic / Google / others
- **Railway**: deployment platform
- **Telegram**: user interface

**Why:** Stack decisions are not the bottleneck. The architecture is. Switching stacks would burn time without improving the product.

**Rejected alternatives:**
- **Move to a single LLM provider (Anthropic direct or OpenAI):** rejected because OpenRouter's flexibility lets us use Gemini for cheap/fast classification and Claude for high-quality reasoning. Single-provider would force compromises.
- **Self-hosted Postgres / Redis:** rejected for ops overhead at v1 scale.
- **Move from Railway to Fly / Render / AWS:** rejected because Railway works fine and switching is overhead. Revisit if we hit Railway limits or need region-specific deployment.

**Tradeoffs accepted:**
- Vendor lock-in to Neon + Upstash + Railway + OpenRouter. Acceptable for v1 velocity. Each can be swapped if needed (Neon → any Postgres host, Upstash → any Redis, Railway → any container platform, OpenRouter → direct provider SDKs).

**Revisit when:**
- A specific limit hurts us (latency, cost, region availability)

---

## 2026-04-25 — Documentation discipline: stable design docs + dynamic DECISIONS.md

**Status:** Decided  
**Slice / Phase:** Process / all slices

**Decision:** Documentation is split into two layers:
1. **Stable architecture docs** (`docs/v5/01_data_model.md`, `02_service_contracts.md`, `03_happy_path.md`, `04_slice_roadmap.md`): updated only when architecture changes. Live in `/docs/v5/` on main.
2. **DECISIONS.md** (this file): chronological log of every meaningful decision. Updated after each slice ships, plus whenever a meaningful design choice gets made mid-implementation.

Branches inherit from main. Every PR that introduces a meaningful architectural decision adds an entry here.

**Why:** Stable docs explain "what dhri is." DECISIONS.md explains "why dhri became this way." Both are needed: the first for onboarding new contributors, the second for understanding evolution and avoiding past mistakes.

For Sarvam application: DECISIONS.md is one of the strongest portfolio signals because it shows engineering judgment, not just engineering output.

**Rejected alternatives:**
- **Per-branch design.md updated independently:** rejected because it fragments information. The branch-by-branch view is git history; the conceptual view is DECISIONS.md.
- **Architecture Decision Records (ADRs) as separate files:** rejected because at v1 scale, a single file is more navigable than 30 individual ADR files. Revisit if log exceeds ~50 entries.
- **Just use commit messages:** rejected because commit messages are too short to capture rejected alternatives and tradeoffs.

**Tradeoffs accepted:**
- A single file may grow long. Acceptable; ctrl-F works fine until it doesn't.

**Revisit when:**
- DECISIONS.md exceeds ~50 entries (consider splitting or moving to ADRs)

---

## 2026-04-25 — v5 tables live in `v5.*` Postgres schema, not `public.*`

**Status:** Decided
**Slice / Phase:** Slice 1 implementation

**Decision:** All v5 tables live in a dedicated Postgres schema named `v5`. v4 tables continue to live in `public`. v5 services qualify all table names (e.g., `v5.students`, `v5.messages`, `v5.sessions`).

**Why:** v4 already owns `public.sessions` and `public.messages` with different schemas than what v5 needs. Sharing the `public` namespace would either force destructive ALTERs to v4 tables (risking v4 functionality during the build) or create collision risks. A separate schema keeps v5 truly append-only — v4 stays 100% functional throughout the v5 build, supporting the strangler-fig migration discipline.

**Rejected alternatives:**
- **Rename v5 tables to avoid collision** (e.g., `students` → `student_profiles_v5`): rejected because the names would become ugly, inconsistent with the design docs, and add cognitive overhead. Schema prefixes are cleaner than name suffixes.
- **Migrate v4 tables in place to v5 shape:** rejected because it forces v4 downtime and breaks the strangler-fig discipline. v4 must keep working until we explicitly retire it post-slice-8.
- **Drop v4 tables before creating v5 ones:** rejected — losing v4 mid-build means losing the rollback path during weeks 1-3 of testing.

**Tradeoffs accepted:**
- Every v5 query has to write `v5.<table>` instead of just `<table>`. Acceptable; the schema prefix is a documentation feature, making the version explicit in every query.
- The `01_data_model.md` design doc refers to tables without a schema prefix. The doc remains conceptually correct; the schema prefix is an implementation detail. Doc was not updated retroactively to keep the conceptual model clean.

**Revisit when:** v4 is fully retired (post-slice-8 quality pass) and `public.*` v4 tables are dropped. At that point, decide whether to keep v5 in its own schema or move it to public. Inertia argues for keeping it in `v5` — moving it would churn every query in the codebase for purely cosmetic gain.

---

## 2026-04-25 — Slice 1 ships with manual webhook switching, not auto-registration

**Status:** Decided
**Slice / Phase:** Slice 1 implementation

**Decision:** Slice 1 keeps `main.py`'s `startup()` function registering the v4 webhook URL automatically on deploy. The v5 webhook is registered manually via curl after deploy and verification. Auto-registration of v5 will be moved into startup() in slice 2 (or later), once v5 is fully verified as the live route.

**Why:** During slice 1, both routes (v4 and v5) coexist. Auto-registering v5 on deploy would silently take over from v4 the moment Railway redeploys, before we've verified v5 works. Manual webhook switching gives us an explicit, deliberate moment to flip from v4 to v5, with the curl command serving as the implicit "I'm ready" signal.

**Rejected alternatives:**
- **Auto-register v5 in startup() immediately:** rejected for the silent-takeover reason above.
- **Delete v4 route entirely in slice 1:** rejected because we want v4 as a rollback target during the v5 build. Killing v4 forecloses safety.
- **Feature-flag the auto-registration based on env var:** rejected as overkill for an 8-slice build that's converging fast.

**Tradeoffs accepted:**
- Every Railway redeploy after the manual webhook switch will silently revert to v4 (because startup() still registers v4). Mitigation: re-run the v5 setWebhook curl command after every deploy, until slice 2 moves auto-registration to v5.
- Easy to forget. If we forget after a deploy, users will hit v4 instead of v5 silently — no error, just stale behavior. Mitigation: update startup() in slice 2's prompt as a stated task.

**Revisit when:** Slice 2 ships. At that point, modify `main.py` startup() to auto-register `/v5/webhook/{secret}` and stop auto-registering v4. Keep v4's route handler in `main.py` for emergency rollback (but no longer auto-registered).

---

## 2026-04-25 — Six architectural principles enshrined as cross-service invariants

**Status:** Decided
**Slice / Phase:** Slice 2.5 architectural review

**Decision:** Six principles now bind every service in v5. They are documented in detail at the top of `docs/v5/02_service_contracts.md`. Summarized:

1. **No auto-serve after answer.** The bot never auto-serves a question after a student answers/skips. Continuation buttons let the student choose. (Diagnostic test mode is the bounded exception.)
2. **Old keyboards must be closed.** When a new question is served, orchestrator removes the inline keyboard from the previous question via Telegram's editMessageReplyMarkup.
3. **Active session state cleared on boundary.** When a new session starts, `state:tg:{tg_id}` is deleted before being recreated. domain_state from the closed session never leaks.
4. **Webhook idempotency via tg_update_id.** Duplicate Telegram retries are detected and short-circuited. UNIQUE partial index enforces this at the DB level.
5. **UX never breaks on infrastructure failure.** Every failure mode has a graceful fallback: planner failure → safe default; LLM failure → retry once + canned error with [Try again]; DB failure → log+continue (post-delivery) or fail loud (pre-delivery).
6. **Profile cache invalidation mandatory on writes.** ANY service writing to student_notes, student_profile, or student_skill_profile MUST DEL `profile:brief:{student_id}`.

**Why:** During slice 2 verification, a UX bug surfaced (bot auto-serving questions after answers) that violated an unwritten design principle. Auditing for similar bugs revealed 25 issues, of which seven required immediate fixes in slice 2.5 and the rest got distributed across remaining slices or deferred to v1.5. The lesson: implicit principles need to be made explicit, or the AI implementing them will optimize the literal spec at the expense of felt experience.

**Rejected alternatives:**
- **Document principles only in slice prompts:** rejected because principles need to bind the architecture, not be re-litigated per slice. Each slice would re-derive (or worse, contradict) them.
- **Treat each bug as a one-off fix:** rejected because the bugs share a pattern. Naming the underlying principles ensures future slices don't reintroduce equivalent bugs.

**Tradeoffs accepted:**
- Implementation now requires checking principles on every code change. Slight overhead for big payoff in consistency.
- The principles add ~150 lines to service contracts. Worth it; they're the backbone of v5's "feels coherent" property.

**Revisit when:** A new principle emerges that should be added (e.g., from real-user feedback). Treat additions as architecturally significant — log here, don't drop into a service contract section silently.

---

## 2026-04-25 — Slice 2.5 retrofit: seven fixes that should have been in slice 2

**Status:** Decided
**Slice / Phase:** Slice 2.5

**Decision:** Slice 2's base implementation (6-tier retrieval + answer scoring + continuation buttons) is verified working. Before slice 3 begins, slice 2.5 retrofits seven fixes:

1. **Skip button** on question keyboards (Bug 8)
2. **Mid-question doubt detection** with back/skip/different-doubt buttons (Bug 1)
3. **Close old keyboards** when serving new question (Bug 11, Principle 2)
4. **Show my stats button** + `get_session_stats` function (Bug 12)
5. **LLM API failure user-facing fallback** with `[Try again]` button (Bug 18)
6. **DB write failure handling** per Principle 5 (Bug 19)
7. **Webhook idempotency** via tg_update_id (Bug 20)

**Why:** Each of these was caught during slice 2 architectural review. Fixes 1-4 are UX-breaking in slice 2's current form (Skip is missing, doubts mid-question abandon the question, old keyboards persist confusingly, no progress visibility). Fixes 5-7 are reliability foundations that subsequent slices will depend on. Retrofitting now costs less than retrofitting after slice 8.

**Rejected alternatives:**
- **Defer all 7 fixes to v1.5:** rejected because slices 3-8 will build on the broken base. Each slice would inherit the bugs, multiplying rework.
- **Fix only the 4 UX bugs (1, 2, 3, 4) and defer reliability bugs (5, 6, 7):** rejected because reliability bugs have higher cost of late discovery (production incidents) than UX bugs (visible during testing).
- **Spread fixes across slices 3-5:** rejected because it muddles slice scoping. Each slice should have a coherent theme; mixing slice 3's session work with slice 2's UX fixes makes both harder to verify.

**Tradeoffs accepted:**
- Adds ~5-7 hours of work between slices 2 and 3. Acceptable given the cost-of-deferral.
- Two new migrations (011 skipped column, 012 tg_update_id index). Both append-only, idempotent.
- The retrofit prompt is large (~250 lines). Manageable in a single Claude Code session.

**Revisit when:** Verification reveals a fix didn't work or introduced regressions. Otherwise the retrofit is a clean slice and we don't revisit.

---

## 2026-04-25 — 17 of 25 architectural review bugs deferred to subsequent slices or v1.5

**Status:** Decided
**Slice / Phase:** Slice 2.5 architectural review

**Decision:** Of 25 bugs caught during architectural review, 7 are addressed in slice 2.5 (above), 11 are addressed in subsequent slice prompts (3-8), and 7 are deferred to v1.5+.

**Bugs distributed to slices 3-8 (incorporated into the slice prompts in 05_claude_code_prompts.md):**

- **Bug 2 (Returning-after-break resume logic) → Slice 3:** When new session starts and previous session has unanswered work, orchestrator detects via `memory_service.detect_session_resume_candidate(student_id)` and VARC composes "want to resume?" response with [Resume] [Start fresh] [Just chat] buttons.
- **Bug 4 (Real-time pattern detection within session) → Slice 8:** Mentor observer mode detects patterns inline; signals flow into AgentContext for the response that follows.
- **Bug 6 (Onboarding pause option) → Slice 6:** During onboarding FSM, every prompt offers a [Pause for now] button. Tapping sets `student_profile.onboarding_paused_at`. Resumes from same step on next message.
- **Bug 13 (Session-state cleared on boundary) → Slice 3:** Orchestrator's session boundary detection explicitly DELs `state:tg:{tg_id}` before creating new session state.
- **Bug 14 (Profile cache invalidation hardening) → Slice 5:** Already enshrined as Principle 6; slice 5 is when it becomes operationally critical.
- **Bug 15 (Mixed-intent secondary signal) → Slice 4:** Planner returns `intent.secondary_signal` for messages with both action and emotional tone (e.g., "I'm stressed, give me an easy one").
- **Bug 22 (Granular subskill enum from planner) → Slice 4:** Planner prompt has the exact 12-subskill enum; VARC falls back to `inference_basic` if planner returns out-of-enum value.
- **Bug 23 (Profile-derived default difficulty) → Slice 5:** `profile_service.get_default_difficulty(student_id)` derives from `preparation_stage`. Used when planner doesn't supply difficulty.
- **Bug 24 (Post-onboarding session boundary) → Slice 6:** Onboarding session is closed when synthesis is sent. New session begins with next message; primary_agent inherited from button choice.
- **Bug 25 (Empty-state fallback in profile brief) → Slice 5:** Tutor brief renders graceful fallbacks for new students with <5 questions, no recent sessions, etc.
- **Bug 9 (Long-message chunking) → Slice 2.5 implementation note:** Orchestrator step 11 splits responses >3500 chars (passage as one message, question as second).

**Bugs deferred to v1.5+ (logged here, not in any slice prompt):**

- **Bug 3 (deeper) — Topic switch with paused contexts preserved:** Active session domain_state would track "paused contexts" — sets of questions abandoned mid-way. When student wants to resume after switching, the bot remembers. Defer because: shallow acknowledgment (slice 4) is sufficient for v1; deeper resume is nice-to-have.
- **Bug 5 — Loading message timeout fallback:** If response takes >7 sec, edit "🤔 Thinking..." to "🤔 Still working..." Defer because: real latency observation in slices 4-5 will tell us if this matters.
- **Bug 7 — Flow mode for power users:** After 5+ consecutive [Next question] taps, offer to enter "flow mode" where buttons are suppressed. Defer because: this is a v1.5 nice-to-have; v1's button overhead is manageable for first 50 users.
- **Bug 10 — /feedback command:** Formal feedback channel. Defer because: ad-hoc feedback channels (DM the founder, GitHub issues) are sufficient for first 5-10 users.
- **Bug 16 — Free text input during onboarding states:** Allow editing previous answers via free text during onboarding. Defer because: v1 onboarding can require taps; this is a nice-to-have.
- **Bug 17 — Voice/image messages:** Currently rejected with friendly message. Real handling defer because: text-only is sufficient for VARC; voice/image is a different product surface.
- **Bug 21 — Long-context within-session summarization:** When session has >30 turns, mini-summarize early turns. Defer because: typical sessions are <30 turns; this is a scaling concern, not a v1 concern.

**Why these specific deferrals:**

- The deferred bugs are nice-to-haves, scaling concerns, or features that don't break the core experience.
- The slice-distributed bugs (3-8) are blocking for their respective slices but not blocking right now.
- The slice-2.5 bugs are blocking right now (UX-breaking or reliability foundations).

**Revisit when:** v1 ships and we have real-user data. Real-user feedback will likely re-prioritize: some deferred bugs may become urgent (e.g., if many users complain about button fatigue, Bug 7 jumps up); some may stay deferred forever (e.g., Bug 16 might prove unnecessary).

**Tradeoffs accepted:**
- Some deferred bugs may surface as user complaints in early testing. Acceptable; that's signal, not failure.
- Maintaining the deferred-bugs list requires discipline. We commit to revisiting this DECISIONS entry every 4-6 weeks during early v1 operation.

---

## 2026-04-25 — v5.student_question_attempts replaces v4.attempts for v5 traffic

**Status:** Decided
**Slice / Phase:** Slice 2 implementation (logged retroactively)

**Decision:** v5 introduces a fresh `v5.student_question_attempts` table for tracking question serves and answers. The v4 `public.attempts` table is left as-is, holding historical v4 data. v5 services do NOT write to `public.attempts`.

This contradicts the original design doc plan (`01_data_model.md` originally said "Add session_id column to attempts"). The retroactive decision: don't touch v4 tables.

**Why:** Modifying v4's schema during the v5 build risks v4 functionality (which we keep alive for rollback). A fresh v5 table:
- Is truly append-only (no risk to v4)
- Has fields v5 needs that v4 doesn't (skipped, is_diagnostic, fallback_tier)
- Doesn't require backfilling historical data into a new column shape
- After v4 is retired, v4.attempts can be archived or analyzed for historical signal; v5 owns the present

**Rejected alternatives:**
- **Add columns to public.attempts in v4:** rejected for v4-functionality risk and the strangler-fig discipline.
- **Migrate public.attempts to v5.student_question_attempts:** rejected because backfilling fields v4 doesn't have (skipped, is_diagnostic, fallback_tier) creates fake data.
- **Use one unified table for v4 + v5:** rejected because v4 and v5 have different semantics (v4 has trap analysis, v5 has tier info); merging adds complexity for no benefit.

**Tradeoffs accepted:**
- Two parallel attempts tables for the duration of v4's life. Both readable; both searchable for analytics. After v4 retirement, the historical v4.attempts can be archived or kept read-only.
- Slight schema duplication. Acceptable.

**Revisit when:** v4 is fully retired and we decide to consolidate.

---

## 2026-04-25 — Slice 2 verified: 6-tier retrieval ladder + answer scoring

**Status:** Decided
**Slice / Phase:** Slice 2

**Decision:** VARC's question retrieval is a 6-tier fallback ladder, not a single retrieval call. Each tier broadens what counts as an acceptable question:

1. Exact subskill + difficulty + unseen + difficulty-balanced (preferred)
2. Exact subskill + unseen (any difficulty)
3. Adjacent subskill + unseen (e.g., inference_basic ↔ inference_advanced)
4. Any subskill + unseen
5. Stale-seen (subskill match, served > 14 days ago) with acknowledgement string
6. Oldest-seen (any subskill, oldest served_at) with acknowledgement string

Tiers 5 and 6 prepend a transparency string ("We've seen this passage before — let's try it with fresh eyes." / "I'm running low on new questions in this category — let me serve one we did a while back to see how your thinking has changed."). Students always get a question; the retrieval ladder never returns null.

Stale button taps (tapping a letter on an old question whose newer question is now active) route to the most recent unanswered attempt — not to the question whose buttons were tapped. This was important until slice 2.5 added keyboard closing; with slice 2.5, old keyboards are removed entirely, making stale taps physically impossible. The fallback logic remains as defense in depth.

**Why:** Question banks have finite content. A student who answers many inference questions will eventually exhaust the unseen pool. The ladder ensures the bot always has *something* to serve, with transparency about why a familiar question is appearing.

**Rejected alternatives:**
- **Single retrieval call returning null on exhaustion:** rejected because students would hit dead ends after 50-100 questions.
- **Generate questions on-the-fly:** rejected because LLM-generated questions don't match real CAT difficulty/style; we'd lose the question-bank quality.
- **Tier acknowledgements as variable LLM-generated text:** rejected for consistency; the deterministic strings are clearer and avoid LLM cost.

**Tradeoffs accepted:**
- Tier 5/6 acknowledgements may feel apologetic. Acceptable; honesty > pretending we have infinite content.
- Six tiers is more code than a single retrieval. Each tier is a thin SQL query; total complexity is ~150 lines. Maintainable.

**Revisit when:** Question bank exceeds ~500 questions per subskill (currently ~50). At that point, tiers 5/6 will rarely fire and we can simplify.

---

## 2026-04-25 — Slice 2 verified: separate v5.student_question_attempts table

**Status:** Decided  
**Slice / Phase:** Slice 2

(Already documented in the slice 2.5 DECISIONS_additions.md entry "v5.student_question_attempts replaces v4.attempts for v5 traffic" — see that entry. Keeping this header here as a chronological marker.)

---

## 2026-04-25 — Slice 2 verified: webhook auto-registration on deploy

**Status:** Decided  
**Slice / Phase:** Slice 2

**Decision:** On Railway deploy, the v5 service auto-registers its webhook with Telegram (POST to `/setWebhook`) at startup, pointing to `/v5/webhook/{secret}`. This replaces v4's manual webhook setup during slice 2's "switching webhook to v5" step.

**Why:** Removes a manual step from every deploy. Without this, every Railway redeploy would require us to manually re-register the webhook, which is error-prone (forgotten registrations cause silent traffic loss).

**Rejected alternatives:**
- **Manual setWebhook via curl after each deploy:** rejected for fragility.
- **Single one-time setup script:** rejected because Railway's container model means startup happens fresh each deploy; doing it inline at startup is more reliable.

**Tradeoffs accepted:**
- Adds ~200ms to startup. Acceptable.
- Multiple instances on the same secret would race to setWebhook; Telegram handles this idempotently.

**Revisit when:** We move to a multi-region or load-balanced deploy. At that point we'd need to handle webhook registration more carefully.

---

## 2026-04-25 — Skip is "seen" but not "answered"

**Status:** Decided
**Slice / Phase:** Slice 2.5

**Decision:** When a student taps [Skip / I don't know] on a question:
- The attempt row gets `skipped = true`, `answered_at = now`, `is_correct = null`, `student_answer = null`, `explanation_shown = true`.
- The student SEES the explanation (the same explanation they'd see for a wrong answer, minus the "you picked X" line).
- The retrieval ladder considers this question "seen" — it won't be re-served via tier 1-4 (unseen tiers); only via tier 5 (stale-seen) or tier 6 (oldest-seen).
- The skip does NOT count as a wrong answer for accuracy stats. It's its own category.

**Why:** Skip respects the student's autonomy ("I don't want to guess; just teach me") while still giving them the educational value of the explanation. Treating skips as wrong answers would penalize honesty; treating them as "didn't see" would re-serve the same question, which feels punitive.

**Rejected alternatives:**
- **Skip = wrong answer:** rejected; penalizes honest "I don't know" responses, encouraging guessing.
- **Skip = unseen (re-serve later):** rejected; punitive after the student already saw the explanation.
- **Skip = no explanation shown:** rejected; misses the teaching opportunity. The student wants to learn from this question; they just don't want to guess.

**Tradeoffs accepted:**
- Stats need to track skip count separately (not in correct/wrong). Acceptable — `get_session_stats` reports it explicitly.
- Some students might over-use Skip to avoid hard questions. Acceptable for v1; observable via session stats; intervention (mentor agent nudge: "noticed you've skipped 5 of 10 — want easier ones?") can land in slice 8 if we see this pattern.

**Revisit when:** Real-user data shows skip-rate > 30% or pattern of avoidance. Currently we have no data; v1 ships and we observe.

---

## 2026-04-25 — Continuation button sets are deterministic, not LLM-generated

**Status:** Decided
**Slice / Phase:** Slice 2.5

**Decision:** The buttons that appear after agent responses (continuation prompts, mentor strategic responses, mid-question doubt acks) are selected by orchestrator code, not by the LLM. Different button sets fire based on `response_type`, `intent.action`, and `intent.emotional_tone`.

Examples:
- After answer/skip explanation → `[Next question] [Different subskill] [I have a doubt] [I'm done]` (and `[Show my stats]` in slice 2.5+)
- After mentor anxiety/frustration response → `[Try one easy one] [Different subskill] [Talk it out more] [Take a break]`
- After mentor strategy response → `[Practice my weak areas] [Show my stats] [Ask another question] [I'm done]`
- Mid-question doubt ack → `[Back to the question] [Skip this question] [Different question]`
- Returning-after-break prompt → `[Resume that question] [Start fresh] [Just chat]`

**Why:** LLM-generated buttons would be inconsistent (different wording every turn), unpredictable (occasionally missing key options), and hard to validate. Deterministic selection ensures every flow has the right options every time.

The agents' LLM is told NOT to include questions, options, or buttons in their output — the system appends them.

**Rejected alternatives:**
- **LLM picks the buttons:** rejected for consistency reasons above.
- **Single universal continuation button set:** rejected because context matters — the right buttons differ by emotional state, by whether a question is open, by whether the student just took a break.
- **No buttons; pure free text:** rejected because button taps are faster on mobile and reduce friction.

**Tradeoffs accepted:**
- Button-set logic is in orchestrator code (~50 lines). Maintainable.
- Adding new continuation contexts requires adding new button sets in code. Acceptable; rare.

**Revisit when:** Real-user data shows consistent dead-ends (a context where no offered button matches what users want). Add the missing option.

---

<!-- 
TEMPLATE FOR NEW ENTRIES — copy and fill:

## YYYY-MM-DD — Title

**Status:** Decided | Reconsidered | Reversed  
**Slice / Phase:** <which slice if applicable>

**Decision:** What we're doing.

**Why:** The reasoning.

**Rejected alternatives:**
- **Option X:** why we rejected it
- **Option Y:** why we rejected it

**Tradeoffs accepted:** What we're giving up.

**Revisit when:** Trigger condition.

---
-->

