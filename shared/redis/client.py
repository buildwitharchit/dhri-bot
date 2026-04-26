# memory/session.py
#
# Upstash Redis HTTP client wrapper.
# Exposes: init_redis, redis facade (get/set/delete/...), get_state,
# set_state, acquire_lock, release_lock.

import json
import logging
from typing import Any, Optional

from upstash_redis.asyncio import Redis

from config import settings

logger = logging.getLogger(__name__)

_client: Optional[Redis] = None

STATE_TTL = 7200  # 2 hours


async def init_redis() -> None:
    """Create the Upstash Redis HTTP client. Called once at startup."""
    global _client
    if _client is not None:
        return
    _client = Redis(
        url=settings.UPSTASH_REDIS_REST_URL,
        token=settings.UPSTASH_REDIS_REST_TOKEN,
    )
    logger.info("upstash redis client initialised")


def _require_client() -> Redis:
    if _client is None:
        raise RuntimeError("redis client not initialised — call init_redis() first")
    return _client


class _RedisFacade:
    """Thin passthrough so call sites can do `await redis.get(...)`."""

    async def get(self, key: str) -> Optional[str]:
        return await _require_client().get(key)

    async def set(self, key: str, value: str, ex: Optional[int] = None) -> Any:
        if ex is not None:
            return await _require_client().set(key, value, ex=ex)
        return await _require_client().set(key, value)

    async def setex(self, key: str, seconds: int, value: str) -> Any:
        return await _require_client().set(key, value, ex=seconds)

    async def delete(self, *keys: str) -> Any:
        return await _require_client().delete(*keys)

    async def incr(self, key: str) -> int:
        return await _require_client().incr(key)

    async def expire(self, key: str, seconds: int) -> Any:
        return await _require_client().expire(key, seconds)

    async def ttl(self, key: str) -> int:
        return await _require_client().ttl(key)

    async def set_nx(self, key: str, value: str, ex: int) -> bool:
        """Set if not exists with expiry. Returns True if lock acquired."""
        result = await _require_client().set(key, value, ex=ex, nx=True)
        return bool(result)

    # ─── LIST OPS (used by v5 working-memory cache) ────────────────────────

    async def lpush(self, key: str, *values: str) -> int:
        return await _require_client().lpush(key, *values)

    async def ltrim(self, key: str, start: int, stop: int) -> Any:
        return await _require_client().ltrim(key, start, stop)

    async def lrange(self, key: str, start: int, stop: int) -> list[str]:
        return await _require_client().lrange(key, start, stop)


redis = _RedisFacade()


# ─── STATE HELPERS ──────────────────────────────────────────────────────────

def _state_key(tg_id: int) -> str:
    return f"state:tg:{tg_id}"


async def get_state(tg_id: int) -> Optional[dict]:
    raw = await redis.get(_state_key(tg_id))
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        logger.warning(f"invalid state JSON for tg_id={tg_id}; clearing")
        await redis.delete(_state_key(tg_id))
        return None


async def set_state(tg_id: int, state: dict, ttl: int = STATE_TTL) -> None:
    await redis.set(_state_key(tg_id), json.dumps(state), ex=ttl)


async def clear_state(tg_id: int) -> None:
    await redis.delete(_state_key(tg_id))


async def update_state(tg_id: int, ttl: int = STATE_TTL, **patch: Any) -> dict:
    """Read-modify-write of `state:tg:{tg_id}`. Used by the v5 bus to track
    last_question_message_id / last_question_attempt_id without clobbering
    other fields written by the orchestrator."""
    state = await get_state(tg_id) or {}
    state.update(patch)
    await set_state(tg_id, state, ttl)
    return state


# ─── LOCKS ──────────────────────────────────────────────────────────────────

async def acquire_lock(key: str, ttl: int) -> bool:
    """SETNX-with-expire. Returns True iff this caller acquired the lock."""
    return await redis.set_nx(key, "1", ex=ttl)


async def release_lock(key: str) -> None:
    await redis.delete(key)
