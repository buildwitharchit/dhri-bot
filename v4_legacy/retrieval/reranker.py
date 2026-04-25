# retrieval/reranker.py


def rerank(
    candidates: list[dict],
    profile_trap: str,
) -> list[dict]:
    """
    Composite score: 0.62 * similarity + 0.38 * trap_match.

    FIX 3 — dropped the 0.2 * 1.0 difficulty_fit dead weight from v4.0.
    Difficulty is already filtered in SQL, so a constant 1.0 term just
    offset every score identically and was dead weight.
    """
    scored: list[tuple[float, dict]] = []
    for q in candidates:
        sim = float(q.get("similarity", 0.0) or 0.0)
        traps = q.get("traps_present") or []
        trap_score = 1.0 if profile_trap and profile_trap in traps else 0.0
        composite = 0.62 * sim + 0.38 * trap_score
        scored.append((composite, q))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [q for _, q in scored]
