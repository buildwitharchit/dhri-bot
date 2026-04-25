# admin/pages/questions.py

import asyncio

import streamlit as st

from config import ALL_SUBSKILLS, STUDENT_SKILLS
from shared.db.client import db, init_db_pool

st.title("Questions")


@st.cache_data(ttl=30)
def load(skill: str, subskill: str, flagged_only: bool) -> list[dict]:
    async def _run() -> list[dict]:
        await init_db_pool()
        clauses = []
        args: list = []
        if skill and skill != "all":
            args.append(skill)
            clauses.append(f"skill = ${len(args)}")
        if subskill and subskill != "all":
            args.append(subskill)
            clauses.append(f"subskill = ${len(args)}")
        if flagged_only:
            clauses.append("needs_review = true")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = await db.fetch(
            f"""
            SELECT question_id, type, skill, subskill, difficulty, needs_review,
                   is_active, created_at
            FROM questions{where}
            ORDER BY created_at DESC LIMIT 200
            """,
            *args,
        )
        return [dict(r) for r in rows]
    return asyncio.run(_run())


skill = st.selectbox("Skill", ["all", *STUDENT_SKILLS])
subskill = st.selectbox("Subskill", ["all", *ALL_SUBSKILLS])
flagged_only = st.checkbox("needs_review only", value=False)

rows = load(skill, subskill, flagged_only)
st.dataframe(rows)

flagged_qid = st.text_input("Clear needs_review on question_id")
if st.button("Toggle needs_review=false") and flagged_qid:
    async def _toggle() -> None:
        await init_db_pool()
        await db.execute(
            "UPDATE questions SET needs_review = false WHERE question_id = $1",
            flagged_qid,
        )
    asyncio.run(_toggle())
    st.success(f"cleared needs_review for {flagged_qid}")
