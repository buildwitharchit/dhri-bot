# handlers/session_cleanup.py
#
# Cron-triggered cleanup. Closes sessions that have been idle > 2 hours.
# Section 10 verbatim for close_session_silently.

import logging

from shared.db.client import db
from v4_legacy.db.queries import get_state_from_db_or_redis, write_session_snapshot
from shared.redis.client import redis
from v4_legacy.memory.summarizer import generate_session_summary

logger = logging.getLogger(__name__)

RESUMABLE_MODES = {"rc", "pj", "va"}


async def cleanup_stale_sessions() -> int:
    stale = await db.fetch("""
        SELECT session_id, tg_id, mode FROM sessions
        WHERE ended_at IS NULL
          AND last_active_at < now() - interval '2 hours'
    """)
    closed = 0
    for session in stale:
        try:
            await close_session_silently(
                str(session['session_id']), session['tg_id'], session['mode']
            )
            closed += 1
        except Exception as e:
            logger.error(f"Failed to close {session['session_id']}: {e}")
            try:
                await db.execute("""
                    UPDATE sessions SET ended_at = now(), was_completed = false,
                    summary = NULL WHERE session_id = $1::uuid AND ended_at IS NULL
                """, str(session['session_id']))
            except Exception as e2:
                logger.error(f"Fallback close failed: {e2}")
    return closed


async def close_session_silently(session_id: str, tg_id: int, mode: str) -> None:
    summary = None
    try:
        summary = await generate_session_summary(session_id, tg_id)
    except Exception as e:
        logger.warning(f"Summary failed for {session_id}: {e}")

    if mode in RESUMABLE_MODES:
        try:
            state = await get_state_from_db_or_redis(tg_id, session_id)
            if state:
                await write_session_snapshot(session_id, tg_id, state)
        except Exception as e:
            logger.warning(f"Snapshot failed for {session_id}: {e}")

    await db.execute("""
        UPDATE sessions SET
            ended_at = now(), was_completed = false, summary = $1,
            duration_mins = EXTRACT(EPOCH FROM (now() - started_at)) / 60
        WHERE session_id = $2::uuid AND ended_at IS NULL
    """, summary, session_id)

    await redis.delete(f"state:tg:{tg_id}")
