# services/memory/main.py
#
# Slice 1: working-memory cache only.
# Backed by a Redis LIST per tg_id; ~50 most recent turns; 24h TTL.
# Postgres-side reads (recent messages, episodic summaries, embedding search)
# are added in later slices; for slice 1 the orchestrator only needs the cache.

import json
import logging
from typing import Any

from shared.redis.client import redis

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 86_400      # 24h
CACHE_MAX_ITEMS = 50


def _key(tg_id: int) -> str:
    return f"memory:tg:{tg_id}"


async def append_turn(tg_id: int, turn: dict) -> None:
    """LPUSH the serialized turn, trim to CACHE_MAX_ITEMS, refresh TTL."""
    key = _key(tg_id)
    await redis.lpush(key, json.dumps(turn))
    await redis.ltrim(key, 0, CACHE_MAX_ITEMS - 1)
    await redis.expire(key, CACHE_TTL_SECONDS)


async def get_recent_turns(tg_id: int, limit: int = 20) -> list[dict]:
    """Return up to `limit` recent turns (most recent first). Empty if cache miss.

    Postgres fallback (rehydrate from v5.messages when cache is empty) is
    intentionally deferred until slice 3 — slice 1 only needs the cache.
    """
    raw = await redis.lrange(_key(tg_id), 0, max(limit, 1) - 1)
    out: list[dict] = []
    for item in raw or []:
        try:
            out.append(json.loads(item))
        except (TypeError, ValueError):
            logger.warning("memory cache: skipping malformed entry tg_id=%s", tg_id)
    return out
