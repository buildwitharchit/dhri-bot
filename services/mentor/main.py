# services/mentor/main.py
#
# Slice 1: stub. Real reactive mode + observer mode land in slice 8.

from typing import Any


async def handle(context: dict) -> dict:  # noqa: ARG001
    return {
        "content": "Hello, I'm DHRI",
        "content_type": "text",
        "keyboard": None,
        "memory_deltas": {},
        "observer_events": [],
        "meta": {"agent": "mentor"},
    }


async def synthesize_diagnostic(student_id: str) -> dict:  # noqa: ARG001
    """Slice-6 stub for onboarding completion."""
    return await handle({"student_id": student_id})


async def inline_observe(*args: Any, **kwargs: Any) -> None:  # noqa: ARG001
    """Slice-8 stub. No-op observer."""
    return None
