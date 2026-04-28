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

# DECISIONS.md — Additions for Slices 2, 2.5, 3

These entries should be appended to your existing `DECISIONS.md` on the v5 branch (then merged to main after slice 3 verification completes). They capture the architectural decisions that emerged from slice 2 verification, slice 2.5 retrofit, slice 3 implementation, and slice 3 verification fixes.

The entries are in chronological order of when each decision was made. Append all of them at the end of the existing DECISIONS.md, before any HTML comment template.

---

## Slice 2 entries (logged retroactively after slice 3 verification surfaced the omission)

These weren't recorded when slice 2 originally shipped; documenting now because they're load-bearing decisions for downstream slices.

---

### 2026-04-25 — Slice 2 verified: 6-tier retrieval ladder + answer scoring

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

### 2026-04-25 — Slice 2 verified: separate v5.student_question_attempts table

**Status:** Decided  
**Slice / Phase:** Slice 2

**Decision:** v5 introduces a fresh `v5.student_question_attempts` table for tracking question serves and answers. The v4 `public.attempts` table is left as-is, holding historical v4 data. v5 services do NOT write to `public.attempts`.

This contradicts an earlier draft of the design doc that said "Add session_id column to attempts." The retroactive decision: don't touch v4 tables.

**Why:** Modifying v4's schema during the v5 build risks v4 functionality (which we keep alive for rollback). A fresh v5 table:
- Is truly append-only (no risk to v4)
- Has fields v5 needs that v4 doesn't (skipped, is_diagnostic, fallback_tier, session_id)
- Doesn't require backfilling historical data into a new column shape
- After v4 is retired, v4.attempts can be archived or analyzed for historical signal; v5 owns the present

**Rejected alternatives:**
- **Add columns to public.attempts in v4:** rejected for v4-functionality risk and the strangler-fig discipline.
- **Migrate public.attempts to v5.student_question_attempts:** rejected because backfilling fields v4 doesn't have creates fake data.
- **Use one unified table for v4 + v5:** rejected because v4 and v5 have different semantics (v4 has trap analysis, v5 has tier info); merging adds complexity for no benefit.

**Tradeoffs accepted:**
- Two parallel attempts tables for the duration of v4's life. Both readable; both searchable for analytics. After v4 retirement, the historical v4.attempts can be archived or kept read-only.
- Slight schema duplication. Acceptable.

**Revisit when:** v4 is fully retired and we decide to consolidate.

---

### 2026-04-25 — Slice 2 verified: webhook auto-registration on deploy

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

## Slice 2.5 entries

These captured the architectural review that surfaced 25 issues, the seven-fix retrofit, and the principles that came out of the review.

---

### 2026-04-25 — Six architectural principles enshrined as cross-service invariants

**Status:** Decided
**Slice / Phase:** Slice 2.5 architectural review

**Decision:** Six principles now bind every service in v5. They are documented in detail at the top of `docs/v5/02_service_contracts.md`. Summarized:

1. **No auto-serve after answer.** The bot never auto-serves a question after a student answers/skips. Continuation buttons let the student choose. (Diagnostic test mode is the bounded exception.)
2. **Old keyboards must be closed.** When a new question is served, orchestrator removes the inline keyboard from the previous question via Telegram's editMessageReplyMarkup.
3. **Active session state cleared on boundary.** When a session closes, `state:tg:{tg_id}` is deleted. Strengthened in slice 3: any code path closing a Postgres session MUST clear Redis state in the same operation.
4. **Webhook idempotency via tg_update_id.** Duplicate Telegram retries are detected and short-circuited. UNIQUE partial index enforces this at the DB level.
5. **UX never breaks on infrastructure failure.** Every failure mode has a graceful fallback: planner failure → safe default; LLM failure → retry once + canned error with [Try again]; DB failure → log+continue (post-delivery) or fail loud (pre-delivery).
6. **Profile cache invalidation mandatory on writes.** ANY service writing to student_notes, student_profile, or student_skill_profile MUST DEL `profile:brief:{student_id}`.

**Why:** During slice 2 verification, a UX bug surfaced (bot auto-serving questions after answers) that violated an unwritten design principle. Auditing for similar bugs revealed 25 issues, of which seven required immediate fixes in slice 2.5 and the rest got distributed across remaining slices or deferred to v1.5. The lesson: implicit principles need to be made explicit, or the AI implementing them will optimize the literal spec at the expense of felt experience.

**Rejected alternatives:**
- **Document principles only in slice prompts:** rejected because principles need to bind the architecture, not be re-litigated per slice.
- **Treat each bug as a one-off fix:** rejected because the bugs share a pattern. Naming the underlying principles ensures future slices don't reintroduce equivalent bugs.

**Tradeoffs accepted:**
- Implementation now requires checking principles on every code change. Slight overhead for big payoff in consistency.
- The principles add ~150 lines to service contracts. Worth it; they're the backbone of v5's "feels coherent" property.

**Revisit when:** A new principle emerges that should be added (e.g., from real-user feedback). Treat additions as architecturally significant — log here, don't drop into a service contract section silently.

---

### 2026-04-25 — Slice 2.5 retrofit: seven fixes that should have been in slice 2

**Status:** Decided
**Slice / Phase:** Slice 2.5

**Decision:** Slice 2's base implementation (6-tier retrieval + answer scoring + continuation buttons) was verified working. Before slice 3 began, slice 2.5 retrofitted seven fixes:

1. **Skip button** on question keyboards (Bug 8)
2. **Mid-question doubt detection** with back/skip/different-doubt buttons (Bug 1)
3. **Close old keyboards** when serving new question (Bug 11, Principle 2)
4. **Show my stats button** + `get_session_stats` function (Bug 12)
5. **LLM API failure user-facing fallback** with `[Try again]` button (Bug 18)
6. **DB write failure handling** per Principle 5 (Bug 19)
7. **Webhook idempotency** via tg_update_id (Bug 20)

**Why:** Each of these was caught during slice 2 architectural review. Fixes 1-4 are UX-breaking in slice 2's current form. Fixes 5-7 are reliability foundations that subsequent slices depend on. Retrofitting now cost less than retrofitting after slice 8.

**Rejected alternatives:**
- **Defer all 7 fixes to v1.5:** rejected because slices 3-8 will build on the broken base.
- **Fix only the 4 UX bugs:** rejected because reliability bugs have higher cost of late discovery.
- **Spread fixes across slices 3-5:** rejected because it muddles slice scoping.

**Tradeoffs accepted:**
- Adds ~5-7 hours of work between slices 2 and 3. Acceptable given the cost-of-deferral.
- Two new migrations (011 skipped column, 012 tg_update_id index). Both append-only, idempotent.

**Revisit when:** Verification reveals a fix didn't work or introduced regressions.

---

### 2026-04-25 — 17 of 25 architectural review bugs deferred to subsequent slices or v1.5

**Status:** Decided
**Slice / Phase:** Slice 2.5 architectural review

**Decision:** Of 25 bugs caught during architectural review, 7 were addressed in slice 2.5, 11 were addressed in subsequent slice prompts (3-8), and 7 were deferred to v1.5+.

**Bugs distributed to slices 3-8:**

- Bug 2 (Returning-after-break resume logic) → Slice 3
- Bug 4 (Real-time pattern detection within session) → Slice 8
- Bug 6 (Onboarding pause option) → Slice 6
- Bug 13 (Session-state cleared on boundary) → Slice 3 (strengthened in slice 3 verification fixes)
- Bug 14 (Profile cache invalidation hardening) → Slice 5
- Bug 15 (Mixed-intent secondary signal) → Slice 4
- Bug 22 (Granular subskill enum from planner) → Slice 4
- Bug 23 (Profile-derived default difficulty) → Slice 5
- Bug 24 (Post-onboarding session boundary) → Slice 6
- Bug 25 (Empty-state fallback in profile brief) → Slice 5
- Bug 9 (Long-message chunking) → Slice 2.5 implementation note

**Bugs deferred to v1.5+:**

- Bug 3 (deeper) — Topic switch with paused contexts preserved
- Bug 5 — Loading message timeout fallback
- Bug 7 — Flow mode for power users
- Bug 10 — /feedback command
- Bug 16 — Free text input during onboarding states
- Bug 17 — Voice/image messages
- Bug 21 — Long-context within-session summarization

**Why these specific deferrals:** The deferred bugs are nice-to-haves, scaling concerns, or features that don't break the core experience. The slice-distributed bugs are blocking for their respective slices but not blocking right now. The slice-2.5 bugs are blocking right now (UX-breaking or reliability foundations).

**Revisit when:** v1 ships and we have real-user data. Real-user feedback will likely re-prioritize.

---

### 2026-04-25 — Skip is "seen" but not "answered"

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
- **Skip = no explanation shown:** rejected; misses the teaching opportunity.

**Tradeoffs accepted:**
- Stats need to track skip count separately. Acceptable — `get_session_stats` reports it explicitly.
- Some students might over-use Skip. Observable via session stats; intervention can land in slice 8 if pattern emerges.

**Revisit when:** Real-user data shows skip-rate > 30% or pattern of avoidance.

---

### 2026-04-25 — Continuation button sets are deterministic, not LLM-generated

**Status:** Decided
**Slice / Phase:** Slice 2.5

**Decision:** The buttons that appear after agent responses (continuation prompts, mentor strategic responses, mid-question doubt acks) are selected by orchestrator code, not by the LLM. Different button sets fire based on `response_type`, `intent.action`, and `intent.emotional_tone`.

Examples:
- After answer/skip explanation → `[Next question]` `[Different subskill]` `[Show my stats]` `[I have a doubt]` `[I'm done]`
- Mid-question doubt ack → `[Back to the question]` `[Skip this question]` `[I have a different question]`
- Returning-after-break prompt → `[Resume that question]` `[Start fresh]` `[Just chat first]`
- After mentor anxiety/frustration response → `[Try one easy one]` `[Different subskill]` `[Talk it out more]` `[Take a break]`

