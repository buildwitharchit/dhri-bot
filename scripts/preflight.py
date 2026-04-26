"""Pre-deploy / pre-ingest sanity check. Not part of the spec — ops guardrail.
Run: `python -m scripts.preflight`. Exits 1 on any FAIL (WARN tolerated)."""

import asyncio
import importlib
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

MODULES = [
    "config",
    # shared/ infrastructure
    "shared.db.client", "shared.redis.client", "shared.llm.openrouter",
    "shared.telegram.keyboards", "shared.telegram.utils",
    "shared.observability.llm_log",
    # v4_legacy/ — kept runnable until v5 services replace each piece
    "v4_legacy.memory.profile", "v4_legacy.memory.summarizer",
    "v4_legacy.agent.classifier", "v4_legacy.agent.explainer", "v4_legacy.agent.prompts",
    "v4_legacy.retrieval.selector", "v4_legacy.retrieval.reranker",
    "v4_legacy.retrieval.technique_queries",
    "v4_legacy.handlers.onboarding", "v4_legacy.handlers.home", "v4_legacy.handlers.stats",
    "v4_legacy.handlers.practice.common", "v4_legacy.handlers.practice.rc",
    "v4_legacy.handlers.practice.pj", "v4_legacy.handlers.practice.va",
    "v4_legacy.bot.router", "v4_legacy.bot.commands", "v4_legacy.bot.callbacks",
    "v4_legacy.bot.free_text",
    "v4_legacy.db.queries",
    # scripts/ingest/ — content tagging pipeline
    "scripts.ingest.embedder", "scripts.ingest.tagger", "scripts.ingest.pipeline",
    # scripts/ — admin entry points
    "scripts.run_v5_migrations",
    # v5 services
    "services.memory.main", "services.profile.main",
    "services.varc.main", "services.mentor.main",
    "services.orchestrator.main", "services.orchestrator.planner",
    "services.message_bus.main",
    "main",
]
REQUIRED_ENV_VARS = [
    "TELEGRAM_BOT_TOKEN", "WEBHOOK_SECRET",
    "DATABASE_URL", "DATABASE_URL_DIRECT",
    "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN",
    "OPENROUTER_API_KEY", "ADMIN_REPORTS_SECRET",
]
EXPECTED_FLAGGED_IDS = {
    "cat_pyq_crafts_q4", "cat2023s1_indian_ocean_q3", "cat2023s1_geography_q1",
}
ASYNC_CALL_NAMES = [
    "embed", "llm_call_with_retry", "llm_call_with_retry_messages",
    "db.fetch", "db.fetchrow", "db.fetchval", "db.execute", "db.executemany",
    "redis.get", "redis.set", "redis.incr", "redis.delete", "redis.expire",
    "redis.incrbyfloat",
    "get_state", "set_state", "acquire_lock", "release_lock",
    "update_skill_score", "update_trap_counts", "get_most_common_trap",
    "create_session", "write_message", "write_session_snapshot",
    "get_state_from_db_or_redis",
]


class Result:
    def __init__(self): self.entries = []
    def add(self, status, num, msg):
        self.entries.append((status, num, msg))
        print(f"[{status}] Check {num} — {msg}")
    def exit_code(self):
        counts = {k: sum(1 for s, *_ in self.entries if s == k) for k in ("PASS", "WARN", "FAIL")}
        verdict = "safe to proceed" if counts["FAIL"] == 0 else "FIX FAILURES BEFORE PROCEEDING"
        print(f"\nSummary: {counts['PASS']} PASS, {counts['WARN']} WARN, {counts['FAIL']} FAIL — {verdict}.")
        return 0 if counts["FAIL"] == 0 else 1


def check_1_imports(r):
    failed = []
    for mod in MODULES:
        try:
            importlib.import_module(mod)
        except Exception as e:
            failed.append(f"{mod}: {type(e).__name__}: {e}")
    if failed:
        r.add("FAIL", 1, f"{len(failed)}/{len(MODULES)} modules failed:\n      "
              + "\n      ".join(failed))
    else:
        r.add("PASS", 1, f"All {len(MODULES)} modules imported cleanly.")


