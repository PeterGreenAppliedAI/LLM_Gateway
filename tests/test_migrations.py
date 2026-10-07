"""Schema upgrades at startup (D-047), on SQLite and PostgreSQL.

Second external review: a database made by an older version started, then
failed on the first key query with `no such column: api_keys.max_concurrent`.
Startup ran `create_all`, which never changes existing tables.
"""

import pytest
import sqlalchemy as sa
from alembic import command

from gateway.storage import DatabaseConfig, create_async_db_engine
from gateway.storage.keys import KeyManager
from gateway.storage.migrate import (
    INITIAL_REVISION,
    SchemaError,
    _config,
    current_revision,
    head_revision,
    missing_schema,
)
from gateway.storage.schema import metadata
from tests.conftest import _PG, PG_URL


@pytest.fixture(
    params=[
        "sqlite",
        pytest.param(
            "postgres", marks=pytest.mark.skipif(not _PG, reason=f"no PostgreSQL at {PG_URL}")
        ),
    ]
)
async def url(request, tmp_path):
    """An empty database (PostgreSQL: every table dropped, alembic_version too)."""
    if request.param == "sqlite":
        yield f"sqlite:///{tmp_path}/gw.db"
        return
    yield await _empty_pg()
    await _empty_pg()


async def _empty_pg() -> str:
    engine = await create_async_db_engine(DatabaseConfig(url=PG_URL), create_tables=False)
    async with engine.begin() as conn:
        await conn.execute(sa.text("DROP SCHEMA public CASCADE"))
        await conn.execute(sa.text("CREATE SCHEMA public"))
    await engine.dispose()
    return PG_URL


async def _bare(url: str):
    return await create_async_db_engine(DatabaseConfig(url=url), create_tables=False)


async def _run(engine, fn):
    async with engine.begin() as conn:
        return await conn.run_sync(fn)


async def _legacy_database(url: str, at: str = "c7d1e5f20a84") -> None:
    """A database as an older gateway left it: the schema of revision `at`,
    made without migrations (no alembic_version), with a key in it."""
    engine = await _bare(url)

    def build(conn):
        command.upgrade(_config(conn), at)
        conn.execute(sa.text("DROP TABLE alembic_version"))
        conn.execute(
            sa.text(
                "INSERT INTO api_keys (key_hash, key_prefix, name, client_id, created_at, "
                "is_active) VALUES ('h', 'gw-old', 'old', 'old-client', CURRENT_TIMESTAMP, :t)"
            ),
            {"t": True},
        )

    await _run(engine, build)
    await engine.dispose()


@pytest.mark.asyncio
async def test_new_database_is_created_at_the_latest_revision(url):
    engine = await create_async_db_engine(DatabaseConfig(url=url))
    assert await _run(engine, current_revision) == head_revision()
    assert await _run(engine, missing_schema) == []
    await engine.dispose()


@pytest.mark.asyncio
async def test_unversioned_old_database_is_upgraded_and_keeps_its_data(url):
    """The review's case: an older schema, then the current code."""
    await _legacy_database(url)
    engine = await create_async_db_engine(DatabaseConfig(url=url))  # startup
    keys = await KeyManager(engine).list_keys()  # failed: no such column
    assert [k["client_id"] for k in keys] == ["old-client"]
    assert await _run(engine, current_revision) == head_revision()
    assert await _run(engine, missing_schema) == []
    await engine.dispose()


@pytest.mark.asyncio
async def test_half_upgraded_database_is_completed(url):
    """An old database already started by a create_all-era version: it has the
    newer tables but not the newer columns."""
    await _legacy_database(url, at=INITIAL_REVISION)
    engine = await _bare(url)
    await _run(engine, metadata.create_all)
    assert "api_keys.max_concurrent" in await _run(engine, missing_schema)
    await engine.dispose()

    engine = await create_async_db_engine(DatabaseConfig(url=url))
    assert await _run(engine, missing_schema) == []
    assert len(await KeyManager(engine).list_keys()) == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_versioned_database_is_upgraded(url):
    engine = await _bare(url)
    await _run(engine, lambda c: command.upgrade(_config(c), "d8e2f3a91b57"))
    await engine.dispose()
    engine = await create_async_db_engine(DatabaseConfig(url=url))
    assert await _run(engine, current_revision) == head_revision()
    await engine.dispose()


@pytest.mark.asyncio
async def test_restart_is_a_no_op(url):
    for _ in range(2):
        engine = await create_async_db_engine(DatabaseConfig(url=url))
        assert await _run(engine, current_revision) == head_revision()
        await engine.dispose()


@pytest.mark.asyncio
async def test_database_from_a_newer_gateway_is_refused(url):
    engine = await create_async_db_engine(DatabaseConfig(url=url))
    await _run(
        engine, lambda c: c.execute(sa.text("UPDATE alembic_version SET version_num='ffff0000'"))
    )
    await engine.dispose()
    with pytest.raises(SchemaError, match="newer gateway"):
        await create_async_db_engine(DatabaseConfig(url=url))


@pytest.mark.asyncio
async def test_migrations_alone_produce_the_current_schema(url):
    """Catches schema changes made in code without a migration."""
    engine = await _bare(url)
    await _run(engine, lambda c: command.upgrade(_config(c), "head"))
    assert await _run(engine, missing_schema) == []
    await engine.dispose()
