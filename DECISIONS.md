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

