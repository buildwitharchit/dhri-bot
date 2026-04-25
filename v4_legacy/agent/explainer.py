# agent/explainer.py
#
# Builds {context} for SYSTEM_PROMPT_TEMPLATE and runs explanation LLM calls.

from typing import Optional

from shared.llm.openrouter import llm_call_with_retry_messages
from v4_legacy.agent.prompts import SYSTEM_PROMPT_TEMPLATE
from config import SKILL_DISPLAY_NAMES, settings


def build_context(profile: dict, last_summaries: Optional[list[str]] = None) -> str:
    """Compose the {context} block for the system prompt."""
    tg_id = profile.get("tg_id")
    weakest = profile.get("weakest_skill")
    weakest_label = (
        SKILL_DISPLAY_NAMES.get(weakest, weakest) if weakest else "not yet determined"
    )
    trap = profile.get("most_common_trap") or "none"
    streak = profile.get("current_streak") or 0
    total_attempts = profile.get("total_attempts") or 0

    summary_block = ""
    if last_summaries:
        summary_block = "\nRecent sessions:\n" + "\n".join(
            f"- {s}" for s in last_summaries if s
        )

    return (
        f"tg_id={tg_id}\n"
        f"Weakest skill: {weakest_label}\n"
        f"Most common trap: {trap}\n"
        f"Current streak: {streak} day(s)\n"
        f"Total attempts: {total_attempts}"
        f"{summary_block}"
    )


def build_messages_for_llm(history: list[dict], user_text: str) -> list[dict]:
    """Compose the message array (excluding system) given prior turns + current user text."""
    messages: list[dict] = []
    for m in history:
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        content = m.get("content")
        if not content:
            continue
        messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_text})
    return messages


async def explain(
    profile: dict,
    user_text: str,
    history: Optional[list[dict]] = None,
    last_summaries: Optional[list[str]] = None,
    model: Optional[str] = None,
) -> str:
    context = build_context(profile, last_summaries)
    system = SYSTEM_PROMPT_TEMPLATE.format(context=context)
    messages = build_messages_for_llm(history or [], user_text)
    chosen_model = model or settings.MODEL_CHAT
    return await llm_call_with_retry_messages(
        system=system, messages=messages, model=chosen_model
    )
