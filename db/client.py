# db/client.py
#
# Asyncpg connection pool with pgvector codec registration.
# Module-level `db` exposes fetch/fetchrow/fetchval/execute/executemany
# with the same signature as an asyncpg Connection — each call acquires
# a connection from the pool.

import logging
from typing import Any, Iterable, Optional

import asyncpg
from pgvector.asyncpg import register_vector

from config import settings

logger = logging.getLogger(__name__)

_pool: Optional[asyncpg.Pool] = None


async def _init_conn(conn: asyncpg.Connection) -> None:
    """Register pgvector codec on every new pool connection."""
    await register_vector(conn)


async def init_db_pool() -> None:
    """Create the asyncpg pool. Called once at startup."""
    global _pool
    if _pool is not None:
        return
    _pool = await asyncpg.create_pool(
        settings.DATABASE_URL,
        min_size=1,
        max_size=10,
        init=_init_conn,
        command_timeout=30,
    )
    logger.info("asyncpg pool initialised")


async def close_db_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def _require_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("db pool not initialised — call init_db_pool() first")
    return _pool


class _DB:
    """Thin facade that acquires a connection per call."""

    async def fetch(self, query: str, *args: Any) -> list[asyncpg.Record]:
        async with _require_pool().acquire() as conn:
            return await conn.fetch(query, *args)

    async def fetchrow(self, query: str, *args: Any) -> Optional[asyncpg.Record]:
        async with _require_pool().acquire() as conn:
            return await conn.fetchrow(query, *args)

    async def fetchval(self, query: str, *args: Any) -> Any:
        async with _require_pool().acquire() as conn:
            return await conn.fetchval(query, *args)

    async def execute(self, query: str, *args: Any) -> str:
        async with _require_pool().acquire() as conn:
            return await conn.execute(query, *args)

    async def executemany(self, query: str, args: Iterable[Iterable[Any]]) -> None:
        async with _require_pool().acquire() as conn:
            await conn.executemany(query, args)


db = _DB()
