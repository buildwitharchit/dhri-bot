# handlers/weekly_reports.py
#
# Weekly report cron. Sends a short stats line to every active user who had
# at least one attempt in the last 7 days.

import logging

from telegram import Bot

from config import SKILL_DISPLAY_NAMES, settings
from shared.db.client import db
from v4_legacy.db.queries import get_week_stats

logger = logging.getLogger(__name__)


async def send_weekly_reports_to_all() -> None:
    bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
    rows = await db.fetch(
        """
        SELECT DISTINCT a.tg_id
        FROM attempts a
        WHERE a.attempted_at > now() - interval '7 days'
        """
    )
    if not rows:
        logger.info("weekly_reports: no active users")
        return

    for row in rows:
        tg_id = row["tg_id"]
        try:
            message = await _build_report(tg_id)
            await bot.send_message(chat_id=tg_id, text=message, parse_mode="HTML")
        except Exception as e:
            logger.warning(f"weekly report failed for tg_id={tg_id}: {e}")


async def _build_report(tg_id: int) -> str:
    week = await get_week_stats(tg_id)
    profile = await db.fetchrow(
        "SELECT weakest_skill, current_streak, most_common_trap "
        "FROM user_profiles WHERE tg_id = $1",
        tg_id,
    )
    weakest = profile["weakest_skill"] if profile else None
    streak = profile["current_streak"] if profile else 0
    trap = profile["most_common_trap"] if profile else "none"

    lines = ["<b>📨 DHRI weekly recap</b>"]
    lines.append(
        f"This week: {week['questions']} questions · {week['accuracy']:.0f}% accuracy"
    )
    if weakest:
        label = SKILL_DISPLAY_NAMES.get(weakest, weakest.title())
        lines.append(f"Weakest skill: <b>{label}</b>")
    if trap and trap != "none":
        lines.append(f"Repeating trap: <i>{trap.replace('_', ' ')}</i>")
    if streak and streak > 1:
        lines.append(f"🔥 {streak}-day streak")
    lines.append("\nReady for another round? Tap /rc, /pj or /va.")
    return "\n".join(lines)