def check_2_taxonomy(r):
    try:
        from config import ALL_SUBSKILLS
        from v4_legacy.retrieval.technique_queries import SUBSKILL_TO_TECHNIQUE_QUERY
    except Exception as e:
        r.add("FAIL", 2, f"Cannot load taxonomy: {e}"); return
    missing = set(ALL_SUBSKILLS) - set(SUBSKILL_TO_TECHNIQUE_QUERY.keys())
    extra = set(SUBSKILL_TO_TECHNIQUE_QUERY.keys()) - set(ALL_SUBSKILLS)
    if missing or extra:
        r.add("FAIL", 2, f"Taxonomy mismatch — missing={missing or '{}'} extra={extra or '{}'}"); return
    r.add("PASS", 2, f"Taxonomy consistent: {len(ALL_SUBSKILLS)} subskills, "
                     f"{len(SUBSKILL_TO_TECHNIQUE_QUERY)} queries.")


def check_3_constants(r):
    try:
        from config import STUDENT_SKILLS, ALL_SUBSKILLS, ALL_TRAPS, SKILL_TYPE_MATRIX
    except Exception as e:
        r.add("FAIL", 3, f"Cannot load constants: {e}"); return
    checks = [
        ("STUDENT_SKILLS", len(STUDENT_SKILLS), 7),
        ("ALL_SUBSKILLS", len(ALL_SUBSKILLS), 18),
        ("ALL_TRAPS", len(ALL_TRAPS), 8),
        ("SKILL_TYPE_MATRIX", len(SKILL_TYPE_MATRIX), 9),
    ]
    bad = [(n, g, w) for n, g, w in checks if g != w]
    if bad:
        r.add("FAIL", 3, "Wrong counts: "
              + ", ".join(f"{n}={g} expected {w}" for n, g, w in bad)); return
    r.add("PASS", 3, f"Constants: {len(STUDENT_SKILLS)} skills, {len(ALL_SUBSKILLS)} subskills, "
                     f"{len(ALL_TRAPS)} traps, {len(SKILL_TYPE_MATRIX)} type mappings.")


def check_4_env(r):
    try:
        from config import settings
    except Exception as e:
        r.add("FAIL", 4, f"Cannot load settings: {e}"); return
    missing = [n for n in REQUIRED_ENV_VARS
               if not isinstance(getattr(settings, n, None), str)
               or not getattr(settings, n)]
    if missing:
        r.add("FAIL", 4, f"Missing/empty env vars: {', '.join(missing)}"); return
    r.add("PASS", 4, f"{len(REQUIRED_ENV_VARS)} required env vars present.")


def check_5_seed(r):
    seed = PROJECT_ROOT / "content" / "pyqs" / "dhri_48_pyqs_v4.json"
    if not seed.exists():
        r.add("FAIL", 5, f"Seed file missing: {seed.relative_to(PROJECT_ROOT)}"); return
    try:
        from config import ALL_SUBSKILLS, SKILL_TYPE_MATRIX
        data = json.loads(seed.read_text())
    except Exception as e:
        r.add("FAIL", 5, f"Cannot load seed: {e}"); return
    if not all(k in data for k in ("_metadata", "passages", "questions")):
        r.add("FAIL", 5, "Seed JSON missing top-level _metadata/passages/questions"); return
    passages, questions = data["passages"], data["questions"]
    if len(passages) != 8 or len(questions) != 48:
        r.add("FAIL", 5, f"Counts off: {len(passages)} passages, "
                         f"{len(questions)} questions (expected 8, 48)"); return
    flagged = {q["question_id"] for q in questions if q.get("needs_review")}
    if flagged != EXPECTED_FLAGGED_IDS:
        r.add("FAIL", 5, f"Flagged mismatch: got {sorted(flagged)} "
                         f"expected {sorted(EXPECTED_FLAGGED_IDS)}"); return
    bad_sub = [q["question_id"] for q in questions if q["subskill"] not in ALL_SUBSKILLS]
    bad_type = [q["question_id"] for q in questions if q["type"] not in SKILL_TYPE_MATRIX]
    bad_pair = [(q["question_id"], q["type"], q["subskill"]) for q in questions
                if q["type"] in SKILL_TYPE_MATRIX
                and q["subskill"] not in SKILL_TYPE_MATRIX[q["type"]]]
    if bad_sub or bad_type or bad_pair:
        r.add("FAIL", 5, f"bad subskill={bad_sub or 0} bad type={bad_type or 0} "
                         f"illegal pairs={bad_pair or 0}"); return
    r.add("PASS", 5, f"Seed JSON: {len(passages)} passages, {len(questions)} questions, "
                     f"{len(flagged)} flagged (correct IDs).")


