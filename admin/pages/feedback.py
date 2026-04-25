# admin/pages/feedback.py

import asyncio

import streamlit as st

from shared.db.client import db, init_db_pool

st.title("Feedback")


@st.cache_data(ttl=30)
def load(resolved: bool) -> list[dict]:
    async def _run() -> list[dict]:
        await init_db_pool()
        rows = await db.fetch(
            """
            SELECT id, tg_id, question_id, session_id, message, is_resolved, created_at
            FROM feedback
            WHERE is_resolved = $1
            ORDER BY created_at DESC
            LIMIT 200
            """,
            resolved,
        )
        return [dict(r) for r in rows]
    return asyncio.run(_run())


show_resolved = st.checkbox("Show resolved", value=False)
st.dataframe(load(show_resolved))