**Why:** LLM-generated buttons would be inconsistent, unpredictable, and hard to validate. Deterministic selection ensures every flow has the right options every time. The agents' LLM is told NOT to include questions, options, or buttons in their output — the system appends them.

**Rejected alternatives:**
- **LLM picks the buttons:** rejected for consistency reasons above.
- **Single universal continuation button set:** rejected because context matters.
- **No buttons; pure free text:** rejected because button taps are faster on mobile and reduce friction.

**Tradeoffs accepted:**
- Button-set logic is in orchestrator code (~50 lines). Maintainable.
- Adding new continuation contexts requires adding new button sets in code. Acceptable; rare.

**Revisit when:** Real-user data shows consistent dead-ends (a context where no offered button matches what users want). Add the missing option.

---

## Slice 3 entries

These cover the slice 3 implementation (working memory + sessions, returning-after-break, LLM-generated explanations) and the verification fixes that surfaced during testing.

---

### 2026-04-26 — Slice 3 verified: 30-minute session boundary

**Status:** Decided  
**Slice / Phase:** Slice 3

**Decision:** A session boundary fires when a new message arrives more than 30 minutes after the previous message in the same session. The closed session is marked `ended_at = now()`, `end_reason = 'inactivity_timeout'`. A new session row is created and `state:tg:{tg_id}` is DELed in Redis before the new state is set (Principle 3 enforcement).

Earlier internal drafts considered 2-hour and 1-hour windows. 30 minutes was chosen because:
- CAT prep sessions are short and intense (typically 20-45 min)
- 30 min is short enough that "I came back the next morning" reliably triggers the returning-after-break flow
- Long enough that bathroom breaks / phone interruptions don't constantly fragment sessions

**Why:** Session boundaries are how the bot scopes "what we worked on today" vs "what we did last time." Wrong threshold either fragments coherent sessions (too short) or merges distinct study sessions (too long). 30 minutes is empirically a good fit for CAT prep behavior.

**Rejected alternatives:**
- **Explicit user-initiated session end only:** rejected because users rarely explicitly end sessions.
- **Fixed clock boundaries (e.g., midnight):** rejected because students study across midnight.
- **Adaptive (per-student learned threshold):** rejected as v1.5+ optimization.

**Tradeoffs accepted:**
- A user who steps away for 31 minutes mid-session will have their session split. Acceptable; the resume prompt softens this.
- Cleanup cron runs every 10 min, so sessions actually close 30-40 min after last activity. Acceptable timing.

**Revisit when:** Real-user data shows fragmenting (lots of short sessions) or merging (sessions spanning days).

---

### 2026-04-26 — Slice 3 verified: queries are session-scoped, not student-scoped

**Status:** Decided  
**Slice / Phase:** Slice 3

**Decision:** All queries that read "current state" — open unanswered attempts, session stats, recent turns for prompt context — are filtered by `session_id` AND `student_id`, not just `student_id`.

This means: the orchestrator's `_fetch_last_unanswered_attempt(student_id, session_id)` returns nothing if the unanswered attempt is in a closed (different) session. The student starting a new session has no "open question" from the old session bleeding into the new one.

Returning-after-break is the explicit exception: `detect_session_resume_candidate(student_id)` looks across sessions to find unfinished work in the most recently closed session. But it surfaces this as a deliberate prompt ("want to resume?"), not as silent state.

**Why:** Bug 13 (session-state leak) was a category of bugs — domain_state, last_question_attempt_id, recent_turns — all liable to cross session boundaries by accident. Enforcing session-scoping at the query level is the structural fix. State physically cannot leak if the query won't return it.

**Rejected alternatives:**
- **Clear all per-student state at boundary:** rejected because some state (working memory cache, profile brief cache) intentionally persists across sessions.
- **Document the rule in code comments only:** rejected because future code changes would re-introduce the bug. Query-level enforcement is structural.

**Tradeoffs accepted:**
- Slice 2/2.5 attempts (with `session_id IS NULL`) are invisible to session-scoped stats queries. Acceptable; they're test data.

**Revisit when:** A new query needs cross-session reads. The pattern is: explicit cross-session functions (like `detect_session_resume_candidate`) are clearly named; default scoping is per-session.

---

### 2026-04-26 — Slice 3 verified: old-session keyboard close deferred to new question serve

**Status:** Decided  
**Slice / Phase:** Slice 3

**Decision:** When a session boundary fires (cleanup or new-message-after-30-min), the OLD session's last question's inline keyboard is NOT removed at boundary time. It's removed when the NEW session's first question is served (orchestrator's Step 11.5 picks up `prior_question_message_id` from `resolve_session`'s third return value).

Trade-off: between the boundary and the next question serve, the OLD question's keyboard remains tappable in chat history. If the user taps it during this window, the answer routes to the new session and finds no matching open attempt → falls through to a default action.

**Why:** Closing keyboards requires a Telegram API call (`editMessageReplyMarkup`). We don't want to add latency or complexity to the cleanup cron path. Doing it lazily costs nothing and is sufficient for the common case.

**Rejected alternatives:**
- **Close keyboard at boundary:** would require cleanup cron to call Telegram API. Rejected.
- **Close keyboard when resume prompt is shown:** premature; user hasn't decided yet.

**Tradeoffs accepted:**
- Stale button tap during the resume-prompt window is theoretically possible, practically rare.

**Revisit when:** Users complain about stale-button confusion in real testing. Likely never; this edge case is too narrow.

---

### 2026-04-26 — Slice 3 verified: LLM call logging is best-effort

**Status:** Decided  
**Slice / Phase:** Slice 3

**Decision:** `record_llm_call` (writes to `v5.llm_calls`) catches all exceptions internally. A logging failure NEVER sinks the user-facing response.

**Why:** Per Principle 5, UX never breaks on infrastructure failure. An observability table going down is exactly the kind of dependency that should fail silently for the user.

**Rejected alternatives:**
- **Make logging blocking:** rejected. Observability is a tool for us, not a contract with the user.
- **Async logging via queue:** overkill for v1.

**Tradeoffs accepted:**
- We may have gaps in `v5.llm_calls` if Postgres has transient issues. Acceptable for cost analytics.

**Revisit when:** We need accurate per-user cost attribution for billing or analytics SLAs.

---

### 2026-04-26 — Slice 3 verified: shared LLM and observability infrastructure

**Status:** Decided  
**Slice / Phase:** Slice 3

**Decision:** Three shared modules introduced in slice 3 are now the canonical interface for any service making LLM calls or needing keyboard close coordination:

1. **`shared.llm.openrouter.chat_with_metadata(system, user, model) → LLMCallResult`** — replaces older content-only wrappers (still around for `v4_legacy`). Returns content + token counts + cost + latency. Has retry-once-on-failure built in.

2. **`shared.observability.llm_log.record_llm_call(...)`** — best-effort logger. Required after every `chat_with_metadata` call.

3. **`shared.telegram.utils.edit_telegram_keyboard(...)`** — keyboard close helper. Used by bus when `response.requires_keyboard_close=True`. Catches all errors; never raises.

**Why:** These cross-cut every service. Without shared modules, each service would re-implement (or worse, miss) cost tracking, retry logic, and keyboard-close handling. Making them shared and required means future slices add features cleanly.

**Rejected alternatives:**
- **Per-service implementations:** rejected for duplication and inconsistency.
- **Wrapper class hierarchy:** rejected as over-engineered for v1.

**Tradeoffs accepted:**
- Backward-compat wrappers still exist for v4_legacy. Some duplication; acceptable until v4 is fully retired.
- The "every LLM call must log" rule is enforced by convention, not code. Caught by reviewing slice prompts.

**Revisit when:** v4_legacy is fully retired; remove the older wrappers.

---

### 2026-04-26 — Slice 3 verification: markdown rendering switched to HTML parse mode

**Status:** Decided  
**Slice / Phase:** Slice 3 verification (mid-cycle fix)

**Decision:** All orchestrator-composed responses now use Telegram's HTML parse mode instead of legacy Markdown. LLM-generated content goes through `html.escape()` before delivery. Bus-side fallback retries without parse_mode if Telegram returns a parse error.

**Why:** The original implementation used legacy Markdown. Subskill names contain underscores (`inference_basic`); the legacy Markdown parser interprets unmatched `_` as italic markers and rejects the message. The bus's fallback delivered plain text — users saw raw asterisks instead of bold headers.

HTML parse mode requires escaping only `<`, `>`, `&` — not present in normal English or subskill names.

**Rejected alternatives:**
- **MarkdownV2:** rejected; requires escaping ~14 special characters that appear in normal sentences.
- **Escape underscores in legacy Markdown:** rejected; brittle, every subskill render needs its own escape.
- **No parse mode at all:** rejected; we lose visual hierarchy in stats and templated responses.

**Tradeoffs accepted:**
- LLM-generated content cannot use formatting for emphasis. Word choice carries the weight.
- HTML escape pass adds ~1ms per turn. Negligible.
- Adding new orchestrator templates requires HTML tags, not markdown.

**Revisit when:** A real-user complaint shows raw HTML tags rendering literally.

---

### 2026-04-26 — Slice 3 verification: message_count + Redis cleanup fixes

**Status:** Decided  
**Slice / Phase:** Slice 3 (verification phase)

**Decision:** Two bugs surfaced during Phase B testing of slice 3:

1. **`v5.sessions.message_count` was not being incremented on each turn.** The orchestrator updated `last_activity_at` correctly but the same UPDATE did not bump `message_count`. Fixed by combining the two into a single UPDATE that runs on every continuation turn. Convention: counts turns (one user + one assistant = +1).

2. **`cleanup_inactive_sessions` closed sessions in Postgres without clearing Redis state.** Fixed by having `close_session` (called by cleanup) do Postgres update + Redis DEL as a single semantic operation. Every caller of `close_session` now inherits the cleanup automatically. Also added a defensive staleness check in `resolve_session` for layered protection.

