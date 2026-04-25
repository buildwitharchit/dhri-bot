# agent/classifier.py
#
# Deterministic intent classifier for free-text messages. No LLM — this
# classifier ships as fast local heuristics so rate-limit accounting and
# handler routing stay inexpensive. Ambiguous messages route to 'doubt'.

import re
from typing import Literal

Intent = Literal["pj_answer", "concept", "doubt"]

_CONCEPT_PREFIXES = (
    "how do i",
    "how should i",
    "how to",
    "what is",
    "what are",
    "explain",
    "teach me",
    "help me understand",
)

# PJ answers are strictly digit-form (e.g. "4,1,2,3" or "4132"). The seed
# JSON keys PJ sentences with "1"-"4" and stores correct_order in digit
# form — that is canonical. Letters are rejected.
_PJ_ANSWER_RE = re.compile(
    r"^\s*([1-4])\s*[,\s-]*\s*([1-4])"
    r"\s*[,\s-]*\s*([1-4])\s*[,\s-]*\s*([1-4])\s*$"
)


def is_pj_answer(text: str) -> bool:
    """True only for 4 distinct digits from {1,2,3,4}."""
    m = _PJ_ANSWER_RE.match(text)
    if not m:
        return False
    return len(set(m.groups())) == 4


def classify_free_text(text: str, in_active_practice: bool = False) -> Intent:
    stripped = (text or "").strip()
    lower = stripped.lower()

    if in_active_practice and is_pj_answer(stripped):
        return "pj_answer"

    for prefix in _CONCEPT_PREFIXES:
        if lower.startswith(prefix):
            return "concept"

    return "doubt"
