"""Alembic environment.

Two ways in:
- From the gateway (startup, `gateway-migrate`): `gateway.storage.migrate`
  passes an open connection in `config.attributes["connection"]`, inside the
  transaction it manages.
- From the alembic CLI (`alembic upgrade head`): the database URL comes from
  GATEWAY_DB_URL (else alembic.ini) and runs through the gateway's own async
  engine, so `sqlite:///...` and `postgresql+asyncpg://...` both work.

Batch mode is on for SQLite ALTER TABLE support.
"""

import asyncio
import os
from logging.config import fileConfig

from alembic import context

from gateway.storage.schema import metadata as target_metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

db_url = os.environ.get("GATEWAY_DB_URL")
if db_url:
    config.set_main_option("sqlalchemy.url", db_url)


def _run(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,  # Required for SQLite ALTER TABLE support
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_offline() -> None:
    """Generate the SQL script without connecting."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_cli() -> None:
    from gateway.storage import DatabaseConfig, create_async_db_engine

    engine = await create_async_db_engine(
        DatabaseConfig(url=config.get_main_option("sqlalchemy.url")), create_tables=False
    )
    try:
        async with engine.begin() as conn:
            await conn.run_sync(_run)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
elif config.attributes.get("connection") is not None:
    _run(config.attributes["connection"])
else:
    asyncio.run(_run_cli())
