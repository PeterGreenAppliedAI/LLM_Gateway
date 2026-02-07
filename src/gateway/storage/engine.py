"""Database engine creation and configuration.

Supports SQLite (default) and PostgreSQL with appropriate connection pooling.
Uses async SQLAlchemy for non-blocking database I/O.
"""

from pathlib import Path

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool, StaticPool
from sqlalchemy import text

from gateway.storage.schema import metadata


class DatabaseConfig(BaseModel):
    """Database configuration."""

    # Connection URL
    # SQLite: sqlite:///./data/gateway.db
    # PostgreSQL: postgresql://user:pass@localhost:5432/gateway
    url: str = Field(default="sqlite:///./data/gateway.db")

    # Connection pool settings (ignored for SQLite)
    pool_size: int = Field(default=5, ge=1, le=50)
    max_overflow: int = Field(default=10, ge=0, le=100)
    pool_timeout: int = Field(default=30, ge=1, le=300)
    pool_recycle: int = Field(default=3600, ge=60)  # Recycle connections after 1 hour

    # What to store (privacy controls)
    store_request_body: bool = False  # Don't store prompts by default
    store_response_body: bool = False  # Don't store completions by default

    # Table creation
    create_tables: bool = True  # Auto-create tables on startup

    # Echo SQL (for debugging)
    echo: bool = False


def _translate_url_for_async(url: str) -> str:
    """Translate a sync database URL to its async driver equivalent.

    postgresql://  -> postgresql+asyncpg://
    sqlite:///     -> sqlite+aiosqlite:///

    Already-translated URLs pass through unchanged.
    """
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if url.startswith("sqlite:///"):
        return url.replace("sqlite:///", "sqlite+aiosqlite:///", 1)
    return url


async def create_async_db_engine(
    config: DatabaseConfig,
    create_tables: bool = True,
) -> AsyncEngine:
    """Create async database engine with appropriate pooling.

    Args:
        config: Database configuration
        create_tables: Whether to create tables if they don't exist

    Returns:
        SQLAlchemy AsyncEngine instance
    """
    url = _translate_url_for_async(config.url)

    if config.url.startswith("sqlite"):
        engine = _create_sqlite_engine(url, config)
    elif config.url.startswith("postgresql"):
        engine = _create_postgresql_engine(url, config)
    else:
        engine = create_async_engine(url, echo=config.echo)

    # Create tables if requested
    if create_tables and config.create_tables:
        async with engine.begin() as conn:
            await conn.run_sync(metadata.create_all)

    # Enable WAL mode for file-based SQLite
    if config.url.startswith("sqlite") and ":memory:" not in config.url:
        async with engine.connect() as conn:
            await conn.execute(text("PRAGMA journal_mode=WAL"))
            await conn.commit()

    return engine


def _create_sqlite_engine(url: str, config: DatabaseConfig) -> AsyncEngine:
    """Create async SQLite engine."""
    # Extract path from original URL and ensure directory exists
    orig_url = config.url
    if ":///" in orig_url and not orig_url.endswith(":memory:"):
        db_path = orig_url.split(":///", 1)[1]
        if db_path.startswith("./"):
            db_path = db_path[2:]
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

        return create_async_engine(
            url,
            poolclass=NullPool,
            echo=config.echo,
        )
    else:
        # In-memory SQLite (for testing)
        return create_async_engine(
            url,
            poolclass=StaticPool,
            echo=config.echo,
        )


def _create_postgresql_engine(url: str, config: DatabaseConfig) -> AsyncEngine:
    """Create async PostgreSQL engine with connection pooling."""
    return create_async_engine(
        url,
        pool_size=config.pool_size,
        max_overflow=config.max_overflow,
        pool_timeout=config.pool_timeout,
        pool_recycle=config.pool_recycle,
        pool_pre_ping=True,
        echo=config.echo,
    )


async def get_table_stats(engine: AsyncEngine) -> dict:
    """Get basic statistics about database tables.

    Useful for health checks and debugging.
    """
    stats = {}

    async with engine.connect() as conn:
        for table in metadata.tables.values():
            try:
                result = await conn.execute(table.count())
                stats[table.name] = {"row_count": result.scalar() or 0}
            except Exception as e:
                stats[table.name] = {"error": str(e)}

    return stats
