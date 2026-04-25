# ingest/pipeline.py
#
# Section 20 (store_question) + Appendix A (run_seed_ingest).
#
# FIX 5 — Seed path uses the shared build_embed_text helper. The main
# tagger path (future supplementary ingest) must also call
# build_embed_text; there is no local duplication of that logic.
#
# FIX 8 — when flagging needs_review because a (type, subskill) pair is
# illegal, log the legal subskills for that question type.

import argparse
import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from shared.llm.openrouter import embed, llm_call_with_retry
from config import SKILL_TYPE_MATRIX, SUBSKILL_TO_SKILL, settings
from shared.db.client import close_db_pool, db, init_db_pool
from scripts.ingest.embedder import build_embed_text
from scripts.ingest.parser import merge_tags, parse_tagger_output
from scripts.ingest.tagger import get_tagger_prompt
from scripts.ingest.verifier import verify_question
from shared.redis.client import init_redis

logger = logging.getLogger(__name__)


# ─── STORE QUESTION (Section 20) ────────────────────────────────────────────

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
        # FIX 8 — include legal subskills in the log for quick debugging.
        logger.warning(
            f"Invalid (type={q_type}, subskill={subskill}) for "
            f"question_id={question['question_id']} — needs_review=true. "
            f"Legal for {q_type}: {legal_subskills}"
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
        is_flagged,
    )


# ─── SEED INGEST (Appendix A) ───────────────────────────────────────────────

async def run_seed_ingest(file_path: str) -> None:
    """
    Ingest pre-tagged PYQs without running tagger/verifier. Only embeddings
    are computed. Uses the shared build_embed_text helper (FIX 5).
    """
    with open(file_path) as f:
        data = json.load(f)

    for p in data.get('passages', []):
        await db.execute("""
            INSERT INTO passages (passage_id, full_text, word_count, topic, tone,
                                  source, year, difficulty, is_active)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            ON CONFLICT (passage_id) DO NOTHING
        """, p['passage_id'], p['full_text'], p['word_count'], p['topic'],
             p['tone'], p['source'], p['year'], p['difficulty'], p.get('is_active', True))

    for q in data.get('questions', []):
        tags = {
            'subskill': q['subskill'],
            'traps_present': q.get('traps_present') or [],
            'option_traps': q.get('option_traps') or {},
            'one_line_technique': q['one_line_technique'],
            'taxonomy_version': 1,
            # schema.sql declares `tagged_at TIMESTAMP` (naive). Strip tz.
            'tagged_at': datetime.now(timezone.utc).replace(tzinfo=None),
            'tagger_model': 'manual_pyq_v4',
            'connector_type': q.get('connector_type'),
            'opening_clue': q.get('opening_clue'),
            'pj_connector_map': q.get('pj_connector_map'),
        }
        embed_text = build_embed_text(tags)  # FIX 5
        vector = await embed(embed_text)
        q['_vector'] = vector
        q['_tags'] = tags
        await store_question(q)

    logger.info(f"Seeded {len(data.get('questions', []))} questions from {file_path}")


# ─── FULL-TAGGING PIPELINE (supplementary ingest) ───────────────────────────

async def run_full_ingest(file_path: str, *, skip_verifier: bool = False) -> None:
    """
    Run the full Flash+Sonnet tagger pipeline on unseen questions. Only used
    for supplementary (non-seed) ingest. Seed data takes --skip-tagger.
    """
    with open(file_path) as f:
        data = json.load(f)

    for p in data.get('passages', []):
        await db.execute("""
            INSERT INTO passages (passage_id, full_text, word_count, topic, tone,
                                  source, year, difficulty, is_active)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            ON CONFLICT (passage_id) DO NOTHING
        """, p['passage_id'], p['full_text'], p['word_count'], p['topic'],
             p['tone'], p['source'], p['year'], p['difficulty'], p.get('is_active', True))

    for q in data.get('questions', []):
        try:
            prompt = get_tagger_prompt(q['type'], q)
            structured = await llm_call_with_retry(
                system="Return JSON matching the requested schema.",
                user=prompt,
                model=settings.MODEL_TAGGER_STRUCTURED,
            )
            tags = parse_tagger_output(structured)

            if not tags.get('one_line_technique'):
                technique = await llm_call_with_retry(
                    system="Return only a single-sentence technique (≤25 words).",
                    user=prompt,
                    model=settings.MODEL_TAGGER_TECHNIQUE,
                )
                tags['one_line_technique'] = technique.strip()

            tags.setdefault('taxonomy_version', 1)
            tags['tagged_at'] = datetime.now(timezone.utc).replace(tzinfo=None)
            tags['tagger_model'] = (
                f"{settings.MODEL_TAGGER_STRUCTURED}+{settings.MODEL_TAGGER_TECHNIQUE}"
            )

            merged = merge_tags(q, tags)
            if not skip_verifier:
                merged = await verify_question(merged)

            embed_text = build_embed_text(merged['_tags'])  # FIX 5
            merged['_vector'] = await embed(embed_text)
            await store_question(merged)
        except Exception as e:
            logger.exception(f"ingest failed for {q.get('question_id')}: {e}")

    logger.info(f"Full ingest complete for {file_path}")


# ─── CLI ────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="DHRI ingest pipeline")
    p.add_argument("--file", required=True, help="Path to input JSON")
    p.add_argument(
        "--skip-tagger", action="store_true",
        help="Seed mode: skip Flash+Sonnet tagger, use pre-tagged fields",
    )
    p.add_argument(
        "--skip-verifier", action="store_true",
        help="Skip the independent verifier pass",
    )
    return p.parse_args()


async def _main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = _parse_args()

    # Spend cap sanity: ingest loads the same config but may not use Redis state
    await init_db_pool()
    await init_redis()

    path = args.file
    if not os.path.exists(path):
        raise SystemExit(f"file not found: {path}")

    # Startup assertion — mirror main.py so ingest fails loudly on taxonomy drift
    from v4_legacy.retrieval.technique_queries import SUBSKILL_TO_TECHNIQUE_QUERY
    from config import ALL_SUBSKILLS
    missing = set(ALL_SUBSKILLS) - set(SUBSKILL_TO_TECHNIQUE_QUERY.keys())
    extra = set(SUBSKILL_TO_TECHNIQUE_QUERY.keys()) - set(ALL_SUBSKILLS)
    if missing or extra:
        raise SystemExit(
            f"Taxonomy mismatch: missing={missing}, extra={extra}"
        )

    try:
        if args.skip_tagger:
            await run_seed_ingest(path)
        else:
            await run_full_ingest(path, skip_verifier=args.skip_verifier)
    finally:
        await close_db_pool()


if __name__ == "__main__":
    asyncio.run(_main())
