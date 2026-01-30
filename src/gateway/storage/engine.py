"""Database engine creation and configuration.

Supports SQLite (default) and PostgreSQL with appropriate connection pooling.
"""

from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field
from sqlalchemy import create_engine, Engine, text
from sqlalchemy.pool import QueuePool, NullPool, StaticPool

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


def create_db_engine(
    config: DatabaseConfig,
    create_tables: bool = True,
) -> Engine:
    """Create database engine with appropriate pooling.

    Args:
        config: Database configuration
        create_tables: Whether to create tables if they don't exist

    Returns:
        SQLAlchemy Engine instance
    """
    url = config.url

    # Determine database type and configure accordingly
    if url.startswith("sqlite"):
        engine = _create_sqlite_engine(url, config)
    elif url.startswith("postgresql"):
        engine = _create_postgresql_engine(url, config)
    elif url.startswith("mysql"):
        engine = _create_mysql_engine(url, config)
    else:
        # Generic fallback
        engine = create_engine(url, echo=config.echo)

    # Create tables if requested
    if create_tables and config.create_tables:
        _ensure_tables(engine)

    return engine


def _create_sqlite_engine(url: str, config: DatabaseConfig) -> Engine:
    """Create SQLite engine.

    SQLite specifics:
    - No connection pooling (use StaticPool for in-memory, NullPool for file)
    - check_same_thread=False for async compatibility
    - Ensure data directory exists
    """
    # Extract path from URL and ensure directory exists
    if ":///" in url and not url.endswith(":memory:"):
        # File-based SQLite
        db_path = url.split(":///", 1)[1]
        if db_path.startswith("./"):
            db_path = db_path[2:]
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

        engine = create_engine(
            url,
            connect_args={"check_same_thread": False},
            poolclass=NullPool,  # SQLite doesn't benefit from pooling
            echo=config.echo,
        )
        # Enable WAL mode for better concurrent read/write performance
        with engine.connect() as conn:
            conn.execute(text("PRAGMA journal_mode=WAL"))
            conn.commit()
        return engine
    else:
        # In-memory SQLite (for testing)
        return create_engine(
            url,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,  # Share single connection for in-memory
            echo=config.echo,
        )


def _create_postgresql_engine(url: str, config: DatabaseConfig) -> Engine:
    """Create PostgreSQL engine with connection pooling."""
    return create_engine(
        url,
        poolclass=QueuePool,
        pool_size=config.pool_size,
        max_overflow=config.max_overflow,
        pool_timeout=config.pool_timeout,
        pool_recycle=config.pool_recycle,
        pool_pre_ping=True,  # Verify connections before use
        echo=config.echo,
    )


def _create_mysql_engine(url: str, config: DatabaseConfig) -> Engine:
    """Create MySQL engine with connection pooling."""
    return create_engine(
        url,
        poolclass=QueuePool,
        pool_size=config.pool_size,
        max_overflow=config.max_overflow,
        pool_timeout=config.pool_timeout,
        pool_recycle=config.pool_recycle,
        pool_pre_ping=True,
        echo=config.echo,
    )


def _ensure_tables(engine: Engine) -> None:
    """Create tables if they don't exist.

    Uses SQLAlchemy's create_all which is idempotent.
    """
    metadata.create_all(engine)


def get_table_stats(engine: Engine) -> dict:
    """Get basic statistics about database tables.

    Useful for health checks and debugging.
    """
    stats = {}

    with engine.connect() as conn:
        # Get row counts for each table
        for table in metadata.tables.values():
            try:
                result = conn.execute(table.count())
                stats[table.name] = {"row_count": result.scalar() or 0}
            except Exception as e:
                stats[table.name] = {"error": str(e)}

    return stats
