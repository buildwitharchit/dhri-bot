# admin/pages/analytics.py

import asyncio

import streamlit as st

from db.client import db, init_db_pool

st.title("Analytics")


@st.cache_data(ttl=60)
def load() -> dict:
    async def _run() -> dict:
        await init_db_pool()
        attempts_week = await db.fetchval(
            "SELECT count(*) FROM attempts WHERE attempted_at > now() - interval '7 days'"
        )
        accuracy_week = await db.fetchval(
            """
            SELECT COALESCE(AVG(CASE WHEN is_correct THEN 1.0 ELSE 0.0 END), 0.0)
            FROM attempts WHERE attempted_at > now() - interval '7 days'
            """
        )
        sessions_week = await db.fetchval(
            "SELECT count(*) FROM sessions WHERE started_at > now() - interval '7 days'"
        )
        return {
            "attempts_week": int(attempts_week or 0),
            "accuracy_week": float(accuracy_week or 0.0) * 100,
            "sessions_week": int(sessions_week or 0),
        }
    return asyncio.run(_run())


stats = load()
c1, c2, c3 = st.columns(3)
c1.metric("Attempts (7d)", stats["attempts_week"])
c2.metric("Accuracy (7d)", f"{stats['accuracy_week']:.0f}%")
c3.metric("Sessions (7d)", stats["sessions_week"])