async def check_6_db(r):
    try:
        from shared.db.client import db, init_db_pool
    except Exception as e:
        r.add("FAIL", 6, f"Cannot import db client: {e}"); return
    try:
        await init_db_pool()
        one = await db.fetchval("SELECT 1")
        if one != 1:
            r.add("FAIL", 6, f"SELECT 1 returned {one!r}"); return
        tables = await db.fetchval(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'"
        )
        if not tables:
            r.add("WARN", 6, "DB reachable but 0 tables in public schema (run migrations/v4/initial_schema.sql)."); return
        r.add("PASS", 6, f"DB reachable; {tables} tables in public schema.")
    except Exception as e:
        r.add("FAIL", 6, f"DB failed: {type(e).__name__}: {e}")


async def check_7_redis(r):
    try:
        from shared.redis.client import init_redis, redis
    except Exception as e:
        r.add("FAIL", 7, f"Cannot import redis client: {e}"); return
    try:
        await init_redis()
        await redis.set("preflight:test", "ok")
        got = await redis.get("preflight:test")
        await redis.delete("preflight:test")
        if got != "ok":
            r.add("FAIL", 7, f"Redis roundtrip mismatch: got {got!r}"); return
        r.add("PASS", 7, "Redis roundtrip OK.")
    except Exception as e:
        r.add("FAIL", 7, f"Redis failed: {type(e).__name__}: {e}")


async def check_8_embedding(r):
    try:
        from shared.llm.openrouter import embed
    except Exception as e:
        r.add("FAIL", 8, f"Cannot import embed: {e}"); return
    try:
        vec = await embed("preflight test")
        if not isinstance(vec, list) or len(vec) != 1536:
            n = len(vec) if hasattr(vec, "__len__") else "n/a"
            r.add("FAIL", 8, f"Unexpected vector: type={type(vec).__name__} len={n}"); return
        r.add("PASS", 8, f"Embedding OK (dim={len(vec)}).")
    except Exception as e:
        r.add("FAIL", 8, f"Embedding call failed: {type(e).__name__}: {e}")


def check_9_awaits(r):
    pattern = re.compile(r"\b(" + "|".join(re.escape(n) for n in ASYNC_CALL_NAMES) + r")\s*\(")
    skip = {"scripts", ".venv", "venv", "admin", "__pycache__", ".git", "docs"}
    suspicious = []
    for py in PROJECT_ROOT.rglob("*.py"):
        if any(p in skip for p in py.relative_to(PROJECT_ROOT).parts):
            continue
        try:
            lines = py.read_text().splitlines()
        except Exception:
            continue
        for i, line in enumerate(lines, 1):
            stripped = line.lstrip()
            if not stripped or stripped.startswith(("#", "import ", "from ", "async def ", "def ")):
                continue
            m = pattern.search(line)
            if not m:
                continue
            before = line[:m.start()]
            if "await" in before:
                continue
            if (before.count('"') + before.count("'")) % 2 == 1:
                continue  # inside string literal
            suspicious.append(f"{py.relative_to(PROJECT_ROOT)}:{i}: {stripped}")
    if suspicious:
        r.add("FAIL", 9, f"{len(suspicious)} possibly missing await(s):\n      "
              + "\n      ".join(suspicious[:30])); return
    r.add("PASS", 9, "No missing-await warnings.")


async def run_all():
    r = Result()
    check_1_imports(r); check_2_taxonomy(r); check_3_constants(r)
    check_4_env(r); check_5_seed(r)
    await check_6_db(r); await check_7_redis(r); await check_8_embedding(r)
    check_9_awaits(r)
    return r.exit_code()


if __name__ == "__main__":
    sys.exit(asyncio.run(run_all()))
