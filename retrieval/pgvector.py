# retrieval/pgvector.py
#
# Light vector helpers. The pgvector codec is registered on the asyncpg pool
# in db/client.py, so callers can pass Python lists directly to parameterised
# queries using `$N::vector`. This module holds only small math helpers.

import math
from typing import Sequence


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    denom = math.sqrt(na) * math.sqrt(nb)
    if denom == 0.0:
        return 0.0
    return dot / denom


def vector_literal(vec: Sequence[float]) -> str:
    """String form for manual SQL. Prefer parameter binding when possible."""
    return "[" + ",".join(f"{float(x):.6f}" for x in vec) + "]"