The pattern: Postgres is the source of truth; Redis is a derived cache; writes to Postgres MUST propagate to Redis.

This is now an invariant in service contracts: **any code path closing a Postgres session MUST clear the corresponding Redis state for that student's tg_id, in the same operation.**

**Why:** Both bugs were latent — neither caused user-facing UX issues during normal flow. But `message_count = 0` for active sessions breaks analytics queries, and stale Redis state would have caused subtle behavior 2-3 weeks in.

**Rejected alternatives:**
- **Defensive staleness check ONLY in `resolve_session`:** insufficient — wouldn't handle a future code path that closes a session without going through resolve_session on next turn.
- **Move `message_count` to Redis:** rejected because the column needs to persist in closed-session analytics anyway.

**Tradeoffs accepted:**
- `cleanup_inactive_sessions` now does an extra JOIN to fetch `tg_id`. Negligible cost.
- `message_count` counts turns, not individual messages. If we later want user-only counts, that's a different column.
- `resolve_session` does an extra Postgres PK lookup on every turn (the staleness check). ~10ms latency. Acceptable.

**Revisit when:** A new code path closes sessions outside of `cleanup_inactive_sessions` — the path inherits Redis cleanup automatically through `close_session`.

---

### 2026-04-26 — Schema drift caught by audit: fallback_tier added mid-cycle (migration 015)

**Status:** Decided  
**Slice / Phase:** Slice 3 verification (mid-cycle fix)

**Decision:** During slice 3 verification, a schema audit revealed that `v5.student_question_attempts.fallback_tier` was specified in the data model but never created by slice 2's migration. The retrieval ladder was already computing the tier and surfacing it in `messages.metadata`, but not landing it on the attempts row.

Fixed via `migrations/v5/015_add_fallback_tier_to_attempts.sql` — adds the column, threads `fallback_tier` through `_record_attempt` and its two call sites in VARC. Existing rows stay NULL (no backfill).

This incident motivated the **Schema Drift Discipline** documented in `04_slice_roadmap.md`:
1. Each slice's roadmap section enumerates migrations + columns explicitly
2. CREATE TABLE migrations include ALL columns from the data model — even future-slice columns
3. `python -m scripts.check_schema_drift` runs after every slice's migrations apply
4. Migration numbering is sequential and never reused
5. Mid-cycle migrations get the next available number

The drift check helper (`scripts/check_schema_drift.py`) was added in this fix's commit.

**Why:** Schema drift is the most common silent bug source. Slice 2's prompt said "create the schema" without saying "create THIS schema with THESE columns." Claude Code, working from a slice prompt, had no way to know the data model had columns the prompt didn't explicitly call out.

The fix: be explicit. Every slice prompt now enumerates migrations file-by-file with column lists. Every slice ends with a drift-check verification step.

**Rejected alternatives:**
- **Wholesale "redo all migrations":** rejected — invites destructive ALTERs and disruption to migration tracker.
- **Fix only the immediate gap and accept future drift:** rejected — drift accumulates.
- **Switch to a migration auto-generator:** considered but rejected for v1; manual migrations are fine at this scale.

**Tradeoffs accepted:**
- Three other columns (onboarding_paused_at, diagnostic_question_count, is_diagnostic) remain unaddressed at end of slice 3. They're scheduled to land with migration 016 in slice 6. The drift check correctly reports these as drift; exit code 1 is expected at end-of-slice-3.
- Each slice now spends a few minutes on drift verification. Cheap; catches recurrence.

**Revisit when:** Slice 6 ships migration 016. Drift check should output ALL `[OK]` after slice 6.

---

## Process Notes

These document how the docs themselves are maintained, so future slices stay aligned.

---

### 2026-04-26 — Documentation versioning: design docs update with slices

**Status:** Decided  
**Slice / Phase:** End of slice 3

**Decision:** The five design docs (`01_data_model.md`, `02_service_contracts.md`, `03_happy_path.md`, `04_slice_roadmap.md`, `05_claude_code_prompts.md`) plus `DECISIONS.md` are updated in lockstep with slice implementations.

When a slice introduces architectural changes:
1. The slice prompt is updated FIRST (in `05_claude_code_prompts.md`) so Claude Code has the right brief
2. After slice ships, the data model and service contracts are updated to match what was actually built
3. The slice roadmap's migrations subsection and "what's real" / "what's stubbed" sections are updated
4. DECISIONS.md gets entries for any architectural decisions made during the slice
5. The happy path doc is updated when major flow changes happen (less frequent than the others)

