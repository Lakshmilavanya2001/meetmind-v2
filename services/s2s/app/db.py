"""Database: async SQLAlchemy on Postgres, and the SQL migrations in ../migrations, applied in order
at startup (each once, recorded in schema_migrations; one process at a time, under an advisory lock)."""
from __future__ import annotations

import logging
import os
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

log = logging.getLogger("s2s.db")
MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"
_engine: AsyncEngine | None = None
_sessions: async_sessionmaker[AsyncSession] | None = None


def database_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    return url


def engine() -> AsyncEngine:
    global _engine, _sessions
    if _engine is None:
        _engine = create_async_engine(database_url(), pool_size=10, max_overflow=10, pool_pre_ping=True)
        _sessions = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def sessionmaker() -> async_sessionmaker[AsyncSession]:
    engine()
    return _sessions  # type: ignore[return-value]


async def dispose():
    global _engine, _sessions
    if _engine is not None:
        await _engine.dispose()
    _engine = _sessions = None


async def migrate() -> list[str]:
    """Apply the migrations not applied yet; returns their names."""
    applied: list[str] = []
    async with engine().begin() as conn:
        await conn.execute(text("SELECT pg_advisory_xact_lock(hashtext('meetmind-v2-migrations'))"))
        await conn.execute(text("CREATE TABLE IF NOT EXISTS schema_migrations (name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"))
        done = {r[0] for r in await conn.execute(text("SELECT name FROM schema_migrations"))}
        for f in sorted(MIGRATIONS.glob("*.sql")):
            if f.name in done:
                continue
            # A migration file holds several statements: run it through asyncpg's simple-query path.
            raw = await conn.get_raw_connection()
            await raw.driver_connection.execute(f.read_text())
            await conn.execute(text("INSERT INTO schema_migrations (name) VALUES (:n)"), {"n": f.name})
            applied.append(f.name)
            log.info("applied migration %s", f.name)
    return applied
