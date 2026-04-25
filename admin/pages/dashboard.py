# admin/pages/dashboard.py

import asyncio

import streamlit as st

from shared.db.client import db, init_db_pool

st.title("Dashboard")


async def _load() -> dict:
    await init_db_pool()
    total_users = await db.fetchval("SELECT count(*) FROM tg_users")
    total_questions = await db.fetchval("SELECT count(*) FROM questions")
    needs_review = await db.fetchval(
        "SELECT count(*) FROM questions WHERE needs_review = true"
    )
    total_attempts = await db.fetchval("SELECT count(*) FROM attempts")
    return {
        "users": total_users or 0,
        "questions": total_questions or 0,
        "needs_review": needs_review or 0,
        "attempts": total_attempts or 0,
    }


stats = asyncio.run(_load())
c1, c2, c3, c4 = st.columns(4)
c1.metric("Users", stats["users"])
c2.metric("Questions", stats["questions"])
c3.metric("needs_review", stats["needs_review"])
c4.metric("Attempts", stats["attempts"])
