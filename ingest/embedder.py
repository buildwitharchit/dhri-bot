# ingest/embedder.py
#
# Shared embed-text builder (Section 16) plus a thin wrapper around
# agent.llm.embed so ingest callers don't couple to the LLM module.

from agent.llm import embed as _embed


def build_embed_text(tags: dict) -> str:
    """
    Three fields, ~40 tokens. The cognitive fingerprint.
    Do NOT add secondary_skill, solving_strategy, cognitive_operation —
    those columns are dropped. The narrow embedding is correct.
    """
    traps = tags.get('traps_present') or []
    trap_str = traps[0] if traps else 'none'
    return (
        f"{tags['one_line_technique']}\n"
        f"Skill: {tags['subskill']}\n"
        f"Trap: {trap_str}"
    )


async def embed_technique(tags: dict) -> list[float]:
    text = build_embed_text(tags)
    return await _embed(text)
