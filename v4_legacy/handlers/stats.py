# handlers/stats.py
#
# Section 23 verbatim — scores shown at student-facing (7-skill) level.

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from shared.telegram.utils import send_long_message
from config import SKILL_DISPLAY_NAMES, STUDENT_SKILLS, SUBSKILL_TO_SKILL
from shared.db.client import db
from v4_legacy.db.queries import get_week_stats


async def handle_stats(message, state, profile, bot) -> None:
    tg_id = profile['tg_id']

    rows = await db.fetch(
        "SELECT subskill, score FROM user_skill_scores WHERE tg_id = $1", tg_id
    )

    skill_scores: dict[str, list[float]] = {}
    for row in rows:
        student_skill = SUBSKILL_TO_SKILL.get(row['subskill'])
        if not student_skill:
            continue
        if student_skill not in skill_scores:
            skill_scores[student_skill] = []
        skill_scores[student_skill].append(row['score'])

    avg_by_skill = {
        skill: sum(scores) / len(scores)
        for skill, scores in skill_scores.items()
    }

    week = await get_week_stats(tg_id)

    lines = ["<b>📊 Your VARC stats</b>\n"]
    if week['questions'] > 0:
        lines.append(
            f"This week: {week['questions']} questions "
            f"· {week['accuracy']:.0f}% accuracy"
        )

    streak = profile.get('current_streak', 0) or 0
    if streak > 1:
        lines.append(f"🔥 {streak}-day streak")

    lines.append("\n<b>Skill breakdown:</b>")
    for skill in STUDENT_SKILLS:
        avg = avg_by_skill.get(skill, 0.5)
        label = SKILL_DISPLAY_NAMES[skill]
        bar = "█" * int(avg * 10) + "░" * (10 - int(avg * 10))
        lines.append(f"{label}: {avg*100:.0f}% {bar}")

    trap = profile.get('most_common_trap', 'none')
    if trap and trap != 'none':
        lines.append(f"\nMost common mistake: {trap.replace('_', ' ')}")

    await send_long_message(
        message.chat_id, "\n".join(lines), bot,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("Drill my weakest", callback_data="drill_weakest"),
            InlineKeyboardButton("Full breakdown", callback_data="stats_full"),
        ]])
    )
