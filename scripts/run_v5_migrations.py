"""Apply v5 migrations in order.

Usage:
    python -m scripts.run_v5_migrations

Connects via DATABASE_URL_DIRECT (direct, not pooled — required for DDL).
Each migration is wrapped in its own transaction; a failure aborts the run
without committing the failed file. Files use IF NOT EXISTS / NOT EXISTS
so re-runs are idempotent.
"""

import asyncio
import logging
import sys
from pathlib import Path

import asyncpg

from config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("v5_migrations")

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations" / "v5"


async def main() -> int:
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        logger.error("No migrations found under %s", MIGRATIONS_DIR)
        return 1

    logger.info("Found %d migration(s) under %s", len(files), MIGRATIONS_DIR)
    for f in files:
        logger.info("  • %s", f.name)

    conn = await asyncpg.connect(settings.DATABASE_URL_DIRECT)
    try:
        for f in files:
            sql = f.read_text()
            logger.info("Applying %s ...", f.name)
            try:
                async with conn.transaction():
                    await conn.execute(sql)
            except Exception as e:
                logger.error("FAILED %s: %s", f.name, e)
                return 2
            logger.info("  ✓ %s", f.name)
    finally:
        await conn.close()

    logger.info("All v5 migrations applied successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
