# retrieval/selector.py
#
# PracticeSelector — retrieves the next question per user profile.
#
# CRITICAL FIX 1 (Gap 1): _fetch_single has TWO SQL branches — one with
# subskill filter, one without. Do not collapse.
#
# CRITICAL FIX 2 (Gap 2): every retrieval query includes
# `AND q.needs_review = false` so flagged PYQs never reach students.

from config import RC_SUBSKILLS, PJ_SUBSKILLS
from v4_legacy.retrieval.technique_queries import SUBSKILL_TO_TECHNIQUE_QUERY
from v4_legacy.retrieval.reranker import rerank
from v4_legacy.memory.profile import get_weakest_subskill_in_group, get_most_common_trap
from shared.llm.openrouter import embed
from shared.db.client import db

DIFFICULTY_ORDER = ['easy', 'medium', 'hard']


class PracticeSelector:

    async def get_rc_passage(self, profile: dict) -> dict | None:
        """
        Fallback chain:
        1. weakest RC subskill + requested difficulty (unseen, not flagged)
        2. weakest RC subskill + adjacent difficulty (both directions)
        3. second-weakest RC subskill + requested difficulty
        4. None — surface exhausted message
        """
        tg_id = profile['tg_id']
        difficulty = profile['current_difficulty']
        seen_ids = await self.get_seen_question_ids(tg_id)
        weakest = await get_weakest_subskill_in_group(tg_id, RC_SUBSKILLS)

        configs = [
            (weakest, difficulty),
            (weakest, self._adjacent(difficulty, 'easier')),
            (weakest, self._adjacent(difficulty, 'harder')),
            (await self._second_weakest(tg_id, RC_SUBSKILLS, weakest), difficulty),
        ]
        for subskill, diff in configs:
            if not subskill or not diff:
                continue
            result = await self._fetch_rc(tg_id, subskill, diff, seen_ids, profile)
            if result:
                return result
        return None

    async def _fetch_rc(self, tg_id, subskill, difficulty, seen_ids, profile):
        query_vector = await embed(SUBSKILL_TO_TECHNIQUE_QUERY[subskill])
        candidates = await db.fetch("""
            SELECT q.question_id, q.passage_id,
                   1 - (q.technique_embedding <=> $1::vector) as similarity,
                   q.subskill, q.traps_present, q.difficulty
            FROM questions q
            WHERE q.type = 'rc_question'
              AND q.subskill = $2
              AND q.difficulty = $3
              AND q.is_active = true
              AND q.needs_review = false
              AND q.question_id != ALL($4)
              AND q.passage_id IS NOT NULL
            ORDER BY q.technique_embedding <=> $1
            LIMIT 20
        """, query_vector, subskill, difficulty, seen_ids)

        if not candidates:
            return None

        reranked = rerank([dict(r) for r in candidates], await get_most_common_trap(tg_id))

        # Pick the passage with the most unseen candidate questions
        passage_counts: dict[str, int] = {}
        for q in reranked:
            pid = q['passage_id']
            passage_counts[pid] = passage_counts.get(pid, 0) + 1

        best_pid = max(passage_counts, key=passage_counts.get)
        if passage_counts[best_pid] < 2:
            return None  # need at least 2 unseen questions on the passage

        unseen_qs = [q for q in reranked if q['passage_id'] == best_pid]
        qids = [q['question_id'] for q in unseen_qs]
        full_questions = await db.fetch("""
            SELECT * FROM questions
            WHERE question_id = ANY($1) AND needs_review = false
            ORDER BY question_order
        """, qids)
        passage = await db.fetchrow(
            "SELECT * FROM passages WHERE passage_id = $1", best_pid
        )
        return {
            "passage": dict(passage) if passage else None,
            "questions": [dict(r) for r in full_questions],
        }

    async def get_pj(self, profile: dict) -> dict | None:
        """Fallback: weakest PJ subskill → any PJ subskill → adjacent difficulty."""
        tg_id = profile['tg_id']
        difficulty = profile['current_difficulty']
        seen_ids = await self.get_seen_question_ids(tg_id)
        weakest = await get_weakest_subskill_in_group(tg_id, PJ_SUBSKILLS)

        configs = [
            (weakest, difficulty),
            (None, difficulty),  # any PJ
            (weakest, self._adjacent(difficulty, 'easier')),
            (weakest, self._adjacent(difficulty, 'harder')),
        ]
        for subskill, diff in configs:
            if not diff:
                continue
            result = await self._fetch_single(
                tg_id, 'pj', subskill, diff, seen_ids, profile
            )
            if result:
                return result
        return None

    async def get_va(
        self, profile: dict, va_type: str, subskill: str | None = None
    ) -> dict | None:
        """Fetch one VA question of a specific type."""
        from config import SKILL_TYPE_MATRIX
        tg_id = profile['tg_id']
        difficulty = profile['current_difficulty']
        seen_ids = await self.get_seen_question_ids(tg_id)

        legal_subskills = SKILL_TYPE_MATRIX.get(va_type, [])
        if not subskill or subskill not in legal_subskills:
            subskill = await get_weakest_subskill_in_group(tg_id, legal_subskills)

        configs = [
            (subskill, difficulty),
            (subskill, self._adjacent(difficulty, 'easier')),
            (subskill, self._adjacent(difficulty, 'harder')),
        ]
        for sk, diff in configs:
            if not sk or not diff:
                continue
            result = await self._fetch_single(
                tg_id, va_type, sk, diff, seen_ids, profile
            )
            if result:
                return result
        return None

    async def _fetch_single(
        self, tg_id, q_type, subskill, difficulty, seen_ids, profile
    ):
        """
        Fetch a single question. TWO SQL BRANCHES — one with subskill filter,
        one without. This avoids the nested f-string $N interpolation bug
        that existed in v4.0. Do not collapse these branches.

        Also enforces needs_review = false so flagged questions never reach
        students during practice.
        """
        query_text = SUBSKILL_TO_TECHNIQUE_QUERY.get(
            subskill, SUBSKILL_TO_TECHNIQUE_QUERY['inference_basic']
        )
        query_vector = await embed(query_text)

        if subskill:
            candidates = await db.fetch("""
                SELECT q.*,
                       1 - (q.technique_embedding <=> $1::vector) as similarity
                FROM questions q
                WHERE q.type = $2
                  AND q.subskill = $3
                  AND q.difficulty = $4
                  AND q.is_active = true
                  AND q.needs_review = false
                  AND q.question_id != ALL($5)
                ORDER BY q.technique_embedding <=> $1
                LIMIT 10
            """, query_vector, q_type, subskill, difficulty, seen_ids)
        else:
            candidates = await db.fetch("""
                SELECT q.*,
                       1 - (q.technique_embedding <=> $1::vector) as similarity
                FROM questions q
                WHERE q.type = $2
                  AND q.difficulty = $3
                  AND q.is_active = true
                  AND q.needs_review = false
                  AND q.question_id != ALL($4)
                ORDER BY q.technique_embedding <=> $1
                LIMIT 10
            """, query_vector, q_type, difficulty, seen_ids)

        if not candidates:
            return None
        reranked = rerank([dict(r) for r in candidates], await get_most_common_trap(tg_id))
        return dict(reranked[0])

    async def get_seen_question_ids(self, tg_id: int) -> list:
        rows = await db.fetch(
            "SELECT DISTINCT question_id FROM attempts WHERE tg_id = $1", tg_id
        )
        return [r['question_id'] for r in rows]

    def _adjacent(self, difficulty: str, direction: str) -> str | None:
        idx = DIFFICULTY_ORDER.index(difficulty)
        if direction == 'easier':
            return DIFFICULTY_ORDER[idx - 1] if idx > 0 else None
        return DIFFICULTY_ORDER[idx + 1] if idx < 2 else None

    async def _second_weakest(self, tg_id, group, exclude) -> str | None:
        rows = await db.fetch("""
            SELECT subskill FROM user_skill_scores
            WHERE tg_id = $1 AND subskill = ANY($2) AND subskill != $3
            ORDER BY score ASC LIMIT 1
        """, tg_id, group, exclude)
        return rows[0]['subskill'] if rows else None
