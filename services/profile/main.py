# services/profile/main.py
#
# Slice 1: ensure a v5.student_profile row exists; minimal brief is hardcoded.
# Real assembly (notes, performance stats, episodic recall) lands in slice 5.

import logging
from typing import Optional

from shared.db.client import db

logger = logging.getLogger(__name__)


async def ensure_profile(student_id: str) -> dict:
    """Idempotent: insert a default profile row if missing, then return it."""
    row = await db.fetchrow(
        "SELECT * FROM v5.student_profile WHERE student_id = $1::uuid",
        student_id,
    )
    if row is not None:
        return dict(row)

    await db.execute(
        """
        INSERT INTO v5.student_profile (student_id)
        VALUES ($1::uuid)
        ON CONFLICT (student_id) DO NOTHING
        """,
        student_id,
    )
    row = await db.fetchrow(
        "SELECT * FROM v5.student_profile WHERE student_id = $1::uuid",
        student_id,
    )
    return dict(row) if row else {}


async def get_minimal_brief(student_id: str) -> str:
    """Slice-1 stub. Real template assembly lands in slice 5."""
    return "Archit, CAT 2026 aspirant"


# Placeholder so callers in later slices can import the symbol now.
async def get_tutor_brief(student_id: str) -> str:  # noqa: ARG001
    return await get_minimal_brief(student_id)