When a slice has verification fixes (mid-cycle bug fixes that fix the slice's deliverables):
- The fix is NOT in a separate slice — it's part of the slice's delivery
- DECISIONS.md gets a "Slice N verification: ..." entry for the fix
- Service contracts and/or data model are updated if the fix changes the contract or schema
- Slice roadmap's "What was actually shipped" section gets the fix added

**Why:** Without this discipline, docs drift from reality. Slice 6 looking at `01_data_model.md` from slice 3's era would have stale schema info. The cost of maintaining docs is small if done immediately; large if deferred.

**Rejected alternatives:**
- **Update all docs at v1 ship:** rejected — too much accumulated drift; impossible to remember 8 slices' worth of decisions.
- **Auto-generate docs from code:** rejected — design docs capture intent and tradeoffs that code can't.
- **No design docs after slice 1:** rejected — without docs, every Claude Code session starts from scratch on context.

**Tradeoffs accepted:**
- Each slice adds ~30-60 minutes of doc maintenance. Acceptable given how much time it saves on the next slice.
- Docs lag code by hours-to-a-day during active development. Acceptable; pre-merge alignment is the bar, not real-time consistency.

**Revisit when:** Doc maintenance becomes burdensome (e.g., every slice taking >2 hours of doc work). At that point, consider tooling.

---

# DECISIONS.md — Additions for Slices 2, 2.5, 3

These entries should be appended to your existing `DECISIONS.md` on the v5 branch (then merged to main after slice 3 verification completes). They capture the architectural decisions that emerged from slice 2 verification, slice 2.5 retrofit, slice 3 implementation, and slice 3 verification fixes.

The entries are in chronological order of when each decision was made. Append all of them at the end of the existing DECISIONS.md, before any HTML comment template.

---

## Slice 2 entries (logged retroactively after slice 3 verification surfaced the omission)

These weren't recorded when slice 2 originally shipped; documenting now because they're load-bearing decisions for downstream slices.

---

### 2026-04-25 — Slice 2 verified: 6-tier retrieval ladder + answer scoring

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

### 2026-04-25 — Slice 2 verified: separate v5.student_question_attempts table

**Status:** Decided  
**Slice / Phase:** Slice 2

**Decision:** v5 introduces a fresh `v5.student_question_attempts` table for tracking question serves and answers. The v4 `public.attempts` table is left as-is, holding historical v4 data. v5 services do NOT write to `public.attempts`.

This contradicts an earlier draft of the design doc that said "Add session_id column to attempts." The retroactive decision: don't touch v4 tables.

**Why:** Modifying v4's schema during the v5 build risks v4 functionality (which we keep alive for rollback). A fresh v5 table:
- Is truly append-only (no risk to v4)
- Has fields v5 needs that v4 doesn't (skipped, is_diagnostic, fallback_tier, session_id)
- Doesn't require backfilling historical data into a new column shape
- After v4 is retired, v4.attempts can be archived or analyzed for historical signal; v5 owns the present

**Rejected alternatives:**
- **Add columns to public.attempts in v4:** rejected for v4-functionality risk and the strangler-fig discipline.
- **Migrate public.attempts to v5.student_question_attempts:** rejected because backfilling fields v4 doesn't have creates fake data.
- **Use one unified table for v4 + v5:** rejected because v4 and v5 have different semantics (v4 has trap analysis, v5 has tier info); merging adds complexity for no benefit.

**Tradeoffs accepted:**
- Two parallel attempts tables for the duration of v4's life. Both readable; both searchable for analytics. After v4 retirement, the historical v4.attempts can be archived or kept read-only.
- Slight schema duplication. Acceptable.

**Revisit when:** v4 is fully retired and we decide to consolidate.

---

### 2026-04-25 — Slice 2 verified: webhook auto-registration on deploy

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

## Slice 2.5 entries

These captured the architectural review that surfaced 25 issues, the seven-fix retrofit, and the principles that came out of the review.

---

### 2026-04-25 — Six architectural principles enshrined as cross-service invariants

**Status:** Decided
**Slice / Phase:** Slice 2.5 architectural review

**Decision:** Six principles now bind every service in v5. They are documented in detail at the top of `docs/v5/02_service_contracts.md`. Summarized:

1. **No auto-serve after answer.** The bot never auto-serves a question after a student answers/skips. Continuation buttons let the student choose. (Diagnostic test mode is the bounded exception.)
2. **Old keyboards must be closed.** When a new question is served, orchestrator removes the inline keyboard from the previous question via Telegram's editMessageReplyMarkup.
3. **Active session state cleared on boundary.** When a session closes, `state:tg:{tg_id}` is deleted. Strengthened in slice 3: any code path closing a Postgres session MUST clear Redis state in the same operation.
4. **Webhook idempotency via tg_update_id.** Duplicate Telegram retries are detected and short-circuited. UNIQUE partial index enforces this at the DB level.
5. **UX never breaks on infrastructure failure.** Every failure mode has a graceful fallback: planner failure → safe default; LLM failure → retry once + canned error with [Try again]; DB failure → log+continue (post-delivery) or fail loud (pre-delivery).
6. **Profile cache invalidation mandatory on writes.** ANY service writing to student_notes, student_profile, or student_skill_profile MUST DEL `profile:brief:{student_id}`.

**Why:** During slice 2 verification, a UX bug surfaced (bot auto-serving questions after answers) that violated an unwritten design principle. Auditing for similar bugs revealed 25 issues, of which seven required immediate fixes in slice 2.5 and the rest got distributed across remaining slices or deferred to v1.5. The lesson: implicit principles need to be made explicit, or the AI implementing them will optimize the literal spec at the expense of felt experience.

**Rejected alternatives:**
- **Document principles only in slice prompts:** rejected because principles need to bind the architecture, not be re-litigated per slice.
- **Treat each bug as a one-off fix:** rejected because the bugs share a pattern. Naming the underlying principles ensures future slices don't reintroduce equivalent bugs.

**Tradeoffs accepted:**
- Implementation now requires checking principles on every code change. Slight overhead for big payoff in consistency.
- The principles add ~150 lines to service contracts. Worth it; they're the backbone of v5's "feels coherent" property.

**Revisit when:** A new principle emerges that should be added (e.g., from real-user feedback). Treat additions as architecturally significant — log here, don't drop into a service contract section silently.

---

### 2026-04-25 — Slice 2.5 retrofit: seven fixes that should have been in slice 2

**Status:** Decided
**Slice / Phase:** Slice 2.5

**Decision:** Slice 2's base implementation (6-tier retrieval + answer scoring + continuation buttons) was verified working. Before slice 3 began, slice 2.5 retrofitted seven fixes:

1. **Skip button** on question keyboards (Bug 8)
2. **Mid-question doubt detection** with back/skip/different-doubt buttons (Bug 1)
3. **Close old keyboards** when serving new question (Bug 11, Principle 2)
4. **Show my stats button** + `get_session_stats` function (Bug 12)
5. **LLM API failure user-facing fallback** with `[Try again]` button (Bug 18)
6. **DB write failure handling** per Principle 5 (Bug 19)
7. **Webhook idempotency** via tg_update_id (Bug 20)

**Why:** Each of these was caught during slice 2 architectural review. Fixes 1-4 are UX-breaking in slice 2's current form. Fixes 5-7 are reliability foundations that subsequent slices depend on. Retrofitting now cost less than retrofitting after slice 8.

**Rejected alternatives:**
- **Defer all 7 fixes to v1.5:** rejected because slices 3-8 will build on the broken base.
- **Fix only the 4 UX bugs:** rejected because reliability bugs have higher cost of late discovery.
- **Spread fixes across slices 3-5:** rejected because it muddles slice scoping.

**Tradeoffs accepted:**
- Adds ~5-7 hours of work between slices 2 and 3. Acceptable given the cost-of-deferral.
- Two new migrations (011 skipped column, 012 tg_update_id index). Both append-only, idempotent.

**Revisit when:** Verification reveals a fix didn't work or introduced regressions.

---

### 2026-04-25 — 17 of 25 architectural review bugs deferred to subsequent slices or v1.5

**Status:** Decided
**Slice / Phase:** Slice 2.5 architectural review

**Decision:** Of 25 bugs caught during architectural review, 7 were addressed in slice 2.5, 11 were addressed in subsequent slice prompts (3-8), and 7 were deferred to v1.5+.

**Bugs distributed to slices 3-8:**

- Bug 2 (Returning-after-break resume logic) → Slice 3
- Bug 4 (Real-time pattern detection within session) → Slice 8
- Bug 6 (Onboarding pause option) → Slice 6
- Bug 13 (Session-state cleared on boundary) → Slice 3 (strengthened in slice 3 verification fixes)
- Bug 14 (Profile cache invalidation hardening) → Slice 5
- Bug 15 (Mixed-intent secondary signal) → Slice 4
- Bug 22 (Granular subskill enum from planner) → Slice 4
- Bug 23 (Profile-derived default difficulty) → Slice 5
- Bug 24 (Post-onboarding session boundary) → Slice 6
- Bug 25 (Empty-state fallback in profile brief) → Slice 5
- Bug 9 (Long-message chunking) → Slice 2.5 implementation note

**Bugs deferred to v1.5+:**

- Bug 3 (deeper) — Topic switch with paused contexts preserved
- Bug 5 — Loading message timeout fallback
- Bug 7 — Flow mode for power users
- Bug 10 — /feedback command
- Bug 16 — Free text input during onboarding states
- Bug 17 — Voice/image messages
- Bug 21 — Long-context within-session summarization

**Why these specific deferrals:** The deferred bugs are nice-to-haves, scaling concerns, or features that don't break the core experience. The slice-distributed bugs are blocking for their respective slices but not blocking right now. The slice-2.5 bugs are blocking right now (UX-breaking or reliability foundations).

**Revisit when:** v1 ships and we have real-user data. Real-user feedback will likely re-prioritize.

---

### 2026-04-25 — Skip is "seen" but not "answered"

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
- **Skip = no explanation shown:** rejected; misses the teaching opportunity.

**Tradeoffs accepted:**
- Stats need to track skip count separately. Acceptable — `get_session_stats` reports it explicitly.
- Some students might over-use Skip. Observable via session stats; intervention can land in slice 8 if pattern emerges.

**Revisit when:** Real-user data shows skip-rate > 30% or pattern of avoidance.

---

### 2026-04-25 — Continuation button sets are deterministic, not LLM-generated

**Status:** Decided
**Slice / Phase:** Slice 2.5

**Decision:** The buttons that appear after agent responses (continuation prompts, mentor strategic responses, mid-question doubt acks) are selected by orchestrator code, not by the LLM. Different button sets fire based on `response_type`, `intent.action`, and `intent.emotional_tone`.

Examples:
- After answer/skip explanation → `[Next question]` `[Different subskill]` `[Show my stats]` `[I have a doubt]` `[I'm done]`
- Mid-question doubt ack → `[Back to the question]` `[Skip this question]` `[I have a different question]`
- Returning-after-break prompt → `[Resume that question]` `[Start fresh]` `[Just chat first]`
- After mentor anxiety/frustration response → `[Try one easy one]` `[Different subskill]` `[Talk it out more]` `[Take a break]`

**Why:** LLM-generated buttons would be inconsistent, unpredictable, and hard to validate. Deterministic selection ensures every flow has the right options every time. The agents' LLM is told NOT to include questions, options, or buttons in their output — the system appends them.

**Rejected alternatives:**
- **LLM picks the buttons:** rejected for consistency reasons above.
- **Single universal continuation button set:** rejected because context matters.
- **No buttons; pure free text:** rejected because button taps are faster on mobile and reduce friction.

**Tradeoffs accepted:**
- Button-set logic is in orchestrator code (~50 lines). Maintainable.
- Adding new continuation contexts requires adding new button sets in code. Acceptable; rare.

**Revisit when:** Real-user data shows consistent dead-ends (a context where no offered button matches what users want). Add the missing option.

---

## Slice 3 entries

These cover the slice 3 implementation (working memory + sessions, returning-after-break, LLM-generated explanations) and the verification fixes that surfaced during testing.

---

### 2026-04-26 — Slice 3 verified: 30-minute session boundary

**Status:** Decided  
**Slice / Phase:** Slice 3

**Decision:** A session boundary fires when a new message arrives more than 30 minutes after the previous message in the same session. The closed session is marked `ended_at = now()`, `end_reason = 'inactivity_timeout'`. A new session row is created and `state:tg:{tg_id}` is DELed in Redis before the new state is set (Principle 3 enforcement).

Earlier internal drafts considered 2-hour and 1-hour windows. 30 minutes was chosen because:
- CAT prep sessions are short and intense (typically 20-45 min)
- 30 min is short enough that "I came back the next morning" reliably triggers the returning-after-break flow
- Long enough that bathroom breaks / phone interruptions don't constantly fragment sessions

**Why:** Session boundaries are how the bot scopes "what we worked on today" vs "what we did last time." Wrong threshold either fragments coherent sessions (too short) or merges distinct study sessions (too long). 30 minutes is empirically a good fit for CAT prep behavior.

**Rejected alternatives:**
- **Explicit user-initiated session end only:** rejected because users rarely explicitly end sessions.
- **Fixed clock boundaries (e.g., midnight):** rejected because students study across midnight.
- **Adaptive (per-student learned threshold):** rejected as v1.5+ optimization.

**Tradeoffs accepted:**
- A user who steps away for 31 minutes mid-session will have their session split. Acceptable; the resume prompt softens this.
- Cleanup cron runs every 10 min, so sessions actually close 30-40 min after last activity. Acceptable timing.

**Revisit when:** Real-user data shows fragmenting (lots of short sessions) or merging (sessions spanning days).

---

### 2026-04-26 — Slice 3 verified: queries are session-scoped, not student-scoped

**Status:** Decided  
**Slice / Phase:** Slice 3

**Decision:** All queries that read "current state" — open unanswered attempts, session stats, recent turns for prompt context — are filtered by `session_id` AND `student_id`, not just `student_id`.

This means: the orchestrator's `_fetch_last_unanswered_attempt(student_id, session_id)` returns nothing if the unanswered attempt is in a closed (different) session. The student starting a new session has no "open question" from the old session bleeding into the new one.

Returning-after-break is the explicit exception: `detect_session_resume_candidate(student_id)` looks across sessions to find unfinished work in the most recently closed session. But it surfaces this as a deliberate prompt ("want to resume?"), not as silent state.

**Why:** Bug 13 (session-state leak) was a category of bugs — domain_state, last_question_attempt_id, recent_turns — all liable to cross session boundaries by accident. Enforcing session-scoping at the query level is the structural fix. State physically cannot leak if the query won't return it.

**Rejected alternatives:**
- **Clear all per-student state at boundary:** rejected because some state (working memory cache, profile brief cache) intentionally persists across sessions.
- **Document the rule in code comments only:** rejected because future code changes would re-introduce the bug. Query-level enforcement is structural.

**Tradeoffs accepted:**
- Slice 2/2.5 attempts (with `session_id IS NULL`) are invisible to session-scoped stats queries. Acceptable; they're test data.

**Revisit when:** A new query needs cross-session reads. The pattern is: explicit cross-session functions (like `detect_session_resume_candidate`) are clearly named; default scoping is per-session.

---

### 2026-04-26 — Slice 3 verified: old-session keyboard close deferred to new question serve

**Status:** Decided  
**Slice / Phase:** Slice 3

**Decision:** When a session boundary fires (cleanup or new-message-after-30-min), the OLD session's last question's inline keyboard is NOT removed at boundary time. It's removed when the NEW session's first question is served (orchestrator's Step 11.5 picks up `prior_question_message_id` from `resolve_session`'s third return value).

Trade-off: between the boundary and the next question serve, the OLD question's keyboard remains tappable in chat history. If the user taps it during this window, the answer routes to the new session and finds no matching open attempt → falls through to a default action.

**Why:** Closing keyboards requires a Telegram API call (`editMessageReplyMarkup`). We don't want to add latency or complexity to the cleanup cron path. Doing it lazily costs nothing and is sufficient for the common case.

**Rejected alternatives:**
- **Close keyboard at boundary:** would require cleanup cron to call Telegram API. Rejected.
- **Close keyboard when resume prompt is shown:** premature; user hasn't decided yet.

**Tradeoffs accepted:**
- Stale button tap during the resume-prompt window is theoretically possible, practically rare.

**Revisit when:** Users complain about stale-button confusion in real testing. Likely never; this edge case is too narrow.

---

### 2026-04-26 — Slice 3 verified: LLM call logging is best-effort

**Status:** Decided  
**Slice / Phase:** Slice 3

**Decision:** `record_llm_call` (writes to `v5.llm_calls`) catches all exceptions internally. A logging failure NEVER sinks the user-facing response.

**Why:** Per Principle 5, UX never breaks on infrastructure failure. An observability table going down is exactly the kind of dependency that should fail silently for the user.

**Rejected alternatives:**
- **Make logging blocking:** rejected. Observability is a tool for us, not a contract with the user.
- **Async logging via queue:** overkill for v1.

**Tradeoffs accepted:**
- We may have gaps in `v5.llm_calls` if Postgres has transient issues. Acceptable for cost analytics.

**Revisit when:** We need accurate per-user cost attribution for billing or analytics SLAs.

---

### 2026-04-26 — Slice 3 verified: shared LLM and observability infrastructure

**Status:** Decided  
**Slice / Phase:** Slice 3

**Decision:** Three shared modules introduced in slice 3 are now the canonical interface for any service making LLM calls or needing keyboard close coordination:

1. **`shared.llm.openrouter.chat_with_metadata(system, user, model) → LLMCallResult`** — replaces older content-only wrappers (still around for `v4_legacy`). Returns content + token counts + cost + latency. Has retry-once-on-failure built in.

2. **`shared.observability.llm_log.record_llm_call(...)`** — best-effort logger. Required after every `chat_with_metadata` call.

3. **`shared.telegram.utils.edit_telegram_keyboard(...)`** — keyboard close helper. Used by bus when `response.requires_keyboard_close=True`. Catches all errors; never raises.

**Why:** These cross-cut every service. Without shared modules, each service would re-implement (or worse, miss) cost tracking, retry logic, and keyboard-close handling. Making them shared and required means future slices add features cleanly.

**Rejected alternatives:**
- **Per-service implementations:** rejected for duplication and inconsistency.
- **Wrapper class hierarchy:** rejected as over-engineered for v1.

**Tradeoffs accepted:**
- Backward-compat wrappers still exist for v4_legacy. Some duplication; acceptable until v4 is fully retired.
- The "every LLM call must log" rule is enforced by convention, not code. Caught by reviewing slice prompts.

**Revisit when:** v4_legacy is fully retired; remove the older wrappers.

---

### 2026-04-26 — Slice 3 verification: markdown rendering switched to HTML parse mode

**Status:** Decided  
**Slice / Phase:** Slice 3 verification (mid-cycle fix)

**Decision:** All orchestrator-composed responses now use Telegram's HTML parse mode instead of legacy Markdown. LLM-generated content goes through `html.escape()` before delivery. Bus-side fallback retries without parse_mode if Telegram returns a parse error.

**Why:** The original implementation used legacy Markdown. Subskill names contain underscores (`inference_basic`); the legacy Markdown parser interprets unmatched `_` as italic markers and rejects the message. The bus's fallback delivered plain text — users saw raw asterisks instead of bold headers.

HTML parse mode requires escaping only `<`, `>`, `&` — not present in normal English or subskill names.

**Rejected alternatives:**
- **MarkdownV2:** rejected; requires escaping ~14 special characters that appear in normal sentences.
- **Escape underscores in legacy Markdown:** rejected; brittle, every subskill render needs its own escape.
- **No parse mode at all:** rejected; we lose visual hierarchy in stats and templated responses.

**Tradeoffs accepted:**
- LLM-generated content cannot use formatting for emphasis. Word choice carries the weight.
- HTML escape pass adds ~1ms per turn. Negligible.
- Adding new orchestrator templates requires HTML tags, not markdown.

**Revisit when:** A real-user complaint shows raw HTML tags rendering literally.

---

### 2026-04-26 — Slice 3 verification: message_count + Redis cleanup fixes

**Status:** Decided  
**Slice / Phase:** Slice 3 (verification phase)

**Decision:** Two bugs surfaced during Phase B testing of slice 3:

1. **`v5.sessions.message_count` was not being incremented on each turn.** The orchestrator updated `last_activity_at` correctly but the same UPDATE did not bump `message_count`. Fixed by combining the two into a single UPDATE that runs on every continuation turn. Convention: counts turns (one user + one assistant = +1).

2. **`cleanup_inactive_sessions` closed sessions in Postgres without clearing Redis state.** Fixed by having `close_session` (called by cleanup) do Postgres update + Redis DEL as a single semantic operation. Every caller of `close_session` now inherits the cleanup automatically. Also added a defensive staleness check in `resolve_session` for layered protection.

The pattern: Postgres is the source of truth; Redis is a derived cache; writes to Postgres MUST propagate to Redis.

This is now an invariant in service contracts: **any code path closing a Postgres session MUST clear the corresponding Redis state for that student's tg_id, in the same operation.**

**Why:** Both bugs were latent — neither caused user-facing UX issues during normal flow. But `message_count = 0` for active sessions breaks analytics queries, and stale Redis state would have caused subtle behavior 2-3 weeks in.

**Rejected alternatives:**
- **Defensive staleness check ONLY in `resolve_session`:** insufficient — wouldn't handle a future code path that closes a session without going through resolve_session on next turn.
- **Move `message_count` to Redis:** rejected because the column needs to persist in closed-session analytics anyway.

**Tradeoffs accepted:**
- `cleanup_inactive_sessions` now does an extra JOIN to fetch `tg_id`. Negligible cost.
- `message_count` counts turns, not individual messages. If we later want user-only counts, that's a different column.
- `resolve_session` does an extra Postgres PK lookup on every turn (the staleness check). ~10ms latency. Acceptable.

**Revisit when:** A new code path closes sessions outside of `cleanup_inactive_sessions` — the path inherits Redis cleanup automatically through `close_session`.

---

### 2026-04-26 — Schema drift caught by audit: fallback_tier added mid-cycle (migration 015)

**Status:** Decided  
**Slice / Phase:** Slice 3 verification (mid-cycle fix)

**Decision:** During slice 3 verification, a schema audit revealed that `v5.student_question_attempts.fallback_tier` was specified in the data model but never created by slice 2's migration. The retrieval ladder was already computing the tier and surfacing it in `messages.metadata`, but not landing it on the attempts row.

Fixed via `migrations/v5/015_add_fallback_tier_to_attempts.sql` — adds the column, threads `fallback_tier` through `_record_attempt` and its two call sites in VARC. Existing rows stay NULL (no backfill).

This incident motivated the **Schema Drift Discipline** documented in `04_slice_roadmap.md`:
1. Each slice's roadmap section enumerates migrations + columns explicitly
2. CREATE TABLE migrations include ALL columns from the data model — even future-slice columns
3. `python -m scripts.check_schema_drift` runs after every slice's migrations apply
4. Migration numbering is sequential and never reused
5. Mid-cycle migrations get the next available number

The drift check helper (`scripts/check_schema_drift.py`) was added in this fix's commit.

**Why:** Schema drift is the most common silent bug source. Slice 2's prompt said "create the schema" without saying "create THIS schema with THESE columns." Claude Code, working from a slice prompt, had no way to know the data model had columns the prompt didn't explicitly call out.

The fix: be explicit. Every slice prompt now enumerates migrations file-by-file with column lists. Every slice ends with a drift-check verification step.

**Rejected alternatives:**
- **Wholesale "redo all migrations":** rejected — invites destructive ALTERs and disruption to migration tracker.
- **Fix only the immediate gap and accept future drift:** rejected — drift accumulates.
- **Switch to a migration auto-generator:** considered but rejected for v1; manual migrations are fine at this scale.

**Tradeoffs accepted:**
- Three other columns (onboarding_paused_at, diagnostic_question_count, is_diagnostic) remain unaddressed at end of slice 3. They're scheduled to land with migration 016 in slice 6. The drift check correctly reports these as drift; exit code 1 is expected at end-of-slice-3.
- Each slice now spends a few minutes on drift verification. Cheap; catches recurrence.

**Revisit when:** Slice 6 ships migration 016. Drift check should output ALL `[OK]` after slice 6.

---

## Process Notes

These document how the docs themselves are maintained, so future slices stay aligned.

---

### 2026-04-26 — Documentation versioning: design docs update with slices

**Status:** Decided  
**Slice / Phase:** End of slice 3

**Decision:** The five design docs (`01_data_model.md`, `02_service_contracts.md`, `03_happy_path.md`, `04_slice_roadmap.md`, `05_claude_code_prompts.md`) plus `DECISIONS.md` are updated in lockstep with slice implementations.

When a slice introduces architectural changes:
1. The slice prompt is updated FIRST (in `05_claude_code_prompts.md`) so Claude Code has the right brief
2. After slice ships, the data model and service contracts are updated to match what was actually built
3. The slice roadmap's migrations subsection and "what's real" / "what's stubbed" sections are updated
4. DECISIONS.md gets entries for any architectural decisions made during the slice
5. The happy path doc is updated when major flow changes happen (less frequent than the others)

When a slice has verification fixes (mid-cycle bug fixes that fix the slice's deliverables):
- The fix is NOT in a separate slice — it's part of the slice's delivery
- DECISIONS.md gets a "Slice N verification: ..." entry for the fix
- Service contracts and/or data model are updated if the fix changes the contract or schema
- Slice roadmap's "What was actually shipped" section gets the fix added

**Why:** Without this discipline, docs drift from reality. Slice 6 looking at `01_data_model.md` from slice 3's era would have stale schema info. The cost of maintaining docs is small if done immediately; large if deferred.

**Rejected alternatives:**
- **Update all docs at v1 ship:** rejected — too much accumulated drift; impossible to remember 8 slices' worth of decisions.
- **Auto-generate docs from code:** rejected — design docs capture intent and tradeoffs that code can't.
- **No design docs after slice 1:** rejected — without docs, every Claude Code session starts from scratch on context.

**Tradeoffs accepted:**
- Each slice adds ~30-60 minutes of doc maintenance. Acceptable given how much time it saves on the next slice.
- Docs lag code by hours-to-a-day during active development. Acceptable; pre-merge alignment is the bar, not real-time consistency.

**Revisit when:** Doc maintenance becomes burdensome (e.g., every slice taking >2 hours of doc work). At that point, consider tooling.

---

## Slice 4 entries

These cover the slice 4 implementation (planner + guardrails) and the verification fixes that surfaced during testing.

---

### 2026-04-28 — Slice 4 verified: planner-driven intent classification with safe defaults

**Status:** Decided  
**Slice / Phase:** Slice 4

**Decision:** Replace slice 2.5's hardcoded routing with a single planner LLM call (Gemini Flash 1.5, `MODEL_PLANNER`). The planner receives the user's message, recent turns, and active session summary, and returns an `IntentClassification` with `domain`, `action`, `subskill`, `difficulty`, `emotional_tone`, `secondary_signal`, `confidence`, plus `context_needs` and `response_guidance`.

The orchestrator routes based on `intent.domain` + `intent.action`:
- `varc + practice_request` / `answer_to_question` / `skip_request` / `doubt_about_current` / `concept_question` → VARC handler
- `varc + small_talk` → orchestrator-direct small_talk_ack with 5-button continuation row (Bug 15)
- `varc + stats_request` → orchestrator-direct stats response
- `mentor + any` → mentor stub (slice 8 wires real)
- `out_of_scope + any` → orchestrator-direct soft-redirect with `[VARC question]` `[Strategy chat]` buttons; observer_event logged
- `orchestrator-direct subskill_picker` → slice 4 picker (4 buttons in 2 rows)

Step 6.5's deterministic detection (slice 2.5 callbacks: skip, continuation buttons, answer regex, retry, resume) STILL runs first to bypass the planner where definitive matches exist. The planner runs only when Step 6.5 returns no match.

**On planner failure (LLM timeout, parse error, malformed response):** safe default classification: `domain=varc`, `action=small_talk`. The bot asks the student what they want rather than auto-serving a question. This is the safest default per Principle 5.

**Cost:** ~$0.0001-0.0003 per planner call. ~500-1500ms latency added per turn. Acceptable for v1; may revisit if cost becomes a concern at scale.

Granular subskill enum (Bug 22): 12 specific subskills (`inference_basic`, `inference_advanced`, `main_idea_full_passage`, `specific_detail`, `passage_summary`, `sentence_insertion`, `sentence_odd_one_out`, `strengthen_weaken`, `purpose_of_example`, `vocab_in_context`, `author_tone`, `para_jumble`). VARC falls back to `inference_basic` if planner returns out-of-enum.

Mixed-intent secondary signal (Bug 15): "I'm stressed, give me an easy one" → `action=practice_request`, `difficulty=easy`, `secondary_signal={type:"emotional_undertone", value:"mild_stress"}`. The secondary_signal flows into VARC's LLM prompt as a tone hint so the explanation is gentler.

**Why:** Slice 2.5's hardcoded routing couldn't distinguish nuanced intent (small_talk vs practice_request) or capture emotional context. The planner makes routing context-aware while keeping deterministic detection for the cases where it matters (button callbacks, answer regex).

**Rejected alternatives:**
- **Defer planner to v1.5+ and ship hardcoded routing in v1:** rejected because the small_talk vs practice_request distinction is UX-critical. Without it, "thanks" or "ok" would auto-serve a new question, breaking conversational feel.
- **Use Sonnet 4.5 for planner:** rejected for cost. Gemini Flash is fast and cheap for classification; the cost difference is significant at scale.
- **Build a rule-based classifier:** rejected because rules can't capture mixed-intent or emotional undertone reliably. LLM understanding is what the planner exists for.

**Tradeoffs accepted:**
- Adds ~$0.0001-0.0003 per turn. Cumulative cost at scale = $5-15/month per active student. Acceptable.
- Adds ~500-1500ms latency. Mitigated by Step 6.5's deterministic bypass for callbacks (which is most user actions).
- Planner can misclassify edge cases. We accept this; the safe default (small_talk_ack) is graceful when wrong.

**Revisit when:** real-user data shows misclassifications causing UX issues. Likely fix is prompt iteration, not architectural change.

---

### 2026-04-28 — Slice 4 verification: observer_events inline persistence (rejected commit_deltas refactor)

**Status:** Decided  
**Slice / Phase:** Slice 4 verification (mid-cycle)

**Decision:** Persist `observer_events` inline at each emission site via a new `persist_observer_event` helper in `services/memory/main.py`. Do NOT build the `commit_deltas` centralized pipeline that earlier docs described.

**Background:** During slice 4 verification of Test B1 (`solve 2x + 3 = 7` → out_of_scope_query observer_event), the `v5.observer_events` table was empty even though the response carried `observer_events: [{event_type: "out_of_scope_query"}]`. Investigation revealed:

- `02_service_contracts.md` documented a `commit_deltas(student_id, tg_id, deltas)` function in the memory service that would iterate AgentResponse deltas (`new_assistant_turn`, `active_context_updates`, `attempt_record`, `notes_proposed`, `observer_events`) and persist them.
- That function was never built (`grep -rn "def commit_deltas"` returned zero matches).
- No code path persisted `observer_events`; the field was silently dropped across slices 1-4.
- All other persistence concerns (assistant message INSERT, attempt INSERT/UPDATE, Redis state updates) were happening inline at the emission sites — the codebase has been operating without commit_deltas all along.

Considered building `commit_deltas` as a slice 4.5 retrofit. Decided against. Reasons:

1. **The actual persistence is already well-organized inline.** Each table has a clear owner: messages → orchestrator (1 INSERT site); attempts → VARC (1 INSERT helper, 3 UPDATEs); Redis state → memory service helpers. Centralization would mostly move code without adding architectural value.

2. **Future slices (7, 8) emit deltas in different timing contexts.** Slice 7's extractor runs in the async session-end pipeline (post-delivery), not the request flow. Slice 8's mentor observer runs async via `asyncio.create_task`. Forcing both through one synchronous `commit_deltas` would require timing-aware logic that defeats centralization.

3. **The class of bug is preventable through inline helpers.** The `persist_observer_event` helper provides the same correctness guarantee — every emission site MUST call it, but the call is local to the emission site, not buried in a central function.

The fix:

- New `persist_observer_event(student_id, session_id, event_type, payload=None)` in `services/memory/main.py`. Best-effort INSERT; never raises; never blocks delivery (Principle 5).
- Each emission site calls the helper directly. Currently 2 sites: `_build_out_of_scope_response` in orchestrator (out_of_scope_query event), `_error_fallback_response` in VARC (llm_failure event).
- `_error_fallback_response` refactored from sync to async, takes full `context` (not just `intent`) so it can populate the observer_event payload with `student_id` and `session_id`.
- AgentResponse retains the `observer_events` field for clarity at emission site (a developer reading the handler can see what kind of event is being emitted), but it is metadata-only — the orchestrator does NOT iterate it.
- Documentation updated in `02_service_contracts.md` to remove the `commit_deltas` section and describe the inline-persistence pattern as the actual architecture.

**Why:** This is the structural fix without the structural cost. We get correctness (every emission site has a clear persist call) without the centralization burden (no synchronous iteration, no timing-aware dispatch).

**Rejected alternatives:**
- **Build commit_deltas as slice 4.5 retrofit:** rejected per the analysis above. ~3-5 hour refactor that touches 5+ persistence sites for marginal architectural benefit.
- **Keep as inline INSERT in `_build_out_of_scope_response` directly:** rejected because future emission sites (slice 8 mentor observer) need the same helper. Centralizing the helper avoids 4-5 different INSERT statements scattered around.
- **Iterate `observer_events` in the orchestrator's Step 13 only (partial commit_deltas):** rejected because slice 8's mentor observer runs async post-delivery and never reaches Step 13. Inconsistent dispatch surface.

**Tradeoffs accepted:**
- Future emission sites must explicitly call `persist_observer_event` — there's no "forgotten by orchestrator" safety net. Mitigated by making the helper the documented pattern in service contracts and by the slice 4 audit pass which verified no other delta types are silently dropped.
- The AgentResponse `observer_events` field is functionally vestigial — it's not iterated anywhere. Kept for readability and for potential v1.5+ centralization.

**Revisit when:** A new code path emits an observer_event without calling `persist_observer_event` and the event silently drops. The audit pass mechanism catches this — every slice should run the schema drift checker AND grep for emission sites without persist calls.

---

### 2026-04-28 — Slice 4 verification: Step 6.5 doubt-vs-small_talk heuristic

**Status:** Decided  
**Slice / Phase:** Slice 4 verification (mid-cycle)

**Decision:** Step 6.5's mid-question doubt detection now requires the message to LOOK like a doubt before intercepting. Short messages (≤2 words) without question marks or question words fall through to the planner. Real doubts ("what does premise mean?", "I'm confused") still route deterministically to mid_question_doubt_ack.

The heuristic, in `_looks_like_doubt(message)`:
- `word_count <= 2 AND no question mark AND no question word` → False (almost certainly small_talk)
- `has question mark OR has question word` → True (likely doubt)
- otherwise → False (let planner decide)

Question words: `what, why, how, when, where, which, who, explain, mean, means, meaning, doesn't, don't, dont, confused, unclear, stuck, lost`.

**Background:** During slice 4 verification of Test B6 (small_talk distinction), typing `thanks` / `ok` / `got it` while a question was unanswered returned `mid_question_doubt_ack` (with the 3-button doubt keyboard) instead of `small_talk_ack` (with the 5-button continuation row). Bug 15 (small_talk distinction) was broken whenever an open question existed.

Root cause: Step 6.5's `_classify_action_deterministic` had a branch that intercepted ANY free text during an open question as a mid-question doubt. The planner never got a chance to classify the message. Pre-slice-4 (when there was no planner), this was correct — every free text needed a deterministic interpretation. Post-slice-4, this defeats the planner entirely for any free text during an open question.

The fix preserves Step 6.5's role (deterministic bypass for definitive matches like answer regex, button callbacks) while letting ambiguous free text reach the planner.

**Edge case:** when the planner classifies as `practice_request` and an open question exists, the orchestrator treats this as the user wanting to abandon the current question. The current question's keyboard closes (Principle 2 path reused from `[Different subskill]` flow), and the new question is served.

**Why:** The planner exists to make nuanced classification decisions. Bypassing it for any free text during open questions makes the planner half-functional. The heuristic is liberal (any question signal triggers doubt-detection) so we don't regress the doubt-handling case, but small_talk acks now correctly bypass.

**Rejected alternatives:**
- **Always run planner during open questions (no Step 6.5 doubt detection):** rejected because real doubts ("what does X mean?") would add planner latency (~500-1500ms) and cost. Step 6.5's instant routing is a UX win for definitive cases.
- **More sophisticated heuristic (regex on intent words):** rejected as over-engineering for v1. The current heuristic correctly handles all 7 small_talk cases tested (`thanks`, `ok`, `got it`, `cool`, `another`, `next`, real-doubts).
- **Train a small classifier model for the doubt-vs-small_talk decision:** rejected as v1.5+ optimization.

**Tradeoffs accepted:**
- A 3-word message like "back to it" (small_talk variant) might now trigger the planner (additional latency). Acceptable; happens occasionally.
- The "any question word triggers doubt" rule means "what" said casually ("what's up?" — though `?` is the actual signal here) might wrongly classify as doubt. Edge case; user can tap [Back to question] to escape.

**Revisit when:** real-user data shows misclassifications. The heuristic is centralized; one function to update.

---

### 2026-04-28 — Slice 4 verification: subskill picker padding to always 4 buttons

**Status:** Decided  
**Slice / Phase:** Slice 4 verification (mid-cycle)

**Decision:** The `[Different subskill]` picker always shows exactly 4 buttons, regardless of student attempt history. Algorithm:

1. Query top 3 weakest subskills (with ≥5 answered attempts, sorted by accuracy ASC). May return 0-3 rows.
2. Pad with defaults to reach exactly 4 — defaults are `[inference_basic, main_idea_full_passage, specific_detail, inference_advanced]`. Skip duplicates.
3. Cold start (0 weakest): return all 4 defaults.

Display labels: `[Inference (basic)]`, `[Main idea]`, `[Specific detail]`, `[Inference (advanced)]`. Callback data: `v5_continue_subskill_<subskill_name>`. On tap: orchestrator routes as `practice_request` with `intent.subskill = <chosen>`.

**Background:** Initial implementation returned `weakest[:4]`, which surfaced a 1-button picker for students with ≥5 attempts on only one subskill (the typical test student state). Verification testing caught this on the test student (1 attempt-rich subskill: `inference_basic`).

**Why:** A 1-button picker is worse than no picker — it gives the student no actual choice. Always showing 4 ensures the picker delivers on its UX promise.

The `inference_basic` always-include rule is implicit via the defaults order: `inference_basic` is the first default, so it's the first padded entry when not already in weakest. This guarantees the student always has a "comfortable subskill" option even if their weakest are all advanced/specialized.

**Rejected alternatives:**
- **Show fewer buttons when fewer weakest exist:** rejected for UX reasons (single-button picker is bad).
- **Always show all 12 subskills:** rejected because keyboard becomes overwhelming on mobile.
- **Top 4 weakest with no default padding:** rejected because cold-start students would get no picker at all (insufficient attempt history).
- **5 buttons (including a "random" option):** rejected as feature creep; can revisit in v1.5+.

**Tradeoffs accepted:**
- Students who've worked on 4+ subskills won't see all their weakest in the picker — only top 3 + 1 default. Acceptable; can add "more options" button in v1.5 if requested.
- Subskill display name mapping is hardcoded in the orchestrator. Adding a new subskill to the enum requires updating the display map.

**Revisit when:** real-user data shows the picker being skipped (low CTR). May indicate the wrong defaults or wrong weakest-detection algorithm.

---

### 2026-04-28 — Slice 4 verification: keyboard reconstruction on idempotency retry

**Status:** Decided  
**Slice / Phase:** Slice 4 verification (mid-cycle, slice 2.5 carry-over)

**Decision:** When the same Telegram `update_id` is delivered twice (idempotency retry), the redelivered message includes the original inline keyboard, not just the text.

Implementation:
- Step 12 (orchestrator's assistant message persist) writes `keyboard_json: response.keyboard_buttons` into `messages.metadata` alongside other metadata fields.
- Step 0's retry path (`_check_telegram_retry`) on duplicate update_id: looks up the paired assistant message, parses metadata (handling both dict and string-encoded JSON), reconstructs the keyboard from `metadata.keyboard_json`, and returns it along with the content.
- Bus's `send_to_telegram` already accepts a keyboard parameter — no bus changes needed.

**Background:** Slice 2.5 introduced webhook idempotency (Principle 4). When Telegram retries a message with the same `update_id`, the orchestrator detects the duplicate and re-delivers the original assistant response. But pre-slice-4, the redelivery only carried the text — the keyboard was reconstructed as `keyboard: null`. This meant on Telegram's mobile clients, taps on the original (now duplicated) message had no buttons to tap on the duplicate.

The slice 4 prompt called for this fix; initial implementation didn't include it. Verification testing surfaced the gap (curl-twice test showed text-only redelivery).

**Why:** Without keyboard reconstruction, idempotency retry is half-broken — the user sees the question text again but can't act on it. The fix completes Principle 4's promise: duplicate updates result in identical user experiences, including interactive elements.

**Rejected alternatives:**
- **Re-run the full handler on retry (bypass idempotency):** rejected because that double-charges the user (LLM costs, attempt records) and breaks Principle 4.
- **Cache the full Telegram response payload (text + keyboard) in a Redis key keyed by update_id:** rejected as redundant — the keyboard is already structurally encoded in `messages.metadata.keyboard_json`. Redis cache adds complexity for no benefit.
- **Skip retries entirely (just respond with success without re-delivering):** rejected because Telegram retries are a real network reliability mechanism; the user expects to see the response.

**Tradeoffs accepted:**
- Slight increase in `messages.metadata` JSONB size (keyboard data per assistant message). Negligible at scale (a 5-button keyboard is ~500 bytes).
- Reconstruction logic must handle both dict and string metadata formats (asyncpg sometimes returns strings, sometimes dicts depending on driver state). Defensive code, ~5 lines.

**Revisit when:** Telegram retries become rare enough (improved network reliability) that the metadata bloat outweighs the reliability benefit. Unlikely; Telegram retries happen frequently in practice.

---

### 2026-04-28 — Slice 4 audit: close_session is now atomic Postgres + Redis

**Status:** Decided  
**Slice / Phase:** Slice 4 audit pass (post-verification)

**Decision:** `close_session(session_id, end_reason)` in memory service now performs the Postgres UPDATE and the Redis DEL of `state:tg:{tg_id}` as a single semantic operation. Internal flow:

1. Look up `tg_id` via JOIN to `v5.students` (BEFORE the UPDATE — handles already-closed sessions idempotently).
2. UPDATE `v5.sessions` with `COALESCE(ended_at, now())` and `COALESCE(end_reason, $2)`. The COALESCE makes double-close a safe no-op — original close timestamp/reason are preserved.
3. DEL `state:tg:{tg_id}` via `clear_active_session`. Unconditional once we have a `tg_id`.

`cleanup_inactive_sessions` simplified: SQL no longer JOINs to `v5.students` (close_session does its own lookup); the manual `clear_active_session` call after `close_session` was removed (now redundant).

`resolve_session`'s defensive staleness check on every turn (DEL Redis if Postgres says ended_at IS NOT NULL) is KEPT for defense in depth — protects any future code path that closes a session via direct SQL or some backdoor.

**Background:** The slice 4 audit pass surfaced this as a docs-vs-code mismatch. `02_service_contracts.md` explicitly promised that `close_session` was a one-stop close handling both Postgres UPDATE and Redis DEL. In practice, `close_session` only did the UPDATE; callers (`cleanup_inactive_sessions`, `resolve_session` boundary path) had to manually call `clear_active_session` afterwards. The Principle 3 invariant was honored only by caller discipline, not structurally.

This was the same shape as the `commit_deltas` audit finding: doc claims a structural enforcement point that doesn't exist in the actual function, leaving callers responsible. A future caller that trusts the doc would silently leave Redis stale.

**Why:** Principle 3 (active session state cleared on session boundary) is too important to depend on caller discipline. Making `close_session` atomic ensures every session-close path inherits the cleanup automatically — cleanup_inactive_sessions cron, future explicit_end handlers, future session-switch paths, future error paths.

The COALESCE on ended_at/end_reason makes the function double-close-safe. If two paths close the same session in quick succession (e.g., cron + user-triggered explicit_end), the second call is a no-op for the Postgres state but still re-clears Redis if it was somehow re-planted.

**Rejected alternatives:**
- **Document the rule in code comments only ("callers must call clear_active_session after"):** rejected because future code changes would re-introduce the bug. Structural enforcement at the function level is what matters.
- **Build a transaction around close_session that rolls back if Redis fails:** rejected because Postgres + Redis can't share a transaction. Best-effort Redis DEL is acceptable; the worst case (Redis fails after Postgres succeeds) is recoverable via resolve_session's defensive staleness check on the next turn.
- **Move all Redis cleanup to resolve_session (defensive only):** rejected because that means Redis stays stale until the next user message. For a 30-min idle student, that's 30+ minutes of stale state.

**Tradeoffs accepted:**
- `close_session` now does an extra Postgres SELECT (to lookup tg_id). ~5ms per close. Acceptable.
- Double-close races are now safe but can briefly produce two Redis DEL calls. Idempotent; no impact.

**Revisit when:** A new code path closes sessions outside of `close_session` (e.g., direct SQL UPDATE in some admin script). That path bypasses the atomic guarantee. Add it to the audit list.

---

### 2026-04-28 — Slice 4 audit pass: docs-vs-code alignment as one-time anti-debt sweep

**Status:** Decided  
**Slice / Phase:** Slice 4 audit pass

**Decision:** Run a one-time docs-vs-code audit pass after slice 4 closes to catch other docs-vs-code mismatches before slice 5+ inherits them. Findings triaged by severity (critical / medium / low). Critical findings get code fixes; medium findings get doc updates; low findings noted for v1 quality pass.

The audit ran against `01_data_model.md`, `02_service_contracts.md`, and `04_slice_roadmap.md` (slices 1-4 only). Findings:

**Critical (2):**
- `commit_deltas` documented but absent → resolved via inline persistence pattern + `persist_observer_event` helper (decision above).
- Step 13 of `handle_message` documented orchestrator iteration of `observer_events` that didn't exist → resolved by rewriting Step 13 to document inline persistence.

**Medium (1):**
- `close_session` doesn't actually clear Redis itself — caller discipline keeps it working → resolved via atomic close_session refactor (decision above).
- `get_default_difficulty` documented in service contracts but absent in code; VARC uses hardcoded `DEFAULT_DIFFICULTY = "medium"` → flagged for slice 5 (carry-over note added to FINAL PROMPT 5 and slice 5 roadmap).

**Low (5):** doc-only fixes — slice tags on forward-looking functions (`update_profile`, `add_note` family, `serve_diagnostic_question`, etc.); `students` "Written by" line was wrong; `student_skill_profile` references; Principle 6 needs "(applies once cache lands in slice 5)" scope note; AgentResponse memory_deltas section had four vestigial delta types.

**Cleared (1):** `clear_active_session` / `update_active_session` — false alarm; they exist as aliases.

**Why run an audit pass:** the silent-drop bug (observer_events) was caught only because slice 4 was the first slice to populate the field with non-empty content. If it hadn't been caught at this point, slice 7's extractor would likely have introduced a similar `notes_proposed` silent-drop. Running a structured audit AT THIS POINT IN TIME (after a slice that exercised previously-empty fields) is the right cadence.

We do NOT plan to run audits every slice. The audit catches docs-vs-code drift that accumulates over multiple slices. After this round of fixes, the docs match the code; future drift is bounded by per-slice documentation discipline (each slice updates its docs alongside code).

**Rejected alternatives:**
- **Run audits every slice:** rejected. Most slices don't introduce enough new contracts to make audit worthwhile; would be busy-work.
- **Skip audits entirely; trust per-slice doc discipline:** rejected because doc discipline is imperfect and silent-drops are exactly the kind of bug that surfaces only when fields populate. The slice 4 audit found 2 critical issues that wouldn't have been caught otherwise.
- **Build automated drift detection (linter for docs vs code):** rejected as over-engineering for v1; the manual audit took ~30-45 minutes and surfaced everything we needed.

**Tradeoffs accepted:**
- Audit takes 30-45 minutes of LLM time + 1-2 hours of triage. Worth it given the ~3 issues fixed before they compound.
- Some findings are subjective (e.g., "low severity" for "Written by: message_bus" line — could argue it should be medium). Triage is judgment, not science.

**Revisit when:** Major architectural change (e.g., introducing a new service, refactoring persistence). Run another audit before that change ships.

---

### 2026-04-28 — Slice 4 verified: subskill picker design (4 buttons, 2 rows, weakest + defaults)

**Status:** Decided  
**Slice / Phase:** Slice 4

**Decision:** The `[Different subskill]` button (which appears in the 5-button continuation row after every answer/skip explanation) opens an orchestrator-direct picker that displays exactly 4 buttons in a 2-row 2-column layout. Each button represents a subskill the student can practice next.

Algorithm (final per slice 4 verification fix):
- Query top 3 weakest subskills with ≥5 answered attempts, sorted by accuracy ASC.
- Pad with defaults `[inference_basic, main_idea_full_passage, specific_detail, inference_advanced]` to reach 4, skipping duplicates.
- Cold start: return all 4 defaults.

The picker is orchestrator-direct (no agent invocation, no LLM call). When the student taps a subskill button, the callback (`v5_continue_subskill_<name>`) routes through Step 6.5's deterministic detection back into the orchestrator, which routes as `practice_request` with `intent.subskill = <chosen>`.

**Why:** Pre-slice-4, `[Different subskill]` was a placeholder that surfaced a hardcoded message. Slice 4's prompt called for the real picker. Implementing it here completes the continuation-row UX promise (every button does something useful).

The "weakest + defaults" algorithm balances personalization (showing the student their weak areas) with discoverability (always showing inference_basic as a comfort option).

**Rejected alternatives:**
- **Show 12 subskills (full enum):** rejected; overwhelming on mobile, low CTR.
- **Random 4 subskills:** rejected; ignores student's actual weakness data.
- **Top 4 weakest with no defaults:** rejected; cold-start students get no picker at all.
- **LLM-generated picker (let the planner pick 4 subskills):** rejected for cost and consistency. Deterministic SQL is faster, cheaper, and more predictable.

**Tradeoffs accepted:**
- Display labels are hardcoded in orchestrator. Adding a new subskill requires updating the display map. Acceptable; subskill enum is bounded.
- Picker doesn't show difficulty preference. Could argue it should (e.g., "[Easy main idea]" + "[Hard main idea]"). Rejected as feature creep; difficulty selection happens via the planner when the student types.

**Revisit when:** Real-user data shows the picker being skipped or surfacing irrelevant subskills.

---

### 2026-04-28 — Slice 4 verified: HTML escape for LLM-generated content; bus retry-without-parse-mode fallback unchanged

**Status:** Decided (slice 3 carryover continued; called out for completeness)  
**Slice / Phase:** Slice 4

**Decision:** Slice 4 maintains the slice-3-verification HTML parse mode pattern unchanged:

- All orchestrator-composed templates (out_of_scope soft-redirect, small_talk_ack, strategy_chat response, subskill picker prompt) use HTML tags where bold/italic is needed — never markdown.
- All LLM-generated content (VARC explanations, future mentor responses) goes through `html.escape()` before being passed to the bus.
- The bus's `_safe_edit_text` and `_safe_send_text` retry without `parse_mode` if Telegram returns "can't parse entities" — this catches any LLM output that disobeys the no-formatting system prompt.

**Why this entry exists:** to confirm the slice 4 implementation correctly carries forward the slice 3 verification fix. No regressions; no changes needed at slice 4.

**Revisit when:** A new content type is introduced (e.g., voice transcription, image OCR). The escape pass needs to extend.

---

### 2026-04-28 — Process: forward-tense doc tags (slice 5/6/7/8) for clarity

**Status:** Decided  
**Slice / Phase:** Slice 4 audit pass

**Decision:** Functions documented in `02_service_contracts.md` that exist as contracts but don't yet exist in code now carry an explicit "Status: function not built yet" tag plus the slice they're scheduled for. Examples:

- `update_profile` — slice 6 (status: not built yet)
- `add_note`, `reinforce_note`, `supersede_note` — slice 7 (status: not built yet)
- `get_default_difficulty` — slice 5 (status: not built yet; current behavior: VARC uses hardcoded DEFAULT_DIFFICULTY constant)
- `serve_diagnostic_question`, `handle_diagnostic_answer` — slice 6 (status: not built yet)

The function appendix at the end of the contract doc also tags forward-looking entries (`# slice 7 (not built yet)`).

When the function is built (during its scheduled slice), the "Status: not built yet" tag is removed.

**Why:** Pre-slice-4, the contract read as if every documented function existed. The audit pass surfaced multiple "function documented but absent" findings. Adding explicit status tags prevents future audits from re-finding the same drift.

**Why not just remove the docs for unbuilt functions:** because the docs encode the contract a future slice will implement. Removing them would require re-spec'ing the function when the slice arrives. Keeping them with explicit status tags is the right balance.

**Rejected alternatives:**
- **Move forward-looking functions to a separate "future contracts" section:** rejected because it fragments the doc — readers want each service's contract in one place.
- **Auto-generate status tags from a manifest:** rejected as over-engineering for v1.
- **Remove them entirely until built:** rejected per above.

**Tradeoffs accepted:**
- Slight visual clutter in the contract doc. Acceptable for the audit-prevention benefit.
- Manual upkeep — must remove status tag when function is built. Easy to forget; solved by adding "remove Status note" to each slice's prompt's PRESERVE section.

**Revisit when:** v1 ships and all forward-tense functions are built. At that point all status tags can be cleaned up.

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

