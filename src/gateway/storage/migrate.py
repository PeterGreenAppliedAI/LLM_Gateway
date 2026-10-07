"""Bring the database schema up to date at startup (D-047).

The gateway used to build its schema with `create_all`, which creates missing
tables but never changes existing ones: a database from an older version
started fine and then failed on the first query that touched a new column
(`no such column: api_keys.max_concurrent`). Startup now runs the migrations:

- **Empty database:** create the current schema and record it as the latest
  revision.
- **Versioned database:** upgrade it to the latest revision.
- **Unversioned database** (made by `create_all` before migrations ran at
  startup): adopt it as the initial revision and upgrade. Each migration
  skips tables and columns that already exist.

Then every table and column the code uses must be present, or startup stops
and names what's missing. A database newer than the code (a gateway
downgrade) is refused rather than half-used.

Also runnable on its own: `gateway-migrate` (or `python -m gateway.storage.migrate`),
e.g. as a deployment step before starting new replicas.
"""

import asyncio
from functools import lru_cache
from pathlib import Path

import sqlalchemy as sa
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.engine import Connection

from gateway.observability import get_logger
from gateway.storage.schema import metadata

logger = get_logger(__name__)

MIGRATIONS = Path(__file__).parent / "migrations"
INITIAL_REVISION = "56dc87704392"
# Serializes schema changes when several PostgreSQL clients start at once
_PG_LOCK_ID = 7_243_650_118


class SchemaError(RuntimeError):
    """The database schema can't be used by this version of the gateway."""


def _config(connection: Connection) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    config.attributes["connection"] = connection
    return config


@lru_cache(maxsize=1)
def _script() -> ScriptDirectory:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    return ScriptDirectory.from_config(config)


def head_revision() -> str:
    return _script().get_current_head()


def current_revision(connection: Connection) -> str | None:
    return MigrationContext.configure(connection).get_current_revision()


def missing_schema(connection: Connection) -> list[str]:
    """Tables and columns the code uses that the database lacks."""
    inspector = sa.inspect(connection)
    existing = set(inspector.get_table_names())
    missing = []
    for name, table in metadata.tables.items():
        if name not in existing:
            missing.append(name)
            continue
        columns = {c["name"] for c in inspector.get_columns(name)}
        missing.extend(f"{name}.{c.name}" for c in table.columns if c.name not in columns)
    return missing


def upgrade(connection: Connection) -> str:
    """Bring the schema to the latest revision; returns what was done.

    Runs inside the caller's transaction, so a failed migration leaves the
    database as it was (both SQLite and PostgreSQL have transactional DDL).
    """
    from alembic import command

    if connection.dialect.name == "postgresql":
        connection.execute(sa.text("SELECT pg_advisory_xact_lock(:id)"), {"id": _PG_LOCK_ID})

    script = _script()
    head = script.get_current_head()
    current = current_revision(connection)
    known = {rev.revision for rev in script.walk_revisions()}

    if current is not None and current not in known:
        raise SchemaError(
            f"The database schema is at revision {current}, which this gateway doesn't know "
            f"(its latest is {head}): it was upgraded by a newer gateway. Run that version, "
            "or restore a backup taken before the upgrade."
        )

    if current is None:
        tables = set(sa.inspect(connection).get_table_names())
        if not tables & set(metadata.tables):
            metadata.create_all(connection)
            MigrationContext.configure(connection).stamp(script, "head")
            action = "created"
        else:
            command.stamp(_config(connection), INITIAL_REVISION)
            command.upgrade(_config(connection), "head")
            action = f"adopted an unversioned database and upgraded it to {head}"
    elif current != head:
        command.upgrade(_config(connection), "head")
        action = f"upgraded from {current} to {head}"
    else:
        action = "current"

    missing = missing_schema(connection)
    if missing:
        raise SchemaError(
            "The database is missing parts of the schema after migrating: "
            + ", ".join(missing)
            + ". Restore a backup, or report this with the output of `alembic history`."
        )
    if action != "current" and action != "created":
        logger.info("Database schema migrated", action=action)
    return action


async def _main() -> None:
    from gateway.settings import get_settings
    from gateway.storage import DatabaseConfig, create_async_db_engine

    settings = get_settings()
    engine = await create_async_db_engine(DatabaseConfig(url=settings.db.url), create_tables=False)
    try:
        async with engine.begin() as conn:
            action = await conn.run_sync(upgrade)
        print(f"Database schema: {action} (revision {head_revision()})")
    finally:
        await engine.dispose()


def main() -> None:
    """`gateway-migrate`: migrate the configured database (GATEWAY_DB_URL) and exit."""
    asyncio.run(_main())


if __name__ == "__main__":
    main()
