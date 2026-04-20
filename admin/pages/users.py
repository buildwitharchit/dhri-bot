# admin/pages/users.py

import asyncio

import streamlit as st

from db.client import db, init_db_pool

st.title("Users")


@st.cache_data(ttl=30)
def load() -> list[dict]:
    async def _run() -> list[dict]:
        await init_db_pool()
        rows = await db.fetch(
            """
            SELECT u.tg_id, u.username, u.first_name, u.joined_at, u.last_active_at,
                   u.is_banned, p.total_attempts, p.weakest_skill, p.current_streak
            FROM tg_users u
            LEFT JOIN user_profiles p ON p.tg_id = u.tg_id
            ORDER BY u.last_active_at DESC
            LIMIT 200
            """,
        )
        return [dict(r) for r in rows]
    return asyncio.run(_run())


st.dataframe(load())
