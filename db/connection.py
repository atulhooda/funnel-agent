"""PostgreSQL connection pool (psycopg3, async).

Owns a single AsyncConnectionPool and exposes a `transaction()` helper. Each
`async with transaction() as cur` block is one DB transaction (committed on
clean exit, rolled back on exception) — so multi-statement operations like
identify+backfill stay atomic. Callers scope every query by site_id.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Optional

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from config.settings import get_settings

_pool: Optional[AsyncConnectionPool] = None


async def open_pool() -> None:
    global _pool
    if _pool is None:
        _pool = AsyncConnectionPool(
            conninfo=get_settings().database_url,
            min_size=1,
            max_size=10,
            open=False,
        )
    await _pool.open()


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def get_pool() -> AsyncConnectionPool:
    if _pool is None:
        raise RuntimeError("DB pool is not initialized; call open_pool() on startup.")
    return _pool


@asynccontextmanager
async def transaction():
    """Yield a dict-row cursor inside a committed-on-success transaction."""
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            yield cur
